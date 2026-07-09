"""Editing the contest time-box: allowed only before accrual starts, and the new
start date can never be in the past."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.raffle.forms import RaffleContestForm
from apps.raffle.models import RaffleContest
from core import rbac
from tests._raffle_utils import enrol_pilot, make_contest

pytestmark = pytest.mark.django_db

FMT = "%Y-%m-%dT%H:%M"


def _dt(days):
    return (timezone.now() + timedelta(days=days)).strftime(FMT)


def _post(contest, **over):
    """A complete, valid edit POST mirroring the contest's current values."""
    d = {
        "name": contest.name,
        "description": contest.description or "",
        "objective": contest.objective or "",
        "public_rules": contest.public_rules or "",
        "admin_notes": contest.admin_notes or "",
        "start_at": timezone.localtime(contest.start_at).strftime(FMT),
        "end_at": timezone.localtime(contest.end_at).strftime(FMT),
        "draw_at": timezone.localtime(contest.draw_at).strftime(FMT),
        "leaderboard_size": contest.leaderboard_size,
        "booster_multiplier": "1",
        # keep sensible checkbox defaults on
        "require_enrolled": "on", "require_valid_token": "on",
        "one_prize_per_pilot": "on", "auto_draw": "on",
        "leaderboard_visible": "on", "show_recent_events": "on", "archive_public": "on",
    }
    d.update(over)
    return d


# --- form-level validation -------------------------------------------------- #
def test_form_rejects_a_past_start_date():
    form = RaffleContestForm(data={
        "name": "Past", "start_at": _dt(-1), "end_at": _dt(7), "draw_at": _dt(8),
        "leaderboard_size": 25, "booster_multiplier": "1",
    })
    assert not form.is_valid()
    assert "start_at" in form.errors
    assert "past" in " ".join(form.errors["start_at"]).lower()


def test_form_allows_a_future_start_date():
    form = RaffleContestForm(data={
        "name": "Future", "start_at": _dt(1), "end_at": _dt(7), "draw_at": _dt(8),
        "leaderboard_size": 25, "booster_multiplier": "1",
    })
    assert form.is_valid(), form.errors


def test_form_does_not_block_editing_when_start_is_unchanged():
    """A draft whose start was already in the past can still have OTHER fields
    edited — the past-start rule only fires when the start is actually changed."""
    now = timezone.now().replace(second=0, microsecond=0)
    contest = RaffleContest.objects.create(
        name="Grandfathered", status=RaffleContest.Status.DRAFT,
        start_at=now - timedelta(days=1), end_at=now + timedelta(days=5),
        draw_at=now + timedelta(days=6),
    )
    form = RaffleContestForm(data=_post(contest, name="Renamed"), instance=contest)
    assert form.is_valid(), form.errors
    assert "start_at" not in form.changed_data


# --- view-level lifecycle gate --------------------------------------------- #
def test_scheduled_contest_timebox_can_be_edited(client, django_user_model):
    officer, _ = enrol_pilot(django_user_model, 8801, roles=(rbac.ROLE_OFFICER,))
    # scheduled, start in the FUTURE (start_days_ago negative → now + 2 days)
    contest = make_contest(status=RaffleContest.Status.SCHEDULED,
                           start_days_ago=-2, end_days_ahead=10, draw_days_ahead=11,
                           seed_sources=False)
    client.force_login(officer)
    resp = client.post(reverse("admin_audit:raffle_edit", args=[contest.pk]),
                       _post(contest, start_at=_dt(3), end_at=_dt(20), draw_at=_dt(21)))
    assert resp.status_code == 302
    contest.refresh_from_db()
    # start pushed to ~3 days out, end to ~20 days out.
    assert contest.start_at > timezone.now() + timedelta(days=2)
    assert contest.end_at > timezone.now() + timedelta(days=19)


def test_active_but_not_yet_accruing_timebox_is_editable(client, django_user_model):
    """An ACTIVE contest whose start is still in the future (activated early) hasn't
    accrued any tickets, so its time box is still editable."""
    officer, _ = enrol_pilot(django_user_model, 8803, roles=(rbac.ROLE_OFFICER,))
    contest = make_contest(status=RaffleContest.Status.ACTIVE,
                           start_days_ago=-2, end_days_ahead=10, draw_days_ahead=11,
                           seed_sources=False)  # start = now + 2 days
    assert contest.is_editable is True
    client.force_login(officer)
    resp = client.post(reverse("admin_audit:raffle_edit", args=[contest.pk]),
                       _post(contest, start_at=_dt(3), end_at=_dt(20), draw_at=_dt(21)))
    assert resp.status_code == 302
    contest.refresh_from_db()
    assert contest.start_at > timezone.now() + timedelta(days=2)   # schedule DID change
    assert contest.end_at > timezone.now() + timedelta(days=19)


def test_active_and_accruing_contest_timebox_is_locked(client, django_user_model):
    """Once accrual has started (start time reached), the time box locks."""
    officer, _ = enrol_pilot(django_user_model, 8802, roles=(rbac.ROLE_OFFICER,))
    contest = make_contest(status=RaffleContest.Status.ACTIVE,
                           start_days_ago=2, end_days_ahead=5, seed_sources=False)  # started 2d ago
    assert contest.is_editable is False
    original_start = contest.start_at
    client.force_login(officer)
    # Attempt to move the (valid, future) start — must be ignored while accruing.
    resp = client.post(reverse("admin_audit:raffle_edit", args=[contest.pk]),
                       _post(contest, start_at=_dt(1), end_at=_dt(30), draw_at=_dt(31)))
    assert resp.status_code == 302
    contest.refresh_from_db()
    assert contest.start_at == original_start   # schedule change ignored while accruing
