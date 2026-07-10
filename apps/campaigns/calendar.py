"""Campaign Command ↔ pingboard calendar sync (design doc 09 §5).

FK-free, provenance-keyed, idempotent — the ``apps.pingboard.calendar`` seam only (never a
``CalendarEvent`` write from here). Every campaign event is keyed by
``(source_system="campaigns", source_object_id)`` so repeated syncs upsert instead of
duplicating, and a human edit *locks* the touched fields (``publish_event`` records a conflict
rather than clobbering).

Two entry points, mirroring ``apps.operations``:

* **write-path** — :func:`publish_campaign` / :func:`cancel_campaign`, called after a campaign's
  status or milestones change, so the calendar reflects the change immediately;
* **sweep** — :func:`sync`, registered as the ``("campaigns", "campaigns", _sync_campaigns)``
  source in ``apps.pingboard.calendar.sync_calendar_sources`` (gated by the director-controlled
  ``calendar.publishing_services`` config), which converges the calendar even if a write-path
  hook was missed and retires events whose campaign no longer qualifies.

Hard rule (doc 09 §5): a ``restricted`` campaign publishes **nothing** — not even a title-only
row; a campaign whose visibility changes to restricted has its events cancelled on the next
publish/sweep.
"""
from __future__ import annotations

import logging

log = logging.getLogger("forca.campaigns")

SOURCE = "campaigns"


def _restricted(campaign) -> bool:
    from .models import Campaign

    return campaign.visibility == Campaign.Visibility.RESTRICTED


def campaign_publishes(campaign) -> bool:
    """Whether a campaign is calendar-worthy: ACTIVE, or APPROVED already carrying a date. The
    write-path (``services.save_milestone`` / milestone status effects) and the ``sync()`` sweep
    share this predicate so a date-less APPROVED campaign's milestone events don't oscillate
    published↔cancelled between an edit and the next sweep (#37)."""
    from .models import Campaign

    if campaign.status == Campaign.Status.ACTIVE:
        return True
    return (campaign.status == Campaign.Status.APPROVED
            and (campaign.start_at is not None or campaign.target_end_at is not None))


def _cal_visibility(campaign) -> str:
    """Map a campaign's visibility onto a calendar tier so the row is never over-shared: members →
    member, directors → director, officers → officer (doc 09 §5, #5). Restricted campaigns publish
    nothing, so they never reach here."""
    from .models import Campaign

    V = Campaign.Visibility
    if campaign.visibility == V.MEMBERS:
        return "member"
    if campaign.visibility == V.DIRECTORS:
        return "director"
    return "officer"


def _window_key(campaign) -> str:
    return f"campaign:{campaign.pk}"


def _milestone_key(milestone) -> str:
    return f"milestone:{milestone.pk}"


def _open_milestones(campaign):
    from .models import Milestone

    return campaign.milestones.filter(due_at__isnull=False).exclude(
        status__in=[Milestone.MilestoneStatus.DONE, Milestone.MilestoneStatus.MISSED]
    )


def publish_campaign(campaign) -> None:
    """Upsert the campaign window + its open milestone deadlines onto the calendar.

    Restricted campaigns cancel any existing rows and publish nothing. The window is published
    only once a ``start_at`` exists (``end_at`` = ``target_end_at``); milestones publish on their
    ``due_at``. Fail-soft — the calendar must never break the campaign action."""
    try:
        from apps.pingboard.calendar import CalendarEventType, publish_event

        if _restricted(campaign):
            cancel_campaign(campaign)
            return

        visibility = _cal_visibility(campaign)
        if campaign.start_at:
            publish_event(
                source_system=SOURCE, source_object_id=_window_key(campaign),
                event_type=CalendarEventType.CUSTOM, title=campaign.name,
                start_at=campaign.start_at, end_at=campaign.target_end_at,
                description=(campaign.summary or "")[:400], visibility=visibility,
            )
        live = {_window_key(campaign)}
        for ms in _open_milestones(campaign).select_related("campaign"):
            publish_event(
                source_system=SOURCE, source_object_id=_milestone_key(ms),
                event_type=CalendarEventType.CUSTOM,
                title=f"Milestone — {ms.title}", start_at=ms.due_at,
                visibility=visibility,
            )
            live.add(_milestone_key(ms))
        _retire_campaign_events(campaign, live)
    except Exception:  # noqa: BLE001 — calendar sync must never break the campaign action
        log.exception("campaigns.calendar publish failed campaign=%s", getattr(campaign, "pk", "?"))


