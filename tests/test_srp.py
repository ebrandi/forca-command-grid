"""Ship Replacement Program: eligibility, payout, claim lifecycle, exposure."""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.utils import timezone

from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit
from apps.identity.models import RoleAssignment
from apps.killboard.models import Killmail
from apps.pilots.models import ContributionEvent
from apps.srp import services
from apps.srp.models import SrpClaim, SrpProgram, SrpRule
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac

RIFTER = 587
AUTOCANNON = 484
POD = 670


def _program(**kw) -> SrpProgram:
    """Configure the live SRP programme for a test."""
    p = services.active_program()
    for key, value in kw.items():
        setattr(p, key, value)
    p.save()
    return p


def _prices():
    """Hull 500k, gun 100k — the standard fixture for payout maths."""
    from apps.market.models import MarketPrice
    MarketPrice.objects.create(type_id=RIFTER, profile=MarketPrice.Profile.JITA_SELL, sell_min=Decimal("500000"))
    MarketPrice.objects.create(type_id=AUTOCANNON, profile=MarketPrice.Profile.JITA_SELL, sell_min=Decimal("100000"))


def _user(django_user_model, name, cid, *roles):
    user = django_user_model.objects.create(username=name)
    for r in roles:
        RoleAssignment.objects.create(user=user, role=ensure_role(r))
    EveCharacter.objects.create(
        character_id=cid, user=user, name=name, is_main=True, is_corp_member=True
    )
    return user


def _doctrine_with_rifter():
    cat = DoctrineCategory.objects.create(key="dps", label="DPS")
    d = Doctrine.objects.create(name="Rifter Doctrine", category=cat, priority=90)
    DoctrineFit.objects.create(
        doctrine=d, name="Rifter", ship_type_id=RIFTER,
        modules=[{"type_id": AUTOCANNON, "quantity": 2, "name": "200mm AutoCannon I"}],
    )
    return d


def _loss(character_id, ship_type_id=RIFTER, km_id=900001):
    return Killmail.objects.create(
        killmail_id=km_id,
        killmail_time=timezone.now(),
        solar_system_id=30000142,
        victim_character_id=character_id,
        victim_ship_type_id=ship_type_id,
        total_value=Decimal("1000000"),
        involves_home_corp=True,
        home_corp_role=Killmail.HomeRole.VICTIM,
    )


@pytest.fixture
def default_rule(db):
    return SrpRule.objects.create(doctrine=None, basis=SrpRule.Basis.FIT, max_payout=0, active=True)


@pytest.mark.django_db
def test_doctrine_loss_is_eligible_and_non_doctrine_is_not(django_user_model, sde, default_rule):
    _doctrine_with_rifter()
    _user(django_user_model, "pilot", 5001, rbac.ROLE_MEMBER)

    doctrine_loss = _loss(5001, RIFTER, 900001)
    assert services.eligibility(doctrine_loss)["eligible"] is True

    # A loss that isn't a doctrine hull is not eligible.
    other_loss = _loss(5001, 999999, 900002)
    assert services.eligibility(other_loss)["eligible"] is False


@pytest.mark.django_db
def test_full_fit_payout_includes_modules(django_user_model, sde, default_rule):
    from apps.market.models import MarketPrice

    MarketPrice.objects.create(type_id=RIFTER, profile=MarketPrice.Profile.JITA_SELL, sell_min=Decimal("500000"))
    MarketPrice.objects.create(type_id=AUTOCANNON, profile=MarketPrice.Profile.JITA_SELL, sell_min=Decimal("100000"))
    _doctrine_with_rifter()
    km = _loss(5001, RIFTER, 900003)

    payout = services.eligibility(km)["payout"]
    # hull 500k + 2 × gun 100k = 700k
    assert payout == Decimal("700000")


@pytest.mark.django_db
def test_claim_lifecycle_and_exposure_and_credit(django_user_model, sde, default_rule):
    _doctrine_with_rifter()
    pilot = _user(django_user_model, "pilot", 5001, rbac.ROLE_MEMBER)
    officer = _user(django_user_model, "fc", 5002, rbac.ROLE_OFFICER)
    km = _loss(5001, RIFTER, 900004)

    claim = services.submit_claim(pilot, km, [5001])
    assert claim is not None and claim.status == SrpClaim.Status.SUBMITTED
    assert services.exposure() == claim.computed_payout

    # Can't double-claim the same killmail.
    assert services.submit_claim(pilot, km, [5001]) is None
    # Can't claim someone else's loss.
    assert services.submit_claim(officer, km, [5002]) is None

    services.decide(claim, officer, approve=True, reason="sanctioned")
    claim.refresh_from_db()
    assert claim.status == SrpClaim.Status.APPROVED

    services.mark_paid(claim, officer)
    claim.refresh_from_db()
    assert claim.status == SrpClaim.Status.PAID
    assert services.exposure() == Decimal("0")  # paid → no longer open exposure
    # Payout credited to the pilot's contribution ledger.
    assert ContributionEvent.objects.filter(user=pilot, kind="srp").count() == 1


