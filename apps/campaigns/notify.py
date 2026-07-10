"""Campaign Command → pingboard emission chokepoint (design doc 09).

The **single** module that pushes a message out of ``apps.campaigns``. Every campaign
notification is catalogued in ``apps.pingboard.notifications.REGISTRY`` (12 ``campaigns.*``
keys) and emitted here through ``apps.pingboard.services.emit_broadcast`` — no new channel
code, no direct provider calls (doc 05 import rules; the ``apps/operations/services.py``
idiom copied in shape: lazy import, ``is_enabled(key)`` gate, stable ``source_object_id`` +
``idempotency_key``, swallow-all ``try/except`` so a notification failure never breaks the
campaign action).

Concentrating emission here means the payload-restraint rules for **restricted** campaigns
(doc 09 §4.1) are enforced at one testable chokepoint:

* a restricted campaign's alerts carry the campaign **name and the event kind only** — no
  objective titles, values, budgets, health reasons or deadlines;
* they are **individually targeted** (``user``/``users`` audiences) — never ``corp``/``officer``/
  ``director``/``channel`` — so a restricted campaign never announces to a shared surface;
* logs record the event key + campaign pk + whether an alert was emitted, never names/values.

``campaigns.health_changed`` carries its own problem-set signature (an ``AppSetting`` row per
campaign) so an unchanged health level never re-pings and a recovery re-arms the next
degradation — the ``apps.pingboard.dedup.fire_on_change`` semantics, reimplemented here so the
alert can carry ``category=CAMPAIGN`` and the restricted-payload rules rather than the generic
``custom`` shape ``fire_on_change`` hard-codes.
"""
from __future__ import annotations

import hashlib
import logging

from apps.pingboard.models import AlertCategory
from core.rbac import ROLE_DIRECTOR

log = logging.getLogger("forca.campaigns")

CATEGORY = AlertCategory.CAMPAIGN

# Registry keys (doc 09 §3) — the 12 governed campaign events.
ASSIGNED = "campaigns.assigned"
DEADLINE_SOON = "campaigns.deadline_soon"
OBJECTIVE_BLOCKED = "campaigns.objective_blocked"
DEPENDENCY_COMPLETED = "campaigns.dependency_completed"
HEALTH_CHANGED = "campaigns.health_changed"
APPROVAL_NEEDED = "campaigns.approval_needed"
STARTED = "campaigns.started"
COMPLETED = "campaigns.completed"
RECOGNITION = "campaigns.recognition"
MANUAL_UPDATE_NEEDED = "campaigns.manual_update_needed"
ISSUE_ESCALATED = "campaigns.issue_escalated"
APPROVED = "campaigns.approved"

_HEALTH_SIG_KEY = "campaigns:health:{pk}"
# Health levels at or above which a change is high-priority leadership traffic (doc 09 §4).
_HIGH_HEALTH = {"at_risk", "critical", "blocked"}


# --------------------------------------------------------------------------- #
#  Low-level helpers
# --------------------------------------------------------------------------- #
def _is_restricted(campaign) -> bool:
    from .models import Campaign

    return campaign.visibility == Campaign.Visibility.RESTRICTED


def _detail_url(campaign) -> str:
    from django.urls import reverse

    return _abs(reverse("campaigns:detail", args=[campaign.pk]))


def _abs(path: str) -> str:
    """Absolute deep link when a canonical base URL is configured, else the bare path."""
    try:
        from apps.pingboard import config as pb_config

        base = (pb_config.get("general") or {}).get("site_url") or ""
    except Exception:  # noqa: BLE001 - a link is best-effort, never a hard dependency
        base = ""
    return f"{base.rstrip('/')}{path}" if base else path


def _restricted_payload(campaign) -> tuple[str, str]:
    """Name-only title/body for a restricted campaign (doc 09 §4.1 rule 2)."""
    name = campaign.name
    return (
        f"Campaign «{name}»",
        f"Campaign «{name}»: you have a campaign notification — open Campaign Command "
        f"for details. {_detail_url(campaign)}",
    )


