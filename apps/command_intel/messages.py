"""Code-defined, gettext-wrapped message scaffolds for the persisted Command Intelligence prose.

**Seam B.** Every sentence below is *written into the database* by the CI Celery jobs
(``command_intel.generate_report``, ``command_intel.analyse_battle``, the pilot-directive
refresh) and read back later by *other* people — an officer on the briefing, a member on
their quest log — each under their own locale.

Wrapping the write site in ``gettext``/``gettext_lazy`` cannot work here, and is in fact a trap:

* Django coerces a lazy proxy to ``str`` on ``.save()``, so the row freezes in whatever locale the
  *writer* had. A Celery worker has no request and no user, so that locale is English — a naive
  ``_()`` at the write site passes ``makemessages`` and translates precisely nothing, forever.
* A lazy proxy placed inside a ``JSONField`` (``IntelligenceReport.body``, ``BattleAnalysis.body``)
  is a hard ``TypeError`` at save time.

So the write site persists a **key + params** (plain, JSON-safe values) *alongside* the English
prose column, and the read site re-resolves the scaffold under the **reader's** locale
(:func:`render`). The prose column stays the English fallback and the audit record: a legacy row
written before this change carries no key and simply renders its stored English — never blank.

Contract (mirrors ``apps/mentorship/messages.py``, the Seam B precedent):

* Every value in :data:`SCAFFOLDS` is a **literal** ``_("…")`` call, so ``xgettext`` can see it.
  A ``_(f"…")`` or ``_(variable)`` is a silent no-op and must never appear.
* Placeholders are named ``%(param)s`` — never f-strings, never positional.
* The interpolated values stay **raw**: EVE game data (doctrine, hull, structure, solar-system and
  pilot names), numbers, and code identifiers (``hulls_in_stock``, ``doctrine:vanguard``, a severity
  code) are substituted verbatim and are never themselves translated. Numbers that the English
  prose formats (``{x:g}``, ``{x:,.0f}``, ``{x:+g}``) are **pre-formatted to a string at the write
  site**, so the msgid needs no format spec and English output is byte-identical.
* The msgid text is the *exact* English written to the prose column (:func:`english` resolves it
  with translation deactivated), so the column and the msgid are one source of truth and cannot
  drift. ``tests/test_command_intel_i18n.py`` pins that.
* Keys are identifiers: they are persisted, compared and looked up. Never translate a key. Never
  translate a value that is compared/filtered — ``CourseOfAction.slug`` is derived from the
  *English* objective, and ``apps/pilots/briefing.py`` dedupes on the *English* directive title.

Composition
-----------
Some sentences embed another scaffolded sentence (a COA's risk quotes the constraint's label; the
degraded briefing body quotes each constraint's detail). A param value may therefore itself be a
**ref** — ``{"_msg": key, "_params": {…}, "_en": "the English"}``, built by :func:`ref` — which is
resolved recursively under the reader's locale, with ``_en`` as its never-blank fallback.

The two ``JSONField`` bodies use the same refs: the write site builds one *document template* whose
prose leaves are refs, persists it in ``body_params`` (JSON-safe: no proxies), and derives the
English ``body`` column from it with :func:`english_doc`. The read site re-renders the identical
structure under the reader's locale with :func:`render_doc`.
"""
from __future__ import annotations

from django.utils.translation import gettext_lazy as _
from django.utils.translation import override

