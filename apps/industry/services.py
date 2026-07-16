"""Industry project services: BOM computation, shopping lists, bottlenecks."""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from apps.market.pricing import price_for
from apps.stockpile.services import (
    available_quantities,
    available_quantity,
    reserve_for_project_bulk,
)

from . import bom
from .models import (
    IndustryEconomyConfig,
    IndustryProject,
    IndustryProjectItem,
    MaterialRequirement,
    ProductionStep,
    ShoppingList,
    ShoppingListItem,
)

_ACTIVITY_METHOD = {
    bom.SdeBlueprintMaterial.MANUFACTURING: MaterialRequirement.AcquireMethod.BUILD,
    bom.SdeBlueprintMaterial.REACTION: MaterialRequirement.AcquireMethod.REACT,
}


def effective_rates(project: IndustryProject | None = None) -> dict:
    """Resolve the tax / fee / cost-index assumptions for a plan.

    Per-plan overrides win; anything left null inherits the active
    :class:`IndustryEconomyConfig` default. Returned as a dict of ``Decimal`` ready
    to hand to :mod:`apps.industry.calc`.
    """
    cfg = IndustryEconomyConfig.active()

    def pick(override, default):
        return override if override is not None else default

    return {
        "sales_tax": pick(getattr(project, "sales_tax", None), cfg.default_sales_tax),
        "broker_fee": pick(getattr(project, "broker_fee", None), cfg.default_broker_fee),
        "system_cost_index": pick(
            getattr(project, "system_cost_index", None), cfg.default_system_cost_index
        ),
        "facility_tax": pick(getattr(project, "facility_tax", None), cfg.default_facility_tax),
    }


@transaction.atomic
def compute_project_bom(project: IndustryProject) -> dict:
    """(Re)compute the full production plan for every item in a project.

    For each item we recursively expand the build tree (manufacturing +
    reactions) down to raw inputs per the item's strategy, net the leaf inputs
    off available corp stock, price the remainder, and record the intermediate
    build/react jobs. Returns a summary with total acquire cost.
    """
    # Pass 1: expand every item, decide build/buy, and collect the requirement
    # specs. Gathering all leaf type_ids up front lets us resolve corp-stock
    # availability for the whole project in a single batch (below) instead of two
    # aggregate queries per distinct leaf material.
    plans: list[tuple[IndustryProjectItem, list[tuple[int, int, int, bool]], list | None]] = []
    needed_ids: set[int] = set()
    # pk order pins the shared-pool allocation below (matches display order).
    for item in project.items.order_by("pk"):
        MaterialRequirement.objects.filter(project_item=item).delete()
        ProductionStep.objects.filter(project_item=item).delete()

        if item.build_or_buy == IndustryProjectItem.BuildOrBuy.BUY:
            # Buy outright — no expansion.
            plans.append((item, [(item.type_id, item.quantity, 0, False)], None))
            needed_ids.add(item.type_id)
            continue

        strategy = (
            bom.STRATEGY_BUILD_TO_MINERALS
            if item.strategy == IndustryProjectItem.Strategy.BUILD_TO_MINERALS
            else bom.STRATEGY_BUILD_VS_BUY
        )
        result = bom.expand(
            item.type_id, item.quantity, strategy=strategy, max_depth=item.max_depth, me=item.me
        )

        if not result.steps:
            # Nothing was buildable (or build-vs-buy chose buy) — single buy line.
            if item.build_or_buy == IndustryProjectItem.BuildOrBuy.UNDECIDED:
                item.build_or_buy = IndustryProjectItem.BuildOrBuy.BUY
                item.save(update_fields=["build_or_buy"])
            plans.append((item, [(item.type_id, item.quantity, 0, False)], None))
            needed_ids.add(item.type_id)
            continue

        if item.build_or_buy == IndustryProjectItem.BuildOrBuy.UNDECIDED:
            item.build_or_buy = IndustryProjectItem.BuildOrBuy.BUILD
            item.save(update_fields=["build_or_buy"])

        specs = [
            (type_id, qty, 0 if type_id == item.type_id else 1, True)
            for type_id, qty in sorted(result.leaves.items())
        ]
        needed_ids.update(result.leaves)
        plans.append((item, specs, result.steps))

    # One batched availability lookup for every leaf material in the project,
    # treated as a shared remaining-pool (P1 WS3): two lines sharing a material
    # must not each net the full stock — each line takes what's left, in item
    # order (deterministic: ``plans`` follows the project's item iteration).
    pool = dict(available_quantities(needed_ids))

    # Pass 2: materialise the requirement + build/react step + invention rows.
    total_cost = Decimal("0")
    for item, specs, steps in plans:
        for type_id, qty, depth, force_buy in specs:
            take = min(pool.get(type_id, 0), qty)
            if take:
                pool[type_id] -= take
            total_cost += _make_requirement(
                item,
                type_id,
                qty,
                depth=depth,
                force_buy=force_buy,
                available=take,
            )
        if steps is not None:
            for node in steps:
                ProductionStep.objects.create(
                    project_item=item,
                    type_id=node.type_id,
                    activity=node.activity,
                    runs=node.runs,
                    output_quantity=node.output_quantity,
                    produced_quantity=node.produced,
                    required_quantity=node.needed,
                    depth=node.depth,
                )
            # T2/T3 nodes need a blueprint copy invented first — cost the datacores.
            total_cost += _cost_invention(item, steps)

    project.estimated_cost = total_cost
    project.estimated_value = _project_value(project)
    project.save(update_fields=["estimated_cost", "estimated_value"])
    return {
        "estimated_cost": total_cost,
        "estimated_value": project.estimated_value,
        "item_count": project.items.count(),
    }