def _director_ids() -> list[int]:
    """Corp members with director rank or above, resolved in one join over ``RoleAssignment``.

    Mirrors ``rbac.has_role(u, ROLE_DIRECTOR)`` — superusers, plus non-expired assignments of a role
    whose key ranks at or above director — but as a single query rather than iterating every corp
    member with a per-row role lookup (which fired 1+N synchronously on each restricted emission, #23).
    """
    from django.contrib.auth import get_user_model
    from django.db.models import Q
    from django.utils import timezone

    from core import rbac

    User = get_user_model()
    now = timezone.now()
    keys = [k for k, rank in rbac.ROLE_RANK.items() if rank >= rbac.ROLE_RANK[ROLE_DIRECTOR]]
    active = Q(role_assignments__expires_at__isnull=True) | Q(role_assignments__expires_at__gte=now)
    return list(
        User.objects.filter(characters__is_corp_member=True)
        .filter(Q(is_superuser=True) | (Q(role_assignments__role__key__in=keys) & active))
        .values_list("pk", flat=True).distinct()
    )


def _viewable_ids(campaign, ids) -> list[int]:
    """Drop ids that cannot view the campaign, de-dup, preserve order (doc 09 §4 belt-and-braces:
    an audience member who cannot see the campaign is never included)."""
    from django.contrib.auth import get_user_model

    from . import services

    User = get_user_model()
    wanted = [i for i in ids if i]
    users = {u.pk: u for u in User.objects.filter(pk__in=set(wanted))}
    out: list[int] = []
    for uid in wanted:
        u = users.get(uid)
        if u is not None and uid not in out and services.can_view(u, campaign):
            out.append(uid)
    return out


def _restricted_leadership_ids(campaign, *, include_participants: bool = False) -> list[int]:
    """The individually-targeted recipients allowed to receive a restricted campaign's
    leadership/broadcast event (doc 09 §4.1 rule 1): directors + commander + sponsor (+ the
    listed participants for start/complete announcements), each re-checked with ``can_view``."""
    ids = list(_director_ids())
    if campaign.commander_id:
        ids.append(campaign.commander_id)
    if campaign.sponsor_id:
        ids.append(campaign.sponsor_id)
    if include_participants:
        ids += list(campaign.restricted_users.values_list("pk", flat=True))
    return _viewable_ids(campaign, ids)


def _emit(campaign, key, *, audience, title, body, source_object_id, idempotency_key,
          priority=None):
    """The one place a campaign alert is created. Fail-soft; enforces the restricted rules."""
    try:
        from apps.pingboard.notifications import is_enabled

        if not is_enabled(key):
            return None
        # A restricted campaign never fans out to a shared audience (doc 09 §4.1 rule 1).
        if _is_restricted(campaign) and (audience or {}).get("kind") not in ("user", "users"):
            log.warning(
                "campaigns.notify refusing non-targeted audience for restricted campaign %s key=%s",
                campaign.pk, key,
            )
            return None
        if _is_restricted(campaign):
            title, body = _restricted_payload(campaign)

        from apps.pingboard import services as pingboard

        alert = pingboard.emit_broadcast(
            category=CATEGORY, title=title, body=body, audience=audience,
            source_service="campaigns", source_object_id=source_object_id,
            idempotency_key=idempotency_key, priority=priority,
        )
        # Log restraint (doc 09 §4.1 rule 5): key + pk + emitted flag, never names/values.
        log.info("campaigns.notify key=%s campaign=%s emitted=%s", key, campaign.pk, alert is not None)
        return alert
    except Exception:  # noqa: BLE001 — a notification must never break the campaign action
        log.exception("campaigns.notify failed key=%s campaign=%s", key, getattr(campaign, "pk", "?"))
        return None


