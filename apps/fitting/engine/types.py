"""Deterministic input/output contract for the fitting engine (the domain boundary).

Everything the engine consumes and produces is a plain, hashable-friendly dataclass
with no dependency on Django request state, ESI, templates or the network. The rest of
FORCA Command Grid only ever passes these in and reads these out, through
:mod:`apps.fitting.engine.adapter` — so the engine implementation can change without
touching the feature.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum

# Bumped whenever a calculation changes in a way that could move a saved fit's numbers.
# Stored on every FitRevision + cache key so historical results never silently drift.
# 2.0.0 — remediation: generic dogma-graph evaluator (passes 1-3 in graph.py, telemetry
# in evaluator.py) replaces the curated per-mechanic engine; corrected capacitor peak
# (2.5·C/τ), active/passive tank, ranges, drone bandwidth gating, charge/rig/group
# validation, implants. See docs/architecture/decisions/tochas-lab-calculation-engine.md.
ENGINE_VERSION = "2.0.0"


class SlotKind(str, Enum):
    HIGH = "high"
    MED = "med"
    LOW = "low"
    RIG = "rig"
    SUBSYSTEM = "subsystem"
    SERVICE = "service"
    DRONE = "drone"
    FIGHTER = "fighter"
    IMPLANT = "implant"
    BOOSTER = "booster"
    CARGO = "cargo"


class ModuleState(str, Enum):
    OFFLINE = "offline"
    ONLINE = "online"
    ACTIVE = "active"
    OVERHEATED = "overheated"


class OperatingMode(str, Enum):
    ALL_ACTIVE = "all_active"
    SUSTAINABLE = "sustainable"
    MAX_DAMAGE = "max_damage"
    MAX_TANK = "max_tank"


class Status(str, Enum):
    IMPOSSIBLE = "impossible"          # structurally invalid (wrong slot, hull restriction)
    OVER_RESOURCES = "over_resources"  # valid layout, exceeds CPU/PG/calibration
    MISSING_SKILLS = "missing_skills"  # valid, but the pilot cannot operate everything
    WARNINGS = "warnings"              # valid with advisories
    VALID = "valid"                    # fully valid


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ModuleInput:
    type_id: int
    slot: SlotKind
    state: ModuleState = ModuleState.ACTIVE
    charge_type_id: int | None = None
    quantity: int = 1

    def canonical(self) -> dict:
        return {
            "type_id": self.type_id, "slot": self.slot.value, "state": self.state.value,
            "charge_type_id": self.charge_type_id, "quantity": self.quantity,
        }


@dataclass(frozen=True)
class FitInput:
    ship_type_id: int
    modules: tuple[ModuleInput, ...] = ()

    def canonical(self) -> dict:
        return {
            "ship_type_id": self.ship_type_id,
            "modules": [m.canonical() for m in self.modules],
        }

    def hash(self) -> str:
        return hashlib.sha256(
            json.dumps(self.canonical(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:32]


@dataclass(frozen=True)
class SkillProfile:
    """A pilot's (or hypothetical) skill levels. ``levels`` maps skill_type_id -> 0..5.

    ``all_five`` short-circuits every lookup to level 5 without materialising a dict for
    every skill in the game — used for the "All V" comparison column.
    """
    levels: frozenset = frozenset()          # frozenset of (skill_id, level) pairs
    all_five: bool = False
    label: str = "current"

    @classmethod
    def from_dict(cls, mapping: dict[int, int], label: str = "current") -> SkillProfile:
        return cls(levels=frozenset((int(k), int(v)) for k, v in mapping.items()), label=label)

    @classmethod
    def omniscient(cls, label: str = "All V") -> SkillProfile:
        return cls(all_five=True, label=label)

    def level(self, skill_id: int) -> int:
        if self.all_five:
            return 5
        for sid, lvl in self.levels:
            if sid == skill_id:
                return lvl
        return 0

    def hash(self) -> str:
        if self.all_five:
            return "allv"
        return hashlib.sha256(
            json.dumps(sorted(self.levels), separators=(",", ":")).encode()
        ).hexdigest()[:16]


@dataclass(frozen=True)
class DamageProfileInput:
    em: float = 0.25
    thermal: float = 0.25
    kinetic: float = 0.25
    explosive: float = 0.25

    def normalised(self) -> DamageProfileInput:
        total = self.em + self.thermal + self.kinetic + self.explosive
        if total <= 0:
            return DamageProfileInput()
        return DamageProfileInput(
            self.em / total, self.thermal / total, self.kinetic / total, self.explosive / total
        )

    def as_map(self) -> dict[str, float]:
        return {"em": self.em, "thermal": self.thermal, "kinetic": self.kinetic,
                "explosive": self.explosive}


@dataclass(frozen=True)
class TargetProfile:
    """The target a fit is measured against, for damage application (missiles) and, later,
    turret tracking. ``None`` on the operating profile means "report raw output"."""
    signature_radius: float = 0.0   # metres
    velocity: float = 0.0           # m/s (transversal is assumed = velocity for missiles)
    label: str = ""

    def hash(self) -> str:
        return f"{self.signature_radius:.1f}:{self.velocity:.1f}"


@dataclass(frozen=True)
class OperatingProfile:
    mode: OperatingMode = OperatingMode.ALL_ACTIVE
    propulsion_active: bool = True
    damage_profile: DamageProfileInput = field(default_factory=DamageProfileInput)
    target: TargetProfile | None = None

    def hash(self) -> str:
        d = self.damage_profile.normalised()
        dp = f"{d.em:.3f}:{d.thermal:.3f}:{d.kinetic:.3f}:{d.explosive:.3f}"
        tgt = self.target.hash() if self.target else "none"
        return f"{self.mode.value}:{int(self.propulsion_active)}:{dp}:{tgt}"


# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #
@dataclass
class Contribution:
    """One line of an explainability trace: how a source moved an attribute."""
    source: str  # e.g. "Rifter (hull)", "Gunnery V", "Gyrostabilizer"
    kind: str  # base|ship_bonus|role_bonus|skill|module|rig|charge|stacking|environment
    detail: str = ""
    value: float | None = None  # the resulting running value after this step (when meaningful)


@dataclass
class AttributeTrace:
    attribute: str
    base: float
    final: float
    unit: str = ""
    contributions: list[Contribution] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "attribute": self.attribute, "base": self.base, "final": self.final,
            "unit": self.unit,
            "contributions": [c.__dict__ for c in self.contributions],
        }


@dataclass
class Diagnostic:
    code: str
    severity: Severity
    title: str
    detail: str = ""
    evidence: str = ""
    suggested_action: str = ""
    contextual: bool = True     # advisory (depends on doctrine/intent), not an objective defect
    # Structured values behind the English title/detail, so the Django-side presentation
    # layer can re-render them in the reader's language (the engine stays i18n-free).
    params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {**self.__dict__, "severity": self.severity.value}


@dataclass
class MissingSkill:
    skill_type_id: int
    required_level: int
    have_level: int
    for_type_id: int


@dataclass
class FittingResult:
    status: Status
    telemetry: dict = field(default_factory=dict)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    traces: dict[str, AttributeTrace] = field(default_factory=dict)
    missing_skills: list[MissingSkill] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    engine_version: str = ENGINE_VERSION
    data_version: str = ""
    compute_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "telemetry": self.telemetry,
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "traces": {k: v.to_dict() for k, v in self.traces.items()},
            "missing_skills": [m.__dict__ for m in self.missing_skills],
            "warnings": self.warnings,
            "errors": self.errors,
            "unsupported": self.unsupported,
            "engine_version": self.engine_version,
            "data_version": self.data_version,
            "compute_ms": self.compute_ms,
        }
