"""Contribution scoring: turn a contribution event into leader-weighted points.

The ledger stores native units (ISK, ships, m³…); ``points_for`` reads the
leadership-tuned :class:`ContributionWeights` singleton and maps an event to a
single comparable score. Kept separate from ``services`` so the (pure-ish)
scoring maths is easy to test and reason about.
"""
from __future__ import annotations

from decimal import Decimal

from .models import ContributionEvent, ContributionWeights

_MILLION = Decimal("1000000")


def active_weights() -> ContributionWeights:
    """The live weights, seeding sensible defaults the first time."""
    weights = ContributionWeights.objects.filter(is_active=True).order_by("-updated_at").first()
    if weights is None:
        weights = ContributionWeights.objects.create(name="Standard", is_active=True)
    return weights


# The scoring-relevant fields captured in a Hall-of-Fame month weight snapshot (4.15).
# (name/is_active/timestamps are deliberately excluded — only what points_for reads.)
_SNAPSHOT_FIELDS = (
    "enabled", "task_points", "fleet_points", "haul_points", "haul_requires_verification",
    "build_points_per_ship", "mining_points_per_mil", "srp_points_per_mil",
    "train_points_per_level", "doctrine_base", "doctrine_priority_coef",
    "doctrine_effort_per_mil_sp", "pvp_points_per_kill", "pvp_final_blow_bonus",
    "pve_points_per_mil", "pve_ref_types",
)
_DECIMAL_FIELDS = frozenset({
    "mining_points_per_mil", "srp_points_per_mil", "doctrine_priority_coef",
    "doctrine_effort_per_mil_sp", "pve_points_per_mil",
})


def weights_snapshot_dict(weights: ContributionWeights) -> dict:
    """A JSON-safe copy of the scoring fields (Decimals → strings) for freezing."""
    return {
        f: (str(getattr(weights, f)) if f in _DECIMAL_FIELDS else getattr(weights, f))
        for f in _SNAPSHOT_FIELDS
    }


def weights_from_snapshot(data: dict) -> ContributionWeights:
    """Rebuild an UNSAVED ContributionWeights from a snapshot dict, for scoring a frozen
    month. Decimal fields are restored to Decimal so points_for's maths still works; the
    instance is never saved."""
    kwargs = {}
    for f in _SNAPSHOT_FIELDS:
        if f not in data:
            continue
        kwargs[f] = Decimal(str(data[f])) if f in _DECIMAL_FIELDS else data[f]
    return ContributionWeights(name="(frozen snapshot)", is_active=False, **kwargs)


def points_for(
    kind: str,
    *,
    magnitude=0,
    doctrine_priority: int = 0,
    required_sp: int = 0,
    final_blows: int = 0,
    weights: ContributionWeights | None = None,
) -> int:
    """Points a single contribution of ``kind`` is worth under the current weights.

    ``magnitude`` is the event's native quantity (ships, ISK, levels, kills…).
    ``doctrine_priority``/``required_sp`` only matter for ``doctrine``; ``final_blows``
    only for ``pvp``. Returns a non-negative whole number; 0 when scoring is disabled.
    """
    w = weights or active_weights()
    if not w.enabled:
        return 0

    K = ContributionEvent.Kind
    mag = Decimal(str(magnitude or 0))

    if kind == K.TASK:
        value = Decimal(w.task_points)
    elif kind == K.FLEET:
        value = Decimal(w.fleet_points)
    elif kind == K.HAUL:
        value = Decimal(w.haul_points)
    elif kind == K.BUILD:
        value = Decimal(w.build_points_per_ship) * mag
    elif kind == K.MINING:
        value = w.mining_points_per_mil * (mag / _MILLION)
    elif kind == K.SRP:
        value = w.srp_points_per_mil * (mag / _MILLION)
    elif kind == "train":
        # Per recommended skill level trained (magnitude = levels in this event).
        value = Decimal(w.train_points_per_level) * (mag if mag > 0 else Decimal(1))
    elif kind == "doctrine":
        value = (
            Decimal(w.doctrine_base)
            + w.doctrine_priority_coef * Decimal(doctrine_priority or 0)
            + w.doctrine_effort_per_mil_sp * (Decimal(required_sp or 0) / _MILLION)
        )
    elif kind == "pvp":
        # magnitude = number of kills the pilot was an attacker on.
        value = Decimal(w.pvp_points_per_kill) * mag + Decimal(w.pvp_final_blow_bonus) * Decimal(final_blows or 0)
    elif kind == "pve":
        # magnitude = ISK of corp PVE (ratting) income from the member.
        value = w.pve_points_per_mil * (mag / _MILLION)
    else:
        value = Decimal(0)

    return max(0, int(value.to_integral_value(rounding="ROUND_HALF_UP")))
