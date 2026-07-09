"""Scheduled leadership-briefing delivery to Discord + email."""
from __future__ import annotations

import pytest
from django.core import mail


@pytest.mark.django_db
def test_digest_includes_headline_metrics():
    from apps.pilots.briefing_delivery import format_leadership_digest

    digest = format_leadership_digest(briefing={
        "index": 72, "losses_24h": 3, "open_tasks": 5, "open_hauls": 2,
        "stock_shortfalls": 4, "srp_exposure": 1_500_000_000,
        "top_gaps": [{"label": "Logi: 1/4 ready"}],
        "leaderboard": [{"name": "Ace Pilot", "points": 120}],
    })
    assert "Readiness index: 72%" in digest
    assert "Losses (24h): 3" in digest
    assert "1,500,000,000 ISK" in digest
    assert "Logi: 1/4 ready" in digest
    assert "Ace Pilot" in digest


@pytest.mark.django_db
def test_digest_top_contributor_uses_real_leaderboard_row(django_user_model):
    """Regression: the real points_leaderboard() emits {"user", "points"} rows, not
    a "name" key, so the digest must resolve the contributor via user.display_name
    (previously it silently rendered "—")."""
    from django.utils import timezone

    from apps.pilots.briefing_delivery import format_leadership_digest
    from apps.pilots.models import ContributionEvent
    from apps.pilots.services import points_leaderboard
    from apps.sso.models import EveCharacter

    user = django_user_model.objects.create(username="eve:42")
    EveCharacter.objects.create(character_id=42, user=user, name="Zoe Voidborn", is_main=True)
    ContributionEvent.objects.create(
        user=user, kind=ContributionEvent.Kind.FLEET, magnitude=1, unit="fleets",
        points=250, occurred_at=timezone.now(),
    )

    rows = points_leaderboard(limit=5)
    assert rows and rows[0].get("name") is None  # real row has no "name" key
    digest = format_leadership_digest(briefing={"leaderboard": rows})
    assert "Top contributor: Zoe Voidborn (250 pts)" in digest
    assert "Top contributor: —" not in digest


@pytest.mark.django_db
def test_delivery_emails_recipients_and_posts_discord(settings, monkeypatch):
    from apps.pilots import briefing_delivery

    settings.FORCA_BRIEFING_EMAILS = ["officer@example.com", "fc@example.com"]
    sent = {}

    def _fake_discord(content, classification=None):
        sent["c"] = content
        sent["cls"] = classification
        return 2

    monkeypatch.setattr(briefing_delivery, "format_leadership_digest", lambda b=None: "**digest**\nbody")
    # broadcast_discord is imported inside the function from notify; patch it there.
    monkeypatch.setattr("apps.recommendations.notify.broadcast_discord", _fake_discord)

    result = briefing_delivery.deliver_leadership_briefing()
    assert result["discord"] == 2
    assert result["email"] == 1  # one message to the recipient list
    assert len(mail.outbox) == 1
    assert set(mail.outbox[0].to) == {"officer@example.com", "fc@example.com"}
    assert "**" not in mail.outbox[0].body  # bold markers stripped for email
    assert sent["c"] == "**digest**\nbody"


@pytest.mark.django_db
def test_delivery_is_a_noop_without_channels(settings, monkeypatch):
    from apps.pilots import briefing_delivery

    settings.FORCA_BRIEFING_EMAILS = []
    monkeypatch.setattr(briefing_delivery, "format_leadership_digest", lambda b=None: "x")
    monkeypatch.setattr("apps.recommendations.notify.broadcast_discord", lambda c, classification=None: 0)

    result = briefing_delivery.deliver_leadership_briefing()
    assert result == {"discord": 0, "email": 0}
    assert mail.outbox == []


@pytest.mark.django_db
def test_real_briefing_composes_on_empty_db():
    # leadership_briefing() must run end-to-end (it's the source of truth).
    from apps.pilots.briefing_delivery import format_leadership_digest

    digest = format_leadership_digest()
    assert "daily briefing" in digest
