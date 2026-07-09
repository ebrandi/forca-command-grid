"""CMD-2 (roadmap 3.6) — directive recognition ledger.

Completing a ranked corp directive earns a recognition ledger entry (feed shout) worth its
points, and feeds a 'directive' raffle ticket source. Future-only, idempotent, never ISK.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.command_intel.models import PilotDirective
from apps.pilots.models import ContributionEvent
from tests._raffle_utils import enrol_pilot

pytestmark = pytest.mark.django_db

_KIND = ContributionEvent.Kind.DIRECTIVE


def _directive(user, *, slug="s1", points=5, state=PilotDirective.State.OPEN, completed_at=None):
    return PilotDirective.objects.create(
        user=user, slug=slug, title="Train Logistics V", points=points, state=state,
        completed_at=completed_at,
    )


def _url(pk):
    return reverse("command_intel:directive_action", args=[pk])


def test_completing_directive_credits_recognition(client, django_user_model):
    user, _ = enrol_pilot(django_user_model, 9600)
    d = _directive(user, points=5)
    client.force_login(user)
    client.post(_url(d.pk), {"action": "done"})
    d.refresh_from_db()
    assert d.state == PilotDirective.State.DONE and d.completed_at is not None
    ev = ContributionEvent.objects.get(kind=_KIND, ref_id=str(d.pk))
    assert ev.user_id == user.id and ev.points == 5


def test_completion_is_idempotent(client, django_user_model):
    user, _ = enrol_pilot(django_user_model, 9601)
    d = _directive(user)
    client.force_login(user)
    for _ in range(2):
        client.post(_url(d.pk), {"action": "done"})
    assert ContributionEvent.objects.filter(kind=_KIND, ref_id=str(d.pk)).count() == 1


def test_cannot_complete_another_users_directive(client, django_user_model):
    owner, _ = enrol_pilot(django_user_model, 9602)
    other, _ = enrol_pilot(django_user_model, 9603)
    d = _directive(owner)
    client.force_login(other)
    assert client.post(_url(d.pk), {"action": "done"}).status_code == 404  # IDOR-safe
    d.refresh_from_db()
    assert d.state == PilotDirective.State.OPEN
    assert not ContributionEvent.objects.filter(kind=_KIND).exists()


def test_dismiss_does_not_credit(client, django_user_model):
    user, _ = enrol_pilot(django_user_model, 9604)
    d = _directive(user)
    client.force_login(user)
    client.post(_url(d.pk), {"action": "dismiss"})
    assert not ContributionEvent.objects.filter(kind=_KIND).exists()


def test_directive_raffle_source_yields_completed(django_user_model):
    from apps.raffle.sources import SOURCES

    user, _ = enrol_pilot(django_user_model, 9605)
    now = timezone.now()
    d = _directive(user, slug="done1", points=3, state=PilotDirective.State.DONE,
                   completed_at=now - timedelta(hours=1))

    class _Cfg:
        config = {"per_directive": 2}

    events = list(SOURCES["directive"].iter_events(None, _Cfg(), now - timedelta(days=1),
                                                   now + timedelta(days=1)))
    assert len(events) == 1
    assert events[0].character_id == 9605
    assert events[0].base_tickets == 2
    assert events[0].source_ref == f"directive:{d.id}"
