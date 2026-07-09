"""Pilot engagement spine: contribution ledger, monthly summary, recognition.

These back the pilot dashboard's "your contribution" card and the corp
recognition feed, and enforce the opt-out privacy decision (PRD §II.6.5).
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from apps.identity.models import RoleAssignment
from apps.pilots.models import ContributionEvent, PilotPreference
from apps.pilots.services import (
    monthly_summary,
    recognition_feed,
    record_contribution,
)
from apps.sso.services import ensure_role
from core import rbac


def _member(django_user_model, name):
    user = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    return user


@pytest.mark.django_db
def test_record_contribution_is_idempotent_per_source_action(django_user_model):
    user = _member(django_user_model, "builder")
    # Two recordings of the SAME source action (same kind+ref) must not double-count.
    record_contribution(user, "build", 5, "ships", ref_type="build_job", ref_id="42")
    record_contribution(user, "build", 5, "ships", ref_type="build_job", ref_id="42")
    assert ContributionEvent.objects.filter(user=user).count() == 1

    # A different source action is a separate credit.
    record_contribution(user, "build", 3, "ships", ref_type="build_job", ref_id="43")
    assert ContributionEvent.objects.filter(user=user).count() == 2


@pytest.mark.django_db
def test_monthly_summary_groups_native_units(django_user_model):
    user = _member(django_user_model, "pilot")
    record_contribution(user, "build", 2, "ships", ref_type="j", ref_id="1")
    record_contribution(user, "build", 3, "ships", ref_type="j", ref_id="2")
    record_contribution(user, "haul", 120000, "m3", ref_type="h", ref_id="1")

    summary = {row["kind"]: row for row in monthly_summary(user)}
    assert summary["build"]["total"] == Decimal("5")
    assert summary["build"]["unit"] == "ships"
    assert summary["build"]["count"] == 2
    assert summary["haul"]["total"] == Decimal("120000")


@pytest.mark.django_db
def test_recognition_defaults_on_and_feed_respects_opt_out(django_user_model):
    loud = _member(django_user_model, "loud")
    quiet = _member(django_user_model, "quiet")
    # Opt-out decision: recognition is ON by default.
    from apps.pilots.services import get_prefs

    assert get_prefs(loud).public_recognition is True

    record_contribution(loud, "task", 1, "tasks", ref_type="t", ref_id="1")
    record_contribution(quiet, "task", 1, "tasks", ref_type="t", ref_id="2")

    quiet_prefs = get_prefs(quiet)
    quiet_prefs.public_recognition = False
    quiet_prefs.save(update_fields=["public_recognition"])
    feed_users = {ev.user_id for ev in recognition_feed()}
    assert loud.id in feed_users
    assert quiet.id not in feed_users  # opted out → never in the corp feed


@pytest.mark.django_db
def test_toggle_recognition_endpoint(client, django_user_model):
    user = _member(django_user_model, "toggler")
    client.force_login(user)
    # Starts on; one POST turns it off.
    resp = client.post("/pilots/recognition/toggle/", {"next": "/privacy/"})
    assert resp.status_code == 302
    assert PilotPreference.objects.get(user=user).public_recognition is False


@pytest.mark.django_db
def test_corp_monthly_totals_aggregate_across_members(django_user_model):
    from apps.pilots.services import corp_monthly_totals

    a = _member(django_user_model, "a")
    b = _member(django_user_model, "b")
    record_contribution(a, "build", 2, "ships", ref_type="j", ref_id="1")
    record_contribution(b, "build", 3, "ships", ref_type="j", ref_id="2")
    record_contribution(a, "haul", 50000, "m³", ref_type="h", ref_id="1")

    totals = {r["kind"]: r for r in corp_monthly_totals()}
    assert totals["build"]["total"] == Decimal("5")   # summed across a + b
    assert totals["build"]["count"] == 2
    assert totals["haul"]["total"] == Decimal("50000")


def test_contribution_kinds_reflect_real_recorders():
    # seed/delivery stay pruned (no recorder); train was re-added with a real
    # recorder (skill progression) alongside the new doctrine-unlock kind.
    values = set(ContributionEvent.Kind.values)
    assert {"seed", "delivery"}.isdisjoint(values)
    assert {"build", "haul", "task", "srp", "mining", "fleet", "train", "doctrine"} <= values


@pytest.mark.django_db
def test_recognition_feed_surfaces_on_dashboard(client, django_user_model):
    from apps.sso.models import EveCharacter

    user = _member(django_user_model, "dash")
    EveCharacter.objects.create(character_id=9001, user=user, name="Dash", is_main=True,
                                is_corp_member=True)
    record_contribution(user, "build", 1, "ships", description="Built a Ferox",
                        ref_type="j", ref_id="1")
    client.force_login(user)
    html = client.get("/dashboard/").content.decode()
    # Recognition merged into the contribution panel as "Recently recognised".
    assert "Recently recognised" in html
    assert "Built a Ferox" in html


@pytest.mark.django_db
def test_points_for_scores_each_kind(django_user_model):
    from apps.pilots.models import ContributionWeights
    from apps.pilots.weights import points_for

    w = ContributionWeights.objects.create(
        name="t", is_active=True, task_points=1, fleet_points=2, haul_points=3,
        build_points_per_ship=2,
    )
    w.mining_points_per_mil = Decimal("0.1")
    w.save()
    assert points_for("task", weights=w) == 1
    assert points_for("fleet", weights=w) == 2
    assert points_for("haul", weights=w) == 3
    assert points_for("build", magnitude=5, weights=w) == 10        # 2 × 5 ships
    assert points_for("mining", magnitude=20_000_000, weights=w) == 2  # 0.1 × 20M/1M
    # Doctrine: base + priority×coef + sp(millions)×coef.
    w.doctrine_base = 5
    w.doctrine_priority_coef = Decimal("0.1")
    w.doctrine_effort_per_mil_sp = Decimal("1")
    assert points_for("doctrine", doctrine_priority=80, required_sp=3_000_000, weights=w) == 16


@pytest.mark.django_db
def test_points_disabled_scores_zero(django_user_model):
    from apps.pilots.models import ContributionWeights
    from apps.pilots.weights import points_for

    w = ContributionWeights.objects.create(name="off", is_active=True, enabled=False,
                                            task_points=99)
    assert points_for("task", weights=w) == 0


@pytest.mark.django_db
def test_record_contribution_stores_points(django_user_model):
    from apps.pilots.models import ContributionWeights

    ContributionWeights.objects.create(name="t", is_active=True, task_points=7)
    user = _member(django_user_model, "scorer")
    ev = record_contribution(user, "task", 1, "tasks", ref_type="t", ref_id="1")
    assert ev.points == 7


@pytest.mark.django_db
def test_points_leaderboard_ranks_and_respects_opt_out(django_user_model):
    from apps.pilots.models import ContributionWeights
    from apps.pilots.services import get_prefs, points_leaderboard

    ContributionWeights.objects.create(name="t", is_active=True, task_points=1, fleet_points=5)
    top = _member(django_user_model, "top")
    mid = _member(django_user_model, "mid")
    hidden = _member(django_user_model, "hidden")
    record_contribution(top, "fleet", 1, "fleets", ref_type="o", ref_id="1")   # 5 pts
    record_contribution(mid, "task", 1, "tasks", ref_type="t", ref_id="1")     # 1 pt
    record_contribution(hidden, "fleet", 1, "fleets", ref_type="o", ref_id="2")  # 5 but opted out
    prefs = get_prefs(hidden)
    prefs.public_recognition = False
    prefs.save(update_fields=["public_recognition"])

    board = points_leaderboard()
    names = [r["user"].username for r in board]
    assert names[0] == "top" and "hidden" not in names
    assert board[0]["points"] == 5


@pytest.mark.django_db
def test_weights_admin_page_saves(client, django_user_model):
    from apps.identity.models import RoleAssignment as RA
    from apps.pilots.weights import active_weights

    officer = django_user_model.objects.create(username="wadmin")
    RA.objects.create(user=officer, role=ensure_role(rbac.ROLE_OFFICER))
    client.force_login(officer)
    assert client.get("/ops/admin/contributions/").status_code == 200
    resp = client.post("/ops/admin/contributions/", {
        "enabled": "on", "task_points": "9", "fleet_points": "2", "haul_points": "3",
        "haul_requires_verification": "on", "build_points_per_ship": "1",
        "mining_points_per_mil": "0.1", "srp_points_per_mil": "0",
        "train_points_per_level": "1", "doctrine_base": "5",
        "doctrine_priority_coef": "0.1", "doctrine_effort_per_mil_sp": "1",
        "pvp_points_per_kill": "1", "pvp_final_blow_bonus": "0",
        "pve_points_per_mil": "0.05", "pve_ref_types": "bounty_prizes,ess_escrow_transfer",
    })
    assert resp.status_code == 302
    assert active_weights().task_points == 9


@pytest.mark.django_db
def test_erasure_removes_contributions(django_user_model):
    from apps.identity.services import delete_user_data

    user = _member(django_user_model, "leaver")
    record_contribution(user, "build", 1, "ships", ref_type="j", ref_id="9")
    PilotPreference.objects.get_or_create(user=user)

    delete_user_data(user, actor=user)
    assert not ContributionEvent.objects.filter(user=user).exists()
    assert not PilotPreference.objects.filter(user=user).exists()