def cancel_campaign(campaign) -> None:
    """Cancel the campaign's window + every campaigns-source event it owns (fail-soft)."""
    try:
        from apps.pingboard.calendar import cancel_event

        cancel_event(source_system=SOURCE, source_object_id=_window_key(campaign))
        for key in _campaign_event_keys(campaign):
            cancel_event(source_system=SOURCE, source_object_id=key)
    except Exception:  # noqa: BLE001
        log.exception("campaigns.calendar cancel failed campaign=%s", getattr(campaign, "pk", "?"))


def cancel_milestone(milestone) -> None:
    """Retire a single milestone's calendar row (done/missed, or campaign left ACTIVE)."""
    try:
        from apps.pingboard.calendar import cancel_event

        cancel_event(source_system=SOURCE, source_object_id=_milestone_key(milestone))
    except Exception:  # noqa: BLE001
        log.exception("campaigns.calendar cancel milestone failed ms=%s", getattr(milestone, "pk", "?"))


def _campaign_event_keys(campaign) -> list[str]:
    """Every live campaigns-source ``source_object_id`` for this campaign (window + milestones)."""
    from apps.pingboard.models import CalendarEvent, CalendarEventStatus

    keys = [_window_key(campaign)]
    keys += [_milestone_key(m) for m in campaign.milestones.all()]
    # Include any stray rows the sweep created, so cancel is exhaustive.
    stray = (
        CalendarEvent.objects.filter(source_system=SOURCE)
        .exclude(status=CalendarEventStatus.CANCELLED)
        .filter(source_object_id__in=keys)
        .values_list("source_object_id", flat=True)
    )
    return sorted(set(keys) | set(stray))


def _retire_campaign_events(campaign, live_keys) -> None:
    """Cancel this campaign's calendar rows that are no longer live (e.g. a milestone marked
    done, or a deadline removed) — the ``_sync_timers`` retire idiom, scoped to one campaign."""
    from apps.pingboard.calendar import cancel_event
    from apps.pingboard.models import CalendarEvent, CalendarEventStatus

    owned = {_window_key(campaign)} | {f"milestone:{m.pk}" for m in campaign.milestones.all()}
    stale = (
        CalendarEvent.objects.filter(source_system=SOURCE, source_object_id__in=owned)
        .exclude(status=CalendarEventStatus.CANCELLED)
        .exclude(source_object_id__in=live_keys)
        .values_list("source_object_id", flat=True)
    )
    for key in list(stale):
        cancel_event(source_system=SOURCE, source_object_id=key)


def sync() -> int:
    """Sweep source: publish every ACTIVE (and APPROVED-with-dates) non-restricted campaign and
    retire events whose campaign no longer qualifies. Idempotent (provenance-keyed upserts).

    Inert while the subsystem is disarmed (doc 13): with the ``campaigns`` feature off the beat
    publishes nothing, matching the three Celery beats' feature check (#18) — otherwise the
    pingboard calendar sweep would keep re-publishing windows whose pages all 404."""
    from core.features import feature_enabled

    if not feature_enabled("campaigns"):
        return 0

    from django.db.models import Q

    from apps.pingboard.models import CalendarEvent, CalendarEventStatus

    from .models import Campaign

    # ACTIVE campaigns, plus APPROVED campaigns that already carry a calendar-worthy date.
    qualifying = Campaign.objects.filter(
        Q(status=Campaign.Status.ACTIVE)
        | (Q(status=Campaign.Status.APPROVED)
           & (Q(start_at__isnull=False) | Q(target_end_at__isnull=False)))
    ).exclude(visibility=Campaign.Visibility.RESTRICTED)

    live_keys: set[str] = set()
    n = 0
    for campaign in qualifying.prefetch_related("milestones"):
        publish_campaign(campaign)
        if campaign.start_at:
            live_keys.add(_window_key(campaign))
        for ms in _open_milestones(campaign):
            live_keys.add(_milestone_key(ms))
        n += 1

    # Retire any campaigns-source event whose source no longer qualifies (converges deletions,
    # visibility→restricted, and campaigns that left ACTIVE even if a hook was missed).
    stale = (
        CalendarEvent.objects.filter(source_system=SOURCE)
        .exclude(status=CalendarEventStatus.CANCELLED)
        .exclude(source_object_id__in=live_keys)
        .values_list("source_object_id", flat=True)
    )
    from apps.pingboard.calendar import cancel_event

    for key in list(stale):
        cancel_event(source_system=SOURCE, source_object_id=key)
    return n
