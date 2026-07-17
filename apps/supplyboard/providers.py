"""Supply Command board — the provider registry, contract, and v1 built-ins.

A provider reads ONE phase's persisted authority and returns a :class:`BoardSection` of
:class:`BoardRow`s. Contract (normative for every provider, including v1's):

* persisted rows only — no live ESI, no per-row pricing;
* bounded queries (constant per provider, never per-row);
* every row carries a ``url`` deep link + an ``action_key`` naming the clearing action
  (no metric without an action — a violation is a test failure);
* one owner per family — re-registering an existing key raises;
* a provider consults its own phase's knobs, never another phase's math.

Row labels/actions/section titles are machine keys resolved to translated strings at
SERVE time (under the reader's locale) via the maps below — the cached payload holds only
raw data (codes, ids, EVE names, numbers), never a ``gettext_lazy`` proxy. Severity is a
machine code; RED means an already-breached condition (overdue / past-due / below a hard
threshold) that flips at most once and persists, so the digest's red-key problem set is
stable across same-day sweeps without extra bookkeeping. AMBER means approaching (within a
window) and is never a digest problem key.
"""
from __future__ import annotations

import contextvars
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from django.db.models import F
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

log = logging.getLogger("forca.supplyboard")

_MAX_DISPLAY = 15       # rows shown per section (reds always kept; ambers/infos fill up)
_FETCH_CAP = 200        # per-family memory bound on rows loaded
_SEV_RANK = {"red": 0, "amber": 1, "info": 2}

# Only ISK-bearing families are director-gated; everything else is officer-safe.
SECTION_ROLES = {"margin": "director"}


def _role_for(key: str) -> str:
    return SECTION_ROLES.get(key, "officer")


@dataclass(frozen=True)
class BoardRow:
    key: str          # stable machine key, e.g. "order:1234" (identity for the digest)
    severity: str     # "red" | "amber" | "info"
    label_key: str    # ROW_LABELS key (a gettext format string)
    params: dict      # raw values for interpolation — never pre-translated
    url: str          # the console that fixes it (reverse()'d)
    action_key: str   # ACTION_LABELS key naming the clearing action


@dataclass(frozen=True)
class BoardSection:
    key: str
    title_key: str
    role: str                    # "officer" | "director" (server-side gate)
    rows: list                   # bounded (reds first, then top ambers/infos)
    total: int                   # full count behind the bound
    source_url: str              # the owning console ("see all")
    freshness: object | None     # owning data's last-sync stamp (datetime | None)


REGISTRY: dict[str, Callable] = {}


def register(key: str, fn: Callable) -> None:
    """Register a section provider. One owner per key — re-registering raises."""
    if key in REGISTRY:
        raise ValueError(f"board provider for {key!r} is already registered")
    REGISTRY[key] = fn


# --- the P6 supersession hook (built + tested now, empty in v1) ----------------
# A HaulingTask linked to a P6 FreightBatch is the richer object; when P6 lands it appends
# a source here that returns those task ids, and the in-transit provider drops them (one
# shipment, one row). v1 registers no source, so the family is unfiltered.
HAUL_SUPPRESSION_SOURCES: list[Callable[[], set]] = []


def suppressed_haul_task_ids() -> set:
    ids: set = set()
    for src in HAUL_SUPPRESSION_SOURCES:
        try:
            ids |= set(src())
        except Exception:  # noqa: BLE001 — a suppression source must not break the board
            log.exception("haul-suppression source failed")
    return ids


# --- shared inventory rows (memoised across the fit-based providers per build) --
_INVENTORY_ROWS: contextvars.ContextVar = contextvars.ContextVar(
    "supplyboard_inventory_rows", default=None
)


def _inventory_rows_shared():
    cached = _INVENTORY_ROWS.get()
    if cached is not None:
        return cached
    from apps.store.models import ShipyardPolicy
    from apps.store.views_inventory import inventory_rows

    return inventory_rows(ShipyardPolicy.active())


# --------------------------------------------------------------------------- #
#  Render maps — machine key → gettext format string (resolved at serve time)
# --------------------------------------------------------------------------- #

SECTION_TITLES = {
    "readiness": _("Doctrine readiness & low stock"),
    "orders": _("At-risk & overdue orders"),
    "shortages": _("Material shortages"),
    "commitments": _("Commitments due & late"),
    "bottlenecks": _("Production bottlenecks"),
    "in_transit": _("In-transit hauls"),
    "discrepancies": _("Stock discrepancies"),
    "obsolete": _("Obsolete & slow-moving stock"),
    "margin": _("Margin erosion & drifted quotes"),
}

