"""KB-31 battle reports v2 — co-occurrence side detection, overrides, timeline, overlays.

Synthetic two-sided fight in Tama:
  * km1  we (HOME) kill an enemy Stabber           (100M)   — our kill
  * km2  they (ENEMY) kill our Guardian (logi)     (250M/200M destroyed) — our loss
  * km3  we + a NEUTRAL corp kill an enemy Rifter  (50M)    — our kill, neutral co-attacks

Co-occurrence therefore clusters HOME + NEUTRAL onto one side and ENEMY onto the
other; the neutral is the entity an officer might reassign. All figures below are
hand-derived so the scoreboard/timeline maths is pinned.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.identity.models import RoleAssignment
from apps.killboard import battle_sides
from apps.killboard.battle import generate_battle_report
from apps.killboard.models import (
    BattleReport,
    BattleReportSideMember,
    BattleReportSideOverride,
    Killmail,
    KillmailParticipant,
)
from apps.sso.services import ensure_role
from core import rbac

HOME_CORP, ENEMY_CORP, NEUTRAL_CORP = 98000001, 98000002, 98000003
OUR_A, OUR_B = 95000001, 95000002
ENEMY_A, ENEMY_B = 97000001, 97000002
NEUTRAL_P = 96000001
GUARDIAN, STABBER, RIFTER = 11987, 622, 587
TAMA = 30002813

_CORP = BattleReportSideMember.EntityType.CORPORATION


def _user(django_user_model, role, suffix=""):
    user, _ = django_user_model.objects.get_or_create(username=f"kb-v2-{role}{suffix}")
    RoleAssignment.objects.get_or_create(user=user, role=ensure_role(role))
    return user


def _km(kid, t, victim_ship, victim_corp, victim_char, value, role, destroyed=0):
    return Killmail.objects.create(
        killmail_id=kid, killmail_hash=f"h{kid}", killmail_time=t, solar_system_id=TAMA,
        victim_ship_type_id=victim_ship, victim_corporation_id=victim_corp,
        victim_character_id=victim_char, total_value=Decimal(value),
        destroyed_value=Decimal(destroyed), involves_home_corp=True, home_corp_role=role,
    )


def _att(km, seq, char, corp, ship, alliance=None):
    KillmailParticipant.objects.create(
        killmail=km, role="attacker", seq=seq, character_id=char, corporation_id=corp,
        ship_type_id=ship, alliance_id=alliance,
    )


def _vic(km, char, corp, ship):
    KillmailParticipant.objects.create(
        killmail=km, role="victim", seq=0, character_id=char, corporation_id=corp, ship_type_id=ship,
    )


def _build_battle(title="Battle in Tama"):
    t0 = timezone.now() - dt.timedelta(minutes=30)
    t1 = timezone.now() - dt.timedelta(minutes=20)
    t2 = timezone.now() - dt.timedelta(minutes=10)

    # km1: HOME kills ENEMY Stabber
    km1 = _km(1, t0, STABBER, ENEMY_CORP, ENEMY_A, 100_000_000, "attacker")
    _vic(km1, ENEMY_A, ENEMY_CORP, STABBER)
    _att(km1, 1, OUR_A, HOME_CORP, RIFTER)
    _att(km1, 2, OUR_B, HOME_CORP, RIFTER)
    # km2: ENEMY kills our Guardian (logi loss), 250M value / 200M destroyed
    km2 = _km(2, t1, GUARDIAN, HOME_CORP, OUR_A, 250_000_000, "victim", destroyed=200_000_000)
    _vic(km2, OUR_A, HOME_CORP, GUARDIAN)
    _att(km2, 1, ENEMY_A, ENEMY_CORP, STABBER)
    _att(km2, 2, ENEMY_B, ENEMY_CORP, STABBER)
    # km3: HOME + NEUTRAL kill ENEMY Rifter
    km3 = _km(3, t2, RIFTER, ENEMY_CORP, ENEMY_B, 50_000_000, "attacker")
    _vic(km3, ENEMY_B, ENEMY_CORP, RIFTER)
    _att(km3, 1, OUR_B, HOME_CORP, RIFTER)
    _att(km3, 2, NEUTRAL_P, NEUTRAL_CORP, RIFTER)

    report = BattleReport.objects.create(
        title=title, system_ids=[TAMA], start_time=t0, end_time=t2,
        sides={"corporations": []}, ship_breakdown={},
    )
    report.killmails.set([1, 2, 3])
    battle_sides.recompute_sides(report)
    return report


def _side(report, index):
    return report.detected_sides.get(index=index)


def _members(side):
    return {(m.entity_type, m.entity_id) for m in side.members.all()}


# --------------------------------------------------------------------------- #
#  Side detection
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_two_sides_detected_with_victims_split_correctly():
    report = _build_battle()
    sides = list(report.detected_sides.all())
    assert len(sides) == 2

    home = _side(report, 0)
    them = _side(report, 1)
    assert home.is_home_side and not them.is_home_side
    # HOME + the co-attacking NEUTRAL cluster together; ENEMY is the other side.
    assert _members(home) == {(_CORP, HOME_CORP), (_CORP, NEUTRAL_CORP)}
    assert _members(them) == {(_CORP, ENEMY_CORP)}


@pytest.mark.django_db
def test_side_detection_is_deterministic():
    report = _build_battle()
    first = battle_sides.detect_sides(report)
    second = battle_sides.detect_sides(report)
    assert first == second
    # Home side first, membership sorted — a stable, repeatable partition.
    assert first[0] == [(_CORP, HOME_CORP), (_CORP, NEUTRAL_CORP)]
    assert first[1] == [(_CORP, ENEMY_CORP)]


@pytest.mark.django_db
def test_pure_gank_is_a_single_side():
    # No counter-fire: everyone shooting is one side (a 1-side partition is valid).
    t = timezone.now()
    km = _km(9, t, GUARDIAN, ENEMY_CORP, ENEMY_A, 10_000_000, "attacker")
    _vic(km, ENEMY_A, ENEMY_CORP, GUARDIAN)
    _att(km, 1, OUR_A, HOME_CORP, RIFTER)
    report = BattleReport.objects.create(
        title="Gank", system_ids=[TAMA], start_time=t, end_time=t, sides={}, ship_breakdown={},
    )
    report.killmails.set([9])
    battle_sides.recompute_sides(report)
    sides = list(report.detected_sides.all())
    assert len(sides) == 2  # attacker HOME on one side, victim ENEMY on the other
    assert _side(report, 0).is_home_side


# --------------------------------------------------------------------------- #
#  Manual reassignment survives recompute
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_manual_reassign_survives_recompute(django_user_model):
    report = _build_battle()
    officer = _user(django_user_model, rbac.ROLE_OFFICER)
    # Detection put NEUTRAL on our side (index 0); move it to the enemy side (index 1).
    assert (_CORP, NEUTRAL_CORP) in _members(_side(report, 0))
    ok = battle_sides.move_entity(report, _CORP, NEUTRAL_CORP, 1, actor=officer)
    assert ok

    them = _side(report, 1)
    assert (_CORP, NEUTRAL_CORP) in _members(them)
    moved = them.members.get(entity_type=_CORP, entity_id=NEUTRAL_CORP)
    assert moved.is_manual is True
    assert BattleReportSideOverride.objects.filter(
        report=report, entity_id=NEUTRAL_CORP, side_index=1
    ).exists()

    # A fresh recompute (detection would re-cluster NEUTRAL with HOME) must respect the override.
    battle_sides.recompute_sides(report)
    assert (_CORP, NEUTRAL_CORP) in _members(_side(report, 1))
    assert (_CORP, NEUTRAL_CORP) not in _members(_side(report, 0))


# --------------------------------------------------------------------------- #
#  Scoreboard maths (hand-derived)
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_side_scoreboard_maths():
    report = _build_battle()
    home = _side(report, 0)
    them = _side(report, 1)

    # HOME+NEUTRAL: killed km1 + km3 (2), lost km2 (1); destroyed 100M+50M, lost 250M.
    assert home.kills == 2 and home.losses == 1
    assert home.isk_destroyed == Decimal("150000000")
    assert home.isk_lost == Decimal("250000000")
    assert home.pilot_count == 3  # OUR_A, OUR_B, NEUTRAL_P
    assert round(home.efficiency * 100) == 38  # 150 / (150+250)

    # ENEMY: killed km2 (1), lost km1 + km3 (2); destroyed 250M, lost 150M.
    assert them.kills == 1 and them.losses == 2
    assert them.isk_destroyed == Decimal("250000000")
    assert them.isk_lost == Decimal("150000000")
    assert them.pilot_count == 2  # ENEMY_A, ENEMY_B
    assert round(them.efficiency * 100) == 62

    # Per-corp breakdown within the home side.
    home_corp = home.members.get(entity_type=_CORP, entity_id=HOME_CORP)
    neutral = home.members.get(entity_type=_CORP, entity_id=NEUTRAL_CORP)
    assert home_corp.kills == 2 and home_corp.losses == 1 and home_corp.isk_lost == Decimal("250000000")
    assert neutral.kills == 1 and neutral.losses == 0 and neutral.isk_lost == Decimal("0")


# --------------------------------------------------------------------------- #
#  Timeline ordering + ISK swing
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_timeline_ordering_and_swing():
    report = _build_battle()
    home = _side(report, 0)
    tl = battle_sides.battle_timeline(report, home)
    rows = tl["rows"]
    assert [r["killmail_id"] for r in rows] == [1, 2, 3]  # chronological
    # +100M (our kill), −250M (our loss), +50M (our kill).
    assert [r["swing"] for r in rows] == [
        Decimal("100000000"), Decimal("-150000000"), Decimal("-100000000"),
    ]
    assert tl["final_swing"] == Decimal("-100000000")
    assert rows[0]["ref_killed"] and rows[1]["ref_lost"] and rows[2]["ref_killed"]
    assert tl["polyline"]  # a non-empty SVG points string


# --------------------------------------------------------------------------- #
#  Public permalink
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_public_permalink_anonymous_access(client, sde):
    report = _build_battle()
    report.is_public = True
    report.save(update_fields=["is_public"])
    assert report.slug  # auto-populated
    resp = client.get(f"/killboard/battles/r/{report.slug}/")
    assert resp.status_code == 200


@pytest.mark.django_db
def test_private_permalink_404s_for_anonymous(client, sde):
    report = _build_battle()  # is_public defaults False
    resp = client.get(f"/killboard/battles/r/{report.slug}/")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_member_pk_page_requires_login(client, django_user_model, sde):
    report = _build_battle()
    assert client.get(f"/killboard/battles/{report.pk}/").status_code in (302, 403)
    client.force_login(_user(django_user_model, rbac.ROLE_MEMBER, "-pk"))
    assert client.get(f"/killboard/battles/{report.pk}/").status_code == 200


# --------------------------------------------------------------------------- #
#  br.evetools export URL
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_brevetools_url_format():
    report = _build_battle()
    url = battle_sides.brevetools_url(report)
    stamp = report.start_time.strftime("%Y%m%d%H%M")
    assert url == f"https://br.evetools.org/related/{TAMA}/{stamp}"


# --------------------------------------------------------------------------- #
#  Overlays: SRP liability, doctrine compliance, op overlap
# --------------------------------------------------------------------------- #
def _permissive_program():
    from apps.srp.models import SrpProgram

    return SrpProgram.objects.create(
        name="Test", is_active=True, enabled=True,
        payout_mode=SrpProgram.PayoutMode.ISK_FULL,
        valuation=SrpProgram.Valuation.ACTUAL_LOSS,
        require_doctrine=False, require_fleet_op=False,
    )


@pytest.mark.django_db
def test_srp_liability_sums_eligibility_payouts():
    from apps.srp import services as srp_services

    report = _build_battle()
    _permissive_program()
    home = _side(report, 0)

    liability = battle_sides.srp_liability(report, home)
    # Our one loss (km2) is eligible; ACTUAL_LOSS pays the destroyed value (200M).
    km2 = Killmail.objects.get(killmail_id=2)
    expected = srp_services.eligibility(km2)["payout"]
    assert expected == Decimal("200000000")
    assert liability["losses"] == 1
    assert liability["eligible"] == 1
    assert liability["total"] == expected

    # The enemy side carries no home-corp SRP liability.
    them = battle_sides.srp_liability(report, _side(report, 1))
    assert them["total"] == Decimal("0")


@pytest.mark.django_db
def test_srp_overlay_officer_only_in_view(client, django_user_model, sde):
    report = _build_battle()
    _permissive_program()
    report.is_public = False
    report.save(update_fields=["is_public"])

    client.force_login(_user(django_user_model, rbac.ROLE_OFFICER, "-srp"))
    officer_body = client.get(f"/killboard/battles/{report.pk}/").content
    assert b"SRP liability" in officer_body

    client.force_login(_user(django_user_model, rbac.ROLE_MEMBER, "-srp"))
    member_body = client.get(f"/killboard/battles/{report.pk}/").content
    assert b"SRP liability" not in member_body


@pytest.mark.django_db
def test_doctrine_compliance_overlay():
    from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit
    from apps.killboard.models import FitDeviation

    report = _build_battle()
    home = _side(report, 0)
    # No doctrine-tagged losses yet.
    assert battle_sides.doctrine_compliance(report, home) is None

    cat = DoctrineCategory.objects.create(key="armor", label="Armor")
    doc = Doctrine.objects.create(name="Armor HAC", category=cat)
    fit = DoctrineFit.objects.create(doctrine=doc, name="Guardian", ship_type_id=GUARDIAN, modules=[])
    km2 = Killmail.objects.get(killmail_id=2)
    km2.doctrine_fit = fit
    km2.save(update_fields=["doctrine_fit"])

    # Tagged + no deviation → 100% compliant.
    clean = battle_sides.doctrine_compliance(report, home)
    assert clean == {"tagged": 1, "clean": 1, "percent": 100}

    # A deviation flips it to 0%.
    FitDeviation.objects.create(killmail=km2, doctrine_fit=fit,
                                missing=[{"type_id": 100, "quantity": 1}], extra=[])
    deviated = battle_sides.doctrine_compliance(report, home)
    assert deviated == {"tagged": 1, "clean": 0, "percent": 0}


@pytest.mark.django_db
def test_op_overlap_chip():
    from apps.operations.models import Operation

    report = _build_battle()
    assert battle_sides.op_overlap(report) is None

    # An op whose window covers the battle is surfaced.
    op = Operation.objects.create(
        name="Home Defence", target_at=report.start_time - dt.timedelta(minutes=5),
        duration_minutes=120, status=Operation.Status.DONE, srp=Operation.Srp.CORP,
    )
    assert battle_sides.op_overlap(report) == op

    op.status = Operation.Status.CANCELLED
    op.save(update_fields=["status"])
    assert battle_sides.op_overlap(report) is None  # cancelled ops don't count


# --------------------------------------------------------------------------- #
#  Officer reassign view is gated + audited
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_side_move_view_is_officer_gated(client, django_user_model, sde):
    report = _build_battle()
    url = f"/killboard/battles/{report.pk}/side/move/"
    payload = {"entity_type": _CORP, "entity_id": NEUTRAL_CORP, "side_index": 1}

    client.force_login(_user(django_user_model, rbac.ROLE_MEMBER, "-mv"))
    assert client.post(url, payload).status_code in (302, 403)
    assert not BattleReportSideOverride.objects.filter(report=report).exists()

    client.force_login(_user(django_user_model, rbac.ROLE_OFFICER, "-mv"))
    assert client.post(url, payload).status_code == 302
    assert BattleReportSideOverride.objects.filter(report=report, entity_id=NEUTRAL_CORP).exists()


@pytest.mark.django_db
def test_generate_battle_report_populates_v2_sides(sde):
    # The end-to-end producer path also fills the v2 side tables.
    t = timezone.now() - dt.timedelta(minutes=5)
    km = _km(21, t, STABBER, ENEMY_CORP, ENEMY_A, 5_000_000, "attacker")
    _vic(km, ENEMY_A, ENEMY_CORP, STABBER)
    _att(km, 1, OUR_A, HOME_CORP, RIFTER)
    report = generate_battle_report(TAMA, hours=1)
    assert report is not None
    assert report.detected_sides.count() >= 1
    assert report.slug
