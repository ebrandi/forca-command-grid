"""ERP services: material readiness, job lifecycle, blueprint coverage."""
from __future__ import annotations

from django.db import transaction
from django.db.models import Sum
from django.utils.translation import gettext as _

from apps.industry.bom import direct_materials
from apps.sde.models import SdeType

from .models import BuildJob, Delivery


def _corp_on_hand(type_ids) -> dict[int, int]:
    from apps.stockpile.models import Asset, Stockpile, StockpileItem

    ids = list(type_ids)
    on_hand: dict[int, int] = {}
    for row in (
        StockpileItem.objects.filter(stockpile__kind=Stockpile.Kind.CORP, type_id__in=ids)
        .values("type_id").annotate(q=Sum("quantity_current"))
    ):
        on_hand[row["type_id"]] = (on_hand.get(row["type_id"], 0) or 0) + (row["q"] or 0)
    for row in (
        Asset.objects.filter(owner_type=Asset.Owner.CORPORATION, type_id__in=ids)
        .values("type_id").annotate(q=Sum("quantity"))
    ):
        on_hand[row["type_id"]] = (on_hand.get(row["type_id"], 0) or 0) + (row["q"] or 0)
    return on_hand


def job_materials(job: BuildJob) -> dict:
    """Materials a job needs, netted against corp on-hand, with a readiness flag."""
    materials = direct_materials(job.output_type_id, runs=job.quantity)
    if not materials:
        return {"buildable": False, "lines": [], "ready": False}
    on_hand = _corp_on_hand(materials)
    names = dict(SdeType.objects.filter(type_id__in=list(materials)).values_list("type_id", "name"))
    lines = []
    ready = True
    for tid, need in sorted(materials.items(), key=lambda kv: names.get(kv[0], "")):
        have = int(on_hand.get(tid, 0))
        short = max(need - have, 0)
        if short:
            ready = False
        lines.append(
            {"type_id": tid, "name": names.get(tid, f"Type {tid}"), "need": need, "have": have, "short": short}
        )
    return {"buildable": True, "lines": lines, "ready": ready}


def recheck_block(job: BuildJob) -> BuildJob:
    """Reconcile a queued/blocked job against current corp material availability.

    A queued job that can't start because corp stock is short is flagged BLOCKED
    with the short materials named; when stock arrives it returns to QUEUED. Only
    touches QUEUED/BLOCKED jobs — never an in-progress, built, delivered or
    cancelled one. Idempotent: writes only when the state actually changes.
    """
    if job.status not in (BuildJob.Status.QUEUED, BuildJob.Status.BLOCKED):
        return job
    mats = job_materials(job)
    if mats["buildable"] and not mats["ready"]:
        short = [ln["name"] for ln in mats["lines"] if ln["short"]]
        reason = "Short: " + ", ".join(short[:6]) + ("…" if len(short) > 6 else "")
        new_status = BuildJob.Status.BLOCKED
    else:
        reason = ""
        new_status = BuildJob.Status.QUEUED
    if job.status != new_status or job.blocked_reason != reason:
        job.status = new_status
        job.blocked_reason = reason
        job.save(update_fields=["status", "blocked_reason", "updated_at"])
    return job


def can_act(user, job: BuildJob, *, is_officer: bool) -> bool:
    return is_officer or (job.owner_id == user.id)


def can_manage(user, job: BuildJob, *, is_officer: bool) -> bool:
    """Who may cancel/edit a job: an officer, the builder (owner), or — while the job
    is still unclaimed — the pilot who created it. A member can never touch a job
    another pilot has already claimed."""
    if is_officer or job.owner_id == user.id:
        return True
    return job.created_by_id == user.id and job.owner_id is None


@transaction.atomic
def claim(job: BuildJob, user) -> bool:
    locked = BuildJob.objects.select_for_update().get(pk=job.pk)
    if locked.owner_id or locked.status != BuildJob.Status.QUEUED:
        return False
    locked.owner = user
    locked.status = BuildJob.Status.BUILDING
    locked.save(update_fields=["owner", "status", "updated_at"])
    return True


# Manual status transitions allowed via set_status (the status endpoint / mark-built).
# DELIVERED is deliberately unreachable here — delivery MUST go through deliver(), which
# also credits corp stock + the builder. Terminal states can't be moved out of.
_ALLOWED_TRANSITIONS = {
    BuildJob.Status.QUEUED: {BuildJob.Status.BLOCKED, BuildJob.Status.BUILDING, BuildJob.Status.CANCELLED},
    BuildJob.Status.BLOCKED: {BuildJob.Status.QUEUED, BuildJob.Status.BUILDING, BuildJob.Status.CANCELLED},
    BuildJob.Status.BUILDING: {BuildJob.Status.BUILT, BuildJob.Status.CANCELLED},
    BuildJob.Status.BUILT: {BuildJob.Status.CANCELLED},
    BuildJob.Status.DELIVERED: set(),
    BuildJob.Status.CANCELLED: set(),
}