def _transition_count(campaign, verb, to_status, *, target_kind="", target_id=0) -> int:
    """How many times this campaign reached ``to_status`` — the idempotency counter that lets a
    re-proposed / re-approved campaign notify again while a replay of the same transition does
    not (doc 09 §4)."""
    from .models import CampaignActivity

    qs = CampaignActivity.objects.filter(campaign=campaign, verb=verb, after__status=to_status)
    if target_kind:
        qs = qs.filter(target_kind=target_kind, target_id=target_id)
    return qs.count()


# --------------------------------------------------------------------------- #
#  Assignment & recognition (member DMs)
# --------------------------------------------------------------------------- #
def assigned(campaign, kind: str, obj_id, user, *, what: str = "") -> None:
    """An objective/workstream/milestone owner (or campaign commander) was set/changed to
    ``user`` — DM the newly assigned pilot (doc 09 §4)."""
    uid = getattr(user, "pk", None)
    if not uid:
        return
    ids = _viewable_ids(campaign, [uid])
    if not ids:
        return
    label = what or {"objective": "an objective", "workstream": "a workstream",
                     "milestone": "a milestone", "campaign": "command of this campaign"}.get(
        kind, "a campaign item")
    body = (
        f"You've been assigned {label} on campaign «{campaign.name}». "
        f"Why it matters: {(campaign.rationale or campaign.summary or '').strip()[:160] or 'see the campaign.'} "
        f"{_detail_url(campaign)}"
    )
    _emit(
        campaign, ASSIGNED, audience={"kind": "user", "id": ids[0]},
        title=f"Assigned on «{campaign.name}»", body=body,
        source_object_id=f"{kind}:{obj_id}",
        idempotency_key=f"campaigns:assigned:{kind}:{obj_id}:{ids[0]}",
    )


def recognition(recognition_row) -> None:
    """A ``CampaignRecognition`` row was created — a private DM to the recognised pilot,
    regardless of their public-recognition opt-out (doc 09 §7)."""
    campaign = recognition_row.campaign
    uid = recognition_row.user_id
    if not uid:
        return
    ids = _viewable_ids(campaign, [uid])
    if not ids:
        return
    body = (
        f"You were recognised for your contribution to campaign «{campaign.name}»"
        f"{' — ' + recognition_row.reason.strip()[:160] if recognition_row.reason else ''}. "
        f"{_detail_url(campaign)}"
    )
    _emit(
        campaign, RECOGNITION, audience={"kind": "user", "id": ids[0]},
        title=f"Recognised on «{campaign.name}»", body=body,
        source_object_id=f"recognition:{recognition_row.pk}",
        idempotency_key=f"campaigns:recognition:{recognition_row.pk}", priority="low",
    )


# --------------------------------------------------------------------------- #
#  Objective / dependency member DMs
# --------------------------------------------------------------------------- #
def objective_blocked(objective, *, blockers) -> None:
    """An objective flipped to blocked — DM its owner + the commander. ``blockers`` is the set of
    stable ids identifying the blocking cause, so a re-block with a *different* cause notifies
    again while the same cause never re-pings (doc 09 §4)."""
    campaign = objective.campaign
    sig = hashlib.sha256("|".join(str(b) for b in sorted(blockers)).encode()).hexdigest()[:16]
    ids = _viewable_ids(campaign, [objective.owner_id, campaign.commander_id])
    if not ids:
        return
    body = (
        f"Objective «{objective.title}» on campaign «{campaign.name}» is blocked"
        f"{' — ' + objective.block_reason.strip()[:160] if objective.block_reason else ''}. "
        f"{_detail_url(campaign)}"
    )
    _emit(
        campaign, OBJECTIVE_BLOCKED, audience={"kind": "users", "ids": ids},
        title=f"Objective blocked on «{campaign.name}»", body=body,
        source_object_id=f"objective:{objective.pk}",
        idempotency_key=f"campaigns:blocked:{objective.pk}:{sig}", priority="high",
    )


