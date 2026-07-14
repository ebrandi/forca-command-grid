"""Code-defined, gettext-wrapped scaffolds for the readiness prose that is PERSISTED.

Seam B. A ``ReadinessFinding`` title / task title / detail, a ``ReadinessAlert`` summary and
the risk lines archived in ``ExecutiveReport.body`` are all written by the ``readiness.warm`` /
``readiness.snapshot`` / ``readiness.alerts`` / ``readiness.weekly_report`` Celery beats. A
Celery worker has **no request and no reader**, so it has no locale: whatever locale is active
when it saves is frozen into the row and every reader — in every language — sees that forever.

Which is why ``gettext``/``gettext_lazy`` at the *write* site is a trap here, not a fix:

* Django coerces a lazy proxy to ``str`` on ``.save()``. Wrapping the sentence would only
  change *which* single language gets frozen (the worker's: English). It would sail through
  ``makemessages`` and translate nothing for the reader.
* A lazy proxy stored into a ``JSONField`` is a hard ``TypeError`` at save time.

So the sentence is never persisted in a translated form. What is persisted is a **key** into
this registry plus a **params** dict of plain JSON-safe values (ints / strings — never a proxy,
never a pre-translated word). The reader's locale is applied at *read* time by
:func:`render_text`, which resolves the scaffold under the currently-active catalogue and
interpolates the raw params.

Contract:

* Every msgid is a real literal ``_("…")`` so ``xgettext`` can see it. ``_(f"…")`` is a silent
  no-op and must never appear.
* Placeholders are **named** ``%(name)s`` — positional/``{}``/f-string interpolation is banned
  because translators reorder clauses.
* The interpolated values stay RAW. EVE game data (hull / system / skill names) and
  corp-authored content (a ``StrategicRoleTarget.label``, a doctrine name, a dimension label)
  pass through untranslated by policy; a sentence that merely *contains* one is still prose and
  is marked here.
* The English prose column is still written at the write site, from
  :func:`english_text` — which resolves the very same msgid with translations *deactivated*, so
  the stored English is the msgid verbatim no matter what locale the writer happened to be in.
  A snapshot triggered from a German officer's browser therefore still stores English.
* Nothing is backfilled. A legacy row (written before this landed) simply carries no key and
  :func:`render_text` returns its stored English verbatim. The render can never blank.
"""
from __future__ import annotations

from django.utils.translation import gettext_lazy as _

