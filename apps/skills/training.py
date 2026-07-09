"""Attribute-aware training rates and the remap advisor (SKL-1).

EVE trains a skill at ``primary + secondary/2`` SP per minute, where a skill's primary
and secondary training attributes come from the SDE (dogma 180/181, stored on
``SdeType``) and the attribute *values* come from the pilot's own
``CharacterAttributes``. With neither available we fall back to a flat rate rather than
inventing precision — every estimate is labelled an estimate in the UI.

The advisor is deliberately conservative: it only recommends a remap for a long plan,
only when the pilot isn't already remapped for it, and it reports the time saved against
a concrete, valid EVE remap (27 / 21 / 17 / 17 / 17, which sums to the 99-point total).
"""
from __future__ import annotations

# dogma attribute type id → CharacterAttributes field.
_ATTR_FIELD = {
    164: "charisma",
    165: "intelligence",
    166: "memory",
    167: "perception",
    168: "willpower",
}
_ATTR_LABEL = {
    "charisma": "Charisma",
    "intelligence": "Intelligence",
    "memory": "Memory",
    "perception": "Perception",
    "willpower": "Willpower",
}

# A concrete, valid neural remap used to quantify the advice (max primary, strong
# secondary, minimum elsewhere). 27 + 21 + 17 + 17 + 17 = 99, EVE's fixed point total.
_REMAP_PRIMARY = 27
_REMAP_SECONDARY = 21
_REMAP_FLOOR = 17


def _rate(primary_val: float, secondary_val: float) -> float:
    """SP/hour for a skill given the effective primary/secondary attribute values."""
    return (primary_val + secondary_val / 2) * 60


def sp_per_hour(attrs, primary_attr_id, secondary_attr_id, default: int) -> int:
    """A skill's SP/hour for this pilot, or ``default`` when we can't be precise.

    ``attrs`` is a ``CharacterAttributes`` (or ``None``); ``primary_attr_id`` /
    ``secondary_attr_id`` are the skill's dogma attribute type ids (or ``None``).
    """
    if attrs is None or not primary_attr_id or not secondary_attr_id:
        return default
    pf = _ATTR_FIELD.get(primary_attr_id)
    sf = _ATTR_FIELD.get(secondary_attr_id)
    if not pf or not sf:
        return default
    rate = _rate(getattr(attrs, pf, 0) or 0, getattr(attrs, sf, 0) or 0)
    # ``>= 1`` (not ``> 0``): a sub-1 rate would int-truncate to 0 and divide-by-zero
    # downstream. Unreachable with real attributes (>=17), but a safe floor.
    return int(rate) if rate >= 1 else default


def remap_advice(attrs, skill_specs, *, min_plan_seconds: int = 3 * 86400,
                 default_rate: int = 2000) -> dict | None:
    """Suggest a neural remap for a plan, or ``None`` if it isn't worth one.

    ``skill_specs`` is an iterable of ``(sp, primary_attr_id, secondary_attr_id)`` for the
    plan's outstanding steps. Returns ``{primary, secondary, current_seconds,
    remapped_seconds, saved_seconds}`` when a remap would meaningfully help a long plan,
    else ``None``. Requires the pilot's attributes and per-skill attribute data — with
    neither, there is nothing honest to advise.
    """
    if attrs is None:
        return None
    specs = [(sp, p, s) for (sp, p, s) in skill_specs if sp > 0 and p and s
             and p in _ATTR_FIELD and s in _ATTR_FIELD]
    if not specs:
        return None

    # Marginal demand per attribute: a primary attribute contributes fully to a skill's
    # rate, a secondary one half — weighted by the SP still to train.
    demand: dict[str, float] = {f: 0.0 for f in _ATTR_LABEL}
    for sp, p, s in specs:
        demand[_ATTR_FIELD[p]] += sp
        demand[_ATTR_FIELD[s]] += sp * 0.5
    ranked = sorted(demand, key=lambda f: demand[f], reverse=True)
    primary_field, secondary_field = ranked[0], ranked[1]

    remap_vals = {f: _REMAP_FLOOR for f in _ATTR_LABEL}
    remap_vals[primary_field] = _REMAP_PRIMARY
    remap_vals[secondary_field] = _REMAP_SECONDARY

    current_seconds = 0.0
    remapped_seconds = 0.0
    for sp, p, s in specs:
        pf, sf = _ATTR_FIELD[p], _ATTR_FIELD[s]
        cur_rate = _rate(getattr(attrs, pf, 0) or 0, getattr(attrs, sf, 0) or 0) or default_rate
        new_rate = _rate(remap_vals[pf], remap_vals[sf]) or default_rate
        current_seconds += sp / cur_rate * 3600
        remapped_seconds += sp / new_rate * 3600

    saved = current_seconds - remapped_seconds
    # Only advise when the plan is genuinely long and the saving is worth a remap
    # (a remap costs a year's cooldown), i.e. >= ~1 day and >= 5% of the plan.
    if current_seconds < min_plan_seconds or saved < 86400 or saved < current_seconds * 0.05:
        return None
    return {
        "primary": _ATTR_LABEL[primary_field],
        "secondary": _ATTR_LABEL[secondary_field],
        "current_seconds": int(current_seconds),
        "remapped_seconds": int(remapped_seconds),
        "saved_seconds": int(saved),
    }