ROW_LABELS = {
    "readiness.reorder": _("%(fit)s is at %(atp)s — at or below its reorder point %(reorder)s"),
    "readiness.safety": _("%(fit)s is at %(atp)s — below its safety stock %(safety)s"),
    "readiness.suggested": _("%(fit)s is at %(atp)s — below the suggested reorder point %(reorder)s"),
    "readiness.no_location": _("%(fit)s has stock but no delivery location set"),
    "readiness.stale": _("%(fit)s has stock stranded on an old fit revision"),
    "orders.overdue": _("%(ship)s order is overdue (was due %(eta)s)"),
    "orders.at_risk": _("%(ship)s order is due within %(days)s day(s) (%(eta)s)"),
    "shortages.overdue": _("%(item)s — %(qty)s short, needed %(due)s"),
    "shortages.net": _("%(item)s — %(qty)s short"),
    "commitments.need_overdue": _("%(fit)s restock is overdue (was due %(due)s)"),
    "commitments.need_due": _("%(fit)s restock is due %(due)s"),
    "bottlenecks.late": _("%(item)s cannot be ready in time — feasible %(feasible)s, needed %(due)s"),
    "in_transit.stalled": _("Haul %(route)s is unclaimed and stalled"),
    "in_transit.active": _("Haul %(route)s is in progress"),
    "discrepancies.stale_reconcile": _("%(fit)s stock has not been reconciled in %(days)s+ days"),
    "discrepancies.over_reserved": _("%(item)s is over-reserved by %(qty)s"),
    "obsolete.obsolete": _("%(fit)s is obsolete but still stocked"),
    "obsolete.slow_mover": _("%(fit)s is a slow mover"),
    "margin.low": _("Evidenced margin is %(pct)s over the last %(days)s days"),
    "margin.drift": _("%(ship)s quote drifted %(pct)s from its basis"),
}

ACTION_LABELS = {
    "receipt_or_fanout": _("Receipt stock or fan out the need"),
    "advance_order": _("Advance or revise the ETA"),
    "fan_out_vehicle": _("Fan out a supply vehicle"),
    "refresh_vehicle": _("Mint or refresh the vehicle"),
    "expedite": _("Expedite — fan out or adjust dates"),
    "manage_haul": _("Claim, complete or cancel the haul"),
    "reconcile": _("Revalidate stock or review reconciliation"),
    "retire_offer": _("Retire the offer or redistribute stock"),
    "review_margin": _("Acknowledge drift or record a settlement"),
}


def render_label(row: BoardRow) -> str:
    fmt = ROW_LABELS.get(row.label_key)
    if fmt is None:
        return row.label_key
    try:
        return str(fmt) % row.params
    except (KeyError, ValueError, TypeError):
        return str(fmt)


def render_action(row: BoardRow) -> str:
    return str(ACTION_LABELS.get(row.action_key, row.action_key))


def render_title(section: BoardSection) -> str:
    return str(SECTION_TITLES.get(section.title_key, section.title_key))


# --------------------------------------------------------------------------- #
#  helpers
# --------------------------------------------------------------------------- #


def _day_str(dt) -> str:
    if dt is None:
        return "?"
    return timezone.localtime(dt).date().isoformat()


def _pct(ratio) -> str:
    if ratio is None:
        return "—"
    return f"{float(ratio) * 100:+.1f}%"


def _section(key: str, rows: list, *, source_url: str, freshness=None,
             total: int | None = None) -> BoardSection:
    reds = [r for r in rows if r.severity == "red"]
    rest = sorted((r for r in rows if r.severity != "red"),
                  key=lambda r: _SEV_RANK.get(r.severity, 9))
    display = reds + rest[: max(0, _MAX_DISPLAY - len(reds))]
    return BoardSection(
        key=key, title_key=key, role=_role_for(key), rows=display,
        total=total if total is not None else len(rows),
        source_url=source_url, freshness=freshness,
    )


def stub_section(key: str) -> BoardSection:
    """An honest 'unavailable' section for a provider that raised — one broken family
    must never blank the whole board."""
    return BoardSection(key=key, title_key=key, role=_role_for(key), rows=[], total=-1,
                        source_url="", freshness=None)


