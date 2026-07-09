"""Core abstractions for the Command Intelligence engine (design docs 04 §1, 05 §2).

Dataclasses (``SourceSlice``/``LimitInput``/``Constraint``), the per-run
``SnapshotContext`` with memoised shared inputs, the ``SourceProvider`` and
``ConstraintProvider`` protocols, and the deterministic severity/score helpers.
All pure Python — no Django or domain imports — so the engine is generic and
unit-testable in isolation (mirrors ``apps.readiness.engine.base``).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# --- severity vocabulary -----------------------------------------------------
INFO = "info"
WATCH = "watch"
HIGH = "high"
CRITICAL = "critical"
SEVERITY_ORDER = {INFO: 0, WATCH: 1, HIGH: 2, CRITICAL: 3}

# --- slice / constraint status vocabulary ------------------------------------
OK = "ok"
PARTIAL = "partial"
UNKNOWN = "unknown"          # provider ran but lacked the inputs to compute
UNAVAILABLE = "unavailable"  # provider disabled or raised (isolation fallback)
COMPUTED = "computed"        # a constraint that produced a real binding metric

# --- capability importance (drives severity) ---------------------------------
PRIMARY = "primary"
SECONDARY = "secondary"
OPTIONAL = "optional"


# --- result dataclasses ------------------------------------------------------
@dataclass(frozen=True)
class SourceSlice:
    """One intelligence source's typed contribution to the snapshot (doc 04 §1)."""

    key: str
    version: int
    facts: dict = field(default_factory=dict)
    as_of: str | None = None          # ISO8601 freshness of the underlying data
    coverage_pct: float | None = None  # 0..100, how complete this slice is
    status: str = OK                   # ok | partial | unknown | unavailable
    notes: tuple[str, ...] = ()        # human caveats ("3 pilots lack asset scope")


@dataclass(frozen=True)
class LimitInput:
    """One candidate limit on a capability — an input to the Liebig minimum (doc 05 §1)."""

    name: str                          # "pilots_qualified" | "hulls_in_stock" | …
    value: float | None                # None = this input couldn't be computed
    unit: str = ""
    evidence_ref: dict = field(default_factory=dict)  # {"source": "doctrine", "path": "…"}


@dataclass
class Constraint:
    """A computed limit on maximum capability — the binding-metric model (doc 05)."""

    key: str
    category: str                      # combat | logistics | industry | financial | …
    label: str
    binding_metric: float | None = None
    unit: str = ""
    limiting_factor: str | None = None  # argmin input name; None when unknown
    headroom: float | None = None       # binding_metric - demand (negative = shortfall)
    score: int | None = None            # optional 0–100 normalisation for the UI band
    severity: str = INFO
    status: str = COMPUTED              # computed | unknown | unavailable
    affected_capabilities: list[str] = field(default_factory=list)
    inputs: list[LimitInput] = field(default_factory=list)
    detail: str = ""                    # deterministic explanation (no LLM)

    def as_dict(self) -> dict:
        """Serialise for the snapshot's ``operational_constraints`` block (doc 04 §3)."""
        return {
            "key": self.key,
            "category": self.category,
            "label": self.label,
            "binding_metric": self.binding_metric,
            "unit": self.unit,
            "limiting_factor": self.limiting_factor,
            "headroom": self.headroom,
            "score": self.score,
            "severity": self.severity,
            "status": self.status,
            "affected_capabilities": list(self.affected_capabilities),
            "evidence": [
                {"name": i.name, "value": i.value, "unit": i.unit, **i.evidence_ref}
                for i in self.inputs
            ],
            "explanation": self.detail,
        }


# --- per-run context ---------------------------------------------------------
class SnapshotContext:
    """Shared inputs built once per snapshot build and passed to every source.

    Sources stash expensive shared work via :meth:`cached` so a computation needed
    by more than one source (e.g. the corp character set) runs only once per build —
    the ``apps.readiness.engine.base.ReadinessContext`` pattern.
    """

    def __init__(self, characters: list | None = None, config: dict | None = None):
        self.characters = characters or []
        self.config = config or {}
        self._memo: dict[str, Any] = {}

    def cached(self, key: str, factory: Callable[[], Any]) -> Any:
        if key not in self._memo:
            self._memo[key] = factory()
        return self._memo[key]


# --- provider protocols ------------------------------------------------------
@runtime_checkable
class SourceProvider(Protocol):
    key: str
    label: str
    category: str
    default_enabled: bool

    def collect(self, ctx: SnapshotContext) -> SourceSlice: ...


@runtime_checkable
class ConstraintProvider(Protocol):
    key: str
    label: str
    category: str
    default_enabled: bool

    def compute(self, snapshot: dict, cfg: dict) -> list[Constraint]: ...


# --- deterministic severity / score helpers ----------------------------------
def severity_for(
    headroom: float | None,
    demand: float | None,
    cfg: dict | None = None,
    *,
    importance: str = SECONDARY,
) -> str:
    """Map (headroom, demand, importance) → a severity band (doc 05 §3).

    Pure function of the numbers and the configured ratio cut-points, so it is
    fully testable. A primary-capability shortfall (headroom < 0) is always at
    least HIGH and escalates to CRITICAL.
    """
    cfg = cfg or {}
    g = cfg.get("global", cfg)
    crit = float(g.get("critical_ratio", -0.10))
    high = float(g.get("high_ratio", 0.05))
    watch = float(g.get("watch_ratio", 0.20))
    if headroom is None:
        return INFO
    if demand is None or demand <= 0:
        # No demand baseline: only a negative headroom (already binding) is notable.
        return HIGH if headroom < 0 else INFO
    ratio = headroom / demand
    if ratio <= crit or (importance == PRIMARY and headroom < 0):
        return CRITICAL
    if ratio <= high:
        return HIGH
    if ratio <= watch:
        return WATCH
    return INFO


def constraint_score(headroom: float | None, demand: float | None) -> int | None:
    """Normalise a constraint to 0–100 for the dashboard band colour (doc 05 §2).

    100 = comfortable headroom against demand; 0 = far short. ``None`` when there is
    no demand baseline to score against (honest-data rule — never a fake 0).
    """
    if headroom is None or demand is None or demand <= 0:
        return None
    # met = binding/demand = (demand + headroom)/demand = 1 + headroom/demand
    met = 1.0 + headroom / demand
    return max(0, min(100, round(100 * met)))


def worst_severity(severities) -> str:
    """The highest severity in a collection (for rollups)."""
    worst = INFO
    for s in severities:
        if SEVERITY_ORDER.get(s, 0) > SEVERITY_ORDER.get(worst, 0):
            worst = s
    return worst