def _cost_invention(item: IndustryProjectItem, steps) -> Decimal:
    """Add datacore requirements for any invented (T2/T3) node in the plan.

    One invention job per manufacturing run is a deliberate over-estimate (real
    invention yields multi-run BPCs at a success probability) — surfaced as
    INVENT lines so the cost is honest rather than silently omitted.
    """
    from apps.sde.models import SdeBlueprintMaterial

    mfg_nodes = [n for n in steps if n.activity == SdeBlueprintMaterial.MANUFACTURING]
    if not mfg_nodes:
        return Decimal("0")

    # One datacore query for every invented node, grouped in Python (was one query
    # per manufacturing step).
    cores_by_product: dict[int, list] = {}
    for core in SdeBlueprintMaterial.objects.filter(
        product_type_id__in={n.type_id for n in mfg_nodes},
        activity=SdeBlueprintMaterial.INVENTION,
    ):
        cores_by_product.setdefault(core.product_type_id, []).append(core)

    extra = Decimal("0")
    for node in mfg_nodes:
        for core in cores_by_product.get(node.type_id, []):
            qty = core.quantity * max(1, node.runs)
            unit_cost = price_for(core.material_type_id)
            MaterialRequirement.objects.create(
                project_item=item,
                type_id=core.material_type_id,
                quantity_required=qty,
                quantity_available=0,
                quantity_to_acquire=qty,
                acquire_method=MaterialRequirement.AcquireMethod.INVENT,
                unit_cost=unit_cost,
                depth=node.depth + 1,
            )
            extra += unit_cost * qty
    return extra


def _project_value(project: IndustryProject) -> Decimal:
    """Market value of the project's finished products (for profit estimate)."""
    return sum(
        (price_for(it.type_id) * it.quantity for it in project.items.all()),
        start=Decimal("0"),
    )


def project_economics(project: IndustryProject) -> dict:
    """Cost / value / profit / margin for a project (build-to-sell view)."""
    cost = project.estimated_cost or Decimal("0")
    value = project.estimated_value or Decimal("0")
    profit = value - cost
    margin = float(profit / value * 100) if value else 0.0
    return {"cost": cost, "value": value, "profit": profit, "margin": margin}


def _make_requirement(
    item: IndustryProjectItem,
    type_id: int,
    qty: int,
    depth: int,
    force_buy: bool = False,
    available: int | None = None,
) -> Decimal:
    # ``available`` is the pool share ALLOCATED TO THIS LINE by
    # ``compute_project_bom`` (not the corp-wide free total — lines sharing a
    # material split the pool). The single-id fallback for any other caller reads
    # the unified availability authority via the stockpile wrapper.
    if available is None:
        available = available_quantity(type_id)
    to_acquire = max(0, qty - available)
    unit_cost = price_for(type_id)
    if force_buy:
        method = MaterialRequirement.AcquireMethod.BUY
    else:
        method = (
            MaterialRequirement.AcquireMethod.BUILD
            if bom.blueprint_for(type_id)
            else MaterialRequirement.AcquireMethod.BUY
        )
    MaterialRequirement.objects.create(
        project_item=item,
        type_id=type_id,
        quantity_required=qty,
        quantity_available=available,
        quantity_to_acquire=to_acquire,
        acquire_method=method,
        unit_cost=unit_cost,
        depth=depth,
    )
    return unit_cost * to_acquire


