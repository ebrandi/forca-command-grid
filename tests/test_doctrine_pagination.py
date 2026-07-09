"""Pagination + readiness ordering for the Doctrines and Shipyard pages, and the
leadership-configurable page size.

Uses the bundled test SDE: Rifter 587 (needs Minmatar Frigate 3331), 200mm
AutoCannon I 484 (needs Small Projectile Turret 3301), Damage Control I 2046
(needs Gunnery 3300).
"""
from __future__ import annotations

import pytest

from apps.characters.models import CharacterSkillSnapshot
from apps.doctrines.browse import readiness_sort_key
from apps.doctrines.fitparser import parse_eft
from apps.doctrines.models import (
    MAX_PER_PAGE,
    Doctrine,
    DoctrineDisplayConfig,
    DoctrineFit,
    clamp_per_page,
)
from apps.doctrines.services import derive_skill_requirements
from apps.identity.models import RoleAssignment
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac

pytestmark = pytest.mark.django_db

# Fits with progressively more skill requirements.
RIFTER_BARE = "[Rifter, Bare]\n"                                      # hull skill 3331 only
RIFTER_AC = "[Rifter, AC]\n200mm AutoCannon I\n"                      # 3331 + 3301
RIFTER_FULL = "[Rifter, Full]\n200mm AutoCannon I\nDamage Control I\n"  # 3331 + 3301 + 3300


def _member(django_user_model, skills=None):
    user = django_user_model.objects.create(username="pilot")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    char = EveCharacter.objects.create(
        character_id=4242, user=user, name="Pilot", is_main=True, is_corp_member=True
    )
    if skills is not None:
        CharacterSkillSnapshot.objects.create(character=char, skills=skills, is_latest=True)
    return user, char


def _officer(django_user_model, name="officer"):
    user = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_OFFICER))
    return user


def _doc(name, eft, priority=0):
    d = Doctrine.objects.create(name=name, priority=priority)
    parsed = parse_eft(eft)
    fit = DoctrineFit.objects.create(
        doctrine=d, name=name, ship_type_id=parsed["ship_type_id"],
        modules=parsed["modules"], eft_text=eft,
    )
    derive_skill_requirements(fit)
    return d


# ============================================================================
# Ordering key (pure)
# ============================================================================
def test_readiness_sort_key_orders_optimal_then_closest():
    keys = [
        readiness_sort_key("unknown", None, "a"),
        readiness_sort_key("not_ready", 3, "b"),
        readiness_sort_key("not_ready", 1, "c"),
        readiness_sort_key("viable", None, "d"),
        readiness_sort_key("optimal", None, "e"),
    ]
    labels = [k[2] for k in sorted(keys)]
    # optimal, viable, not_ready(1 missing), not_ready(3 missing), unknown
    assert labels == ["e", "d", "c", "b", "a"]


def test_readiness_sort_key_ignores_missing_for_flyable():
    # A can-fly row never loses to a not-ready row, whatever the missing count.
    assert readiness_sort_key("viable", None) < readiness_sort_key("not_ready", 0)


# ============================================================================
# Display config
# ============================================================================
def test_display_config_default_and_clamp():
    assert DoctrineDisplayConfig.active().effective_per_page() == 24
    assert clamp_per_page(9999) == MAX_PER_PAGE
    assert clamp_per_page(1) == 6      # below the floor -> floor
    assert clamp_per_page("junk") == 24


# ============================================================================
# Doctrines page — pagination + ordering
# ============================================================================
def _set_per_page(n):
    cfg = DoctrineDisplayConfig.active()
    cfg.per_page = n
    cfg.save()