# Keyed by the string persisted in ``<field>_key``. Params are the ``%(name)s`` slots.
SCAFFOLDS: dict[str, str] = {
    # -- OperationalConstraint.label (written by report._persist_constraints) -----------
    # %(doctrine)s is EVE/corp game data (a doctrine name) — raw, never translated.
    "constraint.doctrine_stock.label": _("%(doctrine)s stock"),
    "constraint.fleet_size.label": _("%(doctrine)s fleet size"),
    "constraint.fuel_runway.label": _("Structure Fuel Runway"),
    "constraint.isk_runway.label": _("ISK Runway"),
    "constraint.srp_solvency.label": _("SRP Solvency"),

    # -- OperationalConstraint.detail --------------------------------------------------
    # %(missing)s / %(factor)s are LimitInput identifiers ("hulls_in_stock") — raw.
    "constraint.doctrine_stock.detail": _(
        "%(hulls)s %(doctrine)s hulls staged crew %(fleets)s full fleet(s) "
        "(%(per_fleet)s pilots each); headroom %(headroom)s vs %(demand)s-fleet demand."
    ),
    "constraint.doctrine_stock.detail.unknown": _(
        "Cannot compute staged %(doctrine)s fleets: %(missing)s missing from the doctrine slice."
    ),
    "constraint.fleet_size.detail": _(
        "Max %(doctrine)s fleet = %(binding)s pilots, limited by %(factor)s "
        "(%(flyable)s qualified, %(hulls)s hulls staged); headroom %(headroom)s vs "
        "%(demand)s-pilot demand."
    ),
    "constraint.fleet_size.detail.no_demand": _(
        "Max %(doctrine)s fleet = %(binding)s pilots, limited by %(factor)s "
        "(%(flyable)s qualified, %(hulls)s hulls staged) (no demand target set)."
    ),
    "constraint.fleet_size.detail.unknown": _(
        "Cannot compute max %(doctrine)s fleet: %(missing)s missing from the doctrine slice."
    ),
    # %(structure)s is an EVE structure name — raw, never translated.
    "constraint.fuel_runway.detail": _(
        "%(structure)s runs out of fuel in %(days)s day(s) — the soonest of %(count)s "
        "low-fuel structure(s); watch margin is %(watch)s days."
    ),
    "constraint.fuel_runway.detail.no_structures": _("No structures are running low on fuel."),
    "constraint.fuel_runway.detail.unknown": _(
        "Cannot compute fuel runway: infrastructure slice has no structure data."
    ),
    "constraint.fuel_runway.detail.unknown_metric": _(
        "Cannot compute fuel runway: no structure reported fuel_days_left."
    ),
    "constraint.isk_runway.detail": _(
        "%(days)s days of runway at the current burn (%(balance)s balance ÷ %(burn)s/day "
        "net outflow); target is %(target)s days."
    ),
    "constraint.isk_runway.detail.positive": _(
        "30-day net cashflow is positive — no ISK runway constraint."
    ),
    "constraint.isk_runway.detail.unknown": _(
        "Cannot compute ISK runway: wallet balance or 30-day net missing."
    ),
    "constraint.srp_solvency.detail": _(
        "SRP can sustain %(months)s month(s) of payouts: (%(budget)s budget − "
        "%(liability)s open liability) ÷ %(spent)s period spend; target is %(target)s month(s)."
    ),
    "constraint.srp_solvency.detail.unknown": _(
        "Cannot compute SRP solvency: budget, open liability or period spend missing."
    ),

    # -- CourseOfAction.objective (the deterministic/templated ready_degraded path) -----
    # An LLM-authored COA has NO key: model free text is stored and rendered verbatim.
    # %(label)s is a capability identifier ("doctrine:vanguard") or a ref to the constraint label.
    "coa.objective.stage_hulls": _("Stage %(n)s more %(label)s"),
    "coa.objective.train_pilots": _("Train %(n)s more pilots for %(label)s"),
    "coa.objective.recruit_logi": _("Recruit/train logistics pilots for %(label)s"),
    "coa.objective.refuel": _("Refuel low-fuel structures"),
    "coa.objective.top_up_srp": _("Top up the SRP budget"),
    "coa.objective.raise_income": _("Raise corp income or cut burn"),
    "coa.objective.relieve": _("Relieve: %(label)s"),
    "coa.risk_if_ignored": _("%(label)s stays binding at %(metric)s %(unit)s."),

    # -- PilotDirective.title / .detail (the member's quest log) ------------------------
    # %(doctrine)s / %(hull)s are EVE game data — raw, never translated.
    "pilot.directive.train.title": _("Train into %(doctrine)s"),
    "pilot.directive.train.detail": _(
        "The corp's binding constraint is %(constraint)s — only %(metric)s pilots can field it. "
        "You're about %(eta)s of training from flying it; training in directly relieves the "
        "corp's shortage and lifts what we can put on grid."
    ),
    "pilot.directive.stage_hull.title": _("Stage a %(hull)s hull"),
    "pilot.directive.stage_hull.detail": _(
        "%(constraint)s is capped by staged hulls. Buying a %(hull)s and bringing it to the home "
        "system adds directly to what the corp can field on short notice."
    ),
    "pilot.directive.fallback_train.detail": _(
        "You're about %(eta)s of training from flying %(doctrine)s, one of the corp's doctrines. "
        "Closing this makes you — and the corp — more ready."
    ),
    "pilot.directive.join_op.title": _("Fly a fleet this week"),
    "pilot.directive.join_op.detail": _(
        "You're current on the corp's doctrines — keep your edge by joining an op."
    ),
    # Training ETA, embedded in the sentences above (a locale may want its own unit suffix).
    "pilot.eta.days": _("%(n)sd"),
    "pilot.eta.hours": _("%(n)sh"),
    "pilot.eta.under_hour": _("under an hour"),

    # -- IntelligenceReport.title / .summary / .body (deterministic + degraded paths) ---
    # An LLM-authored briefing body/summary has NO key and renders verbatim.
    "report.title": _("Command Intelligence Report — %(date)s"),
    "report.summary.degraded": _(
        "%(crit)s critical and %(high)s high operational constraint(s) identified. "
        "Narrative unavailable (AI offline) — deterministic operational picture below."
    ),
    "report.summary.degraded.readiness": _(
        "%(crit)s critical and %(high)s high operational constraint(s) identified. "
        "Overall readiness index %(readiness)s. "
        "Narrative unavailable (AI offline) — deterministic operational picture below."
    ),
    "report.body.posture_statement": _("Deterministic constraint picture (no AI narrative)."),
    # %(severity)s is a Severity code — an identifier, raw by contract.
    "report.body.highlight": _("%(label)s: %(metric)s %(unit)s (%(severity)s)"),
    "report.body.not_assessed": _("%(label)s — %(detail)s"),
    "report.body.strategic_risk": _("%(label)s is binding (%(metric)s %(unit)s)"),
    "report.body.forecast": _(
        "Trend forecasting requires snapshot history (accumulates over time)."
    ),
    "report.body.annex.constraint_evidence": _("Constraint evidence"),

    # -- BattleAnalysis.title / .body (the after-action review) -------------------------
    # %(battle)s is the killboard battle title, %(systems)s are EVE solar systems — raw.
    "battle.title": _("After-action — %(battle)s"),
    "battle.title.default": _("Battle"),
    "battle.systems.default": _("the field"),
    "battle.outcome.favorable": _("Favorable"),
    "battle.outcome.unfavorable": _("Unfavorable"),
    "battle.outcome.even": _("Even"),
    "battle.summary.degraded": _(
        "%(outcome)s engagement in %(systems)s: lost %(our_losses)s ships (%(isk_lost)s ISK), "
        "killed %(our_kills)s (%(isk_destroyed)s ISK); ISK swing %(isk_swing)s. "
        "Narrative unavailable (AI offline) — facts below."
    ),
    "battle.what_happened.degraded": _(
        "Deterministic facts only (AI narrative unavailable). See the panels below."
    ),
    "battle.wrong.off_doctrine": _(
        "%(off)s of %(total)s doctrine losses were off-doctrine."
    ),
    "battle.wrong.logi_lost": _("%(count)s logistics ship(s) lost."),
}


