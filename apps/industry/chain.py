"""Production-chain explorer: the dependency tree behind any buildable item.

Turns a product into a nested tree of build/react jobs down to raw inputs, with a
build-or-buy decision, market cost and (optional) on-hand availability at each node.
Reuses the SDE recipe lookups from :mod:`bom`; the flat :func:`bom.expand` answers
"what do I buy", this answers "how does it fit together" for a tree/graph view.
"""
from __future__ import annotations

import math
from decimal import Decimal

from django.utils.translation import gettext as _

from apps.market.pricing import price_for
from apps.sde.models import SdeType

from . import bom


def _name_map(type_ids) -> dict[int, str]:
    return dict(SdeType.objects.filter(type_id__in=list(type_ids)).values_list("type_id", "name"))


def chain_tree(
    product_type_id: int,
    quantity: int = 1,
    *,
    me: int = 0,
    strategy: str = bom.STRATEGY_BUILD_VS_BUY,
    max_depth: int = bom.DEFAULT_MAX_DEPTH,
    on_hand: dict[int, int] | None = None,
    price=price_for,
) -> dict:
    """A nested dependency tree for building ``quantity`` of ``product_type_id``.

    Each node carries its build-vs-buy decision, buy cost, one-level build cost and
    (if ``on_hand`` given) available stock. ``children`` is populated only for nodes
    the strategy decides to build. Cycles / ``max_depth`` collapse to a buy leaf.
    """
    on_hand = on_hand or {}
    # Collect every type id up front for one name lookup.
    names: dict[int, str] = {}

    def build(tid: int, qty: int, depth: int, path: frozenset[int]) -> dict:
        names.setdefault(tid, "")
        buy_cost = price(tid) * qty
        recipe = None if tid in path or depth >= max_depth else bom.buildable_recipe(tid)
        node = {
            "type_id": tid,
            "quantity": qty,
            "depth": depth,
            "on_hand": int(on_hand.get(tid, 0)),
            "buy_cost": buy_cost,
            "buildable": recipe is not None,
            "decision": "buy",
            "activity": None,
            "build_cost": None,
            "children": [],
        }
        if recipe is None:
            return node

        runs = math.ceil(qty / max(1, recipe.output_quantity))
        eff_me = me if (recipe.activity == bom.SdeBlueprintMaterial.MANUFACTURING and depth == 0) else 0
        child_needs = {
            mat: bom.material_quantity(base, runs, eff_me) for mat, base in recipe.materials.items()
        }
        one_level = sum((price(mat) * need for mat, need in child_needs.items()), start=Decimal("0"))
        node["activity"] = recipe.activity
        node["build_cost"] = one_level

        should_build = strategy == bom.STRATEGY_BUILD_TO_MINERALS or one_level < buy_cost
        node["decision"] = "build" if should_build else "buy"
        if should_build:
            node["runs"] = runs
            node["output_quantity"] = recipe.output_quantity
            child_path = path | {tid}
            for mat, need in sorted(child_needs.items()):
                node["children"].append(build(mat, need, depth + 1, child_path))
        return node

    tree = build(product_type_id, max(1, quantity), 0, frozenset())
    for tid, name in _name_map(names).items():
        names[tid] = name

    def annotate(node: dict) -> None:
        node["name"] = names.get(
            node["type_id"], _("Type %(type_id)s") % {"type_id": node["type_id"]}
        )
        # ``decision`` stays the code the templates compare (``== 'build'`` picks the
        # build cost and the cyan chip); ``decision_label`` is the translated chip text.
        node["decision_label"] = bom.decision_label(node["decision"])
        for c in node["children"]:
            annotate(c)

    annotate(tree)
    return tree