def dependency_completed(dependency, owner_ids) -> None:
    """A dependency target reached terminal-done and the edge auto-resolved — DM the owners of
    the now-unblocked ``from`` items plus the commander (doc 09 §4)."""
    campaign = dependency.campaign
    ids = _viewable_ids(campaign, list(owner_ids) + [campaign.commander_id])
    if not ids:
        return
    body = (
        f"A dependency cleared on campaign «{campaign.name}» — work that was waiting on it can "
        f"proceed. {_detail_url(campaign)}"
    )
    _emit(
        campaign, DEPENDENCY_COMPLETED, audience={"kind": "users", "ids": ids},
        title=f"Dependency cleared on «{campaign.name}»", body=body,
        source_object_id=f"dependency:{dependency.pk}",
        idempotency_key=f"campaigns:dep_done:{dependency.pk}",
    )


# --------------------------------------------------------------------------- #
#  Deadline / staleness sweeps (Phase 3 beats call these; helpers land now)
# --------------------------------------------------------------------------- #
def deadline_soon(campaign, kind: str, item, bucket: str, *, owner_id=None, title="") -> None:
    """A due-soon/overdue reminder for an objective or milestone (doc 09 §4). Bucketed idempotency
    key gives at-most-once per (item, bucket). Owner, falling back to the commander when
    unowned. Used by the Phase 3 ``sweep_deadlines`` beat."""
    ids = _viewable_ids(campaign, [owner_id, campaign.commander_id])
    if not ids:
        return
    when = "is overdue" if bucket == "overdue" else "is due soon"
    body = (
        f"«{title or getattr(item, 'title', 'A campaign item')}» on campaign «{campaign.name}» "
        f"{when}. {_detail_url(campaign)}"
    )
    _emit(
        campaign, DEADLINE_SOON, audience={"kind": "user", "id": ids[0]},
        title=f"Deadline on «{campaign.name}»", body=body,
        source_object_id=f"{kind}:{getattr(item, 'pk', 0)}",
        idempotency_key=f"campaigns:due:{kind}:{getattr(item, 'pk', 0)}:{bucket}",
        priority="high" if bucket == "overdue" else "normal",
    )


def manual_update_needed(campaign, objective, iso_week: str, *, owner_id=None) -> None:
    """A stale manual-metric objective nudge to its owner (doc 09 §4). At most one per objective
    per ISO week. Used by the Phase 3 ``sweep_deadlines`` beat."""
    ids = _viewable_ids(campaign, [owner_id or objective.owner_id, campaign.commander_id])
    if not ids:
        return
    body = (
        f"Objective «{objective.title}» on campaign «{campaign.name}» needs a manual value "
        f"update — its last reading has gone stale. {_detail_url(campaign)}"
    )
    _emit(
        campaign, MANUAL_UPDATE_NEEDED, audience={"kind": "user", "id": ids[0]},
        title=f"Metric needs an update on «{campaign.name}»", body=body,
        source_object_id=f"objective:{objective.pk}",
        idempotency_key=f"campaigns:manual:{objective.pk}:{iso_week}", priority="low",
    )


# --------------------------------------------------------------------------- #
#  Lifecycle & leadership events
# --------------------------------------------------------------------------- #
def approval_needed(campaign, *, milestone=None) -> None:
    """``draft → proposed`` needs a director; a milestone marked ready needs its commander
    (doc 04 §5, doc 09 §4). Both ride the ``campaigns.approval_needed`` event."""
    if milestone is not None:
        if not campaign.commander_id:
            return
        ids = _viewable_ids(campaign, [campaign.commander_id])
        if not ids:
            return
        count = _transition_count(
            campaign, "milestone.status", "ready_for_review",
            target_kind="milestone", target_id=milestone.pk,
        )
        _emit(
            campaign, APPROVAL_NEEDED, audience={"kind": "user", "id": ids[0]},
            title=f"Milestone ready for review on «{campaign.name}»",
            body=(f"Milestone «{milestone.title}» is ready for your review on campaign "
                  f"«{campaign.name}». {_detail_url(campaign)}"),
            source_object_id=f"milestone:{milestone.pk}",
            idempotency_key=f"campaigns:mreview:{milestone.pk}:{count}", priority="high",
        )
        return

    count = _transition_count(campaign, "status.changed", "proposed")
    if _is_restricted(campaign):
        ids = _viewable_ids(campaign, _director_ids())
        if not ids:
            return
        audience = {"kind": "users", "ids": ids}
    else:
        audience = {"kind": "director"}
    _emit(
        campaign, APPROVAL_NEEDED, audience=audience,
        title=f"Campaign proposed: «{campaign.name}»",
        body=(f"Campaign «{campaign.name}» has been proposed and needs a director's approval. "
              f"{_detail_url(campaign)}"),
        source_object_id=f"campaign:{campaign.pk}",
        idempotency_key=f"campaigns:approval:{campaign.pk}:{count}", priority="high",
    )


