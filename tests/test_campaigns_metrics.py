"""Campaign Command Phase 3 metric-source tests (design docs 00 §6, 08, 12 §3).

Each of the 11 auto sources measured against real backing rows, or the documented monkeypatch
seam for the heavy readiness/doctrine/stockpile/srp services (doc 12 §3 — sources lazy-import, so
patching the backing module attribute intercepts the lookup at call time). Also covers the base
framework: the registry, fail-soft isolation, params-schema validation, honest ``as_of``, and the
stale-auto health wiring. House style: ``pytest.mark.django_db``, ``_user`` helpers, no factory-boy.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.campaigns import metrics, services
from apps.campaigns.metrics.base import MetricSource, clean_params, measure_safely
from apps.campaigns.models import Campaign, Objective

pytestmark = pytest.mark.django_db

_ALL_KEYS = {
    "doctrine.qualified_pilots", "readiness.dimension", "stockpile.on_hand",
    "industry.deliveries", "operations.completed", "operations.attendance",
    "finance.wallet_balance", "srp.reserve", "structures.fuel_days",
    "logistics.hauled_m3", "killboard.kills",
}


# --------------------------------------------------------------------------- #
#  Registry + base framework
# --------------------------------------------------------------------------- #
def test_registry_has_all_eleven_sources():
    assert {s.key for s in metrics.all_sources()} == _ALL_KEYS


def test_get_source_unknown_returns_none():
    assert metrics.get_source("nope") is None
    assert metrics.get_source("") is None


def test_measure_safely_isolates_a_raising_source():
    class Boom(MetricSource):
        key = "test.boom"

        def measure(self, params):
            raise RuntimeError("nope")

    assert measure_safely(Boom(), {}) is None


# --------------------------------------------------------------------------- #
#  Params-schema validation (form/service boundary — doc 12 §3c)
# --------------------------------------------------------------------------- #
def test_clean_params_requires_required_field():
    with pytest.raises(ValidationError):
        clean_params(metrics.get_source("doctrine.qualified_pilots"), {})


def test_clean_params_rejects_non_int():
    with pytest.raises(ValidationError):
        clean_params(metrics.get_source("finance.wallet_balance"), {"division": "abc"})


def test_clean_params_parses_int_list():
    cleaned = clean_params(metrics.get_source("industry.deliveries"), {"type_ids": "34, 35 ,36"})
    assert cleaned == {"type_ids": [34, 35, 36]}


def test_clean_params_rejects_bad_choice():
    with pytest.raises(ValidationError):
        clean_params(metrics.get_source("operations.completed"), {"op_type": "not_a_type"})


def test_clean_params_drops_optional_when_absent():
    cleaned = clean_params(metrics.get_source("stockpile.on_hand"), {"stockpile_id": "5"})
    assert cleaned == {"stockpile_id": 5}


# --------------------------------------------------------------------------- #
#  doctrine.qualified_pilots (seam: corp_doctrine_coverage + active_member_ids)
# --------------------------------------------------------------------------- #
def _character(django_user_model, cid, username, snap_as_of=None):
    from apps.characters.models import CharacterSkillSnapshot
    from apps.sso.models import EveCharacter

    u = django_user_model.objects.create(username=username)
    ch = EveCharacter.objects.create(
        character_id=cid, user=u, name=username, is_main=True, is_corp_member=True
    )
    if snap_as_of is not None:
        snap = CharacterSkillSnapshot.objects.create(character=ch, skills={}, is_latest=True)
        CharacterSkillSnapshot.objects.filter(pk=snap.pk).update(as_of=snap_as_of)
    return ch


def test_doctrine_qualified_pilots_value_and_honest_as_of(monkeypatch, django_user_model):
    snap_at = timezone.now() - timedelta(hours=3)
    _character(django_user_model, 1001, "p1", snap_as_of=snap_at)
    monkeypatch.setattr(
        "apps.doctrines.services.corp_doctrine_coverage",
        lambda characters: [{"doctrine_id": 3, "can_fly": 20, "total": len(characters)}],
    )
    m = metrics.get_source("doctrine.qualified_pilots").measure({"doctrine_id": 3})
    assert m.value == Decimal(20)
    assert abs((m.as_of - snap_at).total_seconds()) < 2  # from the snapshot, not now()


def test_doctrine_active_days_intersects_active_set(monkeypatch, django_user_model):
    _character(django_user_model, 1001, "active")
    _character(django_user_model, 1002, "idle")
    monkeypatch.setattr("apps.readiness.dimensions.roles.active_member_ids", lambda days: {1001})
    seen = {}

    def fake_coverage(characters):
        seen["n"] = len(characters)
        return [{"doctrine_id": 3, "can_fly": len(characters), "total": len(characters)}]

    monkeypatch.setattr("apps.doctrines.services.corp_doctrine_coverage", fake_coverage)
    m = metrics.get_source("doctrine.qualified_pilots").measure({"doctrine_id": 3, "active_days": 30})
    assert seen["n"] == 1  # only the recently-active character counted
    assert m.value == Decimal(1)


# --------------------------------------------------------------------------- #
#  readiness.dimension (seam: compute_readiness)
# --------------------------------------------------------------------------- #
def test_readiness_dimension_value(monkeypatch):
    monkeypatch.setattr(
        "apps.readiness.services.compute_readiness", lambda *a, **k: {"dimensions": {"doctrine": 72}}
    )
    m = metrics.get_source("readiness.dimension").measure({"dimension": "doctrine"})
    assert m.value == Decimal(72)


def test_readiness_dimension_unavailable_fails_soft(monkeypatch):
    monkeypatch.setattr(
        "apps.readiness.services.compute_readiness", lambda *a, **k: {"dimensions": {"doctrine": None}}
    )
    assert measure_safely(metrics.get_source("readiness.dimension"), {"dimension": "doctrine"}) is None


# --------------------------------------------------------------------------- #
#  stockpile.on_hand (seam: esi_on_hand_for → covered honesty flag)
# --------------------------------------------------------------------------- #
def _stockpile():
    from apps.stockpile.models import Stockpile, StockpileItem

    sp = Stockpile.objects.create(name="Home")
    StockpileItem.objects.create(stockpile=sp, type_id=34, quantity_current=30, quantity_target=50)
    return sp


def test_stockpile_on_hand_covered_uses_esi(monkeypatch):
    sp = _stockpile()
    monkeypatch.setattr("apps.stockpile.services.esi_on_hand_for", lambda s: ({34: 40}, True))
    m = metrics.get_source("stockpile.on_hand").measure({"stockpile_id": sp.pk})
    assert m.value == Decimal(40)
    assert m.detail["covered"] is True


def test_stockpile_on_hand_uncovered_uses_manual(monkeypatch):
    sp = _stockpile()
    monkeypatch.setattr("apps.stockpile.services.esi_on_hand_for", lambda s: ({}, False))
    m = metrics.get_source("stockpile.on_hand").measure({"stockpile_id": sp.pk})
    assert m.value == Decimal(30)  # manual stocktake authoritative
    assert m.detail["covered"] is False


# --------------------------------------------------------------------------- #
#  industry.deliveries (real erp rows)
# --------------------------------------------------------------------------- #
def test_industry_deliveries_window_and_type_filter():
    from apps.erp.models import BuildJob, Delivery

    job = BuildJob.objects.create(output_type_id=587, quantity=1, status=BuildJob.Status.BUILT)
    Delivery.objects.create(job=job, quantity=5)  # inside window (created_at ≈ now)
    out = Delivery.objects.create(job=job, quantity=3)
    other = BuildJob.objects.create(output_type_id=999, quantity=1, status=BuildJob.Status.BUILT)
    Delivery.objects.create(job=other, quantity=100)  # wrong type
    # Window bounds captured after the in-window row exists so its ``created_at`` sits inside.
    now = timezone.now()
    since = now - timedelta(days=7)
    Delivery.objects.filter(pk=out.pk).update(created_at=now - timedelta(days=30))

    m = metrics.get_source("industry.deliveries").measure(
        {"type_ids": [587], "_since": since, "_now": now}
    )
    assert m.value == Decimal(5)


# --------------------------------------------------------------------------- #
#  operations.completed / operations.attendance (real ops rows)
# --------------------------------------------------------------------------- #
def test_operations_completed_counts_done_in_window():
    from apps.operations.models import Operation

    now = timezone.now()
    since = now - timedelta(days=10)
    t = Operation.Type.HOME_DEFENCE
    Operation.objects.create(name="A", type=t, status=Operation.Status.DONE,
                             target_at=now - timedelta(days=2))
    Operation.objects.create(name="B", type=t, status=Operation.Status.DONE,
                             target_at=now - timedelta(days=30))  # out of window
    Operation.objects.create(name="C", type=t, status=Operation.Status.PLANNED,
                             target_at=now - timedelta(days=1))  # not DONE

    m = metrics.get_source("operations.completed").measure(
        {"op_type": t, "_since": since, "_now": now}
    )
    assert m.value == Decimal(1)


def test_operations_attendance_counts_confirmed(django_user_model):
    from apps.operations.models import Operation, OperationAttendance

    op = Operation.objects.create(name="Op", type=Operation.Type.HOME_DEFENCE)
    u1 = django_user_model.objects.create(username="a1")
    u2 = django_user_model.objects.create(username="a2")
    OperationAttendance.objects.create(operation=op, user=u1, confirmed=True)
    OperationAttendance.objects.create(operation=op, user=u2, confirmed=False)

    m = metrics.get_source("operations.attendance").measure(
        {"_operation_ids": [op.id], "_now": timezone.now()}
    )
    assert m.value == Decimal(1)


# --------------------------------------------------------------------------- #
#  finance.wallet_balance / srp.reserve (sensitive-by-default)
# --------------------------------------------------------------------------- #
def test_finance_wallet_balance_and_sensitive_default():
    from apps.corporation.models import CorpWalletDivision

    CorpWalletDivision.objects.create(division=1, balance=Decimal("123456789"))
    source = metrics.get_source("finance.wallet_balance")
    assert source.sensitive_default is True
    m = source.measure({"division": 1})
    assert m.value == Decimal("123456789")


def test_srp_reserve_arithmetic(monkeypatch):
    from apps.srp.models import SrpBudget

    SrpBudget.objects.create(period="2026-07", allocated=Decimal("1000"))
    monkeypatch.setattr("apps.srp.services.spent_for_period", lambda period: Decimal("200"))
    monkeypatch.setattr("apps.srp.services.exposure", lambda: Decimal("100"))
    source = metrics.get_source("srp.reserve")
    assert source.sensitive_default is True
    m = source.measure({"period": "2026-07", "_now": timezone.now()})
    assert m.value == Decimal("700")  # 1000 − 200 − 100


# --------------------------------------------------------------------------- #
#  structures.fuel_days (real corp rows — min semantics + no-data fail-soft)
# --------------------------------------------------------------------------- #
def test_structures_fuel_days_reports_minimum():
    from apps.corporation.models import CorpStructure

    now = timezone.now()
    CorpStructure.objects.create(structure_id=1, type_id=35834, name="A",
                                 fuel_expires=now + timedelta(days=10))
    CorpStructure.objects.create(structure_id=2, type_id=35834, name="B",
                                 fuel_expires=now + timedelta(days=3))
    m = metrics.get_source("structures.fuel_days").measure({})
    assert Decimal("2.9") <= m.value <= Decimal("3.01")  # the binding minimum


def test_structures_fuel_days_no_data_fails_soft():
    assert measure_safely(metrics.get_source("structures.fuel_days"), {"structure_ids": [999]}) is None


# --------------------------------------------------------------------------- #
#  logistics.hauled_m3 / killboard.kills (real windowed ledgers)
# --------------------------------------------------------------------------- #
def test_logistics_hauled_m3_window(django_user_model):
    from apps.pilots.services import record_contribution

    u = django_user_model.objects.create(username="hauler")
    now = timezone.now()
    since = now - timedelta(days=7)
    record_contribution(u, "haul", 500, "m3", ref_type="h", ref_id="1",
                        occurred_at=now - timedelta(days=1))
    record_contribution(u, "haul", 300, "m3", ref_type="h", ref_id="2",
                        occurred_at=now - timedelta(days=30))  # out of window

    m = metrics.get_source("logistics.hauled_m3").measure({"_since": since, "_now": now})
    assert m.value == Decimal(500)


def test_killboard_kills_window_and_role():
    from apps.killboard.models import Killmail

    now = timezone.now()
    since = now - timedelta(days=7)

    def _km(kid, when, role):
        Killmail.objects.create(
            killmail_id=kid, killmail_hash=f"h{kid}", killmail_time=when,
            solar_system_id=30000142, victim_ship_type_id=587,
            involves_home_corp=True, home_corp_role=role,
        )

    _km(1, now - timedelta(days=1), Killmail.HomeRole.ATTACKER)  # counts
    _km(2, now - timedelta(days=30), Killmail.HomeRole.ATTACKER)  # out of window
    _km(3, now - timedelta(days=1), Killmail.HomeRole.VICTIM)  # a loss, not a kill

    m = metrics.get_source("killboard.kills").measure({"_since": since, "_now": now})
    assert m.value == Decimal(1)


# --------------------------------------------------------------------------- #
#  Stale-auto health wiring (doc 00 §4)
# --------------------------------------------------------------------------- #
def test_stale_auto_metric_drives_watch_health():
    now = timezone.now()
    c = Campaign.objects.create(
        name="Coverage", status=Campaign.Status.ACTIVE,
        start_at=now - timedelta(days=2), target_end_at=now + timedelta(days=30),
    )
    obj = Objective.objects.create(
        campaign=c, title="Kills", metric_source="killboard.kills",
        baseline_value=Decimal(0), target_value=Decimal(100), current_value=Decimal(10),
        status=Objective.ObjectiveStatus.ACTIVE,
    )
    # killmail data_class threshold is 10 min; age it well past 2× that.
    Objective.objects.filter(pk=obj.pk).update(measured_at=now - timedelta(hours=2))
    obj.refresh_from_db()
    state, reasons = services.campaign_health(c)
    assert any(r["code"] == "stale_metrics" for r in reasons)