def _asset_stamp():
    """The corp-assets sync freshness (for stock-derived families), or None."""
    from apps.admin_audit.models import AppSetting

    value = AppSetting.objects.filter(key="sync:corp_assets").values_list(
        "value", flat=True).first()
    at = (value or {}).get("at") if value else None
    if not at:
        return None
    from django.utils.dateparse import parse_datetime

    return parse_datetime(at)


# --------------------------------------------------------------------------- #
#  the nine v1 providers
# --------------------------------------------------------------------------- #


def provide_readiness(config) -> BoardSection:
    """Low-stock / no-location / stale alerts — the exact ``inventory_rows`` composition
    (officer-knob-always-wins inherited, never re-derived)."""
    rows = []
    for r in _inventory_rows_shared():
        alerts = r["alerts"]
        if not alerts:
            continue
        fit, a, ship = r["fit"], r["a"], r["ship_name"]
        if "reorder" in alerts:
            sev, key, params = "red", "readiness.reorder", {
                "fit": ship, "atp": a.atp, "reorder": r["reorder_point"]}
        elif "safety" in alerts:
            sev, key, params = "red", "readiness.safety", {
                "fit": ship, "atp": a.atp, "safety": r["safety_stock"]}
        elif "suggested" in alerts:
            sev, key, params = "amber", "readiness.suggested", {
                "fit": ship, "atp": a.atp, "reorder": r["d"].suggested_reorder}
        elif "no_location" in alerts:
            sev, key, params = "amber", "readiness.no_location", {"fit": ship}
        elif "stale" in alerts:
            sev, key, params = "amber", "readiness.stale", {"fit": ship}
        else:
            continue
        rows.append(BoardRow(
            key=f"fit:{fit.id}", severity=sev, label_key=key, params=params,
            url=reverse("store:inventory_fit", args=[fit.id]), action_key="receipt_or_fanout",
        ))
    return _section("readiness", rows, source_url=reverse("store:inventory"),
                    freshness=_asset_stamp())


def provide_orders(config) -> BoardSection:
    """At-risk (ETA within window) and overdue orders — the EXPLICIT active-status set,
    never ``status__lt`` (statuses are strings; cancelled/delivered sort below ready)."""
    from apps.store.models import StoreOrder

    active = (
        StoreOrder.Status.OPEN, StoreOrder.Status.CLAIMED,
        StoreOrder.Status.DEPOSIT_PAID, StoreOrder.Status.IN_PRODUCTION,
    )
    now = timezone.now()
    horizon = now + timedelta(days=config.at_risk_days)
    base = StoreOrder.objects.filter(
        status__in=active, current_eta__isnull=False, current_eta__lte=horizon,
    )
    total = base.count()
    rows = []
    for o in base.order_by("current_eta")[:_FETCH_CAP]:
        ship = o.ship_name or o.fit_name or f"Order #{o.pk}"
        if o.current_eta < now:
            sev, key, params = "red", "orders.overdue", {"ship": ship, "eta": _day_str(o.current_eta)}
        else:
            sev, key, params = "amber", "orders.at_risk", {
                "ship": ship, "days": config.at_risk_days, "eta": _day_str(o.current_eta)}
        rows.append(BoardRow(
            key=f"order:{o.pk}", severity=sev, label_key=key, params=params,
            url=reverse("store:order", args=[o.pk]), action_key="advance_order",
        ))
    return _section("orders", rows, source_url=reverse("store:board"), total=total)


def _req_names(reqs):
    from apps.sde.models import SdeType

    return dict(
        SdeType.objects.filter(type_id__in={r.type_id for r in reqs})
        .values_list("type_id", "name")
    )


def provide_shortages(config) -> BoardSection:
    """Live net-requirement shortfalls (``net_quantity > 0``). No netting math here."""
    from apps.industry.models import NetRequirement

    base = NetRequirement.objects.filter(
        status__in=(NetRequirement.Status.OPEN, NetRequirement.Status.IN_PROGRESS),
        net_quantity__gt=0,
    )
    total = base.count()
    reqs = list(base.order_by("required_by", "-net_quantity")[:_FETCH_CAP])
    names = _req_names(reqs)
    now = timezone.now()
    url = reverse("industry:mrp")
    rows = []
    for r in reqs:
        item = names.get(r.type_id, str(r.type_id))
        if r.required_by and r.required_by < now:
            sev, key, params = "red", "shortages.overdue", {
                "item": item, "qty": r.net_quantity, "due": _day_str(r.required_by)}
        else:
            sev, key, params = "amber", "shortages.net", {"item": item, "qty": r.net_quantity}
        rows.append(BoardRow(
            key=f"req:{r.pk}", severity=sev, label_key=key, params=params,
            url=url, action_key="fan_out_vehicle",
        ))
    return _section("shortages", rows, source_url=url, total=total)


