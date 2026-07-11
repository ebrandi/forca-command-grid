"""Per-kind ``CareerMilestone.params`` validators (doc 07 §6).

One validator per milestone kind, run at authoring time — template instantiation and manual
edit — before a milestone row is written. The rules mirror doc 07 §6 with the two agreed
amendments: ``doctrine_ready`` gains an optional ``tier`` key (``viable``|``optimal``, default
``viable``), and ``practical``'s ``instructions`` key is optional.

Two cross-cutting rules the dispatcher enforces alongside the per-kind schema:

* **Unknown keys are rejected** — a mass-assignment guard (spec §31): a milestone can only carry
  the keys its kind defines.
* **``verification`` and ``kind`` must agree** — ``auto`` is legal only for a kind with a
  registered checker (the auto-capable kinds); ``practical`` and ``manual`` have no checker and
  must be ``self`` / ``mentor`` / ``officer``. ``verification`` is a model field, never a params
  key (doc 07 §6).

These are pure functions with no upstream reads; the Stage 2 verification engine (``verify.py``)
adds the per-kind *checkers* against live stores and reuses this module's validators. The
built-in template ``structure`` documents carry skill/ship references by name and resolve to
type ids at instantiation, so their skill/ship milestones are validated structurally by
``templates_builtin.validate_structure`` rather than by the type-id validators here (doc 07 §7,
doc 13.1).
"""
from __future__ import annotations

from django.core.exceptions import ValidationError

from .models import MilestoneKind, Verification

# Kinds with a registered auto-checker (Stage 2 verify.py). ``auto`` verification is legal only
# for these; the rest must be human-verified.
AUTO_CAPABLE_KINDS = frozenset({
    MilestoneKind.SKILL_TARGET,
    MilestoneKind.DOCTRINE_READY,
    MilestoneKind.SHIP_OWNED,
    MilestoneKind.CONTRIBUTION,
    MilestoneKind.COMBAT_FIRST,
})
HUMAN_ONLY_KINDS = frozenset({MilestoneKind.PRACTICAL, MilestoneKind.MANUAL})

_DOCTRINE_TIERS = frozenset({"viable", "optimal"})
_DOCTRINE_RESOLVERS = frozenset({"category", "key"})


def _require_keys(params: dict, required: set[str], optional: set[str], where: str) -> None:
    """Reject unknown keys and any missing required key (the mass-assignment guard)."""
    if not isinstance(params, dict):
        raise ValidationError(f"{where} params must be an object.")
    allowed = required | optional
    unknown = set(params) - allowed
    if unknown:
        raise ValidationError(f"{where}: unexpected params key(s): {', '.join(sorted(unknown))}.")
    missing = required - set(params)
    if missing:
        raise ValidationError(f"{where}: missing params key(s): {', '.join(sorted(missing))}.")


