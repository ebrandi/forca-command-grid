"""Persisted-prose i18n seam for capsuleer — a key + params registry ("Seam B").

Two kinds of capsuleer prose are **written into the database by a Celery worker** and read back
later by a *different* request, under a *different* reader's locale:

* ``CareerMilestone.data_source`` — the provenance/honesty line stamped by the verification engine
  (``verify.py`` → ``services._stamp_check_state``) on the hourly sweep and the skills-import hook.
* ``PathSuggestion.title`` / ``PathSuggestion.reason`` — written by the daily suggestion beat
  (``suggest.py``).

Wrapping those sentences in ``gettext``/``gettext_lazy`` at the write site **cannot work**: Django
coerces a lazy proxy to ``str`` on ``.save()`` (and a proxy inside a ``JSONField`` is a hard
``TypeError``), so the row would be frozen in whatever locale the *writer* had — the worker's, i.e.
English — and every reader would see that forever. A naive ``_()`` there passes ``makemessages``
and translates nothing.

So the write site persists a **key + JSON-safe params** next to the English prose, and the read
site re-renders the sentence under the *reader's* locale:

    write (worker, English)  ->  data_source      = "asset mirror as of 2026-07-01 12:00 UTC"
                                 data_source_key  = "milestone.source.asset_mirror"
                                 data_source_params = {"as_of": "2026-07-01 12:00"}
    read  (reader, de)       ->  data_source_i18n -> str(SCAFFOLDS[key]) % params   (German)

The prose column stays: it is the English fallback *and* the audit record, and it is what a legacy
row (written before this change, hence keyless) renders — never blank, never a msgid.

Contract:

* Every msgid below is a real literal ``_("…")`` with ``%(name)s`` placeholders, so ``xgettext``
  sees it. ``_(f"…")`` is a silent no-op and must never appear.
* Every msgid is **byte-identical to the English prose the write site still stores**, so English
  output is unchanged whichever branch renders it.
* Params are plain JSON (``int`` / ``str`` / lists of refs) — never a lazy proxy, never a model.
* Interpolated values (doctrine, ship, operation and goal names, activity/category slugs, dates)
  stay RAW: the i18n boundary lands between the scaffold sentence and the substituted value.
* Keys are identifiers: never translated, never compared against prose.
"""
from __future__ import annotations

from django.utils import translation
from django.utils.translation import gettext_lazy as _

# The source language of every msgid in this module. The prose *column* must be this language no
# matter who or what happens to be writing the row (a worker with no locale, a beat, or — one day —
# a request from a German officer), because it is the audit record and the fallback every legacy and
# unresolvable row degrades to.
SOURCE_LANGUAGE = "en"

# --------------------------------------------------------------------------- #
#  Stable keys (identifiers — never translated, never rendered)
# --------------------------------------------------------------------------- #
# CareerMilestone.data_source — the verification engine's provenance lines (verify.py, plan.py).
SRC_SKILLS_SNAPSHOT = "milestone.source.skills_snapshot"
SRC_NO_SKILL_DATA = "milestone.source.no_skill_data"
SRC_DOCTRINE_READINESS = "milestone.source.doctrine_readiness"
SRC_NO_DOCTRINE = "milestone.source.no_doctrine"
SRC_NO_DERIVED_REQS = "milestone.source.no_derived_requirements"
SRC_CONTRIBUTION_LEDGER = "milestone.source.contribution_ledger"
SRC_KILLBOARD_FIRSTS = "milestone.source.killboard_firsts"
SRC_ASSET_MIRROR = "milestone.source.asset_mirror"
SRC_ASSETS_NOT_SHARED = "milestone.source.assets_not_shared"
SRC_CHARACTER_UNLINKED = "milestone.source.character_unlinked"
SRC_NO_CHECKER = "milestone.source.no_checker"
SRC_CHECK_UNAVAILABLE = "milestone.source.check_unavailable"
SRC_SKILL_UNRESOLVED = "milestone.source.skill_unresolved"
SRC_SHIP_UNRESOLVED = "milestone.source.ship_unresolved"