def approved(campaign) -> None:
    """``proposed → approved`` — DM the commander/proposer (doc 09 §4)."""
    target = campaign.commander_id or campaign.created_by_id
    ids = _viewable_ids(campaign, [target])
    if not ids:
        return
    count = _transition_count(campaign, "status.changed", "approved")
    _emit(
        campaign, APPROVED, audience={"kind": "user", "id": ids[0]},
        title=f"Campaign approved: «{campaign.name}»",
        body=(f"Your campaign «{campaign.name}» was approved and is ready to start. "
              f"{_detail_url(campaign)}"),
        source_object_id=f"campaign:{campaign.pk}",
        idempotency_key=f"campaigns:approved:{campaign.pk}:{count}",
    )


def _visibility_audience(campaign):
    """Audience for start/complete announcements, keyed on visibility (doc 09 §4)."""
    from .models import Campaign

    V = Campaign.Visibility
    vis = campaign.visibility
    if vis == V.MEMBERS:
        return {"kind": "corp"}
    if vis == V.OFFICERS:
        return {"kind": "officer"}
    if vis == V.DIRECTORS:
        return {"kind": "director"}
    return {"kind": "users", "ids": _restricted_leadership_ids(campaign, include_participants=True)}


def started(campaign) -> None:
    """``approved → active`` announcement, audience per visibility (doc 09 §4)."""
    audience = _visibility_audience(campaign)
    if audience.get("kind") == "users" and not audience.get("ids"):
        return
    _emit(
        campaign, STARTED, audience=audience,
        title=f"Campaign started: «{campaign.name}»",
        body=(f"Campaign «{campaign.name}» is now active. "
              f"{(campaign.rationale or campaign.summary or '').strip()[:200]} "
              f"{_detail_url(campaign)}"),
        source_object_id=f"campaign:{campaign.pk}",
        idempotency_key=f"campaigns:status:{campaign.pk}:active",
    )


def completed(campaign, to_status: str) -> None:
    """``active → completed|failed|cancelled`` announcement (doc 09 §4)."""
    audience = _visibility_audience(campaign)
    if audience.get("kind") == "users" and not audience.get("ids"):
        return
    verb = {"completed": "completed", "failed": "ended (failed)",
            "cancelled": "was cancelled"}.get(to_status, to_status)
    _emit(
        campaign, COMPLETED, audience=audience,
        title=f"Campaign {verb}: «{campaign.name}»",
        body=(f"Campaign «{campaign.name}» {verb}. {_detail_url(campaign)}"),
        source_object_id=f"campaign:{campaign.pk}",
        idempotency_key=f"campaigns:status:{campaign.pk}:{to_status}",
    )


def issue_escalated(issue) -> None:
    """An issue was escalated (a human action) — leadership alert (doc 04 §7, doc 09 §4)."""
    campaign = issue.campaign
    if _is_restricted(campaign):
        ids = _restricted_leadership_ids(campaign)
        if not ids:
            return
        audience = {"kind": "users", "ids": ids}
    else:
        audience = {"kind": "officer"}
    _emit(
        campaign, ISSUE_ESCALATED, audience=audience,
        title=f"Issue escalated on «{campaign.name}»",
        body=(f"An issue on campaign «{campaign.name}» has been escalated and needs leadership "
              f"attention. {_detail_url(campaign)}"),
        source_object_id=f"issue:{issue.pk}",
        idempotency_key=f"campaigns:issue_escalated:{issue.pk}", priority="high",
    )