def _pos_int(value, name: str):
    """A strict positive integer (``bool`` is not an int here)."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError(f"{name} must be a positive integer.")
    return value


def _validate_skill_target(params: dict) -> None:
    _require_keys(params, {"skills"}, set(), "skill_target")
    skills = params["skills"]
    if not isinstance(skills, list) or not (1 <= len(skills) <= 25):
        raise ValidationError("skill_target.skills must be a list of 1-25 entries.")
    for entry in skills:
        if not isinstance(entry, dict) or set(entry) - {"type_id", "level"}:
            raise ValidationError("skill_target.skills entries take only type_id and level.")
        _pos_int(entry.get("type_id"), "skill_target.skills[].type_id")
        level = entry.get("level")
        if isinstance(level, bool) or not isinstance(level, int) or not (1 <= level <= 5):
            raise ValidationError("skill_target.skills[].level must be an int in 1-5.")


def _validate_doctrine_ready(params: dict) -> None:
    # A doctrine-linked template that could not resolve its doctrine at instantiation carries
    # ``unresolved: true`` and no live ``doctrine_id`` (degraded mode, doc 05 §13.3); otherwise a
    # positive ``doctrine_id`` is required.
    optional = {"fit_id", "resolver", "unresolved", "tier"}
    if params.get("unresolved") is True:
        _require_keys(params, set(), optional | {"doctrine_id"}, "doctrine_ready")
    else:
        _require_keys(params, {"doctrine_id"}, optional, "doctrine_ready")
        _pos_int(params["doctrine_id"], "doctrine_ready.doctrine_id")
    if "fit_id" in params:
        _pos_int(params["fit_id"], "doctrine_ready.fit_id")
    if params.get("resolver") is not None and params["resolver"] not in _DOCTRINE_RESOLVERS:
        raise ValidationError("doctrine_ready.resolver must be 'category' or 'key'.")
    if "unresolved" in params and not isinstance(params["unresolved"], bool):
        raise ValidationError("doctrine_ready.unresolved must be a boolean.")
    # Amendment: optional tier, viable|optimal, default viable (applied by the checker).
    if params.get("tier") is not None and params["tier"] not in _DOCTRINE_TIERS:
        raise ValidationError("doctrine_ready.tier must be 'viable' or 'optimal'.")


def _validate_ship_owned(params: dict) -> None:
    _require_keys(params, {"type_ids"}, {"require_fitted"}, "ship_owned")
    type_ids = params["type_ids"]
    if not isinstance(type_ids, list) or len(type_ids) < 1:
        raise ValidationError("ship_owned.type_ids must be a non-empty list.")
    for tid in type_ids:
        _pos_int(tid, "ship_owned.type_ids[]")
    if "require_fitted" in params and not isinstance(params["require_fitted"], bool):
        raise ValidationError("ship_owned.require_fitted must be a boolean.")


def _validate_contribution(params: dict) -> None:
    # baseline_count is system-stamped at goal activation, never author-supplied — reject it.
    if "baseline_count" in params:
        raise ValidationError("contribution.baseline_count is system-managed, not author-supplied.")
    _require_keys(params, {"kinds"}, {"count"}, "contribution")
    kinds = params["kinds"]
    if not isinstance(kinds, list) or not kinds or not all(isinstance(k, str) and k for k in kinds):
        raise ValidationError("contribution.kinds must be a non-empty list of strings.")
    if "count" in params:
        _pos_int(params["count"], "contribution.count")


def _validate_combat_first(params: dict) -> None:
    _require_keys(params, {"milestone_key"}, set(), "combat_first")
    key = params["milestone_key"]
    if not isinstance(key, str) or not key.strip():
        raise ValidationError("combat_first.milestone_key must be a non-empty string.")


def _validate_practical(params: dict) -> None:
    # Amendment: instructions is optional.
    _require_keys(params, set(), {"instructions", "evidence_hint", "link"}, "practical")
    if "instructions" in params:
        val = params["instructions"]
        if not isinstance(val, str) or len(val) > 500:
            raise ValidationError("practical.instructions must be a string of at most 500 chars.")
    if "evidence_hint" in params:
        val = params["evidence_hint"]
        if not isinstance(val, str) or len(val) > 200:
            raise ValidationError("practical.evidence_hint must be a string of at most 200 chars.")
    if "link" in params:
        val = params["link"]
        if not isinstance(val, str) or len(val) > 200:
            raise ValidationError("practical.link must be a string of at most 200 chars.")


def _validate_manual(params: dict) -> None:
    _require_keys(params, set(), set(), "manual")


_VALIDATORS = {
    MilestoneKind.SKILL_TARGET: _validate_skill_target,
    MilestoneKind.DOCTRINE_READY: _validate_doctrine_ready,
    MilestoneKind.SHIP_OWNED: _validate_ship_owned,
    MilestoneKind.CONTRIBUTION: _validate_contribution,
    MilestoneKind.COMBAT_FIRST: _validate_combat_first,
    MilestoneKind.PRACTICAL: _validate_practical,
    MilestoneKind.MANUAL: _validate_manual,
}


def validate_verification(kind: str, verification: str) -> None:
    """Enforce the kind ↔ verification rule (doc 07 §6): ``auto`` only for auto-capable kinds;
    ``practical``/``manual`` never ``auto``."""
    if verification not in Verification.values:
        raise ValidationError(f"Unknown verification mode: {verification!r}.")
    if verification == Verification.AUTO and kind not in AUTO_CAPABLE_KINDS:
        raise ValidationError(f"'{kind}' milestones cannot use automatic verification.")


def validate_milestone_params(kind: str, params: dict | None, verification: str) -> None:
    """Validate one milestone's ``params`` + ``verification`` for its ``kind`` (doc 07 §6).

    Raises :class:`~django.core.exceptions.ValidationError` on any violation; returns ``None`` on
    success. ``params`` defaults to an empty dict.
    """
    if kind not in _VALIDATORS:
        raise ValidationError(f"Unknown milestone kind: {kind!r}.")
    validate_verification(kind, verification)
    _VALIDATORS[kind](params or {})
