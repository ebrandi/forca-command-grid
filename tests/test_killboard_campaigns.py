"""KB-32 combat campaigns (WS-C2) — scope matcher, aggregation, overlays, gating.

Synthetic home-corp set (FORCA_HOME_CORP_ID == 98000001 in test settings):
  * km1  we KILL an enemy Stabber in Jita              (100M)  — our kill
  * km2  we LOSE OUR_A's Guardian in Jita  (250M / 200M destroyed) — our loss
  * km3  we KILL an enemy Rifter in Tama                (50M)   — our kill

Every count/ISK figure below is hand-derived so the aggregation maths is pinned.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.apps import apps as django_apps
from django.utils import timezone

from apps.identity.models import RoleAssignment
from apps.killboard import combat_campaigns
from apps.killboard.models import CombatCampaign, Killmail, KillmailParticipant
from apps.sso.services import ensure_role
from core import rbac

HOME_CORP, ENEMY_CORP = 98000001, 98000002
ENEMY_ALLIANCE = 99000001
OUR_A, OUR_B = 95000001, 95000002
ENEMY_A, ENEMY_B = 97000001, 97000002
GUARDIAN, STABBER, RIFTER = 11987, 622, 587
JITA, TAMA = 30000142, 30002813
THE_FORGE, THE_CITADEL = 10000002, 10000033

_VICTIM = Killmail.HomeRole.VICTIM
_ATTACKER = Killmail.HomeRole.ATTACKER


# --------------------------------------------------------------------------- #
#  Fixtures
# --------------------------------------------------------------------------- #
def _user(django_user_model, role, suffix=""):
    user, _ = django_user_model.objects.get_or_create(username=f"kb-c2-{role}{suffix}")
    RoleAssignment.objects.get_or_create(user=user, role=ensure_role(role))
    return user


def _km(kid, t, *, victim_ship, victim_corp, victim_char, value, role, system, region,
        sec_band="highsec", victim_alliance=None, destroyed=0):
    return Killmail.objects.create(
        killmail_id=kid, killmail_hash=f"h{kid}", killmail_time=t, solar_system_id=system,
        region_id=region, sec_band=sec_band, victim_ship_type_id=victim_ship,
        victim_corporation_id=victim_corp, victim_character_id=victim_char,
        victim_alliance_id=victim_alliance, total_value=Decimal(value),
        destroyed_value=Decimal(destroyed), involves_home_corp=True, home_corp_role=role,
    )


def _att(km, seq, char, corp, ship, alliance=None):
    KillmailParticipant.objects.create(
        killmail=km, role="attacker", seq=seq, character_id=char, corporation_id=corp,
        ship_type_id=ship, alliance_id=alliance,
    )


def _vic(km, char, corp, ship, alliance=None):
    KillmailParticipant.objects.create(
        killmail=km, role="victim", seq=0, character_id=char, corporation_id=corp,
        ship_type_id=ship, alliance_id=alliance,
    )


def _build_set():
    """The three-mail synthetic set; returns (km1, km2, km3) and a base window start."""
    t0 = timezone.now() - dt.timedelta(minutes=90)
    t1 = timezone.now() - dt.timedelta(minutes=60)
    t2 = timezone.now() - dt.timedelta(minutes=30)

    km1 = _km(1, t0, victim_ship=STABBER, victim_corp=ENEMY_CORP, victim_char=ENEMY_A,
              victim_alliance=ENEMY_ALLIANCE, value=100_000_000, role=_ATTACKER,
              system=JITA, region=THE_FORGE)
    _vic(km1, ENEMY_A, ENEMY_CORP, STABBER, alliance=ENEMY_ALLIANCE)
    _att(km1, 1, OUR_A, HOME_CORP, RIFTER)
    _att(km1, 2, OUR_B, HOME_CORP, RIFTER)

    km2 = _km(2, t1, victim_ship=GUARDIAN, victim_corp=HOME_CORP, victim_char=OUR_A,
              value=250_000_000, role=_VICTIM, system=JITA, region=THE_FORGE,
              destroyed=200_000_000)
    _vic(km2, OUR_A, HOME_CORP, GUARDIAN)
    _att(km2, 1, ENEMY_A, ENEMY_CORP, STABBER, alliance=ENEMY_ALLIANCE)
    _att(km2, 2, ENEMY_B, ENEMY_CORP, STABBER, alliance=ENEMY_ALLIANCE)

    km3 = _km(3, t2, victim_ship=RIFTER, victim_corp=ENEMY_CORP, victim_char=ENEMY_B,
              value=50_000_000, role=_ATTACKER, system=TAMA, region=THE_CITADEL,
              sec_band="lowsec")
    _vic(km3, ENEMY_B, ENEMY_CORP, RIFTER)
    _att(km3, 1, OUR_B, HOME_CORP, RIFTER)
    return km1, km2, km3


def _campaign(**kwargs):
    defaults = dict(
        name="Test Campaign",
        start_time=timezone.now() - dt.timedelta(hours=2),
        end_time=timezone.now() + dt.timedelta(hours=1),
        scope={},
    )
    defaults.update(kwargs)
    return CombatCampaign.objects.create(**defaults)


def _link_alts(django_user_model):
    """Link OUR_A (main) + OUR_B (alt) to one account so by-main rolls them together."""
    from apps.sso.models import EveCharacter

    user, _ = django_user_model.objects.get_or_create(username="kb-c2-person")
    EveCharacter.objects.create(character_id=OUR_A, user=user, is_main=True, name="Our A")
    EveCharacter.objects.create(character_id=OUR_B, user=user, is_main=False, name="Our B")
    return user


def _window():
    return timezone.now() - dt.timedelta(hours=2), timezone.now() + dt.timedelta(hours=1)


# --------------------------------------------------------------------------- #
#  Matcher truth table (pure function) — each dimension alone + combined + wildcard
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_matcher_wildcard_matches_all_in_window():
    km1, km2, km3 = _build_set()
    start, end = _window()
    for km in (km1, km2, km3):
        assert combat_campaigns.campaign_matches(km, {}, start=start, end=end)


@pytest.mark.django_db
def test_matcher_window_bounds():
    km1, _km2, _km3 = _build_set()
    # A window starting after the mail excludes it; an open (end=None) window keeps it.
    assert not combat_campaigns.campaign_matches(
        km1, {}, start=timezone.now(), end=None)
    assert combat_campaigns.campaign_matches(
        km1, {}, start=timezone.now() - dt.timedelta(hours=3), end=None)


@pytest.mark.django_db
def test_matcher_direction():
    km1, km2, _km3 = _build_set()
    start, end = _window()
    assert combat_campaigns.campaign_matches(km1, {"direction": "kills"}, start=start, end=end)
    assert not combat_campaigns.campaign_matches(km1, {"direction": "losses"}, start=start, end=end)
    assert combat_campaigns.campaign_matches(km2, {"direction": "losses"}, start=start, end=end)
    assert not combat_campaigns.campaign_matches(km2, {"direction": "kills"}, start=start, end=end)


@pytest.mark.django_db
def test_matcher_system_region_secband():
    km1, _km2, km3 = _build_set()
    start, end = _window()
    assert combat_campaigns.campaign_matches(km1, {"system_ids": [JITA]}, start=start, end=end)
    assert not combat_campaigns.campaign_matches(km1, {"system_ids": [TAMA]}, start=start, end=end)
    assert combat_campaigns.campaign_matches(km1, {"region_ids": [THE_FORGE]}, start=start, end=end)
    assert not combat_campaigns.campaign_matches(km1, {"region_ids": [THE_CITADEL]}, start=start, end=end)
    assert combat_campaigns.campaign_matches(km1, {"sec_bands": ["highsec"]}, start=start, end=end)
    assert combat_campaigns.campaign_matches(km3, {"sec_bands": ["lowsec"]}, start=start, end=end)
    assert not combat_campaigns.campaign_matches(km1, {"sec_bands": ["lowsec"]}, start=start, end=end)


@pytest.mark.django_db
def test_matcher_doctrine():
    from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit

    _km1, km2, _km3 = _build_set()
    start, end = _window()
    cat = DoctrineCategory.objects.create(key="armor", label="Armor")
    doc = Doctrine.objects.create(name="Armor HAC", category=cat)
    other = Doctrine.objects.create(name="Shield HAC", category=cat)
    fit = DoctrineFit.objects.create(doctrine=doc, name="Guardian", ship_type_id=GUARDIAN, modules=[])
    km2.doctrine_fit = fit
    km2.save(update_fields=["doctrine_fit"])
    assert combat_campaigns.campaign_matches(km2, {"doctrine_ids": [doc.id]}, start=start, end=end)
    assert not combat_campaigns.campaign_matches(km2, {"doctrine_ids": [other.id]}, start=start, end=end)


@pytest.mark.django_db
def test_matcher_entity_victim_side():
    km1, km2, _km3 = _build_set()
    start, end = _window()
    # km1's victim is the enemy corp/char/alliance → matches on the victim side.
    assert combat_campaigns.campaign_matches(
        km1, {"corporation_ids": [ENEMY_CORP], "entity_side": "victim"}, start=start, end=end)
    assert combat_campaigns.campaign_matches(
        km1, {"character_ids": [ENEMY_A], "entity_side": "victim"}, start=start, end=end)
    assert combat_campaigns.campaign_matches(
        km1, {"alliance_ids": [ENEMY_ALLIANCE], "entity_side": "victim"}, start=start, end=end)
    # km2's victim is US, so an enemy-corp victim-side filter does NOT match it.
    assert not combat_campaigns.campaign_matches(
        km2, {"corporation_ids": [ENEMY_CORP], "entity_side": "victim"}, start=start, end=end)


@pytest.mark.django_db
def test_matcher_entity_attacker_and_either_side():
    _km1, km2, _km3 = _build_set()
    start, end = _window()
    # On our loss (km2) the enemy corp is an ATTACKER.
    assert combat_campaigns.campaign_matches(
        km2, {"corporation_ids": [ENEMY_CORP], "entity_side": "attacker"}, start=start, end=end)
    assert combat_campaigns.campaign_matches(
        km2, {"corporation_ids": [ENEMY_CORP], "entity_side": "either"}, start=start, end=end)
    # A character who never touched km2 does not match.
    assert not combat_campaigns.campaign_matches(
        km2, {"character_ids": [123456], "entity_side": "either"}, start=start, end=end)
    # Passing attacker_rows explicitly (pure, no ORM) yields the same verdict.
    rows = [{"character_id": ENEMY_A, "corporation_id": ENEMY_CORP, "alliance_id": ENEMY_ALLIANCE}]
    assert combat_campaigns.campaign_matches(
        km2, {"alliance_ids": [ENEMY_ALLIANCE], "entity_side": "attacker"},
        start=start, end=end, attacker_rows=rows)


@pytest.mark.django_db
def test_matcher_combined_dimensions_are_anded():
    km1, _km2, _km3 = _build_set()
    start, end = _window()
    # All satisfied → match.
    assert combat_campaigns.campaign_matches(
        km1, {"direction": "kills", "system_ids": [JITA], "sec_bands": ["highsec"]},
        start=start, end=end)
    # One dimension (system) fails → overall no match.
    assert not combat_campaigns.campaign_matches(
        km1, {"direction": "kills", "system_ids": [TAMA], "sec_bands": ["highsec"]},
        start=start, end=end)


@pytest.mark.django_db
def test_sql_matcher_matches_pure_function():
    """The index-friendly SQL set equals the pure per-mail verdict, across scopes."""
    kms = _build_set()
    scopes = [
        {},
        {"direction": "kills"},
        {"direction": "losses"},
        {"system_ids": [JITA]},
        {"region_ids": [THE_FORGE]},
        {"sec_bands": ["highsec"]},
        {"corporation_ids": [ENEMY_CORP], "entity_side": "victim"},
        {"corporation_ids": [ENEMY_CORP], "entity_side": "attacker"},
        {"corporation_ids": [ENEMY_CORP], "entity_side": "either"},
        {"direction": "kills", "system_ids": [JITA]},
    ]
    start = timezone.now() - dt.timedelta(hours=2)
    end = timezone.now() + dt.timedelta(hours=1)
    for scope in scopes:
        camp = _campaign(scope=scope, start_time=start, end_time=end)
        sql_ids = set(combat_campaigns.matched_queryset(camp).values_list("killmail_id", flat=True))
        pure_ids = {
            km.killmail_id for km in kms
            if combat_campaigns.campaign_matches(km, scope, start=start, end=end)
        }
        assert sql_ids == pure_ids, f"mismatch for scope {scope}"


# --------------------------------------------------------------------------- #
#  Aggregation (hand-derived)
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_aggregation_full_set_counts_isk_efficiency():
    _build_set()
    camp = _campaign(scope={})
    s = combat_campaigns.campaign_stats(camp, use_cache=False)
    # km1 + km3 are kills (150M); km2 is the loss (250M).
    assert s["kills"] == 2 and s["losses"] == 1
    assert s["isk_destroyed"] == Decimal("150000000")
    assert s["isk_lost"] == Decimal("250000000")
    assert round(s["efficiency"]) == 38  # 150 / (150+250)
    assert s["participants"] == 2  # OUR_A, OUR_B
    # Ships we destroyed: a Stabber (km1) + a Rifter (km3).
    ships = {r["ship_type_id"]: r["count"] for r in s["top_ships"]}
    assert ships == {STABBER: 1, RIFTER: 1}


@pytest.mark.django_db
def test_aggregation_top_pilots_per_character():
    _build_set()
    camp = _campaign(scope={})
    s = combat_campaigns.campaign_stats(camp, use_cache=False)
    by_id = {p["character_id"]: p for p in s["top_pilots"]}
    # OUR_A: on km1 (kill) + the km2 victim (loss).
    assert by_id[OUR_A]["kills"] == 1 and by_id[OUR_A]["losses"] == 1
    assert by_id[OUR_A]["isk_destroyed"] == Decimal("100000000")
    assert by_id[OUR_A]["isk_lost"] == Decimal("250000000")
    # OUR_B: on km1 + km3 (two kills), no losses.
    assert by_id[OUR_B]["kills"] == 2 and by_id[OUR_B]["losses"] == 0
    assert by_id[OUR_B]["isk_destroyed"] == Decimal("150000000")


@pytest.mark.django_db
def test_aggregation_top_pilots_by_main_rollup(django_user_model):
    _build_set()
    _link_alts(django_user_model)  # OUR_B is an alt of OUR_A's account
    camp = _campaign(scope={})
    s = combat_campaigns.campaign_stats(camp, use_cache=False)
    rolled = s["top_pilots_by_main"]
    assert len(rolled) == 1  # both pilots collapse under the one main
    main = rolled[0]
    assert main["character_id"] == OUR_A
    assert main["kills"] == 3  # 1 (OUR_A) + 2 (OUR_B)
    assert main["losses"] == 1
    assert main["isk_destroyed"] == Decimal("250000000")  # 100M + 150M
    assert main["isk_lost"] == Decimal("250000000")


@pytest.mark.django_db
def test_aggregation_scoped_to_one_system():
    _build_set()
    camp = _campaign(scope={"system_ids": [JITA]})
    s = combat_campaigns.campaign_stats(camp, use_cache=False)
    # Only km1 (kill) + km2 (loss) are in Jita; km3 (Tama) is excluded.
    assert s["kills"] == 1 and s["losses"] == 1
    assert s["isk_destroyed"] == Decimal("100000000")
    assert s["isk_lost"] == Decimal("250000000")


@pytest.mark.django_db
def test_open_ended_campaign_matches_ongoing_kills():
    """A campaign with end=None keeps matching mails after its start."""
    t = timezone.now() - dt.timedelta(minutes=5)
    km = _km(50, t, victim_ship=STABBER, victim_corp=ENEMY_CORP, victim_char=ENEMY_A,
             value=10_000_000, role=_ATTACKER, system=JITA, region=THE_FORGE)
    _vic(km, ENEMY_A, ENEMY_CORP, STABBER)
    _att(km, 1, OUR_A, HOME_CORP, RIFTER)
    camp = _campaign(start_time=timezone.now() - dt.timedelta(hours=1), end_time=None)
    assert camp.end_time is None
    s = combat_campaigns.campaign_stats(camp, use_cache=False)
    assert s["kills"] == 1


# --------------------------------------------------------------------------- #
#  Overlays: SRP spend (actual vs estimate) + doctrine compliance
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
def test_srp_spend_estimate_then_actual_claim(django_user_model):
    from apps.srp.models import SrpClaim

    _km1, km2, _km3 = _build_set()
    _permissive_program()
    camp = _campaign(scope={"system_ids": [JITA]}, srp_budget_isk=Decimal("100000000"))

    # No claim yet → ESTIMATE from eligibility (ACTUAL_LOSS pays the 200M destroyed).
    s = combat_campaigns.campaign_stats(camp, use_cache=False)
    assert s["srp"]["basis"] == "estimate"
    assert s["srp"]["estimated"] == 1 and s["srp"]["actual_claims"] == 0
    assert s["srp"]["spend"] == Decimal("200000000")
    assert s["srp"]["budget"] == Decimal("100000000")
    assert s["srp"]["over_budget"] is True  # 200M > 100M budget

    # File an actual claim for a different amount → spend switches to the ACTUAL payout.
    claimant = _user(django_user_model, rbac.ROLE_MEMBER, "-claim")
    SrpClaim.objects.create(
        killmail=km2, claimant=claimant, status=SrpClaim.Status.APPROVED,
        computed_payout=Decimal("150000000"),
    )
    s2 = combat_campaigns.campaign_stats(camp, use_cache=False)
    assert s2["srp"]["basis"] == "actual"
    assert s2["srp"]["actual_claims"] == 1 and s2["srp"]["estimated"] == 0
    assert s2["srp"]["spend"] == Decimal("150000000")


@pytest.mark.django_db
def test_doctrine_compliance_and_target_delta():
    from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit
    from apps.killboard.models import FitDeviation

    _km1, km2, _km3 = _build_set()
    camp = _campaign(scope={"system_ids": [JITA]}, doctrine_target_pct=90)

    # No doctrine-tagged losses → no compliance figure.
    s = combat_campaigns.campaign_stats(camp, use_cache=False)
    assert s["compliance"] is None

    cat = DoctrineCategory.objects.create(key="armor", label="Armor")
    doc = Doctrine.objects.create(name="Armor HAC", category=cat)
    fit = DoctrineFit.objects.create(doctrine=doc, name="Guardian", ship_type_id=GUARDIAN, modules=[])
    km2.doctrine_fit = fit
    km2.save(update_fields=["doctrine_fit"])

    s2 = combat_campaigns.campaign_stats(camp, use_cache=False)
    assert s2["compliance"] == {"tagged": 1, "clean": 1, "percent": 100}
    assert s2["compliance_delta"] == 10  # 100 − 90 target

    FitDeviation.objects.create(killmail=km2, doctrine_fit=fit,
                                missing=[{"type_id": 100, "quantity": 1}], extra=[])
    s3 = combat_campaigns.campaign_stats(camp, use_cache=False)
    assert s3["compliance"]["percent"] == 0
    assert s3["compliance_delta"] == -90


# --------------------------------------------------------------------------- #
#  Public slug page — gating
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_public_slug_anonymous_200_and_no_officer_figures(client, sde):
    _build_set()
    _permissive_program()
    camp = _campaign(
        visibility=CombatCampaign.Visibility.PUBLIC, scope={"system_ids": [JITA]},
        srp_budget_isk=Decimal("100000000"), doctrine_target_pct=90,
    )
    resp = client.get(f"/killboard/campaigns/r/{camp.slug}/")
    assert resp.status_code == 200
    body = resp.content
    # Core scoreboard is public…
    assert b"efficiency" in body
    # …but the officer overlays never render for an anonymous viewer.
    assert b"SRP spend" not in body
    assert b"Officer overlay" not in body


@pytest.mark.django_db
def test_member_only_campaign_slug_404s_for_anonymous(client, sde):
    _build_set()
    camp = _campaign(visibility=CombatCampaign.Visibility.MEMBER)
    assert client.get(f"/killboard/campaigns/r/{camp.slug}/").status_code == 404


# --------------------------------------------------------------------------- #
#  Member vs officer detail — overlay gating
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_member_detail_hides_officer_overlays(client, django_user_model, sde):
    _build_set()
    _permissive_program()
    camp = _campaign(scope={"system_ids": [JITA]}, srp_budget_isk=Decimal("100000000"))

    client.force_login(_user(django_user_model, rbac.ROLE_MEMBER, "-view"))
    body = client.get(f"/killboard/campaigns/{camp.pk}/").content
    assert b"efficiency" in body       # core scoreboard visible to members
    assert b"SRP spend" not in body    # overlay hidden below officer


@pytest.mark.django_db
def test_officer_detail_shows_srp_overlay(client, django_user_model, sde):
    _build_set()
    _permissive_program()
    camp = _campaign(scope={"system_ids": [JITA]}, srp_budget_isk=Decimal("100000000"))

    client.force_login(_user(django_user_model, rbac.ROLE_OFFICER, "-view"))
    body = client.get(f"/killboard/campaigns/{camp.pk}/").content
    assert b"SRP spend" in body


@pytest.mark.django_db
def test_member_pk_page_requires_login(client, django_user_model, sde):
    camp = _campaign()
    assert client.get(f"/killboard/campaigns/{camp.pk}/").status_code in (302, 403)
    client.force_login(_user(django_user_model, rbac.ROLE_MEMBER, "-login"))
    assert client.get(f"/killboard/campaigns/{camp.pk}/").status_code == 200


@pytest.mark.django_db
def test_op_chip_renders_when_linked(client, django_user_model, sde):
    from apps.operations.models import Operation

    _build_set()
    op = Operation.objects.create(
        name="Home Defence Fleet", target_at=timezone.now() - dt.timedelta(hours=1),
        duration_minutes=120, status=Operation.Status.DONE, srp=Operation.Srp.CORP,
    )
    camp = _campaign(scope={"system_ids": [JITA]}, operation=op)
    client.force_login(_user(django_user_model, rbac.ROLE_MEMBER, "-op"))
    body = client.get(f"/killboard/campaigns/{camp.pk}/").content
    assert b"Home Defence Fleet" in body


# --------------------------------------------------------------------------- #
#  Officer create/edit — gated + audited
# --------------------------------------------------------------------------- #
def _audit_exists(action):
    AuditLog = django_apps.get_model("admin_audit", "AuditLog")
    return AuditLog.objects.filter(action=action).exists()


@pytest.mark.django_db
def test_create_is_officer_gated_and_audited(client, django_user_model, sde):
    start = timezone.now() - dt.timedelta(hours=1)
    payload = {
        "name": "Deployment Alpha",
        "start_time": start.strftime("%Y-%m-%dT%H:%M"),
        "visibility": "public", "is_active": "1",
        "direction": "both", "entity_side": "either",
        "corporation_ids": "98000002, 98000009", "system_ids": "30000142",
    }

    # A member cannot create.
    client.force_login(_user(django_user_model, rbac.ROLE_MEMBER, "-create"))
    assert client.post("/killboard/campaigns/create/", payload).status_code in (302, 403)
    assert not CombatCampaign.objects.filter(name="Deployment Alpha").exists()

    # An officer can — and it is audited, with the scope parsed.
    client.force_login(_user(django_user_model, rbac.ROLE_OFFICER, "-create"))
    resp = client.post("/killboard/campaigns/create/", payload)
    assert resp.status_code == 302
    camp = CombatCampaign.objects.get(name="Deployment Alpha")
    assert camp.visibility == "public" and camp.slug
    assert camp.scope["corporation_ids"] == [98000002, 98000009]
    assert camp.scope["system_ids"] == [30000142]
    assert _audit_exists("combat_campaign.create")


@pytest.mark.django_db
def test_edit_is_officer_gated_and_audited(client, django_user_model, sde):
    camp = _campaign(name="Before", scope={"system_ids": [JITA]})
    start = camp.start_time
    payload = {
        "name": "After",
        "start_time": start.strftime("%Y-%m-%dT%H:%M"),
        "visibility": "member", "is_active": "1",
        "direction": "losses", "entity_side": "either",
        "doctrine_target_pct": "85",
    }

    client.force_login(_user(django_user_model, rbac.ROLE_MEMBER, "-edit"))
    assert client.post(f"/killboard/campaigns/{camp.pk}/edit/", payload).status_code in (302, 403)
    camp.refresh_from_db()
    assert camp.name == "Before"

    client.force_login(_user(django_user_model, rbac.ROLE_OFFICER, "-edit"))
    resp = client.post(f"/killboard/campaigns/{camp.pk}/edit/", payload)
    assert resp.status_code == 302
    camp.refresh_from_db()
    assert camp.name == "After"
    assert camp.scope["direction"] == "losses"
    assert camp.doctrine_target_pct == 85
    assert _audit_exists("combat_campaign.edit")


@pytest.mark.django_db
def test_list_page_renders_for_member(client, django_user_model, sde):
    _build_set()
    _campaign(name="Visible Campaign", scope={"system_ids": [JITA]})
    client.force_login(_user(django_user_model, rbac.ROLE_MEMBER, "-list"))
    resp = client.get("/killboard/campaigns/")
    assert resp.status_code == 200
    assert b"Visible Campaign" in resp.content


@pytest.mark.django_db
def test_list_page_requires_login(client, sde):
    assert client.get("/killboard/campaigns/").status_code in (302, 403)


@pytest.mark.django_db
def test_create_and_edit_forms_render_for_officer(client, django_user_model, sde):
    officer = _user(django_user_model, rbac.ROLE_OFFICER, "-forms")
    client.force_login(officer)
    assert client.get("/killboard/campaigns/create/").status_code == 200

    camp = _campaign(name="Editable", scope={"system_ids": [JITA], "direction": "kills"})
    resp = client.get(f"/killboard/campaigns/{camp.pk}/edit/")
    assert resp.status_code == 200
    assert b"Editable" in resp.content

    # A member cannot open the officer forms.
    client.force_login(_user(django_user_model, rbac.ROLE_MEMBER, "-forms"))
    assert client.get("/killboard/campaigns/create/").status_code in (302, 403)


@pytest.mark.django_db
def test_delete_is_officer_gated_and_audited(client, django_user_model, sde):
    camp = _campaign(name="Doomed")
    url = f"/killboard/campaigns/{camp.pk}/delete/"

    client.force_login(_user(django_user_model, rbac.ROLE_MEMBER, "-del"))
    assert client.post(url).status_code in (302, 403)
    assert CombatCampaign.objects.filter(pk=camp.pk).exists()

    client.force_login(_user(django_user_model, rbac.ROLE_OFFICER, "-del"))
    assert client.post(url).status_code == 302
    assert not CombatCampaign.objects.filter(pk=camp.pk).exists()
    assert _audit_exists("combat_campaign.delete")


@pytest.mark.django_db
def test_create_rejects_blank_name(client, django_user_model, sde):
    client.force_login(_user(django_user_model, rbac.ROLE_OFFICER, "-blank"))
    resp = client.post("/killboard/campaigns/create/", {
        "name": "", "start_time": timezone.now().strftime("%Y-%m-%dT%H:%M"),
    })
    assert resp.status_code == 302  # redirected back with an error message
    assert not CombatCampaign.objects.exists()
