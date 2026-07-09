"""Tests for EVE image/name template helpers and the name resolver."""
from __future__ import annotations

import pytest
import responses

from apps.killboard.ingest import ingest_killmail
from apps.sde.templatetags import eve


def test_isk_formatting():
    assert eve.isk(540) == "540"
    assert eve.isk(12500) == "12.50k"
    assert eve.isk(340_000_000) == "340.00M"
    assert eve.isk(1_200_000_000) == "1.20B"
    assert eve.isk(None) == "0"


def test_image_urls():
    assert eve.eve_portrait(95465499, 64) == "https://images.evetech.net/characters/95465499/portrait?size=64"
    assert eve.eve_type_render(587) == "https://images.evetech.net/types/587/render?size=512"
    assert eve.eve_type_icon(34, 32) == "https://images.evetech.net/types/34/icon?size=32"
    assert eve.eve_corp_logo(98000001, 32) == "https://images.evetech.net/corporations/98000001/logo?size=32"
    assert eve.eve_portrait(None) == ""


def test_image_size_snaps_to_valid_evetech_size():
    # evetech.net only serves 32/64/128/256/512/1024; invalid sizes must snap up
    # to a valid one rather than 404.
    assert eve.eve_portrait(95465499, 40).endswith("size=64")
    assert eve.eve_corp_logo(98000001, 40).endswith("size=64")
    assert eve.eve_type_icon(34, 48).endswith("size=64")
    assert eve.eve_type_render(587, 100).endswith("size=128")
    assert eve.eve_portrait(1, 32).endswith("size=32")  # already valid, unchanged
    assert eve.eve_portrait(1, 5000).endswith("size=1024")  # capped at max
    assert eve.eve_type_icon(1, "bad").endswith("size=64")  # non-numeric → default


def test_sec_class():
    # Four EVE security bands: cyan 1.0–0.8, yellow 0.7–0.5, orange 0.4–0.1, red ≤0.0.
    assert eve.sec_class(1.0) == "text-sechi"
    assert eve.sec_class(0.8) == "text-sechi"
    assert eve.sec_class(0.7) == "text-secmid"
    assert eve.sec_class(0.5) == "text-secmid"
    assert eve.sec_class(0.4) == "text-seclo"
    assert eve.sec_class(0.1) == "text-seclo"
    assert eve.sec_class(0.0) == "text-secnull"
    assert eve.sec_class(-0.1) == "text-secnull"
    # A true-sec that displays rounded still lands in its displayed band.
    assert eve.sec_class(0.45) == "text-secmid"   # shows as 0.5
    assert eve.sec_class(0.44) == "text-seclo"    # shows as 0.4
    assert eve.sec_class("junk") == "text-faint"  # unparseable → neutral


def test_eve_img_base_follows_setting(settings):
    # Client-side (Alpine) image URLs must build against this base so they hit the
    # same-origin /eveimg mirror in prod instead of CCP's server, which the
    # `img-src 'self'` CSP blocks.
    settings.EVE_IMAGE_BASE_URL = "/eveimg"
    assert eve.eve_img_base() == "/eveimg"
    settings.EVE_IMAGE_BASE_URL = "https://images.evetech.net"
    assert eve.eve_img_base() == "https://images.evetech.net"


@pytest.mark.django_db
def test_operations_form_uses_mirror_not_external_images(client, django_user_model, sde, settings):
    # Regression: the fleet-composition builder's ship thumbnails must use the
    # same-origin mirror base, never a hardcoded images.evetech.net URL (CSP-blocked).
    from apps.identity.models import RoleAssignment
    from apps.sso.models import EveCharacter
    from apps.sso.services import ensure_role
    from core import rbac

    settings.EVE_IMAGE_BASE_URL = "/eveimg"
    user = django_user_model.objects.create(username="eve:920002")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_OFFICER))
    EveCharacter.objects.create(character_id=920002, user=user, name="FC",
                                is_main=True, is_corp_member=True)
    client.force_login(user)
    html = client.get("/operations/create/").content.decode()
    assert "/eveimg/types/" in html
    assert "images.evetech.net" not in html


@pytest.mark.django_db
def test_type_name_resolves_from_sde(sde):
    from django.core.cache import cache

    cache.clear()
    assert eve.type_name(587) == "Rifter"
    assert eve.type_name(999999).startswith("Type ")


@responses.activate
@pytest.mark.django_db
def test_name_resolver_stores_evename():
    from apps.corporation.models import EveName
    from core.esi.names import resolve_ids

    responses.add(
        responses.POST,
        "https://esi.evetech.net/universe/names/",
        json=[
            {"id": 95465499, "name": "Some Pilot", "category": "character"},
            {"id": 98000001, "name": "Some Corp", "category": "corporation"},
        ],
        status=200,
    )
    added = resolve_ids([95465499, 98000001])
    assert added == 2
    assert EveName.objects.get(entity_id=95465499).name == "Some Pilot"
    # Second call resolves nothing new (already cached in the table).
    assert resolve_ids([95465499]) == 0


@pytest.mark.django_db
def test_killmail_detail_page_renders(client, sde):
    ingest_killmail(
        424242,
        "h",
        body={
            "killmail_id": 424242,
            "killmail_time": "2026-06-20T10:00:00Z",
            "solar_system_id": 30002053,
            "victim": {
                "character_id": 9,
                "corporation_id": 98000001,
                "ship_type_id": 587,
                "damage_taken": 100,
                "items": [{"item_type_id": 484, "flag": 27, "quantity_destroyed": 1}],
            },
            "attackers": [{"character_id": 1, "corporation_id": 99, "ship_type_id": 587, "final_blow": True}],
        },
    )
    resp = client.get("/killboard/424242/")
    assert resp.status_code == 200
    assert b"images.evetech.net/types/587/render" in resp.content  # ship render present
