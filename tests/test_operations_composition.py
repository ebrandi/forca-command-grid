"""4.19 — On-grid composition vs plan (killboard reconciliation).

Acceptance: after an op, compare its planned ship slots against the home-corp pilots
actually seen on killmails in the op's time window — per-hull planned vs fielded (met/
short) + off-plan ships — as an officer-only AAR. Unique pilots per hull; killmails
outside the window don't count; no target time ⇒ nothing to reconcile.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.identity.models import RoleAssignment
from apps.operations.composition import reconcile_composition
from apps.operations.models import Operation, OperationShipSlot
from apps.sso.services import ensure_role
from core import rbac
from tests._raffle_utils import HOME_CORP, home_kill

pytestmark = pytest.mark.django_db

FEROX = 16227
GUARDIAN = 11987
RIFTER = 587


def _op(*, minutes_ago=60, duration=120):
    return Operation.objects.create(
        name="Strat", type=Operation.Type.HOME_DEFENCE,
        target_at=timezone.now() - dt.timedelta(minutes=minutes_ago), duration_minutes=duration,
    )


def _slot(op, ship_type_id, name, need):
    OperationShipSlot.objects.create(operation=op, ship_name=name,
                                     ship_type_id=ship_type_id, min_pilots=need)


def _in_window(op, minutes=10):
    return op.target_at + dt.timedelta(minutes=minutes)


def test_reconcile_planned_vs_on_grid():
    op = _op()
    _slot(op, FEROX, "Ferox", 3)
    _slot(op, GUARDIAN, "Guardian", 2)
    w = _in_window(op)
    home_kill(1, attackers=[(7001, HOME_CORP, True), (7002, HOME_CORP, False)],
              when=w, ship_type_id=FEROX)          # 2 Ferox pilots
    home_kill(2, attackers=[(7003, HOME_CORP, True)], when=w, ship_type_id=GUARDIAN)  # 1 Guardian
    home_kill(3, attackers=[(7004, HOME_CORP, True)], when=w, ship_type_id=RIFTER)    # off-plan

    res = reconcile_composition(op)
    by_ship = {r["ship_type_id"]: r for r in res["planned_rows"]}
    assert by_ship[FEROX]["planned"] == 3 and by_ship[FEROX]["fielded"] == 2
    assert by_ship[FEROX]["short"] == 1 and by_ship[FEROX]["met"] is False
    assert by_ship[GUARDIAN]["fielded"] == 1 and by_ship[GUARDIAN]["short"] == 1
    assert res["off_plan"] == [{"ship_type_id": RIFTER, "fielded": 1}]
    assert res["total_planned"] == 5 and res["total_on_plan_fielded"] == 3
    assert res["any_participants"] is True and res["has_plan"] is True


def test_met_when_enough_on_grid():
    op = _op()
    _slot(op, FEROX, "Ferox", 2)
    w = _in_window(op)
    home_kill(1, attackers=[(7001, HOME_CORP, True), (7002, HOME_CORP, False)],
              when=w, ship_type_id=FEROX)
    res = reconcile_composition(op)
    assert res["planned_rows"][0]["met"] is True and res["planned_rows"][0]["short"] == 0


def test_killmails_outside_window_excluded():
    op = _op(minutes_ago=60, duration=120)
    _slot(op, FEROX, "Ferox", 2)
    # one inside the window, one 10h before target (well outside)
    home_kill(1, attackers=[(7001, HOME_CORP, True)], when=_in_window(op), ship_type_id=FEROX)
    home_kill(2, attackers=[(7002, HOME_CORP, True)],
              when=op.target_at - dt.timedelta(hours=10), ship_type_id=FEROX)
    res = reconcile_composition(op)
    assert res["planned_rows"][0]["fielded"] == 1  # only the in-window pilot


def test_same_pilot_counted_once_per_hull():
    op = _op()
    _slot(op, FEROX, "Ferox", 2)
    w = _in_window(op)
    home_kill(1, attackers=[(7001, HOME_CORP, True)], when=w, ship_type_id=FEROX)
    home_kill(2, attackers=[(7001, HOME_CORP, True)], when=w, ship_type_id=FEROX)  # same pilot again
    res = reconcile_composition(op)
    assert res["planned_rows"][0]["fielded"] == 1  # deduped


def test_capsules_excluded_from_grid():
    op = _op()
    _slot(op, FEROX, "Ferox", 1)
    w = _in_window(op)
    home_kill(1, attackers=[(7001, HOME_CORP, True)], when=w, ship_type_id=FEROX)
    home_kill(2, attackers=[(7002, HOME_CORP, True)], when=w, ship_type_id=670)  # a pod
    res = reconcile_composition(op)
    assert res["off_plan"] == []  # the pod is AAR noise, not counted as a fielded hull
    assert 670 not in {r["ship_type_id"] for r in res["planned_rows"]}


def test_no_plan_returns_empty():
    op = _op()  # no ship slots
    home_kill(1, attackers=[(7001, HOME_CORP, True)], when=_in_window(op), ship_type_id=FEROX)
    res = reconcile_composition(op)
    assert res["has_plan"] is False and res["planned_rows"] == []
    assert res["any_participants"] is False


def test_window_duration_capped():
    from apps.operations.composition import GRACE_MINUTES, MAX_WINDOW_MINUTES, op_window
    op = _op(minutes_ago=60, duration=525600)  # a mis-entered "year-long" op
    _start, end = op_window(op)
    # window is bounded by the 24h cap (+ grace), not the year.
    assert (end - op.target_at).total_seconds() <= (MAX_WINDOW_MINUTES + GRACE_MINUTES + 5) * 60


def test_no_target_time_returns_none():
    op = Operation.objects.create(name="TBD", type=Operation.Type.HOME_DEFENCE, target_at=None)
    _slot(op, FEROX, "Ferox", 2)
    assert reconcile_composition(op) is None


def test_detail_panel_officer_only(client, django_user_model, sde):
    op = _op()
    _slot(op, FEROX, "Ferox", 2)
    home_kill(1, attackers=[(7001, HOME_CORP, True)], when=_in_window(op), ship_type_id=FEROX)

    officer = django_user_model.objects.create(username="off")
    RoleAssignment.objects.create(user=officer, role=ensure_role(rbac.ROLE_OFFICER))
    client.force_login(officer)
    assert b"On-grid vs plan" in client.get(reverse("operations:detail", args=[op.pk])).content

    member = django_user_model.objects.create(username="mem")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(member)
    assert b"On-grid vs plan" not in client.get(reverse("operations:detail", args=[op.pk])).content


def test_future_op_has_no_panel(client, django_user_model, sde):
    op = Operation.objects.create(name="Future", type=Operation.Type.HOME_DEFENCE,
                                  target_at=timezone.now() + dt.timedelta(hours=3), duration_minutes=120)
    _slot(op, FEROX, "Ferox", 2)
    officer = django_user_model.objects.create(username="off2")
    RoleAssignment.objects.create(user=officer, role=ensure_role(rbac.ROLE_OFFICER))
    client.force_login(officer)
    assert b"On-grid vs plan" not in client.get(reverse("operations:detail", args=[op.pk])).content
