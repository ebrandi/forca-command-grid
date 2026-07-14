"""Code-defined, gettext-wrapped message scaffolds for the persisted mentorship prose.

**Seam B.** The sentences below are not chrome: they are *written into the database* by one
actor (usually a Celery worker — ``mentorship.scan_anomalies``, ``mentorship.sweep_api_validations``,
``mentorship.auto_suggest_pairings``, ``mentorship.expire_stale_pairings``, ``mentorship.refresh_eligibility``)
and read back later by *other* people (the mentor, the cadet, every officer), each under their own
locale.

Wrapping the write site in ``gettext``/``gettext_lazy`` cannot work here, and is in fact a trap:

* Django coerces a lazy proxy to ``str`` on ``.save()``, so the row freezes in whatever locale the
  *writer* had. A Celery worker has no request and no user, so that locale is English — a naive
  ``_()`` at the write site passes ``makemessages`` and translates precisely nothing, forever.
* A lazy proxy placed inside a ``JSONField`` is a hard ``TypeError`` at save time.

So the write site persists a **key + params** (plain, JSON-safe values) *alongside* the English
prose column, and the read site re-resolves the scaffold under the **reader's** locale
(:func:`render`). The prose column stays the English fallback and the audit record: a legacy row
written before this change carries no key and simply renders its stored English — never blank.

Contract:

* Every value in :data:`SCAFFOLDS` is a **literal** ``_("…")`` call, so ``xgettext`` can see it.
  A ``_(f"…")`` or ``_(variable)`` is a silent no-op and must never appear.
* Placeholders are named ``%(param)s`` — never f-strings, never positional.
* The interpolated values stay **raw**: EVE/game data, corp-authored content (a track title, a
  reward-rule label), free text a pilot typed, and numbers are substituted verbatim and are never
  themselves translated.
* The msgid text is the *exact* English that is written to the prose column
  (:func:`english` resolves it with translations deactivated), so English output is unchanged and
  the two can never drift.
* Keys are identifiers: they are persisted, compared and looked up. Never translate a key.
"""
from __future__ import annotations

from django.utils.translation import gettext_lazy as _
from django.utils.translation import override

# Keyed by the string persisted in ``<field>_key``. Params are the ``%(name)s`` slots.
SCAFFOLDS: dict[str, str] = {
    # -- MentorshipPairingEvent.detail -------------------------------------------------
    # One key per initiator rather than a %(initiator)s slot: the initiator label is itself a
    # translatable enum, and a param is substituted raw by contract.
    "pairing.proposed.mentor": _("Proposed (Mentor)."),
    "pairing.proposed.mentee": _("Proposed (Mentee)."),
    "pairing.proposed.leader": _("Proposed (Leadership)."),
    "pairing.proposed.system": _("Proposed (System (auto-suggested))."),
    "pairing.approved_by_leadership": _("Approved by leadership."),
    "pairing.awaiting_approval": _("Awaiting leadership approval."),
    "pairing.auto_activated": _("Auto-activated."),
    "pairing.auto_expired": _("Auto-expired (TTL)."),
    "pairing.resumed": _("Resumed."),
    "pairing.declined_by_pilot": _("Declined by pilot."),
    # %(track)s is corp-authored content (a MentorshipTrack title) — raw, never translated.
    "pairing.track_enrolled": _("Enrolled in track: %(track)s"),
    # %(topic)s is free text the pilot typed — raw, never translated.
    "pairing.session_scheduled": _("Session scheduled: %(topic)s"),
    "pairing.session_scheduled_default": _("Session scheduled: mentoring"),

    # -- MentorshipTaskValidation.detail / MentorshipTaskAssignment.last_reason ---------
    "task.mentee_marked_done": _("Mentee marked done."),
    "task.mentee_self_confirmed": _("Mentee self-confirmed."),
    "task.both_confirmed": _("Both confirmed."),
    "task.mentor_confirmed": _("Mentor confirmed."),
    "task.approved_by_leadership": _("Approved by leadership."),
    "task.leadership_approved": _("Leadership approved."),
    "task.awaiting_activity_data": _("Awaiting activity data."),
    # %(reason)s is free text an officer typed — raw, never translated.
    "task.waived": _("Waived: %(reason)s"),

    # -- validation.Outcome.detail (auto-checks; the Celery sweep writes these) ---------
    "check.unavailable": _("Check unavailable right now."),
    "check.no_skills": _("No skills imported yet."),
    "check.skill_min": _("Trained level %(have)s (need %(want)s)."),
    "check.total_sp_min": _("%(total)s SP (need %(need)s)."),
    "check.skillqueue_present": _("Skill is in the training queue."),
    "check.skillqueue_absent": _("Skill not found in the training queue."),
    "check.doctrine_ready": _("Can fly this doctrine."),
    "check.doctrine_not_ready": _("Not yet flyable."),
    "check.doctrine_any": _("Can fly %(count)s doctrine ship(s)."),
    "check.skill_plan_exists": _("A skill plan exists."),
    "check.skill_plan_missing": _("No skill plan yet."),
    "check.killmail_recent": _("%(count)s killmail(s) in window (need %(need)s)."),
    "check.shared_killmail": _("Mentor & mentee on the same killmail."),
    "check.shared_killmail_none": _("No shared killmail yet."),
    "check.fleet_attended": _("%(count)s fleet(s) attended (need %(need)s)."),
    # %(kind)s is a ContributionEvent.Kind code — an identifier, raw by contract.
    "check.contribution_kind": _("%(count)s %(kind)s event(s)."),
    "check.courier_delivered": _("Delivered a courier contract."),
    "check.courier_none": _("No completed courier yet."),
    "check.buyback_submitted": _("Submitted a buyback lot."),
    "check.buyback_none": _("No buyback lot yet."),
    "check.mining_ledger": _("%(total)s units mined (need %(need)s)."),
    "check.industry_installed": _("Installed an industry job."),
    "check.industry_none": _("No industry job yet."),
    "check.session_confirmed": _("A session was confirmed by both."),
    "check.session_none": _("No confirmed session yet."),
    "check.scopes_none_required": _("No scopes required."),
    "check.scopes_granted": _("Required ESI scopes are granted."),
    "check.scopes_missing": _("Required ESI scopes not granted."),

    # -- MentorshipFlag.detail (raised by the anomaly sweep, read by officers) ----------
    "flag.rapid_completion": _("%(count)s tasks completed within %(minutes)s min."),
    "flag.self_confirm_streak": _("%(count)s recent tasks completed by mentee self-confirmation only."),
    "flag.mentor_rubber_stamp": _("%(count)s mentor approvals landed <%(seconds)ss after submission."),
    "flag.capacity_exceeded": _("Mentor is over their active-mentee capacity."),
    "flag.stale_pair": _("No activity for %(days)s+ days."),
    # %(rule)s is a corp-authored MentorshipRewardRule label — raw, never translated.
    "flag.cap_hit": _("Reward cap reached for rule '%(rule)s'."),

    # -- MentorshipPairing.match_reasons (the auto-suggest worker writes these) ---------
    # %(areas)s / %(languages)s are pilot-entered tags — raw, never translated.
    "match.shared_focus": _("Shared focus: %(areas)s."),
    "match.no_overlap": _("No overlapping focus areas."),
    "match.same_timezone": _("Same time zone (%(timezone)s)."),
    "match.shared_language": _("Shared language: %(languages)s."),
    "match.has_capacity": _("Has capacity (%(active)s/%(capacity)s mentees)."),
    "match.open_to_adhoc": _("Open to ad-hoc questions."),
    "match.prior_cancel": _("Previously unpaired — lower priority."),

    # -- MentorProfile/MenteeProfile.eligibility["reasons"] (refresh_eligibility worker) -
    "elig.no_character": _("No linked character — link a character first."),
    "elig.mentor_age_meets": _("Character is %(years)sy %(days)sd old (meets the %(min)sd minimum)."),
    "elig.mentor_age_below": _("Character is %(years)sy %(days)sd old (below the %(min)sd minimum)."),
    "elig.mentor_age_unknown": _("Character age unknown (ESI unavailable)."),
    "elig.mentor_tenure_meets": _("~%(days)s days in the corp (meets the %(min)sd minimum)."),
    "elig.mentor_tenure_below": _("~%(days)s days in the corp (below the %(min)sd minimum)."),
    "elig.mentor_tenure_unknown": _("Corp tenure unknown."),
    "elig.mentee_disabled": _("Mentee eligibility check is disabled — everyone may join as a cadet."),
    "elig.mentee_tenure_unknown": _("Corp tenure unknown; treating as a new pilot (low confidence)."),
    "elig.mentee_tenure_under": _("~%(days)s days in the corp (under the %(max)sd cap for cadets)."),
    "elig.mentee_tenure_over": _("~%(days)s days in the corp (over the %(max)sd cap for cadets)."),
}


