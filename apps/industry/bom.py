"""Bill-of-materials expansion and build-vs-buy decisions (SDE-driven).

Two layers:

* The one-level helpers (``direct_materials``, ``decide_build_or_buy`` …) answer
  "what does *this* blueprint consume" — fast, used by the recommendation engine.
* ``expand`` recurses the whole production tree (manufacturing **and** reactions)
  down to the materials you actually have to buy or mine, so the corp can plan to
  build *anything* — capitals, T2 hulls, fuel blocks — all the way to minerals.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal

from django.utils.translation import gettext_lazy as _

from apps.market.pricing import price_for
from apps.sde.models import SdeBlueprintMaterial

# Activities that form the deterministic build tree, in preference order.
BUILD_ACTIVITIES = (SdeBlueprintMaterial.MANUFACTURING, SdeBlueprintMaterial.REACTION)

STRATEGY_BUILD_VS_BUY = "build_vs_buy"
STRATEGY_BUILD_TO_MINERALS = "build_to_minerals"
DEFAULT_MAX_DEPTH = 8

# --------------------------------------------------------------------------- #
#  Build-or-buy decision: a CODE, plus a display label
# --------------------------------------------------------------------------- #
# ``decision`` is a code, not prose. It drives the chain templates
# (``{% if node.decision == 'build' %}`` picks the build cost and the cyan chip), the
# doctrine supply form (``<input name="action" value="{{ line.decision }}">``) and the
# strategy branches in this module — so translating the value itself would silently
# break all of them for every non-English pilot. The code stays canonical English; the
# label beside it is the translated half, resolved at render time only.
DECISION_LABELS: dict[str, str] = {
    "build": _("build"),
    "buy": _("buy"),
}


def decision_label(code: str):
    """The human label for a build-or-buy decision code (the code itself if unmapped)."""
    return DECISION_LABELS.get(code, code)

# --------------------------------------------------------------------------- #
# Process-local recipe cache
#
# Recursive BOM expansion (and the doctrine supply aggregation) calls the recipe
# lookups once per node — re-querying SdeBlueprintMaterial for every shared
# sub-component (Tritanium, capital parts, …). SDE recipes are static between
# imports, so we memoise them in-process behind a long TTL. A None recipe (a raw
# material with no blueprint) is cached too, so minerals aren't re-queried.
# --------------------------------------------------------------------------- #
_SDE_TTL = 1800.0  # 30 min; recipes change only on an SDE re-import (+ deploy restart)
_SDE_LOCK = threading.Lock()
_SDE_STATE: dict = {"at": 0.0}
_RECIPE: dict[int, Recipe | None] = {}
_MFG_MATS: dict[int, dict[int, int]] = {}
_BP_FOR: dict[int, int | None] = {}
_PROD_FOR: dict[int, int | None] = {}


def _sde_generation() -> None:
    """Expire the whole recipe cache once per TTL window (cheap clear-and-relazy)."""
    now = time.monotonic()
    if now - _SDE_STATE["at"] <= _SDE_TTL:
        return
    with _SDE_LOCK:
        if time.monotonic() - _SDE_STATE["at"] > _SDE_TTL:
            _RECIPE.clear()
            _MFG_MATS.clear()
            _BP_FOR.clear()
            _PROD_FOR.clear()
            _SDE_STATE["at"] = time.monotonic()


def reset_recipe_cache() -> None:
    """Drop the in-process recipe cache (tests call this between cases)."""
    _RECIPE.clear()
    _MFG_MATS.clear()
    _BP_FOR.clear()
    _PROD_FOR.clear()
    _SDE_STATE["at"] = 0.0


def blueprint_for(product_type_id: int) -> int | None:
    """Return the blueprint type id that manufactures a product, or None."""
    _sde_generation()
    if product_type_id in _BP_FOR:
        return _BP_FOR[product_type_id]
    val = (
        SdeBlueprintMaterial.objects.filter(
            product_type=product_type_id, activity=SdeBlueprintMaterial.MANUFACTURING
        )
        .values_list("blueprint_type_id", flat=True)
        .first()
    )
    _BP_FOR[product_type_id] = val
    return val


def product_for(blueprint_type_id: int) -> int | None:
    """Return the product a blueprint manufactures, or None (inverse of ``blueprint_for``)."""
    _sde_generation()
    if blueprint_type_id in _PROD_FOR:
        return _PROD_FOR[blueprint_type_id]
    val = (
        SdeBlueprintMaterial.objects.filter(
            blueprint_type_id=blueprint_type_id, activity=SdeBlueprintMaterial.MANUFACTURING
        )
        .values_list("product_type", flat=True)
        .first()
    )
    _PROD_FOR[blueprint_type_id] = val
    return val


def material_quantity(base_qty: int, runs: int, me: int, structure_bonus: float = 0.0) -> int:
    """Material needed for ``runs`` runs at blueprint material-efficiency ``me`` (%),
    with an optional ``structure_bonus`` (fraction 0–1) for an Engineering Complex + rig's
    material reduction, applied multiplicatively after ME (4.12). ``structure_bonus=0``
    is byte-identical to the pre-4.12 blueprint-ME-only result."""
    structure_bonus = min(0.99, max(0.0, structure_bonus))  # defensive: never negate/invert qty
    per_run = max(1, math.ceil(base_qty * (100 - me) / 100 * (1 - structure_bonus)))
    return per_run * max(1, runs)


def _manufacturing_materials(product_type_id: int) -> dict[int, int]:
    """Cached base (per-run, ME0) manufacturing materials for a product."""
    _sde_generation()
    cached = _MFG_MATS.get(product_type_id)
    if cached is not None:
        return cached
    out: dict[int, int] = {}
    for mat, qty in SdeBlueprintMaterial.objects.filter(
        product_type=product_type_id, activity=SdeBlueprintMaterial.MANUFACTURING
    ).values_list("material_type_id", "quantity"):
        out[mat] = out.get(mat, 0) + qty
    _MFG_MATS[product_type_id] = out
    return out


def direct_materials(product_type_id: int, runs: int = 1, me: int = 0) -> dict[int, int]:
    """Direct (one-level) material requirements to build a product."""
    return {
        mat: material_quantity(base, runs, me)
        for mat, base in _manufacturing_materials(product_type_id).items()
    }


def build_cost(product_type_id: int, quantity: int = 1, me: int = 0) -> Decimal | None:
    """Cost to build ``quantity`` units (one level), or None if not buildable."""
    materials = direct_materials(product_type_id, runs=quantity, me=me)
    if not materials:
        return None
    return sum((price_for(tid) * qty for tid, qty in materials.items()), start=Decimal("0"))


def buy_cost(product_type_id: int, quantity: int = 1) -> Decimal:
    return price_for(product_type_id) * quantity


def decide_build_or_buy(product_type_id: int, quantity: int = 1, me: int = 0) -> dict:
    """Compare building vs buying (one level). Returns decision + both costs.

    If the item is not buildable (no blueprint/materials in the SDE), the
    decision is always ``buy``.
    """
    b_cost = build_cost(product_type_id, quantity, me)
    p_cost = buy_cost(product_type_id, quantity)
    if b_cost is None:
        return {"decision": "buy", "build_cost": None, "buy_cost": p_cost, "buildable": False}
    decision = "build" if b_cost < p_cost else "buy"
    return {"decision": decision, "build_cost": b_cost, "buy_cost": p_cost, "buildable": True}


# --------------------------------------------------------------------------- #
# Recursive expansion
# --------------------------------------------------------------------------- #
@dataclass
class Recipe:
    activity: str
    blueprint_type_id: int
    output_quantity: int
    materials: dict[int, int]  # material_type_id -> base qty per run


def buildable_recipe(product_type_id: int) -> Recipe | None:
    """The build recipe for a product, preferring manufacturing over reaction.

    Cached in-process: for a recursive expansion the same sub-components recur
    dozens of times, and re-querying each was a large share of the page cost.
    """
    _sde_generation()
    if product_type_id in _RECIPE:
        return _RECIPE[product_type_id]
    rows = list(
        SdeBlueprintMaterial.objects.filter(
            product_type=product_type_id, activity__in=BUILD_ACTIVITIES
        )
    )
    recipe: Recipe | None = None
    for activity in BUILD_ACTIVITIES:
        act_rows = [r for r in rows if r.activity == activity]
        if act_rows:
            recipe = Recipe(
                activity=activity,
                blueprint_type_id=act_rows[0].blueprint_type_id,
                output_quantity=act_rows[0].output_quantity or 1,
                materials={r.material_type_id: r.quantity for r in act_rows},
            )
            break
    _RECIPE[product_type_id] = recipe
    return recipe


@dataclass
class ProductionNode:
    """One intermediate build/react job in the production plan."""

    type_id: int
    activity: str
    runs: int
    output_quantity: int
    produced: int
    needed: int
    depth: int


@dataclass
class BomResult:
    """Result of a recursive expansion.

    ``leaves`` are the raw inputs you must acquire (buy/mine/PI); ``steps`` are
    the intermediate jobs, ordered deepest-first so they read as a build order.
    """

    leaves: dict[int, int] = field(default_factory=dict)
    steps: list[ProductionNode] = field(default_factory=list)

    def add_leaf(self, type_id: int, qty: int) -> None:
        self.leaves[type_id] = self.leaves.get(type_id, 0) + qty

    def total_cost(self) -> Decimal:
        return sum(
            (price_for(tid) * qty for tid, qty in self.leaves.items()), start=Decimal("0")
        )


def _should_build(type_id: int, quantity: int, recipe: Recipe, strategy: str, me: int) -> bool:
    if strategy == STRATEGY_BUILD_TO_MINERALS:
        return True
    # build_vs_buy: build only when one-level build is cheaper than buying.
    runs = math.ceil(quantity / max(1, recipe.output_quantity))
    eff_me = me if recipe.activity == SdeBlueprintMaterial.MANUFACTURING else 0
    one_level = sum(
        (price_for(mat) * material_quantity(base, runs, eff_me) for mat, base in recipe.materials.items()),
        start=Decimal("0"),
    )
    return one_level < buy_cost(type_id, quantity)


def expand(
    type_id: int,
    quantity: int = 1,
    *,
    strategy: str = STRATEGY_BUILD_VS_BUY,
    max_depth: int = DEFAULT_MAX_DEPTH,
    me: int = 0,
    structure_bonus: float = 0.0,
    _depth: int = 0,
    _path: frozenset[int] | None = None,
    _result: BomResult | None = None,
) -> BomResult:
    """Recursively expand ``quantity`` of ``type_id`` into raw inputs + jobs.

    ``strategy`` is ``build_vs_buy`` (build a node only when cheaper than buying)
    or ``build_to_minerals`` (build everything buildable down to raw inputs).
    Cycles and ``max_depth`` collapse a node to a "buy" leaf — EVE has no real
    build cycles, but the guard keeps malformed SDE data from looping forever.
    """
    result = _result if _result is not None else BomResult()
    path = _path or frozenset()

    recipe = None if type_id in path or _depth >= max_depth else buildable_recipe(type_id)
    if recipe is None or not _should_build(type_id, quantity, recipe, strategy, me):
        result.add_leaf(type_id, quantity)
        return result

    runs = math.ceil(quantity / max(1, recipe.output_quantity))
    produced = runs * recipe.output_quantity
    top_mfg = recipe.activity == SdeBlueprintMaterial.MANUFACTURING and _depth == 0
    eff_me = me if top_mfg else 0
    # The structure/rig material bonus applies where the blueprint ME does (the top-level
    # manufacturing job) — consistent with the existing per-depth ME simplification.
    eff_structure = structure_bonus if top_mfg else 0.0
    child_path = path | {type_id}
    for mat, base in recipe.materials.items():
        need = material_quantity(base, runs, eff_me, eff_structure)
        expand(
            mat, need, strategy=strategy, max_depth=max_depth, me=me,
            structure_bonus=structure_bonus,
            _depth=_depth + 1, _path=child_path, _result=result,
        )
    result.steps.append(
        ProductionNode(
            type_id=type_id, activity=recipe.activity, runs=runs,
            output_quantity=recipe.output_quantity, produced=produced,
            needed=quantity, depth=_depth,
        )
    )
    return result