# PathSuggestion.title / .reason — one key per generator branch (suggest.py).
SUG_NEAR_QUAL = "suggestion.near_qualification"
SUG_NEAR_QUAL_CORP = "suggestion.near_qualification_corp"
SUG_EVENT_MATCH = "suggestion.event_match"
SUG_EVENT_MATCH_INTEREST = "suggestion.event_match_interest"
SUG_MENTOR_AVAILABLE = "suggestion.mentor_available"
SUG_STALLED_GOAL = "suggestion.stalled_goal"
SUG_BLOCKED_PREREQ = "suggestion.blocked_prereq"
SUG_SHIP_AVAILABLE = "suggestion.ship_available"
SUG_SHIP_AVAILABLE_BUDGET = "suggestion.ship_available_budget"
SUG_CAMPAIGN_OPPORTUNITY = "suggestion.campaign_opportunity"
SUG_REVIEW_DUE_GOAL = "suggestion.review_due_goal"
SUG_REVIEW_DUE_PROFILE = "suggestion.review_due_profile"


# --------------------------------------------------------------------------- #
#  The registry: key -> gettext_lazy msgid with %(param)s placeholders
# --------------------------------------------------------------------------- #
SCAFFOLDS: dict[str, str] = {
    # --- milestone provenance (verify.py) ---------------------------------- #
    SRC_SKILLS_SNAPSHOT: _("skills snapshot as of %(as_of)s UTC"),
    SRC_NO_SKILL_DATA: _("No skill data yet — import your skills."),
    SRC_DOCTRINE_READINESS: _("doctrine readiness vs skills snapshot as of %(as_of)s UTC"),
    SRC_NO_DOCTRINE: _("no matching doctrine available"),
    SRC_NO_DERIVED_REQS: _("No derived requirements for %(doctrine)s yet."),
    SRC_CONTRIBUTION_LEDGER: _("contribution ledger (account-wide, live)"),
    SRC_KILLBOARD_FIRSTS: _("killboard firsts (nightly scan — up to 24h behind)"),
    SRC_ASSET_MIRROR: _("asset mirror as of %(as_of)s UTC"),
    SRC_ASSETS_NOT_SHARED: _(
        "Asset data not shared for this character — opt in to check ownership."
    ),
    SRC_CHARACTER_UNLINKED: _("This goal's character is not linked."),
    SRC_NO_CHECKER: _("No automatic check for %(kind)s."),
    SRC_CHECK_UNAVAILABLE: _("Automatic check is temporarily unavailable."),
    SRC_SKILL_UNRESOLVED: _("skill milestone unresolved against current SDE"),
    SRC_SHIP_UNRESOLVED: _("ship milestone unresolved against current SDE"),

    # --- suggestion titles (suggest.py) ------------------------------------ #
    f"{SUG_NEAR_QUAL}.title": _("Close to flying %(doctrine)s"),
    f"{SUG_NEAR_QUAL_CORP}.title": _("Close to a corp doctrine: %(doctrine)s"),
    f"{SUG_EVENT_MATCH}.title": _("%(operation)s matches your %(activity)s goal"),
    f"{SUG_EVENT_MATCH_INTEREST}.title": _("%(operation)s fits your interests"),
    f"{SUG_MENTOR_AVAILABLE}.title": _("Mentors available for «%(goal)s»"),
    f"{SUG_STALLED_GOAL}.title": _("«%(goal)s» hasn't moved lately"),
    f"{SUG_BLOCKED_PREREQ}.title": _("«%(goal)s» is blocked"),
    f"{SUG_SHIP_AVAILABLE}.title": _("Shipyard can supply your goal hull"),
    f"{SUG_SHIP_AVAILABLE_BUDGET}.title": _("Shipyard can supply your goal hull"),
    f"{SUG_CAMPAIGN_OPPORTUNITY}.title": _("Campaign «%(campaign)s» needs help"),
    f"{SUG_REVIEW_DUE_GOAL}.title": _("Time to review «%(goal)s»"),
    f"{SUG_REVIEW_DUE_PROFILE}.title": _("Time to review your preferences"),

    # --- suggestion reasons (suggest.py) ----------------------------------- #
    f"{SUG_NEAR_QUAL}.reason": _(
        "You are about %(levels)s skill level(s) — roughly %(days)s day(s) of training — "
        "from flying %(doctrine)s, which your goal «%(goal)s» targets. Based on "
        "your skills as of %(as_of)s."
    ),
    f"{SUG_NEAR_QUAL_CORP}.reason": _(
        "You are about %(levels)s skill level(s) — roughly %(days)s day(s) — from "
        "flying %(doctrine)s, a corp priority-%(priority)s doctrine. "
        "Entirely optional — your call."
    ),
    f"{SUG_EVENT_MATCH}.reason": _(
        "«%(operation)s» (%(op_type)s) forms up "
        "%(when)s EVE and matches your goal «%(goal)s»."
    ),
    f"{SUG_EVENT_MATCH_INTEREST}.reason": _(
        "«%(operation)s» (%(op_type)s) forms up "
        "%(when)s EVE and matches something you enjoy. "
        "Optional — your call."
    ),
    f"{SUG_MENTOR_AVAILABLE}.reason": _(
        "%(count)s mentor(s) currently cover %(category)s, which matches your goal "
        "«%(goal)s». Mentors only ever see what you explicitly share."
    ),
    f"{SUG_STALLED_GOAL}.reason": _(
        "«%(goal)s» hasn't moved in about %(weeks)s week(s). That's "
        "completely fine — interests change. If it helps, you could lower its "
        "priority, pause it, or adjust the target date."
    ),
    f"{SUG_BLOCKED_PREREQ}.reason": _(
        "«%(goal)s» is blocked: %(blockers)s. You can edit the milestone, "
        "pick a different doctrine, or keep the goal parked until this resolves — "
        "nothing expires."
    ),
    f"{SUG_SHIP_AVAILABLE}.reason": _(
        "The corp shipyard can supply a %(ship)s for your goal «%(goal)s»."
    ),
    f"{SUG_SHIP_AVAILABLE_BUDGET}.reason": _(
        "The corp shipyard can supply a %(ship)s for your goal «%(goal)s»."
        " It fits within your configured monthly budget."
    ),
    f"{SUG_CAMPAIGN_OPPORTUNITY}.reason": _(
        "Campaign «%(campaign)s» is asking for help: «%(objective)s». This matches "
        "your interest in %(activity)s. Volunteering is entirely optional and visible to "
        "the campaign team."
    ),
    f"{SUG_REVIEW_DUE_GOAL}.reason": _(
        "It's been a while since you looked at «%(goal)s». A two-minute review "
        "keeps the plan honest — reprioritise, adjust the date, or archive it "
        "guilt-free."
    ),
    f"{SUG_REVIEW_DUE_PROFILE}.reason": _(
        "It's been a while since you reviewed your preferences. A quick look keeps "
        "your suggestions relevant — update what you enjoy, your pace, or your "
        "budget any time."
    ),
}