def test_doctrine_list_paginates(client, django_user_model, sde):
    _set_per_page(6)  # floor; we create 8 so there are 2 pages
    for i in range(8):
        _doc(f"Doc {i:02d}", RIFTER_BARE)
    user, _ = _member(django_user_model)  # no snapshot -> priority/name order
    client.force_login(user)

    p1 = client.get("/doctrines/").content.decode()
    assert "Page 1 of 2" in p1
    assert 'rel="next"' in p1
    assert "Doc 00" in p1 and "Doc 05" in p1 and "Doc 06" not in p1

    p2 = client.get("/doctrines/?page=2").content.decode()
    assert "Doc 06" in p2 and "Doc 07" in p2 and "Doc 00" not in p2


def test_doctrine_list_orders_by_readiness(client, django_user_model, sde):
    # Names deliberately anti-sorted so only readiness ordering yields this order.
    _doc("AA Far", RIFTER_FULL)      # missing 3301 + 3300 -> not_ready(2)
    _doc("MM Close", RIFTER_AC)      # missing 3301 -> not_ready(1)
    _doc("ZZ Optimal", RIFTER_BARE)  # all present -> optimal
    user, _ = _member(django_user_model, skills={"3331": {"trained_level": 5}})
    client.force_login(user)

    html = client.get("/doctrines/").content.decode()
    assert html.index("ZZ Optimal") < html.index("MM Close") < html.index("AA Far")


def test_doctrine_list_htmx_fragment_has_pagination(client, django_user_model, sde):
    _set_per_page(6)
    for i in range(8):
        _doc(f"Doc {i:02d}", RIFTER_BARE)
    user, _ = _member(django_user_model)
    client.force_login(user)
    frag = client.get("/doctrines/", HTTP_HX_REQUEST="true").content.decode()
    assert 'id="dc-results"' in frag
    assert 'hx-target="#dc-results"' in frag   # page links swap only the fragment
    assert "Page 1 of 2" in frag


# ============================================================================
# Shipyard — pagination + ordering
# ============================================================================
def test_shipyard_paginates(client, django_user_model, sde):
    _set_per_page(6)
    for i in range(8):
        _doc(f"Doc {i:02d}", RIFTER_BARE)
    user, _ = _member(django_user_model)
    client.force_login(user)
    p1 = client.get("/doctrines/ships/").content.decode()
    assert "Page 1 of 2" in p1
    p2 = client.get("/doctrines/ships/?page=2").content.decode()
    assert "Page 2 of 2" in p2


def test_shipyard_default_order_is_readiness(client, django_user_model, sde):
    _doc("AA Far", RIFTER_FULL)
    _doc("ZZ Optimal", RIFTER_BARE)
    user, _ = _member(django_user_model, skills={"3331": {"trained_level": 5}})
    client.force_login(user)
    html = client.get("/doctrines/ships/").content.decode()
    # Fit names are shown; the optimal one comes first under the default sort.
    assert html.index("ZZ Optimal") < html.index("AA Far")


# ============================================================================
# Admin settings page
# ============================================================================
def test_settings_officer_can_change_page_size(client, django_user_model, sde):
    client.force_login(_officer(django_user_model))
    assert client.get("/ops/admin/doctrines/settings/").status_code == 200
    client.post("/ops/admin/doctrines/settings/", {"per_page": "48"})
    assert DoctrineDisplayConfig.active().per_page == 48


def test_settings_clamps_out_of_range(client, django_user_model, sde):
    client.force_login(_officer(django_user_model))
    client.post("/ops/admin/doctrines/settings/", {"per_page": "5000"})
    assert DoctrineDisplayConfig.active().per_page == MAX_PER_PAGE


def test_settings_blocks_normal_pilots(client, django_user_model, sde):
    user, _ = _member(django_user_model)
    client.force_login(user)
    assert client.get("/ops/admin/doctrines/settings/").status_code == 403


def test_doctrines_admin_links_display_settings(client, django_user_model, sde):
    client.force_login(_officer(django_user_model))
    html = client.get("/ops/admin/doctrines/").content
    assert b"Display settings" in html
    assert b"/ops/admin/doctrines/settings/" in html
