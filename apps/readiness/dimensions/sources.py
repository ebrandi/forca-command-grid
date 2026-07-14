"""Shared data sources for the built-in dimensions.

These are the **verbatim** v1 formulas from the previous ``compute_readiness`` —
``doctrine_and_skill`` (the member×doctrine skill scan) and ``stock_and_logistics``
— moved here unchanged so the provider/registry refactor reproduces v1 numbers
byte-for-byte (the Phase-0 golden gate). The doctrine and skill providers share the
single ``doctrine_and_skill`` pass via ``ReadinessContext.cached`` so it runs once
per run, preserving the current query profile.
"""
from __future__ import annotations

from apps.doctrines.models import Doctrine
from apps.doctrines.services import character_readiness
from apps.sde.models import SdeType

from ..engine.base import ReadinessContext
from ..messages import english_text

READY = ("viable", "optimal")

# Memo keys for the shared computations stored on the context.
DOCTRINE_SKILL = "doctrine_and_skill"
STOCK_LOGISTICS = "stock_and_logistics"


def corp_characters() -> list:
    from apps.sso.models import EveCharacter

    return list(EveCharacter.objects.filter(is_corp_member=True))


def build_context(config: dict | None = None) -> ReadinessContext:
    """Build the per-run context: fetch corp characters once (doc 05 §2.3)."""
    return ReadinessContext(characters=corp_characters(), config=config or {})


def doctrine_and_skill(characters) -> tuple[dict, dict, list, dict]:
    """Single pass over characters × doctrines → doctrine + skill scores + gaps +
    per-doctrine coverage (``{id: {name, ready, known, priority}}``, for the Gap-B KPIs)."""
    doctrines = list(
        Doctrine.objects.filter(status=Doctrine.Status.ACTIVE)
        .prefetch_related("fits__skill_requirements")
        .order_by("-priority", "name")
    )
    rank = {"optimal": 3, "viable": 2, "not_ready": 1, "unknown": 0}
    char_any_ready = {c.character_id: False for c in characters}
    char_known = {c.character_id: False for c in characters}

    # Preload each character's latest skill snapshot ONCE (a single query) instead
    # of re-fetching it inside character_readiness for every (character, fit) pair.
    from apps.characters.models import CharacterSkillSnapshot

    snaps = {
        s.character_id: s
        for s in CharacterSkillSnapshot.objects.filter(
            is_latest=True, character_id__in=[c.character_id for c in characters]
        )
    }

    total_w = 0.0
    acc = 0.0
    doctrine_gaps = []
    per_doctrine: dict[int, dict] = {}  # id -> {name, ready, known, priority} (Gap B KPIs)
    for doctrine in doctrines:
        counts = {"optimal": 0, "viable": 0, "not_ready": 0, "unknown": 0}
        for ch in characters:
            best = "unknown"
            snapshot = snaps.get(ch.character_id)
            for fit in doctrine.fits.all():
                s = character_readiness(ch, fit, snapshot=snapshot).status
                if rank[s] > rank[best]:
                    best = s
            counts[best] += 1
            if best in READY:
                char_any_ready[ch.character_id] = True
            if best != "unknown":
                char_known[ch.character_id] = True
        known = counts["optimal"] + counts["viable"] + counts["not_ready"]
        ready = counts["optimal"] + counts["viable"]
        per_doctrine[doctrine.id] = {
            "name": doctrine.name, "ready": ready, "known": known,
            "priority": doctrine.priority or 0,
        }
        if known:
            ratio = ready / known
            w = max(doctrine.priority or 0, 1)
            total_w += w
            acc += w * ratio
            if ratio < 1.0:
                # Seam B: the label/task_title are persisted by the beat, so they carry a
                # scaffold key + raw params next to the English. The doctrine NAME is corp
                # content and stays verbatim inside the params (never translated).
                label_params = {
                    "doctrine": doctrine.name, "ready": ready, "known": known,
                }
                task_params = {"doctrine": doctrine.name}
                doctrine_gaps.append(
                    {
                        "kind": "doctrine",
                        "ref_id": str(doctrine.id),
                        "label": english_text("doctrine.coverage_gap", label_params),
                        "label_key": "doctrine.coverage_gap",
                        "label_params": label_params,
                        "weight": round(w * (1 - ratio), 2),
                        "task_type": "train",
                        "task_title": english_text("doctrine.train_task", task_params),
                        "task_title_key": "doctrine.train_task",
                        "task_title_params": task_params,
                    }
                )

    doctrine_score = round(100 * acc / total_w) if total_w else None
    known_members = sum(1 for v in char_known.values() if v)
    ready_members = sum(1 for v in char_any_ready.values() if v)
    skill_score = round(100 * ready_members / known_members) if known_members else None
    coverage = {
        "characters": len(characters),
        "known": known_members,
        "ready_any": ready_members,
    }
    return (
        {"doctrine": doctrine_score, "skill": skill_score},
        coverage,
        doctrine_gaps,
        per_doctrine,
    )


def stock_and_logistics() -> tuple[dict, list]:
    from apps.stockpile.models import HaulingTask, Stockpile, StockpileItem
    from apps.stockpile.services import shortfalls_against_targets

    targets = StockpileItem.objects.filter(
        stockpile__kind=Stockpile.Kind.CORP, quantity_target__isnull=False
    ).count()
    shortfalls = shortfalls_against_targets()
    stock_score = (
        round(100 * (1 - len(shortfalls) / targets)) if targets else None
    )

    open_hauls = HaulingTask.objects.filter(status=HaulingTask.Status.OPEN).count()
    # v1 heuristic: full marks with no backlog, decaying as open hauls pile up.
    logistics_score = max(0, 100 - 8 * open_hauls)

    gaps = []
    names = dict(
        SdeType.objects.filter(
            type_id__in=[s["type_id"] for s in shortfalls]
        ).values_list("type_id", "name")
    )
    for s in sorted(shortfalls, key=lambda r: -r["deficit"])[:8]:
        # The item name is EVE game data and the stockpile name is corp content: both stay raw
        # inside the params. Only the sentence around them is translatable.
        item = str(names.get(s["type_id"], s["type_id"]))
        label_params = {
            "item": item, "deficit": s["deficit"], "stockpile": s["stockpile"],
        }
        task_params = {"item": item, "deficit": s["deficit"]}
        gaps.append(
            {
                "kind": "stock",
                "ref_id": str(s["type_id"]),
                "label": english_text("stock.shortfall", label_params),
                "label_key": "stock.shortfall",
                "label_params": label_params,
                "weight": float(s["deficit"]),
                "task_type": "build",
                "task_title": english_text("stock.restock_task", task_params),
                "task_title_key": "stock.restock_task",
                "task_title_params": task_params,
            }
        )
    return {"stock": stock_score, "logistics": logistics_score}, gaps


# --- context-memoised accessors (shared across providers) --------------------
def get_doctrine_skill(ctx: ReadinessContext) -> tuple[dict, dict, list, dict]:
    return ctx.cached(DOCTRINE_SKILL, lambda: doctrine_and_skill(ctx.characters))


def get_stock_logistics(ctx: ReadinessContext) -> tuple[dict, list]:
    return ctx.cached(STOCK_LOGISTICS, stock_and_logistics)