def generate_shopping_list(project: IndustryProject, location=None) -> ShoppingList:
    """Aggregate everything still to acquire (by buy) into a shopping list."""
    needed: dict[int, int] = {}
    for item in project.items.all():
        for req in item.material_requirements.filter(quantity_to_acquire__gt=0):
            needed[req.type_id] = needed.get(req.type_id, 0) + req.quantity_to_acquire

    sl = ShoppingList.objects.create(
        project=project, name=f"Shopping list — {project.name}", location=location
    )
    for type_id, qty in sorted(needed.items()):
        ShoppingListItem.objects.create(
            shopping_list=sl,
            type_id=type_id,
            quantity=qty,
            estimated_unit_price=price_for(type_id),
        )
    return sl


@transaction.atomic
def reserve_project_stock(project: IndustryProject) -> dict:
    """Earmark available corp stock for this project's materials (FIFO).

    Demand SUMS across lines — two lines needing 100 each reserve 200 (the old
    MAX under-reserved multi-line projects). IDEMPOTENT under concurrency: the
    netting against existing ACTIVE claims (and the truthful-availability cap)
    happen inside :func:`reserve_for_project_bulk` UNDER the item locks, so two
    racing Reserve POSTs serialize and the loser reserves nothing new.

    The project row is locked FIRST (global lock order: BuildJob →
    IndustryProject → StockpileItem → StockReservation) and a closed or archived
    plan reserves nothing — a claim minted on a DONE/CANCELLED/archived plan
    would be stranded forever (nothing consumes or releases it again).

    Reservations cover ``quantity_required``, not the to_acquire-netted
    remainder — a reservation means exactly "don't let others take what my BOM
    needs from stock". Returns how many units were newly reserved across how
    many material types (+ ``closed`` when the plan refused).
    """
    locked = IndustryProject.objects.select_for_update().get(pk=project.pk)
    if locked.is_archived or locked.status in (
        IndustryProject.Status.DONE, IndustryProject.Status.CANCELLED
    ):
        return {"units": 0, "types": 0, "closed": True}
    needed: dict[int, int] = {}
    for item in locked.items.all():
        for req in item.material_requirements.all():
            needed[req.type_id] = needed.get(req.type_id, 0) + req.quantity_required
    got = reserve_for_project_bulk(locked, needed)
    return {
        "units": sum(got.values()),
        "types": sum(1 for v in got.values() if v),
        "closed": False,
    }


@transaction.atomic
def release_project_stock(project: IndustryProject) -> int:
    """Release every active reservation this project holds. Returns the count.

    Locks the rows in ascending pk before the status flip — a bare bulk UPDATE
    locks in scan order, which can deadlock against the delivery path's ordered
    multi-row reservation acquisition.
    """
    from apps.stockpile.models import StockReservation

    ids = list(
        StockReservation.objects.select_for_update()
        .filter(project=project, status=StockReservation.Status.ACTIVE)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    if not ids:
        return 0
    return StockReservation.objects.filter(
        pk__in=ids, status=StockReservation.Status.ACTIVE
    ).update(status=StockReservation.Status.RELEASED)


def project_reservation_summary(project: IndustryProject) -> dict:
    """Active reservation totals for the project (for the detail header)."""
    from django.db.models import Sum

    from apps.stockpile.models import StockReservation

    active = StockReservation.objects.filter(
        project=project, status=StockReservation.Status.ACTIVE
    )
    return {
        "count": active.count(),
        "units": active.aggregate(s=Sum("quantity_reserved"))["s"] or 0,
    }


def detect_bottlenecks(project: IndustryProject, top: int = 3) -> list[dict]:
    """Materials that gate the project, ranked by acquisition cost."""
    agg: dict[int, dict] = {}
    for item in project.items.all():
        # Iterate the (detail page-prefetched) requirements and filter in Python — a
        # .filter() on the related manager would issue a fresh query per item, defeating
        # the project_detail prefetch of items__material_requirements.
        for req in item.material_requirements.all():
            if req.quantity_to_acquire <= 0:
                continue
            entry = agg.setdefault(
                req.type_id, {"type_id": req.type_id, "to_acquire": 0, "cost": Decimal("0")}
            )
            entry["to_acquire"] += req.quantity_to_acquire
            entry["cost"] += (req.unit_cost or Decimal("0")) * req.quantity_to_acquire
    ranked = sorted(agg.values(), key=lambda e: e["cost"], reverse=True)
    return ranked[:top]