# --------------------------------------------------------------------------- #
#  Refs — a scaffolded sentence used as a *param* of another scaffolded sentence.
# --------------------------------------------------------------------------- #
_MSG = "_msg"
_PARAMS = "_params"
_EN = "_en"


def ref(key: str, params: dict | None = None) -> dict:
    """A JSON-safe reference to scaffold ``key`` for use as a param value or a document leaf.

    Carries the resolved English (``_en``) so a retired/unknown key can still never blank a stored
    sentence — the ref is its own audit record.
    """
    params = params or {}
    return {_MSG: key, _PARAMS: params, _EN: english(key, params)}


def is_ref(value) -> bool:
    return isinstance(value, dict) and _MSG in value


def _resolve_value(value, *, translate: bool):
    if is_ref(value):
        key = value.get(_MSG) or ""
        params = value.get(_PARAMS) or {}
        fallback = value.get(_EN) or ""
        if translate:
            return render(key, params, fallback)
        return english(key, params) or fallback
    if isinstance(value, list):
        return [_resolve_value(v, translate=translate) for v in value]
    return value


def _resolve_params(params: dict | None, *, translate: bool) -> dict:
    if not params:
        return {}
    return {k: _resolve_value(v, translate=translate) for k, v in params.items()}


def _interpolate(text: str, params: dict | None) -> str:
    if not params:
        return text
    try:
        return text % params
    except (KeyError, TypeError, ValueError):
        # A translator who mangled a %(slot)s must never blank or crash a stored sentence.
        return text


