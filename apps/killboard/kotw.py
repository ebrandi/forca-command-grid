"""KB-37 (WS-D3) — Kill of the Week: the weekly auto-pick, officer override, and the hall.

Each ISO week's standout home kill is the top home-corp kill by *at-kill* value (ties broken by
points). The pick is idempotent and recompute-safe, and it **never clobbers an officer override**
— once leadership pins a specific mail for a week, the beat leaves it alone. A fresh auto-pick
fires a corp Pingboard ping (``killboard.kill_of_the_week``).
"""
from __future__ import annotations

import datetime as dt
import logging

from django.conf import settings
from django.db.models import F
from django.utils import timezone

from .models import KillOfTheWeek
from .valuation import at_kill_value_expr

log = logging.getLogger("forca.killboard")

_EVENT_KEY = "killboard.kill_of_the_week"
_UTC = dt.UTC
_ATTACKER = "attacker"


def _home() -> int:
    return settings.FORCA_HOME_CORP_ID


def _week_range(iso_year: int, iso_week: int) -> tuple[dt.datetime, dt.datetime]:
    """The ``[start, end)`` datetimes (UTC) of an ISO week (Monday 00:00 → next Monday 00:00)."""
    monday = dt.date.fromisocalendar(iso_year, iso_week, 1)
    start = dt.datetime(monday.year, monday.month, monday.day, tzinfo=_UTC)
    return start, start + dt.timedelta(days=7)


def last_completed_iso_week(now: dt.datetime | None = None) -> tuple[int, int]:
    """The (iso_year, iso_week) of the most recently *completed* ISO week."""
    now = now or timezone.now()
    today = now.astimezone(_UTC).date()
    last_week_day = today - dt.timedelta(days=today.isoweekday())  # yesterday-or-earlier, prev week
    iso = last_week_day.isocalendar()
    return iso[0], iso[1]


def _top_kill(iso_year: int, iso_week: int):
    """The top home kill for the week by at-kill value (ties → points), or ``None``."""
    from .models import Killmail

    start, end = _week_range(iso_year, iso_week)
    km = (
        Killmail.objects.filter(
            involves_home_corp=True,
            home_corp_role=Killmail.HomeRole.ATTACKER,
            is_npc=False,
            killmail_time__gte=start,
            killmail_time__lt=end,
        )
        .annotate(at_kill=at_kill_value_expr())
        .order_by("-at_kill", "-points", "-killmail_id")
        .first()
    )
    return km


def _credited_character(killmail) -> int | None:
    """The home final-blower on a kill (else the top home damage dealer) — for CV mentions."""
    fb = (
        killmail.participants.filter(role=_ATTACKER, corporation_id=_home(), final_blow=True)
        .values_list("character_id", flat=True).first()
        or killmail.participants.filter(role=_ATTACKER, corporation_id=_home())
        .order_by(F("damage_done").desc(nulls_last=True))
        .values_list("character_id", flat=True).first()
    )
    return fb


def pick_kill_of_the_week(iso_year: int | None = None, iso_week: int | None = None) -> dict:
    """Pick (or idempotently recompute) the Kill of the Week for an ISO week.

    Defaults to the most recently completed week. Never overwrites an officer override. Fires a
    corp ping only when the auto-pick lands or *changes* to a different mail.
    """
    if iso_year is None or iso_week is None:
        iso_year, iso_week = last_completed_iso_week()

    existing = KillOfTheWeek.objects.filter(iso_year=iso_year, iso_week=iso_week).first()
    if existing and existing.is_override:
        return {"status": "override", "iso_year": iso_year, "iso_week": iso_week,
                "killmail_id": existing.killmail_id}

    km = _top_kill(iso_year, iso_week)
    if km is None:
        return {"status": "empty", "iso_year": iso_year, "iso_week": iso_week}

    at_kill = km.value_at_kill if km.value_at_kill is not None else km.total_value
    character_id = _credited_character(km)
    changed = existing is None or existing.killmail_id != km.killmail_id
    row, _created = KillOfTheWeek.objects.update_or_create(
        iso_year=iso_year, iso_week=iso_week,
        defaults={
            "killmail": km, "value": at_kill or 0, "points": km.points or 0,
            "character_id": character_id, "is_override": False,
        },
    )
    if changed:
        _notify_kotw(row)
    return {"status": "picked", "iso_year": iso_year, "iso_week": iso_week,
            "killmail_id": km.killmail_id, "changed": changed}


def set_override(iso_year: int, iso_week: int, killmail, officer) -> KillOfTheWeek:
    """Officer override: pin a specific home kill as the week's KOTW (caller audits the action)."""
    at_kill = killmail.value_at_kill if killmail.value_at_kill is not None else killmail.total_value
    row, _created = KillOfTheWeek.objects.update_or_create(
        iso_year=iso_year, iso_week=iso_week,
        defaults={
            "killmail": killmail, "value": at_kill or 0, "points": killmail.points or 0,
            "character_id": _credited_character(killmail), "is_override": True,
            "overridden_by": officer if getattr(officer, "pk", None) else None,
            "overridden_at": timezone.now(),
        },
    )
    return row


def _notify_kotw(row: KillOfTheWeek) -> None:
    """Fire the corp 'kill of the week' celebration (best-effort). No-op if the event is off."""
    from apps.pingboard.notifications import is_enabled

    if not is_enabled(_EVENT_KEY):
        return
    try:
        from apps.pingboard import services as pingboard

        alert = pingboard.emit_broadcast(
            category="custom",
            title="Kill of the Week",
            body=(
                f"Kill of the Week ({row.iso_year}-W{row.iso_week:02d}): "
                f"{row.value:,.0f} ISK. See it on the killboard."
            ),
            template="killboard.kill_of_the_week",
            context={"value": f"{row.value:,.0f}", "week": f"{row.iso_year}-W{row.iso_week:02d}"},
            audience={"kind": "corp"},
            source_service="killboard",
            source_object_id=f"kotw:{row.iso_year}:{row.iso_week}",
            idempotency_key=f"killboard:kotw:{row.iso_year}:{row.iso_week}:{row.killmail_id}",
        )
        if alert is not None:
            row.notified_at = timezone.now()
            row.save(update_fields=["notified_at", "updated_at"])
    except Exception:  # noqa: BLE001 — a notification must never break the pick
        log.exception("kill-of-the-week notification failed for %s-W%s", row.iso_year, row.iso_week)


def recent_kotw(limit: int = 12) -> list[KillOfTheWeek]:
    """The hall list — recent Kills of the Week, newest first."""
    return list(
        KillOfTheWeek.objects.select_related("killmail").order_by("-iso_year", "-iso_week")[:limit]
    )


def kotw_for_character(character_id: int, limit: int = 12) -> list[KillOfTheWeek]:
    """The weeks a pilot's kill was Kill of the Week (for their CV)."""
    return list(
        KillOfTheWeek.objects.filter(character_id=character_id)
        .order_by("-iso_year", "-iso_week")[:limit]
    )
