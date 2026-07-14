"""Core abstractions for the readiness engine (design doc 05 §2).

Dataclasses (``KpiResult``/``DimensionResult``/``Finding``), the per-run
``ReadinessContext`` holding shared pre-fetched inputs, the ``DimensionProvider``
protocol, and the value→score helpers (``ratio_score``/``threshold_score``/
``status_for``) plus the index ``combine`` rule. All pure Python — no Django or
domain imports — so the engine stays generic and unit-testable in isolation.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# --- status vocabulary -------------------------------------------------------
GREEN = "green"
AMBER = "amber"
RED = "red"
UNKNOWN = "unknown"
UNAVAILABLE = "unavailable"


# --- result dataclasses ------------------------------------------------------
@dataclass
class Finding:
    """A gap/risk/forecast emitted by a KPI or dimension.

    Carries everything ``ReadinessFinding`` (domain doc 03 §2.2) will persist in a
    later phase — severity, owner tag, task hints, ref and forecast breach — but in
    Phase 0 only the v1 "gap" subset (``as_gap``) is serialised into the payload.
    """

    kind: str                       # "doctrine" | "stock" | "forecast" | …
    label: str = ""
    weight: float = 0.0             # ranking weight (higher = more urgent)
    ref_type: str = ""
    ref_id: str = ""
    task_type: str = ""             # suggested tasks.Task.Type
    task_title: str = ""
    severity: str = "warn"          # info | warn | high | critical
    owner_tag: str = ""             # officer-responsibility tag (config-mapped)
    dimension_key: str = ""
    kpi_key: str = ""
    # The 0–100 score of the KPI this finding represents, when it maps to one. Set
    # centrally from the matching KpiResult so score-precise alert rules
    # (``score_below``/``score_above``) can match without per-provider plumbing.
    score: int | None = None
    predicted_breach_at: Any | None = None
    detail: dict = field(default_factory=dict)
    # --- Seam B: the translatable form of the prose above ---------------------
    # ``label``/``task_title`` are PERSISTED (ReadinessFinding.title/.task_title) by a Celery
    # beat that has no reader and therefore no locale, so the sentence itself can only ever be
    # frozen English. These carry the ``messages.SCAFFOLDS`` key + its plain JSON-safe params
    # alongside it, so a reader re-renders the sentence under *their* locale. A provider that
    # sets neither is unchanged: the row keeps its English prose and renders it verbatim.
    label_key: str = ""
    label_params: dict = field(default_factory=dict)
    task_title_key: str = ""
    task_title_params: dict = field(default_factory=dict)
    detail_key: str = ""
    detail_params: dict = field(default_factory=dict)

    def as_gap(self) -> dict:
        """The v1 dashboard "gap" shape (kept byte-identical for backward compat)."""
        return {
            "kind": self.kind,
            "ref_id": self.ref_id,
            "label": self.label,
            "weight": self.weight,
            "task_type": self.task_type,
            "task_title": self.task_title,
        }


@dataclass
class KpiResult:
    key: str
    value: float | None
    score: int | None
    status: str = UNKNOWN
    coverage: float = 1.0
    detail: dict = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)


@dataclass
class DimensionResult:
    key: str
    score: int | None
    status: str = UNKNOWN
    coverage: float = 1.0
    # The provider's declared weight, carried onto the result so ``combine`` can
    # use it as the fallback when no config weight is set (config overrides it).
    default_weight: float = 1.0
    # False only when the pipeline's isolation fallback produced this result (the
    # provider raised). Distinguishes "ran and emitted no findings" (computed=True,
    # score may still be None — e.g. stock with no targets) from "did not run", so a
    # transient provider failure never silently resolves that dimension's findings.
    computed: bool = True
    kpis: list[KpiResult] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    detail: dict = field(default_factory=dict)


# --- per-run context ---------------------------------------------------------
class ReadinessContext:
    """Shared inputs built once per run and passed to every provider.

    Providers stash expensive shared work here via :meth:`cached` so a computation
    needed by more than one dimension (e.g. the member×doctrine skill scan, which
    both the ``doctrine`` and ``skill`` dimensions read) runs only once per run —
    keeping a full pass at roughly the cost of the current engine (doc 05 §2.3).
    """

    def __init__(self, characters: list, config: dict | None = None):
        self.characters = characters
        self.config = config or {}
        self._memo: dict[str, Any] = {}

    def cached(self, key: str, factory: Callable[[], Any]) -> Any:
        if key not in self._memo:
            self._memo[key] = factory()
        return self._memo[key]


@runtime_checkable
class DimensionProvider(Protocol):
    key: str
    label: str
    default_weight: float
    data_sources: list[str]

    def compute(self, ctx: ReadinessContext) -> DimensionResult: ...


# --- value → score helpers ---------------------------------------------------
def ratio_score(ready: float, known: float) -> int | None:
    """``round(100·ready/known)`` — coverage-aware. ``None`` when nothing is known
    (the honest-score rule: an empty sample is *unavailable*, never a zero)."""
    if not known:
        return None
    return round(100 * ready / known)


def threshold_score(value: float | None, amber: float, red: float,
                    direction: str = "higher_is_better") -> int | None:
    """Piecewise-linear 0–100 against two band edges.

    ``amber`` is the value scoring 100, ``red`` the value scoring 0; the score
    interpolates linearly between them and clamps outside. ``direction`` flips the
    orientation for metrics where *less is better* (backlog, burn, wait time).
    """
    if value is None:
        return None
    if direction == "lower_is_better":
        # amber (good, low) → 100 ; red (bad, high) → 0
        if red == amber:
            return 100 if value <= amber else 0
        if value <= amber:
            return 100
        if value >= red:
            return 0
        return round(100 * (red - value) / (red - amber))
    # higher_is_better: amber (good, high) → 100 ; red (bad, low) → 0
    if amber == red:
        return 100 if value >= amber else 0
    if value >= amber:
        return 100
    if value <= red:
        return 0
    return round(100 * (value - red) / (amber - red))


def status_for(score: int | None, *, green: int = 75, amber: int = 50) -> str:
    """Map a 0–100 score to a traffic-light status against two cut points."""
    if score is None:
        return UNAVAILABLE
    if score >= green:
        return GREEN
    if score >= amber:
        return AMBER
    return RED


# --- index combination -------------------------------------------------------
def dimension_weight(config: dict, key: str, default: float = 1.0) -> float:
    """Configured weight for a dimension, falling back to ``default`` (doc 05 §3.1).

    Phase 0 ships no config documents, so every dimension keeps its default weight
    and the weighted mean reduces to the plain equal-weight mean of v1.
    """
    weights = (config or {}).get("weights") or {}
    try:
        return float(weights.get(key, default))
    except (TypeError, ValueError):
        return default


def combine_scores(scores) -> int | None:
    """Mean of the available (non-``None``) scores, rounded — or ``None`` if none are.

    Used by providers to fold their KPI scores into a single dimension score; a KPI
    that is unavailable (awaiting data) is excluded, never counted as zero.
    """
    available = [s for s in scores if s is not None]
    if not available:
        return None
    return round(sum(available) / len(available))


def combine_kpi_scores(kpis, kpi_config: dict | None = None) -> int | None:
    """Weighted mean of a dimension's KPI scores, honouring per-KPI config (doc 04 §3).

    ``kpi_config`` maps ``kpi.key → {enabled, weight}``. A KPI absent from the config
    defaults to *enabled at weight 1.0*, so an empty config reproduces
    :func:`combine_scores` exactly — the index stays byte-stable until leadership
    disables or re-weights a KPI. A disabled KPI is dropped from the denominator; an
    unavailable (``score is None``) KPI is excluded as always (the honest-score rule).
    """
    kpi_config = kpi_config or {}
    pairs: list[tuple[int, float]] = []
    for k in kpis:
        if k.score is None:
            continue
        cfg = kpi_config.get(k.key) or {}
        if not cfg.get("enabled", True):
            continue
        try:
            weight = float(cfg.get("weight", 1.0))
        except (TypeError, ValueError):
            weight = 1.0
        pairs.append((k.score, weight))
    if not pairs:
        return None
    denominator = sum(w for _, w in pairs)
    if not denominator:
        return None
    return round(sum(s * w for s, w in pairs) / denominator)


def combine(results: list[DimensionResult], config: dict | None = None) -> int:
    """Weighted mean of available dimension scores, rounded (doc 05 §3.1).

    Dimensions whose score is ``None`` (unavailable/awaiting data) are excluded
    from the denominator — never counted as zero — preserving the honest-score
    rule. With Phase-0 equal weights this is exactly v1's ``round(mean(scores))``.
    """
    config = config or {}
    pairs = [
        (r.score, dimension_weight(config, r.key, r.default_weight))
        for r in results
        if r.score is not None
    ]
    if not pairs:
        return 0
    numerator = sum(score * weight for score, weight in pairs)
    denominator = sum(weight for _, weight in pairs)
    if not denominator:
        return 0
    return round(numerator / denominator)