# --------------------------------------------------------------------------- #
#  The two ends of the seam.
# --------------------------------------------------------------------------- #
def english(key: str, params: dict | None = None) -> str:
    """The **English** sentence for ``key`` — what gets written to the prose column.

    ``override(None)`` deactivates translation entirely, so ``str()`` yields the msgid verbatim no
    matter which locale the writer happens to be in (a Celery worker has none; a web request may be
    German). This is what makes the stored English column and the msgid a single source of truth:
    they cannot drift, because the column *is* the msgid.
    """
    scaffold = SCAFFOLDS.get(key)
    if scaffold is None:
        return ""
    with override(None):
        return _interpolate(str(scaffold), _resolve_params(params, translate=False))


def render(key: str, params: dict | None, fallback: str) -> str:
    """The sentence for ``key`` under the **reader's** active locale.

    Falls back to the stored English prose whenever there is no key (a legacy row, or an
    LLM-authored sentence that is free text and must render verbatim), the key is unknown (a
    retired scaffold), or the resolved text is empty. Never blanks, never raises.
    """
    if not key:
        return fallback
    scaffold = SCAFFOLDS.get(key)
    if scaffold is None:
        return fallback
    # str() forces the gettext_lazy proxy to resolve NOW, under the reader's active catalogue.
    text = _interpolate(str(scaffold), _resolve_params(params, translate=True))
    return text or fallback


# --------------------------------------------------------------------------- #
#  Documents — the JSONField bodies (IntelligenceReport.body, BattleAnalysis.body).
# --------------------------------------------------------------------------- #
def _walk(node, *, translate: bool):
    if is_ref(node):
        return _resolve_value(node, translate=translate)
    if isinstance(node, dict):
        return {k: _walk(v, translate=translate) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk(v, translate=translate) for v in node]
    return node


def english_doc(doc):
    """The English document for a ref-bearing template — what gets written to the JSON column."""
    return _walk(doc, translate=False)


def render_doc(doc, fallback):
    """The document re-rendered under the reader's locale, or ``fallback`` when unrenderable.

    ``fallback`` is the stored English JSON body: a legacy row (no template), an LLM-authored body
    (free text, verbatim by contract) and any malformed template all degrade to it.
    """
    if not doc:
        return fallback
    try:
        return _walk(doc, translate=True)
    except Exception:  # noqa: BLE001 - a read path must never break a briefing
        return fallback