def provide_commitments(config) -> BoardSection:
    """Restock commitments (live ``FitSupplyNeed`` rows) due within window or past."""
    from apps.store.models import FitSupplyNeed

    now = timezone.now()
    horizon = now + timedelta(days=config.commitments_due_days)
    base = FitSupplyNeed.objects.filter(
        status__in=(FitSupplyNeed.Status.OPEN, FitSupplyNeed.Status.IN_PROGRESS),
        required_by__isnull=False, required_by__lte=horizon,
    ).select_related("doctrine_fit")
    total = base.count()
    rows = []
    for n in base.order_by("required_by")[:_FETCH_CAP]:
        fit = n.doctrine_fit
        ship = fit.name if fit else f"Need #{n.pk}"
        url = reverse("store:inventory_fit", args=[fit.id]) if fit else reverse("store:inventory")
        if n.required_by < now:
            sev, key = "red", "commitments.need_overdue"
        else:
            sev, key = "amber", "commitments.need_due"
        rows.append(BoardRow(
            key=f"need:{n.pk}", severity=sev, label_key=key,
            params={"fit": ship, "due": _day_str(n.required_by)},
            url=url, action_key="refresh_vehicle",
        ))
    return _section("commitments", rows, source_url=reverse("store:inventory"), total=total)


def provide_bottlenecks(config) -> BoardSection:
    """Requirements the plan can't make in time (``feasible_at > required_by``) — the v1
    feasible-vs-required proxy. P5 replaces this provider under the same key (reading
    ``bottleneck_code``); the page never changes."""
    from apps.industry.models import NetRequirement

    base = NetRequirement.objects.filter(
        status__in=(NetRequirement.Status.OPEN, NetRequirement.Status.IN_PROGRESS),
        required_by__isnull=False, feasible_at__isnull=False,
        feasible_at__gt=F("required_by"),
    )
    total = base.count()
    reqs = list(base.order_by("feasible_source", "required_by")[:_FETCH_CAP])
    names = _req_names(reqs)
    url = reverse("industry:mrp")
    rows = [
        BoardRow(
            key=f"req:{r.pk}", severity="red", label_key="bottlenecks.late",
            params={"item": names.get(r.type_id, str(r.type_id)),
                    "feasible": _day_str(r.feasible_at), "due": _day_str(r.required_by)},
            url=url, action_key="expedite",
        )
        for r in reqs
    ]
    return _section("bottlenecks", rows, source_url=url, total=total)


def _route(task) -> str:
    src = task.source_location.name if task.source_location else "?"
    dst = task.dest_location.name if task.dest_location else "?"
    return f"{src} → {dst}"


def provide_in_transit(config) -> BoardSection:
    """Active internal hauls (``HaulingTask`` not DONE), minus any P6-superseded task ids.
    HaulingTask carries no deadline, so 'overdue' is an OPEN haul stalled past
    ``at_risk_days``; everything else in-flight is info."""
    from apps.stockpile.models import HaulingTask

    now = timezone.now()
    stalled_before = now - timedelta(days=config.at_risk_days)
    suppressed = suppressed_haul_task_ids()
    # Explicit active set (never exclude-terminal) — the orders-provider discipline: a
    # future terminal status added to HaulingTask must not silently leak into the board.
    active = (
        HaulingTask.Status.OPEN, HaulingTask.Status.CLAIMED, HaulingTask.Status.IN_PROGRESS,
    )
    base = (
        HaulingTask.objects.filter(status__in=active)
        .exclude(pk__in=suppressed)
        .select_related("source_location", "dest_location")
    )
    total = base.count()
    url = reverse("stockpile:logistics")
    rows = []
    for t in base.order_by("created_at")[:_FETCH_CAP]:
        if t.status == HaulingTask.Status.OPEN and t.created_at < stalled_before:
            sev, key = "red", "in_transit.stalled"
        else:
            sev, key = "info", "in_transit.active"
        rows.append(BoardRow(
            key=f"haul:{t.pk}", severity=sev, label_key=key, params={"route": _route(t)},
            url=url, action_key="manage_haul",
        ))
    return _section("in_transit", rows, source_url=url, total=total)


