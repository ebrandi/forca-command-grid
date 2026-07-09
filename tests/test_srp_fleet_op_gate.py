"""SRP-1 (roadmap 2.8) — fleet-op eligibility gate.

Acceptance: losses inside a sanctioned op window auto-qualify; non-fleet losses are gated
per policy; the gate ships OFF (backwards-compatible) and only affects future checks.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.killboard.models import Killmail
from apps.operations.models import Operation, OperationAttendance
from apps.srp import services
from apps.srp.models import SrpProgram

pytestmark = pytest.mark.django_db

CID = 95000001


def _program(**kw) -> SrpProgram:
    kw.setdefault("enabled", True)
    kw.setdefault("require_doctrine", False)  # focus on the fleet-op gate, not doctrine matching
    kw.setdefault("valuation", SrpProgram.Valuation.ACTUAL_LOSS)
    p = services.active_program()
    for key, value in kw.items():
        setattr(p, key, value)
    p.save()
    return p


def _loss(km_time, cid=CID, km_id=900500) -> Killmail:
    return Killmail.objects.create(
        killmail_id=km_id, killmail_time=km_time, solar_system_id=30000142,
        victim_character_id=cid, victim_ship_type_id=587, total_value=Decimal("1000000"),
        involves_home_corp=True, home_corp_role=Killmail.HomeRole.VICTIM,
    )


def _op(target_at, *, srp=Operation.Srp.CORP, status=Operation.Status.DONE, duration=120) -> Operation:
    return Operation.objects.create(
        name="Op", type=Operation.Type.PVP, target_at=target_at,
        duration_minutes=duration, srp=srp, status=status,
    )


def test_gate_off_is_backwards_compatible():
    _program(require_fleet_op=False)
    assert services.eligibility(_loss(timezone.now()))["eligible"] is True


def test_loss_in_sanctioned_op_window_qualifies():
    t0 = timezone.now()
    _op(target_at=t0 - timedelta(minutes=10))  # started 10 min ago, 120 min long
    _program(require_fleet_op=True)
    info = services.eligibility(_loss(t0))
    assert info["eligible"] is True
    assert info["operation"] is not None


def test_loss_outside_any_op_is_gated():
    _op(target_at=timezone.now() - timedelta(days=3))  # far from the loss
    _program(require_fleet_op=True)
    info = services.eligibility(_loss(timezone.now()))
    assert info["eligible"] is False
    assert "fleet op" in info["reason"].lower()


def test_unspecified_or_no_srp_op_does_not_qualify():
    t0 = timezone.now()
    _op(target_at=t0 - timedelta(minutes=10), srp=Operation.Srp.NONE)
    _op(target_at=t0 - timedelta(minutes=10), srp="")  # blank SRP designation
    _program(require_fleet_op=True)
    assert services.eligibility(_loss(t0))["eligible"] is False


def test_cancelled_op_does_not_qualify():
    t0 = timezone.now()
    _op(target_at=t0 - timedelta(minutes=10), status=Operation.Status.CANCELLED)
    _program(require_fleet_op=True)
    assert services.eligibility(_loss(t0))["eligible"] is False


def test_loss_after_op_window_plus_grace_is_gated():
    t0 = timezone.now()
    # op ran [t0-200m, t0-200m+120m]=ends t0-80m; +30m grace ends t0-50m; loss at t0 is out
    _op(target_at=t0 - timedelta(minutes=200), duration=120)
    _program(require_fleet_op=True, fleet_op_grace_minutes=30)
    assert services.eligibility(_loss(t0))["eligible"] is False


def test_long_op_over_24h_still_qualifies():
    # A 48h deployment op that started 30h before the loss — its window still covers t0, so
    # the query prefilter must not drop it (regression for the hardcoded-24h floor).
    t0 = timezone.now()
    _op(target_at=t0 - timedelta(hours=30), duration=48 * 60)
    _program(require_fleet_op=True)
    assert services.eligibility(_loss(t0))["eligible"] is True


def test_grace_window_covers_a_pre_formup_loss():
    t0 = timezone.now()
    _op(target_at=t0 + timedelta(minutes=20))  # op starts in 20 min; 30 min grace covers it
    _program(require_fleet_op=True, fleet_op_grace_minutes=30)
    assert services.eligibility(_loss(t0))["eligible"] is True


def test_attendance_requirement_needs_confirmed_pap(django_user_model):
    t0 = timezone.now()
    op = _op(target_at=t0 - timedelta(minutes=10))
    _program(require_fleet_op=True, fleet_op_require_attendance=True)
    assert services.eligibility(_loss(t0))["eligible"] is False  # no PAP recorded
    u = django_user_model.objects.create(username="pap")
    # An UNCONFIRMED self-report must NOT satisfy the gate (3.1 confirmed-only invariant).
    OperationAttendance.objects.create(operation=op, user=u, character_id=CID, confirmed=False)
    assert services.eligibility(_loss(t0, km_id=900501))["eligible"] is False
    # A CONFIRMED / ESI-verified PAP does.
    OperationAttendance.objects.filter(operation=op, character_id=CID).update(confirmed=True)
    assert services.eligibility(_loss(t0, km_id=900502))["eligible"] is True