@pytest.mark.django_db
def test_queue_is_officer_only(client, django_user_model, sde, default_rule):
    client.force_login(_user(django_user_model, "m", 5003, rbac.ROLE_MEMBER))
    assert client.get("/srp/queue/").status_code == 403
    assert client.get("/srp/").status_code == 200


@pytest.mark.django_db
def test_srp_budget_view_and_live_spend(client, django_user_model, sde, default_rule):
    """SRP-6: officer-gated budget page; spend is derived live from PAID claims
    (SrpBudget stores only the allocation)."""
    from apps.srp.models import SrpBudget

    _prices()
    _doctrine_with_rifter()
    pilot = _user(django_user_model, "p", 5101, rbac.ROLE_MEMBER)
    officer = _user(django_user_model, "o", 5102, rbac.ROLE_OFFICER)
    claim = services.submit_claim(pilot, _loss(5101, RIFTER, 920001), [5101])
    services.decide(claim, officer, approve=True)
    services.mark_paid(claim, officer)
    claim.refresh_from_db()

    period = timezone.now().strftime("%Y-%m")
    assert claim.payout > 0
    assert services.spent_for_period(period) == claim.payout

    # Member is locked out; officer sees the page.
    client.force_login(_user(django_user_model, "m", 5103, rbac.ROLE_MEMBER))
    assert client.get("/srp/budget/").status_code == 403
    client.force_login(officer)
    assert client.get("/srp/budget/").status_code == 200

    # Officer sets this period's allocation; it persists.
    resp = client.post("/srp/budget/save/", {"period": period, "allocated": "1000000000"})
    assert resp.status_code == 302
    assert SrpBudget.objects.get(period=period).allocated == Decimal("1000000000")

    # A malformed period is rejected (no row created).
    assert client.post("/srp/budget/save/", {"period": "nope", "allocated": "5"}).status_code == 302
    assert SrpBudget.objects.count() == 1


@pytest.mark.django_db
def test_loss_impact_board_is_officer_only(client, django_user_model, sde):
    """SRP-7: the corp-wide loss-impact board is officer-gated."""
    client.force_login(_user(django_user_model, "m", 5301, rbac.ROLE_MEMBER))
    assert client.get("/srp/loss-impact/").status_code == 403
    client.force_login(_user(django_user_model, "o", 5302, rbac.ROLE_OFFICER))
    assert client.get("/srp/loss-impact/").status_code == 200


# --- payout modes ------------------------------------------------------------
@pytest.mark.django_db
def test_isk_full_mode_pays_loss_value(django_user_model, sde, default_rule):
    _prices()
    _doctrine_with_rifter()
    _program(payout_mode=SrpProgram.PayoutMode.ISK_FULL, valuation=SrpProgram.Valuation.DOCTRINE_FIT)
    info = services.eligibility(_loss(5001, RIFTER, 910001))
    assert info["payout"] == Decimal("700000")  # hull 500k + 2×100k
    assert info["insurance_estimate"] == Decimal("0")


@pytest.mark.django_db
def test_insurance_topup_mode_subtracts_insurance(django_user_model, sde, default_rule):
    _prices()
    _doctrine_with_rifter()
    _program(payout_mode=SrpProgram.PayoutMode.ISK_INSURANCE_TOPUP,
             valuation=SrpProgram.Valuation.DOCTRINE_FIT, insurance_fraction=Decimal("0.400"))
    info = services.eligibility(_loss(5001, RIFTER, 910002))
    # gross 700k − insurance (hull 500k × 0.40 = 200k) = 500k
    assert info["insurance_estimate"] == Decimal("200000")
    assert info["payout"] == Decimal("500000")


@pytest.mark.django_db
def test_replacement_mode_keeps_value_and_marks_mode(django_user_model, sde, default_rule):
    _prices()
    _doctrine_with_rifter()
    _program(payout_mode=SrpProgram.PayoutMode.REPLACEMENT, valuation=SrpProgram.Valuation.DOCTRINE_FIT)
    info = services.eligibility(_loss(5001, RIFTER, 910003))
    assert info["payout_mode"] == SrpProgram.PayoutMode.REPLACEMENT
    assert info["payout"] == Decimal("700000")  # informational ISK value of the hull+fit


# --- valuation bases ---------------------------------------------------------
@pytest.mark.django_db
def test_hull_only_valuation(django_user_model, sde, default_rule):
    _prices()
    _doctrine_with_rifter()
    _program(valuation=SrpProgram.Valuation.HULL_ONLY)
    assert services.eligibility(_loss(5001, RIFTER, 910004))["payout"] == Decimal("500000")


@pytest.mark.django_db
def test_actual_loss_valuation_uses_killmail_destroyed_value(django_user_model, sde, default_rule):
    _doctrine_with_rifter()
    _program(valuation=SrpProgram.Valuation.ACTUAL_LOSS)
    km = _loss(5001, RIFTER, 910005)
    km.destroyed_value = Decimal("1234567")
    km.save(update_fields=["destroyed_value"])
    assert services.eligibility(km)["payout"] == Decimal("1234567")


