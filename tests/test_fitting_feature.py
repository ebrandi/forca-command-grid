"""Tocha's Lab feature tests: services, views, import/export, sharing, and security.

Uses a small self-contained fixture built from real EVE type ids (so EFT name resolution
works) — original data, nothing copied from an external fit library.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.fitting import services
from apps.fitting.engine import attributes as A
from apps.fitting.engine.types import SkillProfile
from apps.fitting.models import Fit
from apps.sso.models import EveCharacter

RIFTER, AC, FUSION, DC = 587, 484, 192, 2046
MINFRIG, GUNNERY, SPT = 3331, 3300, 3320


@pytest.fixture
def dogma(db):
    from apps.admin_audit.models import AppSetting
    from apps.market.models import MarketPrice
    from apps.sde.models import (
        SdeCategory,
        SdeGroup,
        SdeShipBonus,
        SdeType,
        SdeTypeAttribute,
        SdeTypeEffect,
        SdeTypeSkill,
    )
    R, AR, HR = A.SHIELD_RESONANCE, A.ARMOR_RESONANCE, A.HULL_RESONANCE
    for cid, name in [(6, "Ship"), (7, "Module"), (8, "Charge"), (16, "Skill")]:
        SdeCategory.objects.get_or_create(category_id=cid, defaults={"name": name})
    for gid, cid, name in [(25, 6, "Frigate"), (55, 7, "Projectile Turret"), (60, 7, "Damage Control"),
                           (83, 8, "Ammo"), (349, 16, "Skill")]:
        SdeGroup.objects.get_or_create(group_id=gid, defaults={"category_id": cid, "name": name})
    types = {
        RIFTER: ("Rifter", 25, {
            A.CPU_OUTPUT: 125, A.POWER_OUTPUT: 41, A.CALIBRATION: 400,
            A.HI_SLOTS: 4, A.MED_SLOTS: 3, A.LOW_SLOTS: 3, A.RIG_SLOTS: 3,
            A.TURRET_HARDPOINTS: 3, A.LAUNCHER_HARDPOINTS: 1,
            A.SHIELD_HP: 450, A.ARMOR_HP: 400, A.HULL_HP: 350,
            R["em"]: 1.0, R["thermal"]: 0.84, R["kinetic"]: 0.6, R["explosive"]: 0.5,
            AR["em"]: 0.5, AR["thermal"]: 0.55, AR["kinetic"]: 0.75, AR["explosive"]: 0.9,
            HR["em"]: 1.0, HR["thermal"]: 1.0, HR["kinetic"]: 1.0, HR["explosive"]: 1.0,
            A.CAP_CAPACITY: 330, A.CAP_RECHARGE_RATE: 187500, A.MASS: 1067000, A.AGILITY: 2.9,
            A.MAX_VELOCITY: 355, A.SIGNATURE_RADIUS: 35, A.WARP_SPEED_MULT: 5.0,
            A.MAX_TARGET_RANGE: 20000, A.MAX_LOCKED_TARGETS: 5, A.SCAN_RESOLUTION: 730,
            A.SENSOR_STRENGTHS["gravimetric"]: 11, A.CAPACITY_CARGO: 140,
        }),
        AC: ("200mm AutoCannon I", 55, {A.CPU_USAGE: 3, A.POWER_USAGE: 6, A.DAMAGE_MULTIPLIER: 1.0,
             A.RATE_OF_FIRE: 2475, A.OPTIMAL_RANGE: 1200, A.FALLOFF: 7500, A.TRACKING_SPEED: 0.198}),
        FUSION: ("Fusion S", 83, {A.EXPLOSIVE_DAMAGE: 8.8}),
        DC: ("Damage Control I", 60, {A.CPU_USAGE: 5, A.POWER_USAGE: 1,
             R["em"]: 0.875, R["thermal"]: 0.875, R["kinetic"]: 0.875, R["explosive"]: 0.875,
             AR["em"]: 0.85, AR["thermal"]: 0.85, AR["kinetic"]: 0.85, AR["explosive"]: 0.85,
             HR["em"]: 0.5, HR["thermal"]: 0.5, HR["kinetic"]: 0.5, HR["explosive"]: 0.5}),
    }
    for tid, (name, gid, attrs) in types.items():
        SdeType.objects.get_or_create(type_id=tid, defaults={"group_id": gid, "name": name})
        SdeTypeAttribute.objects.bulk_create(
            [SdeTypeAttribute(type_id=tid, attribute_id=k, value=v) for k, v in attrs.items()],
            ignore_conflicts=True)
        MarketPrice.objects.get_or_create(
            type_id=tid, location=None, profile=MarketPrice.Profile.JITA_SELL,
            defaults={"sell_min": 1000})
    SdeTypeEffect.objects.bulk_create([
        SdeTypeEffect(type_id=AC, effect_id=A.EFFECT_HI_POWER, is_default=True),
        SdeTypeEffect(type_id=DC, effect_id=A.EFFECT_LO_POWER, is_default=True),
    ], ignore_conflicts=True)
    for sid, name in [(MINFRIG, "Minmatar Frigate"), (GUNNERY, "Gunnery"), (SPT, "Small Projectile Turret")]:
        SdeType.objects.get_or_create(type_id=sid, defaults={"group_id": 349, "name": name})
    SdeTypeSkill.objects.get_or_create(type_id=RIFTER, skill_type_id=MINFRIG, defaults={"level": 1})
    SdeTypeSkill.objects.get_or_create(type_id=AC, skill_type_id=GUNNERY, defaults={"level": 1})
    SdeTypeSkill.objects.get_or_create(type_id=AC, skill_type_id=SPT, defaults={"level": 1})
    SdeShipBonus.objects.get_or_create(
        ship_type_id=RIFTER, key="minfrig_dmg",
        defaults={"target_attribute_id": A.DAMAGE_MULTIPLIER, "amount": 5.0, "per_level": True,
                  "skill_type_id": MINFRIG, "match_group_ids": [55], "label": "Minmatar Frigate"})
    AppSetting.objects.update_or_create(key="dogma_data_version", defaults={"value": {"version": "test"}})
    return True


def _member(username, char_id, name, role=None):
    from apps.identity.models import RoleAssignment
    from apps.sso.services import ensure_role
    from core import rbac
    User = get_user_model()
    u = User.objects.create(username=username, first_name=name)
    u.set_unusable_password()
    u.save()
    EveCharacter.objects.create(character_id=char_id, user=u, name=name, is_main=True,
                                is_corp_member=True, is_corp_director=(role == rbac.ROLE_DIRECTOR))
    RoleAssignment.objects.create(user=u, role=ensure_role(rbac.ROLE_MEMBER))
    if role:
        RoleAssignment.objects.create(user=u, role=ensure_role(role))
    return u


@pytest.fixture
def owner(dogma):
    return _member("eve:2001", 2001, "Owner Pilot")


@pytest.fixture
def other(dogma):
    return _member("eve:2002", 2002, "Other Pilot")


EFT = "[Rifter, Test Rifter]\n200mm AutoCannon I, Fusion S\n200mm AutoCannon I, Fusion S\n\nDamage Control I"


# --------------------------------------------------------------------------- #
# Services: import/export
# --------------------------------------------------------------------------- #
def test_import_eft_preserves_charge_and_infers_slots(dogma):
    parsed = services.import_eft(EFT)
    assert parsed["ship_type_id"] == RIFTER
    guns = [i for i in parsed["items"] if i["type_id"] == AC]
    assert len(guns) == 2
    assert all(g["slot"] == "high" for g in guns)          # inferred from hiPower effect
    assert all(g["charge_type_id"] == FUSION for g in guns)  # charge preserved (unlike lossy parser)
    dc = [i for i in parsed["items"] if i["type_id"] == DC][0]
    assert dc["slot"] == "low"                              # inferred from loPower effect


def test_import_eft_reports_unresolved(dogma):
    parsed = services.import_eft("[Rifter, X]\nNonexistent Module 9000")
    assert "Nonexistent Module 9000" in parsed["unresolved"]


def test_export_eft_is_deterministic(dogma):
    parsed = services.import_eft(EFT)
    a = services.export_eft(RIFTER, parsed["items"], "Test Rifter")
    b = services.export_eft(RIFTER, parsed["items"], "Test Rifter")
    assert a == b
    assert a.startswith("[Rifter, Test Rifter]")
    assert "200mm AutoCannon I, Fusion S" in a


def test_import_eft_rejects_non_eft(dogma):
    with pytest.raises(ValueError):
        services.import_eft("just some text, not a fit")


# --------------------------------------------------------------------------- #
# Services: persistence + pricing + compare
# --------------------------------------------------------------------------- #
def test_create_save_and_fork(owner, dogma):
    parsed = services.import_eft(EFT)
    fit = services.create_fit(owner, name="R1", ship_type_id=RIFTER, items=parsed["items"])
    assert fit.current_revision.revision_number == 1
    services.save_revision(fit, ship_type_id=RIFTER, items=parsed["items"][:1], user=owner)
    fit.refresh_from_db()
    assert fit.current_revision.revision_number == 2
    assert fit.revisions.count() == 2  # history is append-only
    fork = services.fork_fit(fit, fit.current_revision, owner)
    assert fork.forked_from_id == fit.pk and fork.origin == "fork"


def test_price_fit_uses_market_authority(owner, dogma):
    parsed = services.import_eft(EFT)
    priced = services.price_fit(RIFTER, parsed["items"])
    assert priced["total"] > 0            # 1000 ISK per seeded type
    assert priced["as_of"] is not None


def test_compare_shows_deltas(owner, dogma):
    parsed = services.import_eft(EFT)
    fit = services.create_fit(owner, name="R", ship_type_id=RIFTER, items=parsed["items"])
    r2 = services.save_revision(fit, ship_type_id=RIFTER, items=parsed["items"][:1], user=owner)
    diff = services.compare(fit.revisions.get(revision_number=1), r2, SkillProfile.omniscient())
    dps = next(m for m in diff["metrics"] if m["key"] == "total_dps")
    assert dps["delta"] < 0  # removing a gun lowers DPS
    assert diff["removed"]


# --------------------------------------------------------------------------- #
# Views + security
# --------------------------------------------------------------------------- #
def test_index_and_create_flow(client, owner, dogma):
    client.force_login(owner)
    assert client.get(reverse("fitting:index")).status_code == 200
    resp = client.post(reverse("fitting:create"), {"ship": "Rifter", "name": "My Rifter"})
    assert resp.status_code == 302
    fit = Fit.objects.get(owner=owner)
    detail = client.get(reverse("fitting:detail", args=[fit.pk]))
    assert detail.status_code == 200
    assert b"Tocha" in detail.content


def test_import_eft_view_creates_fit(client, owner, dogma):
    client.force_login(owner)
    resp = client.post(reverse("fitting:import_eft"), {"eft": EFT})
    assert resp.status_code == 302
    fit = Fit.objects.get(owner=owner)
    assert fit.ship_type_id == RIFTER
    assert fit.current_revision.items
    # the detail page renders REAL computed telemetry, not just labels (regression: the
    # engine's nested telemetry must be flattened for the template).
    detail = client.get(reverse("fitting:detail", args=[fit.pk]) + "?skills=none")
    assert detail.status_code == 200
    assert b"450" in detail.content  # Rifter base shield HP (untrained, no Shield Management)


def test_telemetry_endpoint_renders_server_side(client, owner, dogma):
    client.force_login(owner)
    parsed = services.import_eft(EFT)
    resp = client.post(reverse("fitting:telemetry"), {
        "ship_type_id": RIFTER, "items": json.dumps(parsed["items"]), "skills": "allv"})
    assert resp.status_code == 200
    assert b"DPS" in resp.content and b"EHP" in resp.content


def test_telemetry_rejects_oversized_payload(client, owner, dogma):
    client.force_login(owner)
    huge = json.dumps([{"type_id": AC} for _ in range(400)])  # over the 300 item cap
    resp = client.post(reverse("fitting:telemetry"), {"ship_type_id": RIFTER, "items": huge})
    assert resp.status_code == 400


def test_share_link_lifecycle(client, owner, other, dogma):
    client.force_login(owner)
    fit = services.create_fit(owner, name="R", ship_type_id=RIFTER, items=[])
    client.post(reverse("fitting:share", args=[fit.pk]))
    fit.refresh_from_db()
    assert fit.share_token and fit.public_link_active
    # another pilot can open the public link by token
    client.force_login(other)
    assert client.get(reverse("fitting:shared", args=[fit.share_token])).status_code == 200
    # owner revokes -> token 404s
    client.force_login(owner)
    client.post(reverse("fitting:unshare", args=[fit.pk]))
    assert client.get(reverse("fitting:shared", args=[fit.share_token])).status_code == 404


def test_idor_private_fit_not_viewable(client, owner, other, dogma):
    fit = services.create_fit(owner, name="secret", ship_type_id=RIFTER, items=[])
    client.force_login(other)
    assert client.get(reverse("fitting:detail", args=[fit.pk])).status_code == 404
    # and cannot save into it
    assert client.post(reverse("fitting:save", args=[fit.pk]),
                       {"items": "[]", "ship_type_id": RIFTER}).status_code == 404


def test_manage_rename_duplicate_archive_restore_delete(client, owner, dogma):
    client.force_login(owner)
    fit = services.create_fit(owner, name="Orig", ship_type_id=RIFTER, items=[])
    client.post(reverse("fitting:rename", args=[fit.pk]), {"name": "Renamed"})
    fit.refresh_from_db()
    assert fit.name == "Renamed"
    client.post(reverse("fitting:duplicate", args=[fit.pk]))
    assert Fit.objects.filter(owner=owner, origin="duplicate").exists()
    client.post(reverse("fitting:archive", args=[fit.pk]))
    fit.refresh_from_db()
    assert fit.is_archived
    client.post(reverse("fitting:restore", args=[fit.pk]))
    fit.refresh_from_db()
    assert not fit.is_archived
    client.post(reverse("fitting:delete", args=[fit.pk]))
    assert not Fit.objects.filter(pk=fit.pk).exists()  # hard delete


def test_restore_revision_appends_a_new_revision(client, owner, dogma):
    client.force_login(owner)
    parsed = services.import_eft(EFT)
    fit = services.create_fit(owner, name="R", ship_type_id=RIFTER, items=parsed["items"])   # rev1
    services.save_revision(fit, ship_type_id=RIFTER, items=parsed["items"][:1], user=owner)  # rev2
    fit.refresh_from_db()
    assert len(fit.current_revision.items) == 1
    client.post(reverse("fitting:restore_revision", args=[fit.pk, 1]))
    fit.refresh_from_db()
    assert fit.current_revision.revision_number == 3                       # append-only history
    assert len(fit.current_revision.items) == len(parsed["items"])         # rev3 content == rev1


def test_management_is_owner_only(client, owner, other, dogma):
    fit = services.create_fit(owner, name="Secret", ship_type_id=RIFTER, items=[])
    client.force_login(other)
    assert client.post(reverse("fitting:rename", args=[fit.pk]), {"name": "x"}).status_code == 404
    assert client.post(reverse("fitting:delete", args=[fit.pk])).status_code == 404
    assert Fit.objects.filter(pk=fit.pk).exists()


def test_load_doctrine_into_simulator(client, owner, dogma):
    from apps.doctrines.models import Doctrine, DoctrineFit
    doc = Doctrine.objects.create(name="Test Doctrine", status=Doctrine.Status.ACTIVE)
    dfit = DoctrineFit.objects.create(
        doctrine=doc, name="Rifter Tackle", ship_type_id=RIFTER,
        modules=[{"type_id": AC, "quantity": 3, "slot": "high"}])
    client.force_login(owner)
    assert b"Test Doctrine" in client.get(reverse("fitting:index")).content    # listed to a member
    resp = client.post(reverse("fitting:import_doctrine", args=[dfit.pk]))
    assert resp.status_code == 302
    loaded = Fit.objects.filter(owner=owner, origin="doctrine").first()
    assert loaded and loaded.ship_type_id == RIFTER
    assert any(it["type_id"] == AC for it in loaded.current_revision.items)


def test_shared_view_requires_valid_token(client, owner, dogma):
    services.create_fit(owner, name="R", ship_type_id=RIFTER, items=[])  # exists but never shared
    # a guessed token resolves nothing
    assert client.get(reverse("fitting:shared", args=["deadbeefdeadbeef"])).status_code == 404


def test_export_eft_view(client, owner, dogma):
    client.force_login(owner)
    parsed = services.import_eft(EFT)
    fit = services.create_fit(owner, name="R", ship_type_id=RIFTER, items=parsed["items"])
    resp = client.get(reverse("fitting:export_eft", args=[fit.pk]))
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/plain")
    assert resp.content.startswith(b"[Rifter,")


def test_brand_localised_to_pt_br():
    """pt-BR must render the mandated 'Laboratório do Tocha'; other locales keep the
    brand 'Tocha's Lab' (English fallback). 'Tocha' is a proper name, never translated."""
    import re
    from pathlib import Path

    from django.conf import settings
    po = Path(settings.BASE_DIR) / "locale" / "pt_BR" / "LC_MESSAGES" / "django.po"
    text = po.read_text(encoding="utf-8")
    m = re.search(r'msgid "Tocha\'s Lab"\s*\nmsgstr "([^"]*)"', text)
    assert m and m.group(1) == "Laboratório do Tocha"


def test_training_export(client, owner, dogma):
    client.force_login(owner)
    parsed = services.import_eft(EFT)
    fit = services.create_fit(owner, name="R", ship_type_id=RIFTER, items=parsed["items"])
    resp = client.get(reverse("fitting:training_export", args=[fit.pk]) + "?skills=none")
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/plain")
    body = resp.content.decode()
    # untrained pilot -> the fit's required skills appear as an EVE skill-planner paste
    assert "Gunnery 1" in body or "Minmatar Frigate 1" in body


def test_search_endpoints(client, owner, dogma):
    client.force_login(owner)
    hulls = client.get(reverse("fitting:search_hulls"), {"q": "Rif"})
    assert hulls.status_code == 200 and any(r["type_id"] == RIFTER for r in hulls.json()["results"])
    mods = client.get(reverse("fitting:search_modules"), {"q": "AutoCannon"})
    assert any(r["type_id"] == AC for r in mods.json()["results"])
