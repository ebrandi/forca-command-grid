"""Capsuleer Path milestone verification engine (doc 11 §8-§9).

A registry of per-kind checkers, mirroring ``apps.campaigns.metrics.base``: :func:`register`
populates ``CHECKERS`` at import; :func:`check_safely` wraps a checker fail-soft so any exception
degrades to an honest ``unknown`` (logging the *kind* only — never the params, which describe a
pilot's private plan). Checkers are strictly **read-only** over other apps' stores and never call
ESI (brief §2.4): they read the goal's evidence character's pre-loaded data through a
:class:`CheckContext` so a batch sweep does one query per store per character, not per milestone.

Each checker returns a :class:`CheckResult` (met / state / provenance / evidence / structural). The
caller (the reconcile sweep or the import hook) stamps ``last_checked_at`` / ``check_state`` /
``data_source`` on every evaluation and credits per the monotonicity rule (§8.2): ``met`` credits on
``ok``, or on ``stale`` for a monotonic kind (skills only grow, ledgers are append-only, firsts are
durable) — ``ship_owned`` is non-monotonic and never credits on stale asset data. Automation never
un-credits; the evidence snapshot records what was true at credit time.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from core import freshness

from . import messages
from .models import MilestoneKind

logger = logging.getLogger("forca.capsuleer")

_UNSET = object()

# Kinds whose truth is monotonic w.r.t. their (possibly stale) input — safe to credit on stale
# data (doc 11 §8.2). ship_owned is deliberately absent: a hull can be sold or lost.
MONOTONIC_KINDS = frozenset({
    MilestoneKind.SKILL_TARGET,
    MilestoneKind.DOCTRINE_READY,
    MilestoneKind.CONTRIBUTION,
    MilestoneKind.COMBAT_FIRST,
})


@dataclass
class CheckResult:
    """The outcome of one milestone check (doc 11 §8 return contract).

    ``data_source`` is the English provenance sentence that is persisted (the audit record and the
    fallback). ``data_source_key`` + ``data_source_params`` are the *same* sentence as a message
    scaffold key and its raw params (``messages.py``): the sweep runs in a Celery worker with no
    locale, so only the key/params can be re-rendered later under a *reader's* language. Both are
    stamped together; keeping them in one dataclass is what keeps them from drifting apart.
    """

    met: bool | None            # None = cannot evaluate
    state: str                  # "ok" | "unknown" | "stale"
    data_source: str            # human provenance (English — persisted verbatim)
    evidence: dict | None = None    # §9.6 shape, present only when met is True
    structural: bool = False    # True when the unknown is permanent (feeds the blocked flag)
    data_source_key: str = ""   # messages.SCAFFOLDS key for ``data_source``
    data_source_params: dict = field(default_factory=dict)  # JSON-safe only — never a lazy proxy


def _result(met, state, key, *, evidence=None, structural=False, **params) -> CheckResult:
    """A :class:`CheckResult` whose English prose and scaffold key/params come from one msgid.

    Never wrap the prose in ``gettext``/``gettext_lazy`` instead: this runs in a worker, the proxy
    would be coerced to ``str`` on ``.save()`` and the row would freeze in the writer's locale.
    """
    prose, key, params = messages.english(key, **params)
    return CheckResult(met, state, prose, evidence, structural,
                       data_source_key=key, data_source_params=params)


CHECKERS: dict[str, callable] = {}


def register(kind: str):
    """Register a per-kind checker in ``CHECKERS`` (one per auto-verifiable kind)."""

    def decorator(func):
        CHECKERS[kind] = func
        return func

    return decorator


def should_credit(kind: str, result: CheckResult) -> bool:
    """Whether a ``met`` result may credit given its freshness state (doc 11 §8.2)."""
    if not result.met:
        return False
    if result.state == "ok":
        return True
    return result.state == "stale" and kind in MONOTONIC_KINDS


def check_safely(milestone, ctx) -> CheckResult:
    """Run a milestone's checker, degrading any failure to an honest ``unknown`` (never the params)."""
    checker = CHECKERS.get(milestone.kind)
    if checker is None:
        # ``kind`` is an identifier (a MilestoneKind value), never translated — only the sentence
        # around it is.
        return _result(None, "unknown", messages.SRC_NO_CHECKER, structural=True,
                       kind=str(milestone.kind))
    try:
        return checker(milestone, ctx)
    except Exception:  # noqa: BLE001 — a checker failure must never break the sweep
        logger.exception("capsuleer check failed kind=%s milestone=%s",
                         milestone.kind, milestone.pk)
        return _result(None, "unknown", messages.SRC_CHECK_UNAVAILABLE)