def provide_discrepancies(config) -> BoardSection:
    """Reconciliation-stale stocked fits + over-reserved types. (Stranded stock from a fit
    edit is the readiness 'stale' alert — not repeated here.)"""
    from apps.stockpile.availability import available_detail

    rows_data = _inventory_rows_shared()
    now = timezone.now()
    stale_before = now - timedelta(days=config.stale_reconcile_days)
    rows = []
    # (b) stocked fits not reconciled within the window (or never).
    for r in rows_data:
        a = r["a"]
        if a.on_hand <= 0:
            continue
        last = r["last_reconciled"]
        if last is None or last < stale_before:
            fit = r["fit"]
            rows.append(BoardRow(
                key=f"recon:{fit.id}", severity="amber", label_key="discrepancies.stale_reconcile",
                params={"fit": r["ship_name"], "days": config.stale_reconcile_days},
                url=reverse("store:inventory_fit", args=[fit.id]), action_key="reconcile",
            ))
    # (c) per-type over-reservation across the doctrine hulls (one batched call).
    hull_ids = {r["fit"].ship_type_id for r in rows_data if r["fit"].ship_type_id}
    names = {r["fit"].ship_type_id: r["ship_name"] for r in rows_data}
    if hull_ids:
        for tid, detail in available_detail(list(hull_ids)).items():
            if detail["over_reserved"] > 0:
                rows.append(BoardRow(
                    key=f"overreserved:{tid}", severity="red", label_key="discrepancies.over_reserved",
                    params={"item": names.get(tid, str(tid)), "qty": detail["over_reserved"]},
                    url=reverse("stockpile:dashboard"), action_key="reconcile",
                ))
    return _section("discrepancies", rows, source_url=reverse("store:inventory"),
                    freshness=_asset_stamp())


def provide_obsolete(config) -> BoardSection:
    """Obsolete (retired-doctrine stock) and slow-moving fits — ``demand_for_fits`` flags,
    read through the shared inventory rows."""
    rows = []
    for r in _inventory_rows_shared():
        flags = r["d"].flags
        fit, ship = r["fit"], r["ship_name"]
        if "obsolete" in flags:
            sev, key = "amber", "obsolete.obsolete"
        elif "slow_mover" in flags:
            sev, key = "info", "obsolete.slow_mover"
        else:
            continue
        rows.append(BoardRow(
            key=f"fit:{fit.id}", severity=sev, label_key=key, params={"fit": ship},
            url=reverse("store:inventory_fit", args=[fit.id]), action_key="retire_offer",
        ))
    return _section("obsolete", rows, source_url=reverse("store:demand_policy"))


def provide_margin(config) -> BoardSection:
    """Director-only: window margin erosion + flagged quote drift. Links to the margin
    console; never re-renders journal sums (that stays Corp Finance)."""
    from apps.store.margin import margin_summary
    from apps.store.models import MarginConfig, OrderBasisDrift

    cfg = MarginConfig.active()
    summary = margin_summary(window_days=cfg.margin_window_days)
    url = reverse("store:margin")
    rows = []
    ratio = summary["totals"]["evidence_margin_ratio"]
    if ratio is not None and ratio < cfg.margin_alert_floor_pct:
        rows.append(BoardRow(
            key="margin:window", severity="red", label_key="margin.low",
            params={"pct": _pct(ratio), "days": summary["window_days"]},
            url=url, action_key="review_margin",
        ))
    base = OrderBasisDrift.objects.filter(flagged=True).select_related("order")
    total_flagged = base.count()
    for d in base.order_by("-checked_at")[:_FETCH_CAP]:
        o = d.order
        ship = o.ship_name or o.fit_name or f"Order #{o.pk}"
        rows.append(BoardRow(
            key=f"drift:{o.pk}", severity="amber", label_key="margin.drift",
            params={"ship": ship, "pct": _pct(d.drift_pct)},
            url=reverse("store:order", args=[o.pk]), action_key="review_margin",
        ))
    # total = flagged drifts (+1 for the window row if present) behind the bound.
    total = total_flagged + (1 if rows and rows[0].key == "margin:window" else 0)
    return _section("margin", rows, source_url=url, total=total)


# --- v1 built-in registration (order = display order) --------------------------
register("readiness", provide_readiness)
register("orders", provide_orders)
register("shortages", provide_shortages)
register("commitments", provide_commitments)
register("bottlenecks", provide_bottlenecks)
register("in_transit", provide_in_transit)
register("discrepancies", provide_discrepancies)
register("obsolete", provide_obsolete)
register("margin", provide_margin)
