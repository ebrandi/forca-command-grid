"""Best-effort programme notifications.

The platform has one outbound push channel — the shared Discord webhook broadcast
(``apps.recommendations.notify.broadcast_discord``, SSRF-guarded). We reuse it and
never invent a new channel. Delivery is **disarmed by default**
(``MentorshipProgram.notify_discord`` is False) and always wrapped so a failed send
never blocks the business action. Pilot-facing "notifications" are the dashboard
and the pairing worklists, not push messages.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("forca.mentorship")


def _enabled() -> bool:
    from . import services

    return bool(services.active_program().notify_discord)


def broadcast(message: str) -> int:
    """Send to Discord iff the programme has it armed. Never raises.

    Mentor/cadet review traffic is officer-facing, so it is governed by the
    ``mentorship.review`` notification event and stamped with its audience
    classification — reaching leadership-cleared chat channels, not a mass corp channel.
    """
    if not _enabled():
        return 0
    try:
        from apps.pingboard import notifications
        from apps.recommendations.notify import broadcast_discord

        policy = notifications.resolve("mentorship.review")
        if not policy["enabled"]:
            return 0
        return broadcast_discord(f"🎓 Mentorship — {message}", classification=policy["classification"])
    except Exception:  # noqa: BLE001 - notification must never break the flow
        logger.warning("mentorship discord broadcast failed", exc_info=True)
        return 0


def mentor_application(profile) -> None:
    broadcast(f"New mentor application from {profile.user.display_name} pending review.")


def mentee_application(profile) -> None:
    broadcast(f"New cadet application from {profile.user.display_name} pending review.")


def pairing_pending(pairing) -> None:
    broadcast(
        f"Pairing awaiting approval: {pairing.mentor.user.display_name} → "
        f"{pairing.mentee.user.display_name}."
    )


def stalled_pairs(count: int) -> None:
    if count:
        broadcast(f"{count} active mentorship pair(s) have gone quiet — check the dashboard.")


def pairing_proposed(pairing) -> None:
    """DM the counterparty that a proposed pairing awaits their response.

    A mentee's request notifies the mentor ("a cadet wants you"); a mentor's invite
    notifies the mentee ("a mentor wants you"); an auto-suggested (SYSTEM) or
    leadership pairing notifies both, since neither pilot initiated it. Delivered as a
    low-noise, deduped, per-pilot **in-app** notification via the Pingboard
    ``mentorship`` category — the pairing also always shows in the counterparty's
    worklist, so this closes the "suggestions sit unseen" gap without any mass ping.
    Best-effort; never raises.
    """
    from apps.mentorship.models import MentorshipPairing

    mentor_uid, mentee_uid = pairing.mentor.user_id, pairing.mentee.user_id
    mentor_name = pairing.mentor.user.display_name
    mentee_name = pairing.mentee.user.display_name
    by = pairing.initiated_by
    if by == MentorshipPairing.InitiatedBy.MENTEE:
        targets = [(mentor_uid, f"{mentee_name} has requested you as their mentor.")]
    elif by == MentorshipPairing.InitiatedBy.MENTOR:
        targets = [(mentee_uid, f"{mentor_name} has invited you as their cadet.")]
    else:  # SYSTEM / LEADER — neither pilot initiated, so tell both.
        targets = [
            (mentor_uid, f"We suggested {mentee_name} as a cadet you could mentor."),
            (mentee_uid, f"We found you a mentor match: {mentor_name}."),
        ]
    for user_id, body in targets:
        _dm_counterparty(user_id, pairing, body)


def _dm_counterparty(user_id, pairing, body: str) -> None:
    if not user_id:
        return
    try:
        from apps.pingboard import services as pingboard

        pingboard.emit_broadcast(
            category="mentorship",
            title="Mentorship pairing",
            body=body + " Respond on your mentorship dashboard.",
            audience={"kind": "user", "id": user_id},
            channels=["in_app"],
            source_service="mentorship",
            source_object_id=f"pairing:{pairing.id}:{user_id}",
            idempotency_key=f"mentorship:pairing:{pairing.id}:{user_id}",
        )
    except Exception:  # noqa: BLE001 - notification must never break the flow
        logger.warning("mentorship pairing DM failed", exc_info=True)