# --- caps --------------------------------------------------------------------
@pytest.mark.django_db
def test_program_default_cap_limits_payout(django_user_model, sde, default_rule):
    _prices()
    _doctrine_with_rifter()
    _program(valuation=SrpProgram.Valuation.DOCTRINE_FIT, default_cap=Decimal("300000"))
    assert services.eligibility(_loss(5001, RIFTER, 910006))["payout"] == Decimal("300000")


# --- eligibility knobs -------------------------------------------------------
@pytest.mark.django_db
def test_pod_loss_blocked_unless_covered(django_user_model, sde, default_rule):
    _doctrine_with_rifter()
    _program(cover_pod=False, require_doctrine=False)
    assert services.eligibility(_loss(5001, POD, 910007))["eligible"] is False
    _program(cover_pod=True, require_doctrine=False)
    assert services.eligibility(_loss(5001, POD, 910008))["eligible"] is True


@pytest.mark.django_db
def test_require_doctrine_off_allows_any_corp_loss(django_user_model, sde, default_rule):
    _program(require_doctrine=False, valuation=SrpProgram.Valuation.ACTUAL_LOSS)
    km = _loss(5001, 999999, 910009)  # not a doctrine hull
    km.destroyed_value = Decimal("42000000")
    km.save(update_fields=["destroyed_value"])
    info = services.eligibility(km)
    assert info["eligible"] is True and info["payout"] == Decimal("42000000")


@pytest.mark.django_db
def test_disabled_program_blocks_eligibility(django_user_model, sde, default_rule):
    _doctrine_with_rifter()
    _program(enabled=False)
    assert services.eligibility(_loss(5001, RIFTER, 910010))["eligible"] is False


# --- officer discretion: payout override + payment reference -----------------
@pytest.mark.django_db
def test_officer_can_override_payout_on_approval(django_user_model, sde, default_rule):
    _prices()
    _doctrine_with_rifter()
    pilot = _user(django_user_model, "pilot", 5001, rbac.ROLE_MEMBER)
    officer = _user(django_user_model, "fc", 5002, rbac.ROLE_OFFICER)
    claim = services.submit_claim(pilot, _loss(5001, RIFTER, 910011), [5001])
    services.decide(claim, officer, approve=True, approved_payout=Decimal("250000"))
    claim.refresh_from_db()
    assert claim.approved_payout == Decimal("250000")
    assert claim.payout == Decimal("250000")        # override wins
    assert services.exposure() == Decimal("250000")  # exposure follows the override


@pytest.mark.django_db
def test_mark_paid_records_reference(django_user_model, sde, default_rule):
    _prices()
    _doctrine_with_rifter()
    pilot = _user(django_user_model, "pilot", 5001, rbac.ROLE_MEMBER)
    officer = _user(django_user_model, "fc", 5002, rbac.ROLE_OFFICER)
    claim = services.submit_claim(pilot, _loss(5001, RIFTER, 910012), [5001])
    services.decide(claim, officer, approve=True)
    services.mark_paid(claim, officer, reference="wallet tx 998877")
    claim.refresh_from_db()
    assert claim.status == SrpClaim.Status.PAID
    assert claim.payment_reference == "wallet tx 998877"


# --- leadership settings page + rule CRUD ------------------------------------
@pytest.mark.django_db
def test_settings_page_is_officer_only(client, django_user_model, sde):
    client.force_login(_user(django_user_model, "m", 5003, rbac.ROLE_MEMBER))
    assert client.get("/srp/settings/").status_code == 403
    client.force_login(_user(django_user_model, "fc", 5004, rbac.ROLE_OFFICER))
    assert client.get("/srp/settings/").status_code == 200


@pytest.mark.django_db
def test_officer_updates_program_and_adds_deletes_rule(client, django_user_model, sde):
    officer = _user(django_user_model, "fc", 5004, rbac.ROLE_OFFICER)
    client.force_login(officer)
    # Update the programme to insurance top-up.
    resp = client.post("/srp/settings/", {
        "enabled": "on", "payout_mode": "isk_topup", "valuation": "doctrine",
        "default_cap": "0", "insurance_fraction": "0.5", "intro_text": "Fly cheap.",
        "fleet_op_grace_minutes": "30", "fleet_op_default_duration_minutes": "120",
    })
    assert resp.status_code == 302
    prog = services.active_program()
    assert prog.payout_mode == "isk_topup" and prog.insurance_fraction == Decimal("0.500")
    # Add a rule, then delete it.
    client.post("/srp/rules/add/", {"basis": "fixed", "max_payout": "5000000", "active": "on"})
    rule = SrpRule.objects.filter(basis="fixed").first()
    assert rule is not None
    client.post(f"/srp/rules/{rule.pk}/delete/")
    assert not SrpRule.objects.filter(pk=rule.pk).exists()