# --------------------------------------------------------------------------- #
#  Per-character read context (one query per store per character, doc 11 §8)
# --------------------------------------------------------------------------- #
class CheckContext:
    """Pre-loaded per-character evidence, lazily fetched once and reused across milestones.

    ``snapshot`` is the character's latest ``CharacterSkillSnapshot`` (or ``None``); the asset
    ownership map, asset-mirror freshness and fitted-hull set are loaded on first access. A ``None``
    character (detached alt) degrades every character-scoped check to ``unknown`` (doc 07 §2).
    """

    def __init__(self, character, snapshot=_UNSET):
        self.character = character
        if snapshot is _UNSET:
            snapshot = (
                character.skill_snapshots.filter(is_latest=True).first() if character else None
            )
        self.snapshot = snapshot
        self._owned = _UNSET
        self._assets_as_of = _UNSET
        self._fitted = _UNSET

    def _load_assets(self) -> None:
        from apps.doctrines.prep import owned_by_type
        from apps.stockpile.models import Asset

        if self.character is None:
            self._owned, self._assets_as_of = {}, None
            return
        cid = self.character.character_id
        self._owned = owned_by_type([cid])
        latest = (
            Asset.objects.filter(owner_type=Asset.Owner.CHARACTER, owner_id=cid)
            .order_by("-as_of").values_list("as_of", flat=True).first()
        )
        self._assets_as_of = latest

    @property
    def owned(self) -> dict[int, int]:
        if self._owned is _UNSET:
            self._load_assets()
        return self._owned

    @property
    def assets_as_of(self):
        if self._assets_as_of is _UNSET:
            self._load_assets()
        return self._assets_as_of

    @property
    def fitted_type_ids(self) -> set[int]:
        if self._fitted is _UNSET:
            if self.character is None:
                self._fitted = set()
            else:
                from apps.characters.models import CharacterFittedShip

                self._fitted = set(
                    CharacterFittedShip.objects.filter(
                        character=self.character, is_latest=True
                    ).values_list("ship_type_id", flat=True)
                )
        return self._fitted


def context_for(character, snapshot=_UNSET) -> CheckContext:
    """Build a per-character check context (pass a fresh ``snapshot`` to thread the import hook's)."""
    return CheckContext(character, snapshot)


def _type_names(type_ids) -> dict[int, str]:
    from apps.sde.models import SdeType

    return dict(
        SdeType.objects.filter(type_id__in=list(type_ids)).values_list("type_id", "name")
    )


# --------------------------------------------------------------------------- #
#  Checkers (doc 11 §9)
# --------------------------------------------------------------------------- #
@register(MilestoneKind.SKILL_TARGET)
def _check_skill_target(milestone, ctx) -> CheckResult:
    snap = ctx.snapshot
    if snap is None:
        return _result(None, "unknown", messages.SRC_NO_SKILL_DATA)
    entries = milestone.params.get("skills", [])
    names = _type_names(int(e["type_id"]) for e in entries)
    rows, met = [], True
    for entry in entries:
        tid, need = int(entry["type_id"]), int(entry["level"])
        have = snap.trained_level(tid)
        rows.append({"type_id": tid, "name": names.get(tid, str(tid)), "required": need,
                     "trained": have})
        if have < need:
            met = False
    state = "stale" if freshness.is_stale(snap.as_of, "skills") else "ok"
    evidence = None
    if met:
        evidence = {"kind": "skill_target", "as_of": snap.as_of.isoformat(),
                    "snapshot_id": snap.pk, "skills": rows}
    return _result(met, state, messages.SRC_SKILLS_SNAPSHOT, evidence=evidence,
                   as_of=f"{snap.as_of:%Y-%m-%d %H:%M}")