@transaction.atomic
def set_status(job: BuildJob, to_status: str) -> bool:
    """Apply a manual status transition if it's allowed. Returns False (no-op) for an
    invalid value or a forbidden transition — notably DELIVERED, which only deliver()
    may set so corp stock and the builder's credit are never skipped."""
    if to_status not in BuildJob.Status.values:
        return False
    locked = BuildJob.objects.select_for_update().get(pk=job.pk)
    if to_status not in _ALLOWED_TRANSITIONS.get(locked.status, set()):
        return False
    locked.status = to_status
    locked.save(update_fields=["status", "updated_at"])
    job.status = to_status
    return True


@transaction.atomic
def update_quantity(job: BuildJob, quantity: int, note: str = "") -> bool:
    """Edit an unclaimed job's quantity/note under a row lock.

    Re-checks the status inside the lock so a job that another pilot claims mid-edit
    (claim() flips it to BUILDING) can't have its quantity rewritten. Returns False if
    the job is no longer editable. Re-evaluates the material shortfall afterwards."""
    locked = BuildJob.objects.select_for_update().get(pk=job.pk)
    if locked.status not in (BuildJob.Status.QUEUED, BuildJob.Status.BLOCKED):
        return False
    locked.quantity = max(1, int(quantity))
    locked.note = (note or locked.note or "").strip()[:200]
    locked.save(update_fields=["quantity", "note", "updated_at"])
    recheck_block(locked)
    job.quantity = locked.quantity
    return True


def _default_corp_stockpile():
    from apps.stockpile.models import Stockpile

    sp = Stockpile.objects.filter(kind=Stockpile.Kind.CORP).order_by("pk").first()
    if sp is None:
        sp = Stockpile.objects.create(name="Production", kind=Stockpile.Kind.CORP)
    return sp


@transaction.atomic
def deliver(job: BuildJob, user, quantity: int | None = None) -> Delivery | None:
    """Mark a job delivered: add to corp stock and credit the builder."""
    locked = BuildJob.objects.select_for_update().get(pk=job.pk)
    # Only an in-progress build can be delivered — never a cancelled/queued/delivered
    # one (a delivery adds corp stock + credits the builder, so it must be a real build).
    if locked.status not in (BuildJob.Status.BUILDING, BuildJob.Status.BUILT):
        return None
    qty = quantity or locked.quantity
    stockpile = locked.deliver_to or _default_corp_stockpile()

    from apps.stockpile.models import StockpileItem

    item, _created = StockpileItem.objects.get_or_create(
        stockpile=stockpile, type_id=locked.output_type_id
    )
    item.quantity_current = (item.quantity_current or 0) + qty
    item.save(update_fields=["quantity_current"])

    # IND-2 (3.4): decrement the inputs this build consumed so corp stock stays honest
    # (opt-in; clamped so stock never goes negative). Same atomic txn as the stock add.
    consumed = _consume_materials(locked, qty) if _consumption_enabled() else {}

    locked.status = BuildJob.Status.DELIVERED
    locked.save(update_fields=["status", "updated_at"])
    delivery = Delivery.objects.create(
        job=locked, stockpile=stockpile, quantity=qty, delivered_by=user, consumed=consumed
    )

    # Credit the builder (idempotent per job).
    from apps.pilots.services import record_contribution

    credited = locked.owner or user
    record_contribution(
        credited, kind="build", magnitude=qty, unit="units",
        description=f"Built {qty}× {locked.output_type_id}",
        ref_type="build_job", ref_id=str(locked.pk),
    )

    # IND-1 (3.3): a plan-linked delivery flows back to the plan's status.
    if locked.source_item_id:
        _reconcile_project_from_delivery(locked.source_item)
    return delivery


def _reconcile_project_from_delivery(item) -> None:
    """Mark the originating plan DONE once every BUILD line has a delivered job (IND-1 / 3.3).
    Stock + builder credit are already handled by ``deliver``; this closes the plan loop."""
    from apps.industry.models import IndustryProject, IndustryProjectItem

    # Lock the project row (deliver() is atomic) so concurrent final deliveries serialize —
    # otherwise two txns delivering the last two lines can each miss the other's uncommitted
    # DELIVERED row and the plan is stranded in ACTIVE.
    project = IndustryProject.objects.select_for_update().get(pk=item.project_id)
    if project.status in (IndustryProject.Status.DONE, IndustryProject.Status.CANCELLED):
        return
    build_item_ids = set(
        project.items.filter(build_or_buy=IndustryProjectItem.BuildOrBuy.BUILD)
        .values_list("id", flat=True)
    )
    if not build_item_ids:
        return
    delivered_item_ids = set(
        BuildJob.objects.filter(
            source_item_id__in=build_item_ids, status=BuildJob.Status.DELIVERED
        ).values_list("source_item_id", flat=True)
    )
    if build_item_ids <= delivered_item_ids:
        project.status = IndustryProject.Status.DONE
        project.save(update_fields=["status", "updated_at"])