# key -> gettext msgid with named %(param)s placeholders.
#
# Namespaced by dimension. A key is an identifier: it is persisted, compared and looked up, so
# it is NEVER translated and never reworded — changing a key orphans every row that carries it
# (which degrades safely to the stored English, but loses the translation).
SCAFFOLDS: dict[str, str] = {
    # --- activity -----------------------------------------------------------
    "activity.low_active_ratio": _(
        "Only %(active)s/%(total)s members active in the last 30 days"
    ),
    "activity.reengage_task": _("Re-engage dormant members"),
    # --- doctrine (shared coverage pass + leadership's per-doctrine config) --
    "doctrine.coverage_gap": _("%(doctrine)s: %(ready)s/%(known)s can fly"),
    "doctrine.train_task": _("Train pilots into %(doctrine)s"),
    "doctrine.mandatory_under_crewed": _("Mandatory doctrine under-crewed — %(doctrine)s"),
    "doctrine.crew_mandatory_task": _("Crew the mandatory %(doctrine)s doctrine"),
    "doctrine.past_retirement": _("Doctrine past its retirement date — %(doctrine)s"),
    "doctrine.retire_task": _("Retire or replace %(doctrine)s"),
    # --- stock --------------------------------------------------------------
    "stock.shortfall": _("%(item)s: %(deficit)s short at %(stockpile)s"),
    "stock.restock_task": _("Restock %(item)s (need %(deficit)s)"),
    # --- fleet_comp ---------------------------------------------------------
    "fleet_comp.role_short": _("%(role)s short by %(short)s"),
    "fleet_comp.train_role_task": _("Train pilots into %(role)s"),
    # --- infrastructure -----------------------------------------------------
    # Two keys rather than one: the unnamed variant folds the "A structure" wording into the
    # msgid instead of freezing that English word inside a param (a param is never translated).
    "infrastructure.fuel_low": _("%(structure)s has %(days)s days of fuel left"),
    "infrastructure.fuel_low_unnamed": _("A structure has %(days)s days of fuel left"),
    "infrastructure.refuel_task": _("Refuel %(structure)s"),
    "infrastructure.refuel_task_unnamed": _("Refuel structure"),
    # --- financial ----------------------------------------------------------
    "financial.runway_critical": _(
        "Corp runway is %(runway)s months — below 1 month of cover"
    ),
    "financial.runway_task": _("Shore up the corp wallet — runway critical"),
    "financial.over_burn": _("Monthly burn (%(burn)sB) is over target (%(target)sB)"),
    "financial.burn_task": _("Review corp expenses — over burn target"),
    # --- recruitment --------------------------------------------------------
    "recruitment.headcount_below_target": _(
        "Active headcount %(active)s below target %(target)s"
    ),
    "recruitment.headcount_task": _("Recruit to grow active headcount"),
    # --- staging ------------------------------------------------------------
    "staging.hulls_off_staging": _(
        "Only %(pct)s%% of doctrine hulls are at %(system)s"
    ),
    "staging.consolidate_task": _("Consolidate doctrine hulls at staging"),
    # --- srp ----------------------------------------------------------------
    "srp.pending_backlog": _("%(backlog)s SRP claims pending (max %(max)s)"),
    "srp.clear_backlog_task": _("Clear the SRP backlog (%(backlog)s pending)"),
    "srp.oldest_claim": _("Oldest SRP claim is %(age)sd old (max %(max)sd)"),
    "srp.oldest_claim_task": _("Decide the oldest pending SRP claim"),
    # --- support (fleet-support skill bench) --------------------------------
    "support.thin_bench": _("Thin %(skill)s bench — %(trained)s/%(known)s at L%(level)s"),
    "support.grow_bench_task": _("Grow the %(skill)s (L%(level)s) bench"),
    # --- leadership ---------------------------------------------------------
    "leadership.officers_unfilled": _(
        "%(count)s officer role(s) unfilled: %(roles)s"
    ),
    "leadership.assign_owners_task": _(
        "Assign owners to unfilled officer responsibilities"
    ),
    "leadership.role_bench": _("%(role)s bench %(qualified)s/%(desired)s"),
    "leadership.grow_bench_task": _("Grow the %(role)s bench"),
    # --- strategic ----------------------------------------------------------
    "strategic.mandatory_ship_coverage": _(
        "Only %(pct)s%% mandatory-ship coverage across the corp"
    ),
    "strategic.mandatory_ship_task": _("Get pilots into their mandatory ships"),
    "strategic.role_bench": _("%(role)s bench %(qualified)s/%(desired)s"),
    "strategic.grow_bench_task": _("Grow the %(role)s bench"),
    # --- forecast -----------------------------------------------------------
    "forecast.breach_title": _(
        "Forecast: %(dimension)s may breach red in ~%(days)sd"
    ),
    "forecast.breach_detail": _(
        "%(label)s trending toward its red band in ~%(days)sd"
    ),
}


def render_text(key: str, params: dict | None, fallback: str) -> str:
    """The sentence for ``key`` under the READER's active locale — or ``fallback``.

    This is the read side of the seam. ``str(...)`` forces the ``gettext_lazy`` proxy to resolve
    *now*, against whatever catalogue is active for the reader (a request's language, or a
    ``translation.override(lang)`` the notification dispatcher entered).

    Never blanks and never raises: an unknown key, a params dict missing a placeholder, or any
    interpolation error all fall back to the stored English prose, which every row has.
    """
    if not key:
        return fallback
    scaffold = SCAFFOLDS.get(key)
    if scaffold is None:
        return fallback  # key from a newer/older deploy — degrade to the stored English
    try:
        return str(scaffold) % (params or {})
    except (KeyError, TypeError, ValueError):
        return fallback


def english_text(key: str, params: dict | None = None) -> str:
    """The msgid for ``key`` interpolated with ``params``, in **source English**.

    ``translation.override(None)`` deactivates translation entirely, so ``str(proxy)`` returns the
    msgid verbatim rather than the active catalogue's msgstr. That is what makes the persisted
    prose column locale-independent: a snapshot warmed from a German officer's request stores the
    same English bytes as the Celery beat does.
    """
    from django.utils import translation

    scaffold = SCAFFOLDS.get(key)
    if scaffold is None:
        return ""
    with translation.override(None):
        return str(scaffold) % (params or {})
