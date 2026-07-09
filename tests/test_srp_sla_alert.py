"""SRP-2 (roadmap 2.7) — SRP SLA & solvency alerts.

Acceptance: a breach of the configured SRP bounds (backlog / oldest claim / average
wait / budget) fires exactly one deduped SRP-officer digest; an unchanged breach set
is a no-op; a return to within-SLA resets the dedup so a recurrence re-alerts;
leadership can switch the event off; no active programme → nothing to grade.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.pingboard import config as pconfig
from apps.pingboard.models import Alert
from apps.readiness import config as rconfig
from apps.srp.alerts import scan_srp_health
from apps.srp.models import SrpBudget, SrpClaim, SrpProgram
from tests._raffle_utils import HOME_CORP, home_kill

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _reset_config():
    for reset in (lambda: pconfig.reset("notifications"), lambda: pconfig.reset("general"),
                  lambda: rconfig.reset("srp")):
        reset()
    yield
    pconfig.reset("notifications")
    pconfig.reset("general")
    rconfig.reset("srp")


@pytest.fixture
def program():
    return SrpProgram.objects.create(name="Standard", is_active=True)


@pytest.fixture
def claimant(django_user_model):
    return django_user_model.objects.create_user(username="pilot", password="x")


def _alerts():
    return Alert.objects.filter(source_service="srp")


def _claim(claimant, km_id, *, status=SrpClaim.Status.SUBMITTED, payout="10000000", age_days=0):
    km = home_kill(km_id, attackers=[(1, HOME_CORP, True)])
    c = SrpClaim.objects.create(
        killmail=km, claimant=claimant, status=status, computed_payout=Decimal(payout)
    )
    if age_days:
        SrpClaim.objects.filter(pk=c.pk).update(created_at=timezone.now() - timedelta(days=age_days))
    return c


def test_no_active_programme_is_a_noop(claimant):
    _claim(claimant, 1)
    assert scan_srp_health()["status"] == "ok"
    assert not _alerts().exists()


def test_backlog_breach_fires_one_digest_then_dedupes(program, claimant):
    rconfig.set("srp", {"max_pending_claims": 2})
    for i in range(3):
        _claim(claimant, 100 + i)
    assert scan_srp_health()["status"] == "alerted"
    assert _alerts().count() == 1
    assert "pending" in _alerts().first().body.lower()
    # Unchanged breach set → no second digest.
    assert scan_srp_health()["status"] == "unchanged"
    assert _alerts().count() == 1


def test_oldest_claim_breach(program, claimant):
    rconfig.set("srp", {"max_claim_age_days": 3, "max_pending_claims": 99})
    _claim(claimant, 200, age_days=10)
    assert scan_srp_health()["status"] == "alerted"
    assert "oldest" in _alerts().first().body.lower()


def test_solvency_breach(program, claimant):
    # Default thresholds (max_pending 25) → backlog does not breach with one claim;
    # only the budget is underwater.
    period = timezone.now().strftime("%Y-%m")
    SrpBudget.objects.create(period=period, allocated=Decimal("1000000"))
    _claim(claimant, 300, payout="2000000")  # 2M owed > 1M allocated
    assert scan_srp_health()["status"] == "alerted"
    assert "budget" in _alerts().first().body.lower()


def test_recovery_resets_dedup_and_recurrence_realerts(program, claimant):
    rconfig.set("srp", {"max_pending_claims": 2})
    c = [_claim(claimant, 400 + i) for i in range(3)]
    scan_srp_health()
    assert _alerts().count() == 1
    # Clear the backlog (decide the claims) → within SLA.
    SrpClaim.objects.filter(pk__in=[x.pk for x in c]).update(status=SrpClaim.Status.PAID)
    assert scan_srp_health()["status"] == "ok"
    # A fresh backlog later re-alerts.
    for i in range(3):
        _claim(claimant, 500 + i)
    assert scan_srp_health()["status"] == "alerted"
    assert _alerts().count() == 2


def test_disabled_event_is_a_noop(program, claimant):
    rconfig.set("srp", {"max_pending_claims": 2})
    for i in range(3):
        _claim(claimant, 600 + i)
    pconfig.set("notifications", {"events": {"srp.sla_alert": {"enabled": False}}})
    assert scan_srp_health()["status"] == "disabled"
    assert not _alerts().exists()