def health_changed(campaign) -> None:
    """Emit at most one alert per distinct health problem-set signature (doc 08 §4.2, doc 09 §4).

    ``problems = [level] + reason codes``; an unchanged signature is a no-op and a recovery/level
    change re-arms the next. ``unknown`` health is not a signal — it resets the stored signature
    without emitting. The signature slot is burned only after pingboard accepts the alert, so a
    disabled/suppressed emit retries on the next recompute (the reliability rule from
    ``apps.pingboard.dedup``)."""
    try:
        from apps.admin_audit.models import AppSetting

        from .models import Campaign

        level = campaign.health
        sig_key = _HEALTH_SIG_KEY.format(pk=campaign.pk)
        stored = AppSetting.objects.filter(key=sig_key).first()

        if level == Campaign.Health.UNKNOWN:
            if stored is not None:
                stored.delete()  # unknown → clear so the next real level notifies
            return

        reasons = campaign.health_reasons or []
        problems = [level] + [r.get("code", "") for r in reasons if isinstance(r, dict)]
        sig = hashlib.sha256("|".join(problems).encode()).hexdigest()
        if stored is not None and (stored.value or {}).get("sig") == sig:
            return  # unchanged — never re-ping

        from django.utils import timezone

        # The stored signature is the real dedup (an unchanged problem-set returned above). The
        # idempotency key gets a per-firing UTC stamp so a *recurrence* after recovery — a genuine
        # signature change that happens to reuse the {level}:{sig} pair — is not swallowed by
        # pingboard returning the prior alert for a reused key (#3, mirrors apps/pingboard/dedup.py).
        stamp = timezone.now().strftime("%Y%m%d%H%M%S%f")
        base_key = f"campaigns:health:{campaign.pk}:{level}:{sig[:16]}:{stamp}"
        priority = "high" if level in _HIGH_HEALTH else "normal"
        labels = ", ".join(r.get("label", "") for r in reasons if isinstance(r, dict))[:200]
        title = f"Campaign health {campaign.get_health_display()}: «{campaign.name}»"
        body = (f"Campaign «{campaign.name}» is now {campaign.get_health_display()}"
                f"{' — ' + labels if labels else ''}. {_detail_url(campaign)}")

        if _is_restricted(campaign):
            ids = _restricted_leadership_ids(campaign)  # already includes commander + sponsor
            if not ids:
                return
            audience = {"kind": "users", "ids": ids}
        else:
            from apps.pingboard.notifications import resolve

            audience = {"kind": resolve(HEALTH_CHANGED).get("audience") or "officer"}

        alert = _emit(
            campaign, HEALTH_CHANGED, audience=audience, title=title, body=body,
            source_object_id=f"campaign:{campaign.pk}", idempotency_key=base_key, priority=priority,
        )
        if not _is_restricted(campaign):
            # doc 09 line 120: commander + sponsor are always appended as individual recipients, so a
            # member-commander receives health alerts the role-kind broadcast would never reach (#17).
            lead_ids = _viewable_ids(campaign, [campaign.commander_id, campaign.sponsor_id])
            if lead_ids:
                _emit(
                    campaign, HEALTH_CHANGED, audience={"kind": "users", "ids": lead_ids},
                    title=title, body=body, source_object_id=f"campaign:{campaign.pk}",
                    idempotency_key=f"{base_key}:leads", priority=priority,
                )
        if alert is not None:
            AppSetting.objects.update_or_create(
                key=sig_key,
                defaults={"value": {"sig": sig, "at": timezone.now().isoformat(),
                                    "problems": problems}},
            )
    except Exception:  # noqa: BLE001 — health notification must never break the recompute
        log.exception("campaigns.notify health_changed failed campaign=%s", getattr(campaign, "pk", "?"))