def _interpolate(text: str, params: dict | None) -> str:
    if not params:
        return text
    try:
        return text % params
    except (KeyError, TypeError, ValueError):
        # A translator who mangled a %(slot)s must never blank or crash a stored sentence.
        return text


def english(key: str, params: dict | None = None) -> str:
    """The **English** sentence for ``key`` — what gets written to the prose column.

    ``override(None)`` deactivates translation entirely, so ``str()`` yields the msgid verbatim
    no matter which locale the writer happens to be in (a Celery worker has none; a web request
    may be German). This is what makes the stored English column and the msgid a single source of
    truth: they cannot drift, because the column *is* the msgid.
    """
    scaffold = SCAFFOLDS.get(key)
    if scaffold is None:
        return ""
    with override(None):
        return _interpolate(str(scaffold), params)


def render(key: str, params: dict | None, fallback: str) -> str:
    """The sentence for ``key`` under the **reader's** active locale.

    Falls back to the stored English prose whenever there is no key (a legacy row), the key is
    unknown (a scaffold that was retired), or the resolved text is empty. Never returns blank when
    ``fallback`` is non-empty, and never raises.
    """
    if not key:
        return fallback
    scaffold = SCAFFOLDS.get(key)
    if scaffold is None:
        return fallback
    # str() forces the gettext_lazy proxy to resolve NOW, under the reader's active catalogue.
    text = _interpolate(str(scaffold), params)
    return text or fallback


def render_list(entries: list | None, fallback: list | None) -> list[str]:
    """Reader-locale render of a persisted ``[{"key": …, "params": {…}}, …]`` list.

    ``fallback`` is the parallel English prose list (same order, written in lockstep). A legacy row
    has no entries at all and renders its stored English verbatim.
    """
    prose = list(fallback or [])
    if not entries:
        return prose
    out: list[str] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        default = prose[index] if index < len(prose) else ""
        text = render(entry.get("key", ""), entry.get("params") or {}, default)
        if text:
            out.append(text)
    return out or prose


def english_list(entries: list | None) -> list[str]:
    """The English prose list for ``entries`` — what gets written to the prose column."""
    out = []
    for entry in entries or []:
        text = english(entry.get("key", ""), entry.get("params") or {})
        if text:
            out.append(text)
    return out