# How a list of nested refs (the blocked_prereq blockers) is joined once each is resolved.
_JOIN = "; "


def _resolve_value(value):
    """Resolve one interpolation value, recursively rendering nested scaffold refs.

    A ref is ``{"key": …, "params": {…}, "text": "…"}`` — the shape ``progress.blocked_refs``
    persists for each structural blocker, so a suggestion's ``%(blockers)s`` slot localises with
    the reader too instead of embedding the worker's frozen English.
    """
    if isinstance(value, dict) and ("key" in value or "text" in value):
        return text(value.get("text", ""), value.get("key", ""), value.get("params") or {})
    if isinstance(value, list | tuple):
        return _JOIN.join(str(_resolve_value(v)) for v in value)
    return value


def render(key: str, params: dict | None = None) -> str:
    """The scaffold for ``key`` resolved under the **active** locale and interpolated.

    Returns ``""`` when the key is unknown or the params do not satisfy the msgid (a translator
    typo, a stale row from an older deploy) — the caller falls back to the stored English, so this
    never blanks a page. ``str(...)`` is what forces the ``gettext_lazy`` proxy to resolve *now*,
    inside the reader's locale, rather than at write time.
    """
    msgid = SCAFFOLDS.get(key)
    if msgid is None:
        return ""
    values = {k: _resolve_value(v) for k, v in (params or {}).items()}
    try:
        return str(msgid) % values
    except (KeyError, ValueError, TypeError):
        return ""


def english(key: str, **params) -> tuple[str, str, dict]:
    """``(english_prose, key, params)`` for one write site.

    The prose column and the key/params are produced from the **same** msgid here, so the stored
    English and the translated re-render can never drift apart, and the column stays English even
    if the writer ever happens to run under another active locale.
    """
    with translation.override(SOURCE_LANGUAGE):
        prose = render(key, params)
    return prose, key, params


def text(stored: str, key: str, params: dict | None = None) -> str:
    """The reader-locale sentence for a persisted prose field. **Never blank.**

    ``stored`` is the English prose column (the audit record and the legacy-row fallback); ``key``
    is empty for every row written before this seam existed, and for any free-text a human typed.
    """
    if not key:
        return stored
    return render(key, params) or stored
