"""Celery tasks for the Mentorship Program.

All heavy imports are lazy (inside the task body) so worker-only deps never load
in a web request (ADR-0008). Cadences are registered in ``config/celery.py``.
Every task is convergent/idempotent — a missed run catches up on the next.
"""
from __future__ import annotations

from celery import shared_task


@shared_task(name="mentorship.refresh_eligibility")
def refresh_eligibility() -> dict:
    """Recompute eligibility snapshots off the request path (public ESI, cached)."""
    from . import eligibility, services
    from .models import MenteeProfile, MentorProfile

    program = services.active_program()
    updated = 0
    for profile in MentorProfile.objects.filter(
        status__in=[MentorProfile.Status.PENDING, MentorProfile.Status.ACTIVE]
    ).select_related("user"):
        if profile.character:
            eligibility.invalidate(profile.character.character_id)
        profile.eligibility = eligibility.evaluate(profile.user, program, "mentor")
        profile.save(update_fields=["eligibility", "updated_at"])
        updated += 1
    for profile in MenteeProfile.objects.filter(
        status__in=[MenteeProfile.Status.PENDING, MenteeProfile.Status.ACTIVE]
    ).select_related("user"):
        if profile.character:
            eligibility.invalidate(profile.character.character_id)
        profile.eligibility = eligibility.evaluate(profile.user, program, "mentee")
        profile.save(update_fields=["eligibility", "updated_at"])
        updated += 1
    return {"profiles_refreshed": updated}


@shared_task(name="mentorship.expire_stale_pairings")
def expire_stale_pairings() -> dict:
    """Expire suggested/requested pairings that were never actioned in time."""
    from datetime import timedelta

    from django.utils import timezone

    from . import services
    from .models import MentorshipPairing

    program = services.active_program()
    if not program.pairing_ttl_days:
        return {"expired": 0}
    cutoff = timezone.now() - timedelta(days=program.pairing_ttl_days)
    expired = 0
    stale = MentorshipPairing.objects.filter(
        status__in=[MentorshipPairing.Status.SUGGESTED, MentorshipPairing.Status.REQUESTED],
        created_at__lt=cutoff,
    )
    for pairing in stale:
        if services.set_status(pairing, MentorshipPairing.Status.EXPIRED, detail="Auto-expired (TTL)."):
            expired += 1
    return {"expired": expired}


@shared_task(name="mentorship.sweep_api_validations")
def sweep_api_validations() -> dict:
    """Re-run auto-checks for assignments waiting on ESI/internal data to arrive."""
    from . import workflow
    from .models import MentorshipTaskAssignment

    completed = 0
    pending = MentorshipTaskAssignment.objects.filter(
        status=MentorshipTaskAssignment.Status.PENDING_API
    ).select_related("task", "pairing__mentee__user", "pairing__mentor__user")
    for assignment in pending:
        if workflow.sweep_pending_api(assignment):
            completed += 1
    return {"checked": pending.count(), "completed": completed}


@shared_task(name="mentorship.scan_anomalies")
def scan_anomalies() -> dict:
    """Detect farming / rubber-stamping / stale pairs and raise flags."""
    from . import trust

    raised = trust.run_scan()
    return {"flags_raised": raised}


@shared_task(name="mentorship.reward_active_days")
def reward_active_days() -> dict:
    """Grant PAIRING_ACTIVE_DAYS rewards for pairs that have stayed active."""
    from django.utils import timezone

    from . import rewards
    from .models import MentorshipPairing

    now = timezone.now()
    granted = 0
    for pairing in MentorshipPairing.objects.filter(
        status=MentorshipPairing.Status.ACTIVE, started_at__isnull=False
    ):
        days = (now - pairing.started_at).days
        granted += len(rewards.on_pairing_active_days(pairing, days))
    return {"rewards_granted": granted}


@shared_task(name="mentorship.auto_suggest_pairings")
def auto_suggest_pairings() -> dict:
    """Suggest a best-match mentor for each unpaired active cadet (idempotent)."""
    from . import matching

    return {"suggested": matching.auto_suggest(limit_per_mentee=1)}


@shared_task(name="mentorship.poll_session_presence")
def poll_session_presence() -> dict:
    """Best-effort live presence check for sessions currently in their window.

    Only polls a participant who granted the opt-in ``mentorship_presence`` scope;
    reads ``/characters/{id}/online/`` (+ ``/location/``) and records a corroborating
    presence signal. Real-time only — EVE keeps no session history. Never raises.
    """
    from datetime import timedelta

    from django.utils import timezone

    from .models import MentorshipSession

    now = timezone.now()
    checked = 0
    sessions = MentorshipSession.objects.filter(
        status=MentorshipSession.Status.SCHEDULED, scheduled_at__isnull=False,
        scheduled_at__lte=now + timedelta(minutes=5),
        scheduled_at__gte=now - timedelta(hours=6),
    ).select_related("pairing")
    for session in sessions:
        window_end = session.scheduled_at + timedelta(minutes=session.duration_minutes or 30)
        if now > window_end + timedelta(minutes=10):
            continue
        result = dict(session.presence_result or {})
        for part in session.participants.select_related("user"):
            present = _poll_participant(part, session.location_system_id)
            if present is not None:
                part.present = present
                part.save(update_fields=["present"])
                result[str(part.user_id)] = present
        session.presence_result = result
        session.presence_checked_at = now
        session.save(update_fields=["presence_result", "presence_checked_at", "updated_at"])
        checked += 1
    return {"sessions_checked": checked}


def _poll_participant(participant, location_system_id) -> bool | None:
    """Return True/False if we could check presence, None if we couldn't."""
    try:
        from apps.sso.models import EveCharacter
        from apps.sso.token_service import get_valid_access_token
        from core.esi.client import get_client

        chars = EveCharacter.objects.filter(user=participant.user)
        client = get_client()
        for char in chars:
            try:
                token = get_valid_access_token(char, required_scopes=["esi-location.read_online.v1"])
            except Exception:  # noqa: BLE001,S112 - no scope / no token for this char, try next
                continue
            online = client.get(f"/characters/{char.character_id}/online/", token=token).data or {}
            if not online.get("online"):
                continue
            if location_system_id:
                try:
                    loc_token = get_valid_access_token(
                        char, required_scopes=["esi-location.read_location.v1"]
                    )
                    loc = client.get(f"/characters/{char.character_id}/location/", token=loc_token).data or {}
                    return loc.get("solar_system_id") == location_system_id
                except Exception:  # noqa: BLE001
                    return True  # online but couldn't confirm system
            return True
        return None
    except Exception:  # noqa: BLE001 - presence is best-effort only
        return None
