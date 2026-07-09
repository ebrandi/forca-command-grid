"""4.15 — Weight-version snapshotting for Hall of Fame months.

Acceptance: a COMPLETED month's board is scored with the weights frozen for it, so
retuning the live weights never silently reshuffles past months; the in-progress month
still tracks the live weights. The save-console freezes completed months at the
pre-change weights before applying an edit.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from django.utils import timezone

from apps.pilots.halloffame import (
    freeze_completed_months,
    invalidate_cache,
    scoreboard,
    weights_for_month,
)
from apps.pilots.models import MonthlyWeightSnapshot
from apps.pilots.weights import active_weights, points_for, weights_from_snapshot, weights_snapshot_dict
from tests._raffle_utils import HOME_CORP, home_kill

pytestmark = pytest.mark.django_db


def _prev_month():
    now = timezone.now()
    return (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)


def _set_pvp(value):
    w = active_weights()
    w.enabled = True
    w.pvp_points_per_kill = value
    w.pvp_final_blow_bonus = 0
    w.save()
    return w


def _pvp_points(board, cid):
    for cat in board["categories"]:
        if cat["key"] == "pvp":
            for r in cat["rows"]:
                if r["character_id"] == cid:
                    return r["points"]
    return 0


def test_snapshot_roundtrip_preserves_scoring():
    w = _set_pvp(7)
    w.mining_points_per_mil = __import__("decimal").Decimal("0.250")
    w.save()
    rebuilt = weights_from_snapshot(weights_snapshot_dict(w))
    assert points_for("pvp", magnitude=3, weights=rebuilt) == points_for("pvp", magnitude=3, weights=w)
    # Decimal field survived the JSON round-trip (would TypeError if left a str/float).
    assert points_for("mining", magnitude=4_000_000, weights=rebuilt) == 1


def test_current_month_uses_live_weights():
    _set_pvp(10)
    now = timezone.now()
    assert weights_for_month(now.year, now.month).pvp_points_per_kill == 10
    _set_pvp(99)
    assert weights_for_month(now.year, now.month).pvp_points_per_kill == 99  # tracks live


def test_completed_month_frozen_against_weight_change():
    py, pm = _prev_month()
    MonthlyWeightSnapshot.objects.create(year=py, month=pm,
                                         weights=weights_snapshot_dict(_set_pvp(7)))
    _set_pvp(99)  # retune live
    assert weights_for_month(py, pm).pvp_points_per_kill == 7  # frozen, not 99


def test_future_month_is_never_frozen():
    # A future month must never mint a forward-dated snapshot (review L1) — it tracks live.
    now = timezone.now()
    fy, fm = now.year + 1, now.month
    _set_pvp(10)
    assert weights_for_month(fy, fm).pvp_points_per_kill == 10
    assert not MonthlyWeightSnapshot.objects.filter(year=fy, month=fm).exists()
    _set_pvp(50)
    assert weights_for_month(fy, fm).pvp_points_per_kill == 50  # still live, no stale snap


def test_lazy_freeze_on_first_read_then_stable():
    py, pm = _prev_month()
    _set_pvp(10)
    w = weights_for_month(py, pm)  # no snapshot yet → lazily frozen at 10
    assert w.pvp_points_per_kill == 10
    assert MonthlyWeightSnapshot.objects.filter(year=py, month=pm).exists()
    _set_pvp(50)
    assert weights_for_month(py, pm).pvp_points_per_kill == 10  # stays frozen


def test_scoreboard_past_month_does_not_shift_on_retune():
    py, pm = _prev_month()
    when = timezone.make_aware(datetime(py, pm, 15, 12, 0))
    home_kill(970001, attackers=[(7001, HOME_CORP, True)], when=when)
    _set_pvp(10)
    board1 = scoreboard(py, pm)      # lazy-freezes the month at pvp=10
    assert _pvp_points(board1, 7001) == 10
    _set_pvp(100)                    # big retune
    invalidate_cache()
    board2 = scoreboard(py, pm)      # completed month → frozen weights
    assert _pvp_points(board2, 7001) == 10  # unchanged, not 100


def test_freeze_completed_months_skips_current(settings):
    py, pm = _prev_month()
    # seed data so the previous month is in available_months()
    home_kill(970002, attackers=[(7002, HOME_CORP, True)],
              when=timezone.make_aware(datetime(py, pm, 10, 12, 0)))
    _set_pvp(5)
    n = freeze_completed_months()
    assert n >= 1
    now = timezone.now()
    assert MonthlyWeightSnapshot.objects.filter(year=py, month=pm).exists()
    assert not MonthlyWeightSnapshot.objects.filter(year=now.year, month=now.month).exists()


def test_freeze_is_idempotent():
    py, pm = _prev_month()
    home_kill(970003, attackers=[(7003, HOME_CORP, True)],
              when=timezone.make_aware(datetime(py, pm, 10, 12, 0)))
    _set_pvp(5)
    freeze_completed_months()
    stored = MonthlyWeightSnapshot.objects.get(year=py, month=pm).weights
    _set_pvp(999)
    freeze_completed_months()  # must NOT overwrite the existing snapshot
    assert MonthlyWeightSnapshot.objects.get(year=py, month=pm).weights == stored


def test_save_hook_freezes_at_pre_change_weights(client, django_user_model):
    from apps.pilots.forms import ContributionWeightsForm
    from apps.sso.services import ensure_role
    from core import rbac
    py, pm = _prev_month()
    home_kill(970004, attackers=[(7004, HOME_CORP, True)],
              when=timezone.make_aware(datetime(py, pm, 10, 12, 0)))
    w = _set_pvp(10)  # OLD live value

    director = django_user_model.objects.create(username="dir")
    from apps.identity.models import RoleAssignment
    RoleAssignment.objects.create(user=director, role=ensure_role(rbac.ROLE_DIRECTOR))
    client.force_login(director)

    # Build a valid POST that changes pvp to 50.
    initial = ContributionWeightsForm(instance=w).initial
    data = {}
    for k, v in initial.items():
        if isinstance(v, bool):
            if v:
                data[k] = "on"
        elif v is not None:
            data[k] = str(v)
    data["pvp_points_per_kill"] = "50"
    from django.urls import reverse
    resp = client.post(reverse("admin_audit:contribution_weights"), data)
    assert resp.status_code in (302, 200)
    # Live weights changed…
    assert active_weights().pvp_points_per_kill == 50
    # …but the completed month was frozen at the OLD value first.
    snap = MonthlyWeightSnapshot.objects.get(year=py, month=pm)
    assert int(snap.weights["pvp_points_per_kill"]) == 10
