"""MRP v1 (P3) — the corp-wide net-requirements planning run.

One deterministic run explodes demand (P2 composed demand + live Shipyard
supply needs) through the BOM recipe graph, nets each type ONCE at its
structural low-level code against P1 availability and incoming supply, and
writes consolidated :class:`NetRequirement` rows — every number decomposable
into its demand (``sources``) and supply (``incoming_refs``) provenance.

Load-bearing rules (each one guards a real double-count — see the P3 plan §3.3):

* **Attribution**: supply attached to a demand object offsets that object only;
  unattached supply offsets the pooled demand of its type exactly once. At
  depth 0 MRP therefore subtracts neither fit ATP nor need-linked vehicles
  again — P2's suggestion already netted the former, the need offset covers
  the latter.
* **Low-level code**: a type demanded at several BOM depths is netted exactly
  once, at its deepest structural depth (pre-pass over the recipe graph).
  Explosion always converts the NET quantity — never gross.
* **Cascade**: an undelivered internal build (BuildJob queued/blocked/building,
  or a project BUILD line) is supply for its output type AND dependent demand
  for its components — its inputs have not been drawn from corp stock yet.
  ESI jobs are exempt: the game already consumed their inputs.
* **ESI wins**: a BuildJob whose physical build appears as a corp ESI job
  (linked via ``esi_job_id``, or conservatively heuristic-matched) counts once,
  on the ESI side, which also carries the real ``end_date``.
* **Digest stability**: nothing now-anchored enters ``required_by`` or the
  inputs digest; feasible dates are day-quantized and excluded from it, so an
  unchanged world re-runs to zero row writes.

Dates assume unconstrained industry slots (capacity is P5) and the page says so.
Units are FINISHED UNITS everywhere; blueprint runs exist only at the recipe
seam. Buy vs import is a location rule (the market feed is Jita-only): the
price-reference hub buys, everywhere else imports at Jita + freight lead.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import time as dt_time
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.industry import calc
from apps.industry.bom import (
    STRATEGY_BUILD_VS_BUY,
    _should_build,
    buildable_recipe,
    material_quantity,
)
from apps.sde.models import SdeBlueprintMaterial

from .models import IndustryProject, IndustryProjectItem, MrpConfig, MrpRun, NetRequirement

#: Machine codes with translated labels (the DECISION_LABELS discipline).
SUGGESTION_LABELS = {
    "build": _("build"),
    "buy": _("buy"),
    "import": _("import"),
}
FEASIBLE_SOURCE_LABELS = {
    "esi_job": _("in-flight ESI job"),
    "build_time": _("build duration"),
    "lead_time": _("lead time"),
    "po": _("purchase order"),
    "capacity": _("committed capacity"),
    "in_transit": _("in transit"),
    "unknown": _("unknown"),
}

_STALE_HEARTBEAT = timedelta(minutes=10)
_LIVE_STATUSES = (NetRequirement.Status.OPEN, NetRequirement.Status.IN_PROGRESS)
_INTERNAL_JOB_STATUSES = ("queued", "blocked", "building", "built")
_CASCADE_JOB_STATUSES = ("queued", "blocked", "building")  # built = inputs already burned
_ESI_ACTIVITIES = (1, 9)  # manufacturing + reactions; invention (8) is probabilistic


class MrpAlreadyRunning(Exception):
    """A planning run with a fresh heartbeat is already in flight."""


def suggestion_label(code: str):
    return SUGGESTION_LABELS.get(code, code)


# --------------------------------------------------------------------------- #
#  Single-flight claim
# --------------------------------------------------------------------------- #
def _claim_run(actor) -> MrpRun:
    """Insert THE running row, taking over a crashed run in the same transaction.

    The partial unique on ``status='running'`` makes exactly one claimant win;
    a fresh heartbeat means a live run — refuse.
    """
    now = timezone.now()
    cutoff = now - _STALE_HEARTBEAT
    try:
        with transaction.atomic():
            current = (
                MrpRun.objects.select_for_update()
                .filter(status=MrpRun.Status.RUNNING).first()
            )
            if current is not None:
                beat = current.heartbeat_at or current.started_at
                if beat >= cutoff:
                    raise MrpAlreadyRunning
                current.status = MrpRun.Status.FAILED
                current.finished_at = now
                current.save(update_fields=["status", "finished_at"])
            return MrpRun.objects.create(triggered_by=actor, heartbeat_at=now)
    except IntegrityError as exc:  # a rival claimed between our check and insert
        raise MrpAlreadyRunning from exc


def _heartbeat(run: MrpRun) -> None:
    MrpRun.objects.filter(pk=run.pk).update(heartbeat_at=timezone.now())


# --------------------------------------------------------------------------- #
#  Snapshot helpers (step 0)
# --------------------------------------------------------------------------- #
def _pinned_price(snapshot: dict):
    """A ``price_for``-compatible callable over one immutable snapshot — the
    price-cache TTL must not flip numbers mid-run."""
    jita, adjusted = snapshot["jita"], snapshot["adjusted"]

    def price(type_id: int, profile=None) -> Decimal:
        value = jita.get(type_id)
        if value is None:
            value = adjusted.get(type_id)
        return value if value is not None else Decimal("0")

    return price


def _day(dt) -> datetime | None:
    """Quantize a datetime to its (local) day — digest stability for dates."""
    if dt is None:
        return None
    local = timezone.localtime(dt)
    return timezone.make_aware(datetime.combine(local.date(), dt_time.min))


def _units_for_runs(type_id: int, runs: int) -> int:
    recipe = buildable_recipe(type_id)
    return runs * (recipe.output_quantity if recipe else 1)


@dataclass
class _Incoming:
    """One unattributed supply lot, consumed greedily by (type, location) rows."""

    kind: str            # esi_job | build_job | project_item | po_line | in_transit | received_unsynced
    ref_id: int          # job_id for ESI (stable), pk otherwise (line pk for freight — our own table)
    type_id: int
    remaining: int
    end_date: datetime | None = None
    project_id: int | None = None  # for project_item lots (self-feedback guard)
    po_id: int | None = None       # for po_line lots (self-feedback + attribution)
    # P6: destination-pinned lots (freight in-transit / received-unsynced) only cover a
    # cell at the SAME location; None-pinned lots (all other kinds) stay corp-pooled.
    location_id: int | None = None


@dataclass
class _Cell:
    """Accumulated gross demand for one (type_id, location_id) before netting."""

    type_id: int
    location_id: int | None
    gross: int = 0
    required_by: datetime | None = None
    sources: list = field(default_factory=list)
    # Ship/fit-level demand (fit manifests): NEVER netted against P1 stock —
    # the asset mirror can't distinguish loose from fitted corp assets, and fit
    # ATP already covered the assembled ones inside P2's suggestion (§3.3 step 1;
    # the loose-hull trade-off is §11's disclosed non-goal).
    ship_level: bool = False

    def add(self, qty: int, source: dict, required_by=None) -> None:
        self.gross += qty
        self.sources.append(source)
        if source.get("kind") in ("fit_demand", "supply_need"):
            self.ship_level = True
        if required_by is not None:
            if self.required_by is None or required_by < self.required_by:
                self.required_by = required_by


def _collect_incoming(config: MrpConfig) -> tuple[list[_Incoming], list[dict], set[int]]:
    """The unattributed incoming-supply pool + the cascade (dependent-demand) list.

    Returns ``(pool, cascade, esi_linked_job_pks)``. The pool is pluggable by
    design — P4 registers purchase orders here without touching the netting
    core. Everything is read once (snapshot-replace tables must not be re-read
    mid-run).
    """
    from apps.erp.models import BuildJob, CorpIndustryJob
    from apps.erp.services import suggest_esi_matches

    esi_statuses = ["active", "paused"] + (["ready"] if config.include_ready_jobs else [])
    esi_jobs = list(
        CorpIndustryJob.objects.filter(
            status__in=esi_statuses, activity_id__in=_ESI_ACTIVITIES,
            product_type_id__isnull=False,
        )
    )
    live_jobs = list(
        BuildJob.objects.filter(status__in=_INTERNAL_JOB_STATUSES).select_related("owner")
    )

    # ESI wins: explicitly linked jobs, plus conservative heuristic matches the
    # builder hasn't confirmed yet (flagged on the board) — one physical build,
    # one count.
    linked = {j.pk for j in live_jobs if j.esi_job_id}
    heuristic = suggest_esi_matches([j for j in live_jobs if not j.esi_job_id])
    matched_pks = linked | set(heuristic)

    # Project BUILD lines count only when no non-cancelled job exists for them
    # (the push_project_to_jobs dedup filter) — never once per layer.
    items = list(
        IndustryProjectItem.objects.filter(
            project__is_archived=False,
            project__status__in=(
                IndustryProject.Status.ACTIVE, IndustryProject.Status.BLOCKED,
            ),
            build_or_buy=IndustryProjectItem.BuildOrBuy.BUILD,
        ).select_related("project")
    )
    items_with_jobs = set(
        BuildJob.objects.filter(source_item__isnull=False)
        .exclude(status="cancelled")
        .values_list("source_item_id", flat=True)
    )

    pool: list[_Incoming] = []
    cascade: list[dict] = []
    for job in esi_jobs:
        pool.append(_Incoming(
            kind="esi_job", ref_id=job.job_id, type_id=job.product_type_id,
            remaining=_units_for_runs(job.product_type_id, job.runs),
            end_date=job.end_date,
        ))
    for job in live_jobs:
        if job.pk in matched_pks:
            continue  # its ESI row carries the supply and the real end date
        pool.append(_Incoming(
            kind="build_job", ref_id=job.pk, type_id=job.output_type_id,
            remaining=int(job.quantity), end_date=job.due_at,
        ))
        if job.status in _CASCADE_JOB_STATUSES:
            cascade.append({
                "kind": "vehicle", "vehicle": "build_job", "id": job.pk,
                "type_id": job.output_type_id, "qty": int(job.quantity),
                "location_id": (
                    job.deliver_to.location_id if job.deliver_to_id and job.deliver_to else None
                ),
            })
    for item in items:
        if item.pk in items_with_jobs:
            continue
        pool.append(_Incoming(
            kind="project_item", ref_id=item.pk, type_id=item.type_id,
            remaining=int(item.quantity), end_date=item.project.due_at,
            project_id=item.project_id,
        ))
        cascade.append({
            "kind": "vehicle", "vehicle": "project_item", "id": item.pk,
            "type_id": item.type_id, "qty": int(item.quantity),
            "location_id": item.project.target_location_id,
        })

    # Purchase orders (P4): each open line on a PO in a counted status is a supply
    # lot for its type, sized by what has NOT yet been received. The lot shrinks by
    # exactly what a receipt adds to the stock ledger (quantity_received), so the
    # pair can never double-count. Bought goods have no component demand — no cascade.
    from apps.procurement.models import PurchaseOrderLine
    from apps.procurement.services import COUNTED_STATUSES

    for line in (
        PurchaseOrderLine.objects.filter(po__status__in=COUNTED_STATUSES)
        .select_related("po")
    ):
        remaining = int(line.quantity_ordered) - int(line.quantity_received)
        if remaining <= 0:
            continue
        pool.append(_Incoming(
            kind="po_line", ref_id=line.pk, type_id=line.type_id,
            remaining=remaining, end_date=line.po.promised_by, po_id=line.po_id,
        ))

    # Freight in-transit (P6): every unreceipted line quantity is a destination-pinned
    # scheduled receipt (bought ≠ available, until receipted). At ESI-covered
    # destinations a received-but-unsynced lot bridges the mirror lag so the requirement
    # never reopens in the sync window. Read once, exactly where the pool is pluggable.
    # In-transit goods are supply only — no cascade (the ISK is spent, not corp materials).
    from apps.logistics.freight import in_transit

    for lot in in_transit():
        pool.append(_Incoming(
            kind=lot.kind, ref_id=lot.line_id, type_id=lot.type_id,
            remaining=lot.remaining, end_date=lot.eta, location_id=lot.destination_id,
        ))

    return pool, cascade, matched_pks


# --------------------------------------------------------------------------- #
#  Low-level codes (structural pre-pass)
# --------------------------------------------------------------------------- #
def _low_level_codes(seed_types, max_depth: int) -> dict[int, int]:
    """Each reachable type's deepest possible depth in the recipe graph.

    Structural: build/buy decisions made later only prune branches, and
    deferring a type deeper than its actual demand is harmless (its gross has
    simply finished accumulating early)."""
    llc: dict[int, int] = {}

    def walk(tid: int, depth: int, path: frozenset) -> None:
        if depth > max_depth or tid in path:
            return
        if llc.get(tid, -1) < depth:
            llc[tid] = depth
        recipe = buildable_recipe(tid)
        if recipe is None:
            return
        child_path = path | {tid}
        for mat in recipe.materials:
            walk(mat, depth + 1, child_path)

    for tid in seed_types:
        walk(int(tid), 0, frozenset())
    return llc


# --------------------------------------------------------------------------- #
#  The run
# --------------------------------------------------------------------------- #
def run_mrp(*, actor=None) -> MrpRun:
    """Execute one planning run (single-flight; raises :class:`MrpAlreadyRunning`)."""
    run = _claim_run(actor)
    try:
        stats = _execute(run)
    except Exception:
        MrpRun.objects.filter(pk=run.pk).update(
            status=MrpRun.Status.FAILED, finished_at=timezone.now(),
        )
        raise
    MrpRun.objects.filter(pk=run.pk).update(
        status=MrpRun.Status.DONE, finished_at=timezone.now(),
        stats=stats, inputs_digest=stats.get("digest", ""),
    )
    run.refresh_from_db()
    return run


def _execute(run: MrpRun) -> dict:  # noqa: PLR0915 — the run is one deliberate pipeline
    from apps.market.models import MarketLocation
    from apps.market.pricing import price_maps
    from apps.stockpile.availability import available
    from apps.store.availability import availability_for_fits
    from apps.store.demand import demand_for_fits, planning_universe
    from apps.store.models import FitSupplyNeed, ShipyardPolicy

    config = MrpConfig.active()
    now = timezone.now()
    window_end = now + timedelta(days=int(config.consolidation_window_days))
    price = _pinned_price(price_maps())

    # ---- Step 0/1: demand intake (fit grain) --------------------------------
    fits = planning_universe()
    fit_avail = availability_for_fits(fits, policy=ShipyardPolicy.active())
    # The consolidation window bounds inclusion for ALL dated demand: P2's dated
    # events (ops, manual lines) are computed against the same horizon, so a
    # line dated beyond it never enters the suggestion quantity either.
    fit_demand = demand_for_fits(
        fits, availability=fit_avail,
        horizon_days=int(config.consolidation_window_days),
    )

    needs = list(
        FitSupplyNeed.objects.filter(
            status__in=(FitSupplyNeed.Status.OPEN, FitSupplyNeed.Status.IN_PROGRESS)
        ).select_related("build_job", "industry_project", "purchase_order")
    )
    needs_by_fit: dict[int, list] = {}
    beyond_window: list[dict] = []
    attributed_job_pks: set[int] = set()
    attributed_project_pks: set[int] = set()
    attributed_po_ids: set[int] = set()

    # Beyond-window listing for manual demand lines (their quantities are already
    # excluded from P2's suggestion by the shared horizon above).
    from apps.store.models import DemandLine

    for line in DemandLine.objects.filter(
        status=DemandLine.Status.OPEN, needed_by__isnull=False,
        needed_by__gt=window_end.date(),
    ).values("pk", "fit_id", "quantity", "needed_by"):
        beyond_window.append({
            "kind": "demand_line", "id": line["pk"], "fit_id": line["fit_id"],
            "qty": int(line["quantity"]), "required_by": line["needed_by"].isoformat(),
        })

    for need in needs:
        if need.required_by and need.required_by > window_end:
            beyond_window.append({
                "kind": "supply_need", "id": need.pk, "fit_id": need.doctrine_fit_id,
                "qty": int(need.quantity_required),
                "required_by": need.required_by.isoformat(),
            })
            continue
        needs_by_fit.setdefault(need.doctrine_fit_id, []).append(need)
        # Attribution: a need-linked vehicle offsets ITS need only — never the pool.
        if need.build_job_id:
            attributed_job_pks.add(need.build_job_id)
        if need.industry_project_id:
            attributed_project_pks.add(need.industry_project_id)
        if need.purchase_order_id:
            attributed_po_ids.add(need.purchase_order_id)

    pool, cascade, _matched = _collect_incoming(config)
    # Attribution governs the SUPPLY side only: a need-linked vehicle's output is
    # removed from the pool (it offsets its need, never the pool as well) — but
    # its component demand stays in the cascade. An undelivered build still needs
    # its materials whoever it belongs to; dropping the demand half makes the
    # plan eat its own shopping list (§3.3 cascade rule, §12 trap).
    pool = [
        lot for lot in pool
        if not (lot.kind == "build_job" and lot.ref_id in attributed_job_pks)
    ]
    if attributed_project_pks:
        attributed_items = set(
            IndustryProjectItem.objects.filter(
                project_id__in=attributed_project_pks
            ).values_list("pk", flat=True)
        )
        pool = [
            lot for lot in pool
            if not (lot.kind == "project_item" and lot.ref_id in attributed_items)
        ]
    # A need-linked PO's fit-level lot leaves the pool (it offsets its own need's
    # depth-0 demand below, never the pool) — the build_job/project rule, keyed by
    # need.purchase_order_id. Without this a pooled hull lot would silently cover
    # some OTHER fit's ship-level demand (phantom coverage).
    if attributed_po_ids:
        pool = [
            lot for lot in pool
            if not (lot.kind == "po_line" and lot.po_id in attributed_po_ids)
        ]

    # ---- Step 2: explode fits to (type, location) cells ---------------------
    cells: dict[tuple[int, int | None], _Cell] = {}

    def cell(tid: int, loc_id: int | None) -> _Cell:
        key = (int(tid), loc_id)
        if key not in cells:
            cells[key] = _Cell(type_id=int(tid), location_id=loc_id)
        return cells[key]

    def manifest(fit) -> dict[int, int]:
        out: dict[int, int] = {}
        if fit.ship_type_id:
            out[fit.ship_type_id] = 1
        for module in fit.modules or []:
            tid = module.get("type_id")
            if tid:
                out[int(tid)] = out.get(int(tid), 0) + int(module.get("quantity", 1) or 1)
        return out

    # Need-linked projects' hull output, batched (one query, not one per need).
    project_hull_output: dict[tuple[int, int], int] = {}
    if attributed_project_pks:
        for pid, tid, qty in IndustryProjectItem.objects.filter(
            project_id__in=attributed_project_pks
        ).values_list("project_id", "type_id", "quantity"):
            key = (pid, tid)
            project_hull_output[key] = project_hull_output.get(key, 0) + int(qty)

    # Need-linked POs' fit-level output (remaining, in a counted status), batched.
    po_fit_output: dict[int, int] = {}
    if attributed_po_ids:
        from apps.procurement.models import PurchaseOrderLine
        from apps.procurement.services import COUNTED_STATUSES

        for po_id, ordered, received in PurchaseOrderLine.objects.filter(
            po_id__in=attributed_po_ids, doctrine_fit__isnull=False,
            po__status__in=COUNTED_STATUSES,
        ).values_list("po_id", "quantity_ordered", "quantity_received"):
            po_fit_output[po_id] = po_fit_output.get(po_id, 0) + max(0, int(ordered) - int(received))

    for fit in fits:
        d = fit_demand[fit.id]
        loc = fit_avail[fit.id].location
        loc_id = loc.pk if loc is not None else None

        # P2's suggestion already nets fit ATP and need-linked incoming — depth 0
        # subtracts neither again (attribution).
        suggestion_units = int(d.suggested_order_qty)
        need_units = 0
        earliest = None
        for need in needs_by_fit.get(fit.id, []):
            vehicle_output = 0
            if need.build_job_id and need.build_job and need.build_job.is_active:
                vehicle_output += int(need.build_job.quantity)
            if need.industry_project_id:
                vehicle_output += project_hull_output.get(
                    (need.industry_project_id, fit.ship_type_id), 0
                )
            if need.purchase_order_id:
                vehicle_output += po_fit_output.get(need.purchase_order_id, 0)
            uncovered = max(0, int(need.quantity_required) - vehicle_output)
            need_units += uncovered
            if need.required_by and (earliest is None or need.required_by < earliest):
                earliest = need.required_by
        for when, _units, _kind in d.events:
            event_dt = timezone.make_aware(datetime.combine(when, dt_time.min))
            if event_dt <= window_end and (earliest is None or event_dt < earliest):
                earliest = event_dt

        total_units = suggestion_units + need_units
        if total_units <= 0:
            continue
        for tid, per_ship in manifest(fit).items():
            c = cell(tid, loc_id)
            if suggestion_units:
                c.add(per_ship * suggestion_units,
                      {"kind": "fit_demand", "id": fit.id, "qty": per_ship * suggestion_units},
                      _day(earliest))
            if need_units:
                c.add(per_ship * need_units,
                      {"kind": "supply_need",
                       "id": needs_by_fit[fit.id][0].pk if needs_by_fit.get(fit.id) else fit.id,
                       "qty": per_ship * need_units},
                      _day(earliest))

    # Cascade: undelivered internal builds demand their components.
    for c_item in cascade:
        recipe = buildable_recipe(c_item["type_id"])
        if recipe is None:
            continue
        runs = math.ceil(c_item["qty"] / max(1, recipe.output_quantity))
        eff_me = int(config.default_me) if recipe.activity == SdeBlueprintMaterial.MANUFACTURING else 0
        for mat, base in recipe.materials.items():
            qty = material_quantity(base, runs, eff_me)
            cell(mat, c_item["location_id"]).add(
                qty, {"kind": "vehicle", "id": c_item["id"],
                      "vehicle": c_item["vehicle"], "qty": qty}, None,
            )

    # ---- Step 3: LLC pre-pass + wave netting ---------------------------------
    llc = _low_level_codes({k[0] for k in cells}, int(config.max_depth))
    pool_by_type: dict[int, list[_Incoming]] = {}
    for lot in pool:
        pool_by_type.setdefault(lot.type_id, []).append(lot)

    locations = {
        loc.pk: loc for loc in MarketLocation.objects.filter(
            pk__in={k[1] for k in cells if k[1] is not None}
        )
    }
    hub_ids = set(
        MarketLocation.objects.filter(is_price_reference=True).values_list("pk", flat=True)
    )

    # Jump-freight per unit for the import lane's landed-price comparison —
    # the forecaster's own primitives; degrades to 0 without a route/rate card.
    from apps.doctrines.hulls import hull_class_for_group
    from apps.logistics.costing import _freight_unit, _staging_hops
    from apps.logistics.services import active_rate_card
    from apps.sde.models import SdeType

    card = active_rate_card()
    hops_by_loc: dict[int, int] = {}
    freight_cache: dict[tuple[int, int], Decimal] = {}

    def freight_for(tid: int, loc_id: int) -> Decimal:
        cache_key = (tid, loc_id)
        if cache_key in freight_cache:
            return freight_cache[cache_key]
        value = Decimal("0")
        try:
            if card is not None:
                loc = locations.get(loc_id)
                if loc_id not in hops_by_loc:
                    hops_by_loc[loc_id] = _staging_hops(
                        (loc.system_id if loc else None) or 0
                    )
                hops = hops_by_loc[loc_id]
                if hops > 0:
                    group_id = SdeType.objects.filter(type_id=tid).values_list(
                        "group_id", flat=True).first()
                    hull_class = hull_class_for_group(group_id) if group_id else "Other"
                    value = _freight_unit(
                        card, hops, hull_class, price(tid),
                        packaged_vol=_packaged_volume(tid) or None,
                    )
        except Exception:  # noqa: BLE001 — freight is best-effort, never fatal
            value = Decimal("0")
        freight_cache[cache_key] = value
        return value

    visited_keys: set[tuple[int, int | None]] = set()
    row_by_key: dict[tuple[int, int | None], NetRequirement] = {}
    children_of: dict[tuple[int, int | None], list[tuple[int, int | None]]] = {}
    durations: dict[tuple[int, int | None], int | None] = {}
    counters = {"rows_written": 0, "rows_unchanged": 0, "rows_closed": 0}
    digest_items: list = []

    max_wave = max(llc.values(), default=0)
    wave = 0
    while wave <= max_wave:
        wave_keys = [k for k, c in cells.items() if llc.get(k[0], 0) == wave]
        if not wave_keys:
            wave += 1
            continue
        _heartbeat(run)

        # Batched availability per location bucket (never per type). Ship-level
        # cells never net stock (§3.3 step 1) so their types stay out of the query.
        by_loc: dict[int | None, list[int]] = {}
        for tid, loc_id in wave_keys:
            if not cells[(tid, loc_id)].ship_level:
                by_loc.setdefault(loc_id, []).append(tid)
        avail_map: dict[tuple[int, int | None], int] = {}
        credited: dict[int, int] = {}
        for loc_id in sorted([loc for loc in by_loc if loc is not None]):
            got = available(by_loc[loc_id], location=locations[loc_id])
            for tid, qty in got.items():
                avail_map[(tid, loc_id)] = qty
                credited[tid] = credited.get(tid, 0) + qty
        if None in by_loc:
            corp_wide = available(by_loc[None])
            for tid, qty in corp_wide.items():
                # Don't hand the None bucket stock already credited to a located row.
                avail_map[(tid, None)] = max(0, qty - credited.get(tid, 0))

        for key in sorted(wave_keys, key=lambda k: (k[0], k[1] if k[1] is not None else -1)):
            tid, loc_id = key
            c = cells[key]
            # Depth-0 attribution: fit ATP already netted the assembled ships
            # inside P2's suggestion, and the mirror can't tell loose from
            # fitted — ship-level cells net pooled incoming only, never stock.
            avail_qty = 0 if c.ship_level else min(c.gross, avail_map.get(key, 0))

            # Greedy exactly-once allocation of the pooled incoming.
            incoming_qty = 0
            incoming_refs: list[dict] = []
            esi_end = None
            po_end = None
            transit_end = None
            still_needed = max(0, c.gross - avail_qty)
            for lot in pool_by_type.get(tid, ()):
                if still_needed - incoming_qty <= 0:
                    break
                # Destination-pinned lots (freight in-transit / received-unsynced) cover
                # only their own lane; None-pinned lots (all other kinds) stay corp-pooled.
                if lot.location_id is not None and lot.location_id != loc_id:
                    continue
                take = min(lot.remaining, still_needed - incoming_qty)
                if take <= 0:
                    continue
                lot.remaining -= take
                incoming_qty += take
                ref = {"kind": lot.kind, "id": lot.ref_id, "qty": take}
                if lot.project_id:
                    ref["project_id"] = lot.project_id
                if lot.po_id:
                    ref["po_id"] = lot.po_id
                incoming_refs.append(ref)
                if lot.kind == "esi_job" and lot.end_date:
                    if esi_end is None or lot.end_date > esi_end:
                        esi_end = lot.end_date
                if lot.kind == "po_line" and lot.end_date:
                    if po_end is None or lot.end_date > po_end:
                        po_end = lot.end_date
                # in_transit lots carry the batch ETA; received_unsynced carry None.
                if lot.kind == "in_transit" and lot.end_date:
                    if transit_end is None or lot.end_date > transit_end:
                        transit_end = lot.end_date

            net = max(0, c.gross - avail_qty - incoming_qty)

            # Suggestion: build when the corp can and it's cheaper; else the
            # location rule decides buy vs import (the market feed is Jita-only —
            # there is no local price to compare). At a non-hub location the buy
            # side of the comparison is LANDED: Jita + jump freight (§3.3);
            # freight degrades to 0 when no route/rate card exists.
            recipe = buildable_recipe(tid)
            at_hub = loc_id is None or loc_id in hub_ids
            freight = Decimal("0") if at_hub else freight_for(tid, loc_id)
            if (
                net > 0 and recipe is not None
                and _should_build(
                    tid, net, recipe, STRATEGY_BUILD_VS_BUY,
                    int(config.default_me),
                    price=(lambda t, _f=freight, _tid=tid: price(t) + (_f if t == _tid else Decimal("0"))),
                )
            ):
                suggestion = "build"
            elif at_hub:
                suggestion = "buy"
            else:
                suggestion = "import"

            # Own duration (for feasible + required_by offsetting).
            duration = None
            if recipe is not None:
                runs = math.ceil(max(1, net or c.gross) / max(1, recipe.output_quantity))
                if recipe.activity == SdeBlueprintMaterial.MANUFACTURING:
                    duration = calc.production_seconds(tid, runs, te=0)
                else:
                    duration = calc.reaction_seconds(tid, runs)
            durations[key] = duration

            # Write the parent row BEFORE exploding, so children can carry its
            # real pk in provenance from the start — a post-pass backfill would
            # make every re-run rewrite child sources (id None → pk → None…).
            row = _write_row(
                run=run, key=key, cell_data=c, avail_qty=avail_qty,
                incoming_qty=incoming_qty, incoming_refs=incoming_refs, net=net,
                suggestion=suggestion, depth=wave, esi_end=esi_end,
                location=locations.get(loc_id), counters=counters, po_end=po_end,
                transit_end=transit_end,
            )
            row_by_key[key] = row
            visited_keys.add(key)
            digest_items.append((
                tid, loc_id, c.gross, avail_qty, incoming_qty,
                c.required_by.isoformat() if c.required_by else "",
                suggestion,
            ))

            # Explode NET (never gross) one level when building.
            if net > 0 and suggestion == "build" and wave < int(config.max_depth):
                runs = math.ceil(net / max(1, recipe.output_quantity))
                eff_me = (
                    int(config.default_me)
                    if recipe.activity == SdeBlueprintMaterial.MANUFACTURING else 0
                )
                child_required = None
                if c.required_by is not None:
                    offset = timedelta(seconds=duration) if duration else timedelta(0)
                    child_required = _day(c.required_by - offset)
                for mat, base in recipe.materials.items():
                    qty = material_quantity(base, runs, eff_me)
                    child = cell(mat, loc_id)  # components inherit the location
                    child.add(qty, {"kind": "parent", "id": row.pk, "qty": qty,
                                    "parent_type_id": tid}, child_required)
                    children_of.setdefault(key, []).append((int(mat), loc_id))
                    if llc.get(int(mat), 0) <= wave:
                        # Structural LLC should prevent this; guard anyway.
                        llc[int(mat)] = wave + 1
                max_wave = max(max_wave, max(
                    llc.get(int(m), wave + 1) for m in recipe.materials
                ))
        wave += 1

    # ---- Step 4: feasible dates (children first — deepest wave upward) -------
    _apply_feasible_dates(row_by_key, children_of, durations, config, now, counters, run=run)

    # ---- Step 4b: reconcile linked vehicles (self-feedback guard) -------------
    _reconcile_vehicles(row_by_key, counters)

    # ---- Step 5: stale sweep + digest ----------------------------------------
    _sweep_stale_rows(run, visited_keys, counters)

    digest = hashlib.sha256(json.dumps({
        # None location sorts as -1 — a bare sorted() would TypeError on a
        # (tid, None) vs (tid, loc) tie.
        "cells": sorted(
            digest_items,
            key=lambda t: (t[0], t[1] if t[1] is not None else -1),
        ),
        "config": [int(config.consolidation_window_days), int(config.buy_lead_days),
                   int(config.import_lead_days), bool(config.include_ready_jobs),
                   int(config.default_me), int(config.max_depth)],
    }, default=str, sort_keys=True).encode()).hexdigest()

    return {
        "digest": digest,
        "fits": len(fits),
        "needs": len(needs),
        "cells": len(cells),
        "beyond_window": beyond_window,
        **counters,
    }


def _write_row(*, run, key, cell_data, avail_qty, incoming_qty, incoming_refs, net,
               suggestion, depth, esi_end, location, counters, po_end=None,
               transit_end=None) -> NetRequirement:
    """Create-or-update the live row for (type, location) — the
    ``recompute_supply_need`` locking pattern, value-compare before every write."""
    tid, loc_id = key
    with transaction.atomic():
        live = (
            NetRequirement.objects.select_for_update()
            .filter(type_id=tid, location_id=loc_id, status__in=_LIVE_STATUSES)
            .first()
        )
        if live is None:
            try:
                with transaction.atomic():
                    live = NetRequirement.objects.create(
                        type_id=tid, location_id=loc_id, last_run=run,
                    )
            except IntegrityError:
                live = (
                    NetRequirement.objects.select_for_update()
                    .filter(type_id=tid, location_id=loc_id, status__in=_LIVE_STATUSES)
                    .first()
                ) or NetRequirement.objects.create(
                    type_id=tid, location_id=loc_id, last_run=run,
                )
            # No counter bump here: the value-compare below counts the fresh
            # row's first real write exactly once.

        new_status = live.status
        if net <= 0 and not live.has_vehicle:
            new_status = NetRequirement.Status.DONE
        elif live.has_vehicle:
            new_status = NetRequirement.Status.IN_PROGRESS
        elif live.status == NetRequirement.Status.IN_PROGRESS and not live.has_vehicle:
            new_status = NetRequirement.Status.OPEN

        new_values = {
            "gross_quantity": int(cell_data.gross),
            "available_quantity": int(avail_qty),
            "incoming_quantity": int(incoming_qty),
            "net_quantity": int(net),
            "required_by": cell_data.required_by,
            "suggestion": suggestion,
            "depth": int(depth),
            "sources": cell_data.sources,
            "incoming_refs": incoming_refs,
            "status": new_status,
        }
        changed = [f for f, v in new_values.items() if getattr(live, f) != v]
        if changed:
            for f in changed:
                setattr(live, f, new_values[f])
            live.last_run = run
            live.save(update_fields=[*changed, "last_run", "updated_at"])
            counters["rows_written"] += 1
        else:
            counters["rows_unchanged"] += 1
        # Stash for the feasible pass (no extra reads).
        live._esi_end = esi_end
        live._po_end = po_end
        live._transit_end = transit_end
        return live


def _child_feasible_max(child_keys, row_by_key):
    """Latest feasible date among a row's still-open children (net > 0, dated) — the
    P3 child-max propagation, shared by the P3 build branch and the capacity pass."""
    child_max = None
    for child_key in child_keys:
        child = row_by_key.get(child_key)
        if child is not None and child.net_quantity > 0 and child.feasible_at:
            if child_max is None or child.feasible_at > child_max:
                child_max = child.feasible_at
    return child_max


def _any_child_refused(child_keys, row_by_key):
    """True if any still-open child was REFUSED by the capacity pass this sweep
    (net > 0, no feasible date, ``feasible_source="capacity"``). A parent build that
    consumes a component with no honest date cannot be honestly promised — this is
    read only on the capacity branch, so the P3 inert path is unaffected."""
    for child_key in child_keys:
        child = row_by_key.get(child_key)
        if (
            child is not None and child.net_quantity > 0
            and child.feasible_at is None and child.feasible_source == "capacity"
        ):
            return True
    return False


def _apply_feasible_dates(row_by_key, children_of, durations, config, now, counters,
                          run=None) -> None:
    """Deepest wave first, so a parent can look at its children's feasible dates.

    With ``config.capacity_enabled`` OFF this is the P3 unconstrained-slots pass,
    byte-identical (the ``build`` branch below). ON, the ``build`` branch and every
    own-vehicle row instead take their date from the finite-capacity scheduler
    (:func:`apps.industry.capacity.schedule_feasible`), which may hold the date or
    refuse it (``feasible_at=None``) and name the binding ``bottleneck_code``.
    Day-quantized and excluded from the digest — a same-day re-run writes nothing.
    """
    scheduler = None
    if config.capacity_enabled:
        from apps.industry.capacity import CapacityScheduler, derive_resources

        if run is not None:
            _heartbeat(run)
        # Refresh the resource ledger from current skills BEFORE any NetRequirement
        # row is touched (its own writes; never inside a _write_row txn) — the officer
        # "Re-derive now" POST calls the same idempotent path.
        derive_resources(config)
        scheduler = CapacityScheduler(config, now, row_by_key)

    processed = 0
    for key, row in sorted(
        row_by_key.items(),
        key=lambda kv: (-kv[1].depth, kv[1].type_id,
                        kv[1].location_id if kv[1].location_id is not None else -1),
    ):
        feasible = None
        source = "unknown"
        bottleneck = ""
        esi_end = getattr(row, "_esi_end", None)
        po_end = getattr(row, "_po_end", None)
        transit_end = getattr(row, "_transit_end", None)
        if row.net_quantity <= 0 and esi_end is not None:
            feasible, source = _day(esi_end), "esi_job"
        elif row.net_quantity <= 0 and po_end is not None:
            # A PO covers this requirement — show its promised date, not a guess.
            feasible, source = _day(po_end), "po"
        elif row.net_quantity <= 0 and transit_end is not None:
            # A freight batch covers this requirement — show its ETA (the batch's
            # honest arrival), not the flat import lead time. 10 chars, fits max_length=12.
            feasible, source = _day(transit_end), "in_transit"
        elif scheduler is not None and scheduler.owns_row(row):
            # Capacity owns every build row and every covered own-vehicle row when
            # armed. ``child_max``/``child_refused`` read children already written this
            # sweep (deepest first), exactly as the P3 build branch does below.
            children = children_of.get(key, ())
            child_max = _child_feasible_max(children, row_by_key)
            child_refused = _any_child_refused(children, row_by_key)
            feasible, source, bottleneck = scheduler.schedule_row(
                row, child_max, durations.get(key), child_refused
            )
        elif row.net_quantity > 0:
            if row.suggestion == "build":
                duration = durations.get(key)
                if duration is not None:
                    # Anchor on the quantized day, not the wall clock — a
                    # same-day re-run must not flip the feasible day just
                    # because now+duration crossed midnight later in the day.
                    base = _day(now) + timedelta(seconds=duration)
                    child_max = _child_feasible_max(children_of.get(key, ()), row_by_key)
                    if child_max is not None:
                        base += (child_max - _day(now))
                    feasible, source = _day(base), "build_time"
            elif row.suggestion == "buy":
                feasible = _day(now + timedelta(days=int(config.buy_lead_days)))
                source = "lead_time"
            elif row.suggestion == "import":
                feasible = _day(now + timedelta(days=int(config.import_lead_days)))
                source = "lead_time"
        # Compare-before-write. When capacity is OFF, ``bottleneck`` is always ""
        # and every row's stored code is already "" — so this reduces byte-for-byte
        # to the P3 save (feasible_at + feasible_source), the inert guarantee.
        feasible_changed = row.feasible_at != feasible or row.feasible_source != source
        bottleneck_changed = row.bottleneck_code != bottleneck
        if feasible_changed or bottleneck_changed:
            row.feasible_at = feasible
            row.feasible_source = source
            fields = ["feasible_at", "feasible_source", "updated_at"]
            if bottleneck_changed:
                row.bottleneck_code = bottleneck
                fields.append("bottleneck_code")
            row.save(update_fields=fields)
        processed += 1
        if run is not None and processed % 200 == 0:
            _heartbeat(run)


def _sweep_stale_rows(run: MrpRun, visited_keys, counters) -> None:
    """Zero-and-close every live row this run did not visit (its demand vanished).

    Vehicle-linked rows are zeroed, kept IN_PROGRESS and flagged ``diverged``
    (the vehicle says one thing, the plan another) — never silently deleted."""
    for row in NetRequirement.objects.filter(status__in=_LIVE_STATUSES):
        if (row.type_id, row.location_id) in visited_keys:
            continue
        with transaction.atomic():
            locked = NetRequirement.objects.select_for_update().get(pk=row.pk)
            if (locked.type_id, locked.location_id) in visited_keys:
                continue
            locked.gross_quantity = 0
            locked.net_quantity = 0
            locked.incoming_quantity = 0
            locked.available_quantity = 0
            if locked.has_vehicle:
                locked.status = NetRequirement.Status.IN_PROGRESS
                locked.diverged = True
            else:
                locked.status = NetRequirement.Status.DONE
            locked.last_run = run
            locked.save(update_fields=[
                "gross_quantity", "net_quantity", "incoming_quantity",
                "available_quantity", "status", "diverged", "last_run", "updated_at",
            ])
            counters["rows_closed"] += 1


# --------------------------------------------------------------------------- #
#  Reconciliation (runs inside every planning pass)
# --------------------------------------------------------------------------- #
def _own_vehicle_output(row: NetRequirement) -> int:
    """Units of the row's OWN linked vehicles that were counted in incoming.

    The self-feedback guard: a fanned-out vehicle's output collapses the row's
    displayed net toward 0 while the work is merely promised — the vehicle
    TARGET must exclude it, or every re-run would shrink the vehicle toward 0.
    """
    own = 0
    freight_own = 0
    for ref in row.incoming_refs or []:
        kind = ref.get("kind")
        if kind == "build_job" and ref.get("id") == row.build_job_id:
            own += int(ref.get("qty", 0))
        elif (
            kind == "project_item"
            and row.industry_project_id
            and ref.get("project_id") == row.industry_project_id
        ):
            own += int(ref.get("qty", 0))
        elif (
            kind == "po_line"
            and row.purchase_order_id
            and ref.get("po_id") == row.purchase_order_id
        ):
            own += int(ref.get("qty", 0))
        elif (
            kind in ("in_transit", "received_unsynced")
            and row.freight_line_id
            and ref.get("id") == row.freight_line_id
        ):
            freight_own += int(ref.get("qty", 0))
    if freight_own and row.freight_line_id:
        # A consolidated freight line also carries officer-typed units (third-party
        # supply); only the MRP-attributed planned share is this row's own promised
        # output. Crediting the whole merged lot would inflate _vehicle_target.
        from apps.logistics.models import FreightBatchLine

        planned = FreightBatchLine.objects.filter(pk=row.freight_line_id).values_list(
            "planned_quantity", flat=True).first() or 0
        own += min(freight_own, int(planned))
    return own


def _vehicle_target(row: NetRequirement) -> int:
    """The quantity a linked vehicle should carry: net with own output excluded."""
    return max(0, int(row.net_quantity) + _own_vehicle_output(row))


def _reconcile_vehicles(row_by_key, counters) -> None:
    """Refresh unclaimed linked vehicles in place; flag claimed-but-diverged ones.

    The drift gap ``push_project_to_jobs`` has: a vehicle minted at 40 while the
    plan now says 25 either silently diverges (claimed — someone is building 40)
    or should just say 25 (unclaimed — nothing human is attached yet).
    """
    from apps.erp.models import BuildJob
    from apps.stockpile.models import HaulingTask
    from apps.tasks.models import Task
    from core.audit import audit_log

    for row in row_by_key.values():
        if not row.has_vehicle:
            if row.diverged:
                row.diverged = False
                row.save(update_fields=["diverged", "updated_at"])
            continue
        target = _vehicle_target(row)
        diverged = False

        if row.build_job_id:
            job = BuildJob.objects.filter(pk=row.build_job_id).first()
            if job is None or job.status in ("delivered", "cancelled"):
                # Terminal or deleted: release the slot so the row can close
                # (delivered stock clears the gross side next run) and a NEW
                # shortfall can fan out again (§3.5).
                row.build_job = None
                row.save(update_fields=["build_job", "updated_at"])
            elif job.status in ("queued", "blocked") and job.owner_id is None:
                if job.quantity != target and target > 0:
                    from apps.erp.services import update_quantity

                    update_quantity(job, target, note=job.note)
                    audit_log(None, "industry.mrp.vehicle_refresh",
                              target_type="build_job", target_id=str(job.pk),
                              metadata={"requirement": row.pk, "quantity": target})
            elif job.is_active and job.quantity != target:
                diverged = True

        if row.hauling_task_id:
            haul = HaulingTask.objects.filter(pk=row.hauling_task_id).first()
            if haul is None or haul.status == HaulingTask.Status.DONE:
                row.hauling_task = None
                row.save(update_fields=["hauling_task", "updated_at"])
            elif haul.status == HaulingTask.Status.OPEN:
                if (haul.quantity or 0) != target and target > 0:
                    haul.quantity = target
                    haul.volume_m3 = _packaged_volume(row.type_id) * target
                    haul.save(update_fields=["quantity", "volume_m3"])
                    audit_log(None, "industry.mrp.vehicle_refresh",
                              target_type="hauling_task", target_id=str(haul.pk),
                              metadata={"requirement": row.pk, "quantity": target})
            elif (haul.quantity or 0) != target:
                diverged = True

        if row.freight_line_id:
            from apps.logistics.models import FreightBatch, FreightBatchLine

            # A freight line has two writers (officer line ops + this reconcile) and the
            # planned-share refresh is a read-modify-write on `quantity`. Lock the batch
            # then the line (the global FreightBatch → FreightBatchLine order) and re-read
            # the CURRENT quantity/cost/received under the lock, so a concurrent officer
            # edit can never be clobbered by a stale delta (the two-writers-one-row rule).
            with transaction.atomic():
                # Fetch the (immutable) batch id unlocked, then acquire the locks in the
                # global order: FreightBatch first, then the line. A line deleted in the
                # gap leaves ``locked`` None → the FK-release branch below.
                batch_id = (
                    FreightBatchLine.objects.filter(pk=row.freight_line_id)
                    .values_list("batch_id", flat=True).first()
                )
                locked = None
                if batch_id is not None:
                    FreightBatch.objects.select_for_update().filter(pk=batch_id).first()
                    locked = (
                        FreightBatchLine.objects.select_for_update()
                        .select_related("batch").filter(pk=row.freight_line_id).first()
                    )
                if (
                    locked is None
                    or locked.batch.status in (
                        FreightBatch.Status.CLOSED, FreightBatch.Status.CANCELLED,
                    )
                    or int(locked.quantity_received) >= int(locked.quantity)
                ):
                    # Line gone, batch terminal, or fully received → release the FK so the
                    # row can close / re-offer fan-out. Safe at covered destinations: the
                    # received_unsynced lot holds net at 0 until the mirror syncs, so the
                    # row never re-offers fan-out in the window.
                    row.freight_line = None
                    row.save(update_fields=["freight_line", "updated_at"])
                elif (
                    locked.batch.status == FreightBatch.Status.OPEN
                    and locked.unit_purchase_cost is None
                    and int(locked.quantity_received) == 0
                ):
                    # Unclaimed line (batch OPEN, cost untyped, nothing received): refresh
                    # ONLY the MRP-attributed planned share off the LOCKED quantity.
                    # Officer-typed units (quantity − planned_quantity) are untouched.
                    if int(locked.planned_quantity) != target:
                        new_quantity = int(locked.quantity) + (target - int(locked.planned_quantity))
                        if target > 0 and new_quantity >= 1:
                            locked.quantity = new_quantity
                            locked.planned_quantity = target
                            locked.save(update_fields=["quantity", "planned_quantity"])
                            audit_log(None, "industry.mrp.vehicle_refresh",
                                      target_type="freight_line", target_id=str(locked.pk),
                                      metadata={"requirement": row.pk, "quantity": target})
                elif int(locked.planned_quantity) != target:
                    # Claimed line (cost typed / partial receipt) or batch past OPEN with a
                    # planned-share mismatch → flag; never auto-shrink a real purchase.
                    diverged = True

        if row.task_id:
            task = Task.objects.filter(pk=row.task_id).first()
            active_task = task is not None and task.status in (
                Task.Status.OPEN, Task.Status.CLAIMED, Task.Status.IN_PROGRESS,
            )
            if not active_task:
                # Done/cancelled/deleted: the intended tasks-app semantics —
                # a new shortfall gets a new task through the shared factory.
                row.task = None
                row.save(update_fields=["task", "updated_at"])
            else:
                from apps.erp.messages import english_text
                from apps.sde.models import SdeType

                name = SdeType.objects.filter(type_id=row.type_id).values_list(
                    "name", flat=True).first() or str(row.type_id)
                expected = english_text(
                    "task.mrp_buy", {"quantity": max(1, target), "item": name}
                )[:200]
                if task.status == Task.Status.OPEN and task.assignee_id is None:
                    if task.title != expected and target > 0:
                        task.title = expected
                        task.save(update_fields=["title", "updated_at"])
                        audit_log(None, "industry.mrp.vehicle_refresh",
                                  target_type="task", target_id=str(task.pk),
                                  metadata={"requirement": row.pk, "quantity": target})
                elif task.title != expected:
                    diverged = True

        if row.industry_project_id:
            project = IndustryProject.objects.filter(pk=row.industry_project_id).first()
            if project is None or project.is_archived or project.status in (
                IndustryProject.Status.DONE, IndustryProject.Status.CANCELLED,
            ):
                row.industry_project = None
                row.save(update_fields=["industry_project", "updated_at"])

        if row.purchase_order_id:
            from apps.procurement import services as _proc

            # DRAFT: refresh the single line to the MOQ-rounded target (audited).
            # SUBMITTED+: a committed PO that drifted is flagged, never silently
            # rewritten. Terminal (cancelled/reconciled): release the FK so a new
            # shortfall can fan out again.
            outcome = _proc.reconcile_mrp_po(row.purchase_order_id, target)
            if outcome == "released":
                row.purchase_order = None
                row.save(update_fields=["purchase_order", "updated_at"])
            elif outcome == "diverged":
                diverged = True

        if row.diverged != diverged:
            row.diverged = diverged
            row.save(update_fields=["diverged", "updated_at"])


# --------------------------------------------------------------------------- #
#  Fan-out (officer-clicked, idempotent, vehicle-FK guarded)
# --------------------------------------------------------------------------- #
def _packaged_volume(type_id: int) -> float:
    """Packaged m³ — NEVER the assembled volume (a Rifter is 27,289 m³ assembled
    vs 2,500 m³ packaged; hauls move packaged hulls).

    Thin delegator to the :mod:`apps.logistics.costing` authority (kept as a name so
    ``apps.procurement.receipts`` and the reconcile path import it unchanged)."""
    from apps.logistics.costing import packaged_volume

    return packaged_volume(type_id)


def _corp_stockpile_at(location_id: int | None):
    from apps.stockpile.models import Stockpile

    if location_id is None:
        return None
    return Stockpile.objects.filter(
        kind=Stockpile.Kind.CORP, location_id=location_id
    ).order_by("pk").first()


@transaction.atomic
def create_project_for_requirement(requirement: NetRequirement, *, actor) -> IndustryProject:
    """Fan a build requirement out to an Industry Project (idempotent) and land
    it BOM-exploded (unlike the store fan-out, which skips the compute)."""
    from apps.erp.messages import english_text
    from apps.sde.models import SdeType

    from .services import compute_project_bom

    locked = NetRequirement.objects.select_for_update().get(pk=requirement.pk)
    if locked.industry_project_id:
        return locked.industry_project
    target = _vehicle_target(locked)
    name = SdeType.objects.filter(type_id=locked.type_id).values_list(
        "name", flat=True).first() or str(locked.type_id)
    params = {"item": name, "quantity": target}
    project = IndustryProject.objects.create(
        # Seam B: pinned English (the store's gettext-at-creation freezes the
        # creator's locale — a known quirk MRP does not copy).
        name=english_text("plan.mrp_requirement", params)[:200],
        objective_type=IndustryProject.Objective.STOCK,
        status=IndustryProject.Status.ACTIVE,
        source=IndustryProject.Source.MRP,
        target_location=locked.location,
        created_by=actor,
        due_at=locked.required_by,
    )
    IndustryProjectItem.objects.create(
        project=project, type_id=locked.type_id, product_name=name,
        quantity=max(1, target),
        build_or_buy=IndustryProjectItem.BuildOrBuy.BUILD,
        me=MrpConfig.active().default_me,
    )
    compute_project_bom(project)
    locked.industry_project = project
    locked.status = NetRequirement.Status.IN_PROGRESS
    locked.save(update_fields=["industry_project", "status", "updated_at"])
    return project


@transaction.atomic
def create_build_job_for_requirement(requirement: NetRequirement, *, actor):
    """Fan a single-type build requirement out to an ERP BuildJob (idempotent)."""
    from apps.erp.messages import english_text
    from apps.erp.models import BuildJob
    from apps.sde.models import SdeType

    locked = NetRequirement.objects.select_for_update().get(pk=requirement.pk)
    if locked.build_job_id:
        return locked.build_job
    target = _vehicle_target(locked)
    name = SdeType.objects.filter(type_id=locked.type_id).values_list(
        "name", flat=True).first() or str(locked.type_id)
    note_params = {"item": name}
    job = BuildJob.objects.create(
        output_type_id=locked.type_id,
        quantity=max(1, target),
        status=BuildJob.Status.QUEUED,
        deliver_to=_corp_stockpile_at(locked.location_id),
        due_at=locked.required_by,
        created_by=actor,
        note=english_text("job.mrp_restock", note_params)[:200],
        note_key="job.mrp_restock",
        note_params=note_params,
    )
    locked.build_job = job
    locked.status = NetRequirement.Status.IN_PROGRESS
    locked.save(update_fields=["build_job", "status", "updated_at"])
    return job


@transaction.atomic
def create_hauling_task_for_requirement(requirement: NetRequirement, *, actor):
    """Fan an import requirement out to a HaulingTask (idempotent via the FK —
    deliberately NOT generate_hauling_tasks, whose demand source is stockpile
    targets and which double-counts across routes)."""
    from apps.market.models import MarketLocation
    from apps.stockpile.models import HaulingTask

    locked = NetRequirement.objects.select_for_update().get(pk=requirement.pk)
    if locked.hauling_task_id:
        return locked.hauling_task
    target = _vehicle_target(locked)
    source = MarketLocation.objects.filter(is_price_reference=True).order_by("pk").first()
    haul = HaulingTask.objects.create(
        type_id=locked.type_id,
        quantity=max(1, target),
        volume_m3=_packaged_volume(locked.type_id) * max(1, target),
        source_location=source,
        dest_location=locked.location,
        status=HaulingTask.Status.OPEN,
    )
    locked.hauling_task = haul
    locked.status = NetRequirement.Status.IN_PROGRESS
    locked.save(update_fields=["hauling_task", "status", "updated_at"])
    return haul


@transaction.atomic
def create_buy_task_for_requirement(requirement: NetRequirement, *, actor):
    """Fan a buy requirement out to a claimable BUY task via the shared
    ``create_task`` factory (ADR-0006) — its active-status dedup is the second
    guard behind the vehicle FK."""
    from apps.erp.messages import english_text
    from apps.sde.models import SdeType
    from apps.tasks.models import Task
    from apps.tasks.services import create_task

    locked = NetRequirement.objects.select_for_update().get(pk=requirement.pk)
    if locked.task_id:
        return locked.task
    target = _vehicle_target(locked)
    name = SdeType.objects.filter(type_id=locked.type_id).values_list(
        "name", flat=True).first() or str(locked.type_id)
    task = create_task(
        task_type=Task.Type.BUY,
        title=english_text("task.mrp_buy", {"quantity": max(1, target), "item": name})[:200],
        priority=7,
        due_at=locked.required_by,
        related_type="net_requirement",
        related_id=str(locked.pk),
        created_by=actor,
    )
    locked.task = task
    locked.status = NetRequirement.Status.IN_PROGRESS
    locked.save(update_fields=["task", "status", "updated_at"])
    return task


@transaction.atomic
def create_purchase_order_for_requirement(requirement: NetRequirement, *, actor, supplier):
    """Fan a buy/import requirement out to a DRAFT purchase order (idempotent via
    the FK). A second officer action beside the BUY task — the board offers it first
    when an active SupplierItem covers the type. The officer then submits/approves."""
    from apps.procurement.services import create_draft_po
    from apps.sde.models import SdeType

    locked = NetRequirement.objects.select_for_update().get(pk=requirement.pk)
    if locked.purchase_order_id:
        return locked.purchase_order
    target = _vehicle_target(locked)
    name = SdeType.objects.filter(type_id=locked.type_id).values_list(
        "name", flat=True).first() or str(locked.type_id)
    po = create_draft_po(
        supplier=supplier, location=locked.location, actor=actor,
        lines=[{"type_id": locked.type_id, "quantity": max(1, target)}],
        note_key="po.mrp_source", note_params={"item": name},
    )
    locked.purchase_order = po
    locked.status = NetRequirement.Status.IN_PROGRESS
    locked.save(update_fields=["purchase_order", "status", "updated_at"])
    return po