@register(MilestoneKind.DOCTRINE_READY)
def _check_doctrine_ready(milestone, ctx) -> CheckResult:
    from apps.doctrines.models import Doctrine
    from apps.doctrines.services import character_readiness

    params = milestone.params
    if params.get("unresolved"):
        return _result(None, "unknown", messages.SRC_NO_DOCTRINE, structural=True)
    doctrine = (
        Doctrine.objects.filter(id=params.get("doctrine_id"), status=Doctrine.Status.ACTIVE)
        .prefetch_related("fits__skill_requirements").first()
    )
    if doctrine is None:
        return _result(None, "unknown", messages.SRC_NO_DOCTRINE, structural=True)
    snap = ctx.snapshot
    if snap is None:
        return _result(None, "unknown", messages.SRC_NO_SKILL_DATA)

    tier = params.get("tier", "viable")
    fit_id = params.get("fit_id")
    rank = {"unknown": 0, "not_ready": 1, "viable": 2, "optimal": 3}
    best = None
    for fit in doctrine.fits.all():
        if fit_id and fit.id != fit_id:
            continue
        readiness = character_readiness(ctx.character, fit, snapshot=snap)
        if best is None or rank[readiness.status] > rank[best.status]:
            best = readiness
    if best is None or best.status == "unknown":
        # The doctrine *name* is EVE/corp data and stays raw; the sentence around it is translated.
        return _result(None, "unknown", messages.SRC_NO_DERIVED_REQS, doctrine=doctrine.name)
    met = best.status == "optimal" or (tier == "viable" and best.status == "viable")
    state = "stale" if freshness.is_stale(snap.as_of, "skills") else "ok"
    evidence = None
    if met:
        evidence = {"kind": "doctrine_ready", "doctrine_id": doctrine.id,
                    "doctrine_name": doctrine.name, "fit_id": best.fit_id,
                    "fit_name": best.fit_name, "status": best.status,
                    "tier_required": tier, "as_of": snap.as_of.isoformat()}
    return _result(met, state, messages.SRC_DOCTRINE_READINESS, evidence=evidence,
                   as_of=f"{snap.as_of:%Y-%m-%d %H:%M}")


@register(MilestoneKind.CONTRIBUTION)
def _check_contribution(milestone, ctx) -> CheckResult:
    from apps.pilots.models import ContributionEvent

    params = milestone.params
    kinds = params.get("kinds", [])
    baseline = int(params.get("baseline_count", 0))
    need = int(params.get("count", 1))
    qs = ContributionEvent.objects.filter(user_id=milestone.goal.user_id, kind__in=kinds)
    total = qs.count()
    met = (total - baseline) >= need
    evidence = None
    if met:
        recent = list(qs.order_by("-occurred_at")[:5])
        evidence = {
            "kind": "contribution", "kinds": list(kinds), "baseline_count": baseline,
            "count_at_credit": total, "required": need,
            "recent_refs": [
                {"ref_type": r.ref_type, "ref_id": r.ref_id,
                 "occurred_at": r.occurred_at.isoformat()} for r in recent
            ],
        }
    # The ledger is durable append-only data — always ``ok``, never stale.
    return _result(met, "ok", messages.SRC_CONTRIBUTION_LEDGER, evidence=evidence)


@register(MilestoneKind.COMBAT_FIRST)
def _check_combat_first(milestone, ctx) -> CheckResult:
    from apps.killboard.models import PilotMilestone

    if ctx.character is None:
        return _result(None, "unknown", messages.SRC_CHARACTER_UNLINKED, structural=True)
    key = milestone.params.get("milestone_key")
    row = PilotMilestone.objects.filter(
        character_id=ctx.character.character_id, kind=key
    ).first()
    if row is None:
        # Durable store: an absent first is an honest "not yet", not "unknown".
        return _result(False, "ok", messages.SRC_KILLBOARD_FIRSTS)
    evidence = {"kind": "combat_first", "milestone_key": key,
                "achieved_at": row.achieved_at.isoformat(), "killmail_id": row.killmail_id}
    return _result(True, "ok", messages.SRC_KILLBOARD_FIRSTS, evidence=evidence)


@register(MilestoneKind.SHIP_OWNED)
def _check_ship_owned(milestone, ctx) -> CheckResult:
    if ctx.character is None:
        return _result(None, "unknown", messages.SRC_CHARACTER_UNLINKED, structural=True)
    type_ids = [int(t) for t in milestone.params.get("type_ids", [])]
    as_of = ctx.assets_as_of
    if as_of is None:
        # No asset mirror for this character = no scope opt-in; never "not owned" (doc 07 §6.3).
        return _result(None, "unknown", messages.SRC_ASSETS_NOT_SHARED)
    owned = ctx.owned
    matched, qty = None, 0
    for tid in type_ids:
        q = owned.get(tid, 0)
        if q >= 1:
            matched, qty = tid, q
            break
    require_fitted = bool(milestone.params.get("require_fitted", False))
    fitted = matched is not None and matched in ctx.fitted_type_ids
    met = matched is not None and (fitted if require_fitted else True)
    state = "stale" if freshness.is_stale(as_of, "assets") else "ok"
    evidence = None
    if met:
        names = _type_names([matched])
        evidence = {"kind": "ship_owned", "ship_type_id": matched,
                    "ship_name": names.get(matched, str(matched)), "quantity": qty,
                    "fitted": bool(fitted), "character_id": ctx.character.character_id,
                    "as_of": as_of.isoformat()}
    return _result(met, state, messages.SRC_ASSET_MIRROR, evidence=evidence,
                   as_of=f"{as_of:%Y-%m-%d %H:%M}")