def _consumption_enabled() -> bool:
    from apps.industry.models import IndustryEconomyConfig

    return IndustryEconomyConfig.active().consume_materials_on_delivery


def _consume_materials(job: BuildJob, qty: int) -> dict:
    """Decrement a job's input materials from corp stockpiles on delivery (IND-2 / 3.4).

    Only the tracked corp ``StockpileItem`` rows are decremented — never the read-only ESI
    Asset mirror — and each is clamped so stock can never go negative. Returns
    ``{type_id: consumed}`` for the delivery's audit trail.
    """
    import math

    from apps.industry.bom import buildable_recipe, direct_materials
    from apps.stockpile.models import Stockpile, StockpileItem

    # Convert delivered *units* → blueprint *runs* (a batch recipe like ammo/drones yields
    # many units per run), and use the plan line's ME when the job came from a plan — so a
    # batch/researched build doesn't over-burn. Falls back to me=0 for ad-hoc jobs.
    recipe = buildable_recipe(job.output_type_id)
    if recipe is None:
        return {}
    runs = math.ceil(qty / max(1, recipe.output_quantity))
    me = job.source_item.me if job.source_item_id else 0
    needs = direct_materials(job.output_type_id, runs=runs, me=me)
    if not needs:
        return {}
    consumed: dict[int, int] = {}
    for type_id, need in needs.items():
        remaining = int(need)
        rows = (
            StockpileItem.objects.select_for_update()
            .filter(stockpile__kind=Stockpile.Kind.CORP, type_id=type_id, quantity_current__gt=0)
            .order_by("-quantity_current")
        )
        for row in rows:
            if remaining <= 0:
                break
            take = min(int(row.quantity_current), remaining)
            row.quantity_current = int(row.quantity_current) - take
            row.save(update_fields=["quantity_current"])
            remaining -= take
        taken = int(need) - remaining
        if taken:
            consumed[type_id] = taken
    return consumed


def blueprint_coverage() -> dict:
    """Which active-doctrine hulls have an owned blueprint, and which don't."""
    from apps.doctrines.models import Doctrine
    from apps.industry.bom import blueprint_for

    from .models import Blueprint

    hulls: dict[int, str] = {}
    for d in Doctrine.objects.filter(status=Doctrine.Status.ACTIVE).prefetch_related("fits"):
        for fit in d.fits.all():
            hulls.setdefault(fit.ship_type_id, d.name)

    # Only count blueprints that can actually build something: an original, or a
    # copy with runs left. A spent BPC (quantity -2, runs 0) doesn't cover a hull.
    usable = Blueprint.objects.exclude(quantity=-2, runs=0)
    owned_bp_types = set(usable.values_list("type_id", flat=True))
    owned_products = set(
        usable.exclude(product_type_id__isnull=True).values_list(
            "product_type_id", flat=True
        )
    )
    names = dict(SdeType.objects.filter(type_id__in=list(hulls)).values_list("type_id", "name"))
    covered, gaps = [], []
    for hull_id, doctrine_name in hulls.items():
        bp_type = blueprint_for(hull_id)
        has_bp = hull_id in owned_products or (bp_type is not None and bp_type in owned_bp_types)
        row = {
            "type_id": hull_id,
            "name": names.get(hull_id, _("Type %(type_id)s") % {"type_id": hull_id}),
            "doctrine": doctrine_name,
        }
        (covered if has_bp else gaps).append(row)
    return {"covered": covered, "gaps": gaps}


def in_production(limit: int = 50) -> list[dict]:
    """Active corp industry jobs (imported from ESI), newest finish first, named."""
    from .models import CorpIndustryJob

    jobs = list(
        CorpIndustryJob.objects.filter(status__in=["active", "paused"]).order_by("end_date")[:limit]
    )
    type_ids = {j.product_type_id for j in jobs if j.product_type_id} | {
        j.blueprint_type_id for j in jobs
    }
    names = dict(SdeType.objects.filter(type_id__in=list(type_ids)).values_list("type_id", "name"))
    out = []
    for j in jobs:
        label_id = j.product_type_id or j.blueprint_type_id
        out.append({
            "job": j,
            "name": names.get(label_id, _("Type %(type_id)s") % {"type_id": label_id}),
            "activity": j.activity_label,
            "runs": j.runs,
            "ends": j.end_date,
        })
    return out
