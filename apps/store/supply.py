"""Backorders → actionable supply (SHIP-1 part 4).

A backordered Shipyard order never just sits on a list: it folds into the one
live :class:`FitSupplyNeed` for its (fit, location), which an officer turns into
whichever supply vehicle fits — an Industry Project (finally wiring the
long-dormant ``IndustryProject.source=store_order`` / ``store_order_id``
scaffolding), an ERP build job, or a claimable task. Several orders consolidate
into that single need; duplicate creation is blocked by the partial unique
constraint and the status-guarded helpers here.

Completing a vehicle does NOT deliver customer orders and does NOT add fitted
ships to stock — building hulls is not the same as assembling complete doctrine
packages. It notifies the officers so a human receipts the finished ships
(:func:`apps.store.inventory.receive_stock`), which is what actually allocates
stock to waiting orders.
"""
from __future__ import annotations

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext as _

from .inventory import unfilled_quantity
from .models import FitSupplyNeed, StoreOrder

_LIVE = (FitSupplyNeed.Status.OPEN, FitSupplyNeed.Status.IN_PROGRESS)
_WAITING_ORDER_STATUSES = (
    StoreOrder.Status.OPEN, StoreOrder.Status.CLAIMED, StoreOrder.Status.IN_PRODUCTION,
)


def open_backorder_demand(fit, *, location) -> tuple[int, object | None]:
    """(units of unfilled backorder demand, earliest current ETA) for fit+location.

    Demand is LIVE: an order's unfilled quantity is what no active-or-consumed
    reservation covers — deliberately not the frozen ``quantity_backordered``
    snapshot, which is an order-time audit record. An expired reservation turns
    a once-fully-reserved order back into demand here. Orders count toward the
    need matching their FROZEN delivery location exactly (None matches None) so
    demand is never double-counted across locations."""
    orders = StoreOrder.objects.filter(
        kind=StoreOrder.Kind.DOCTRINE_FIT, doctrine_fit=fit,
        status__in=_WAITING_ORDER_STATUSES,
        delivery_location=location,
    )
    total = 0
    earliest = None
    for order in orders:
        unfilled = unfilled_quantity(order)
        if unfilled <= 0:
            continue
        total += unfilled
        if order.current_eta and (earliest is None or order.current_eta < earliest):
            earliest = order.current_eta
    return total, earliest


def waiting_orders(need: FitSupplyNeed) -> list[StoreOrder]:
    """The customer orders a need exists to satisfy (derived, not denormalised).

    Same live semantics as :func:`open_backorder_demand`: anything unfilled by a
    live-or-consumed reservation counts, whatever the frozen order-time split said."""
    return [
        o for o in StoreOrder.objects.filter(
            kind=StoreOrder.Kind.DOCTRINE_FIT, doctrine_fit=need.doctrine_fit,
            status__in=_WAITING_ORDER_STATUSES,
            delivery_location=need.location,
        ).select_related("buyer").order_by("created_at")
        if unfilled_quantity(o) > 0
    ]


def _locked_live_need(fit, location) -> FitSupplyNeed | None:
    return (
        FitSupplyNeed.objects.select_for_update()
        .filter(doctrine_fit=fit, location=location, status__in=_LIVE)
        .first()
    )


def recompute_supply_need(fit, *, location) -> FitSupplyNeed | None:
    """Fold current backorder demand into the live need for (fit, location).

    Creates the need when demand appears, updates its quantity and required-by
    date while demand shifts, and auto-closes it when demand disappears and no
    production vehicle is attached (an attached vehicle stays for the officer or
    the completion hook to resolve).

    Race-safe twice over: concurrent creation collapses onto the partial unique
    constraint, and demand is (re)counted only while HOLDING the need's row
    lock, so racing recomputes serialize and the last one to run writes a total
    that includes every order committed before it. Placement additionally
    schedules a post-commit recompute (see ``place_fit_order``) so totals
    converge even when the in-transaction pass couldn't see a rival's
    uncommitted order."""
    with transaction.atomic():
        live = _locked_live_need(fit, location)
        if live is None:
            # Peek before minting a row so cancel/delivery paths with no demand
            # don't litter DONE rows. The authoritative count happens under the
            # lock below.
            peek, _by = open_backorder_demand(fit, location=location)
            if peek <= 0:
                return None
            try:
                with transaction.atomic():
                    live = FitSupplyNeed.objects.create(doctrine_fit=fit, location=location)
            except IntegrityError:
                live = _locked_live_need(fit, location)
                if live is None:  # the rival's need already closed — mint fresh
                    live = FitSupplyNeed.objects.create(doctrine_fit=fit, location=location)

        demand, required_by = open_backorder_demand(fit, location=location)
        if demand > 0:
            if live.quantity_required != demand or live.required_by != required_by:
                live.quantity_required = demand
                live.required_by = required_by
                live.save(update_fields=["quantity_required", "required_by", "updated_at"])
        elif live.industry_project_id or live.build_job_id or live.task_id:
            if live.quantity_required != 0:
                live.quantity_required = 0
                live.save(update_fields=["quantity_required", "updated_at"])
        else:
            live.status = FitSupplyNeed.Status.DONE
            live.quantity_required = 0
            live.save(update_fields=["status", "quantity_required", "updated_at"])
        return live


def _ship_name(fit) -> str:
    from apps.sde.models import SdeType

    return (
        SdeType.objects.filter(type_id=fit.ship_type_id)
        .values_list("name", flat=True).first()
        or fit.name
    )


@transaction.atomic
def create_industry_project_for_need(need: FitSupplyNeed, *, actor):
    """Turn a need into an Industry Project (idempotent: returns the linked one).

    The project carries the hull as its item — modules are import/buy lines the
    plan's own BOM & shopping-list tooling handles far better than a static dump
    here would."""
    from apps.industry.models import IndustryProject, IndustryProjectItem

    locked = FitSupplyNeed.objects.select_for_update().get(pk=need.pk)
    if locked.industry_project_id:
        return locked.industry_project

    fit = locked.doctrine_fit
    orders = waiting_orders(locked)
    project = IndustryProject.objects.create(
        name=_("Restock %(fit)s") % {"fit": fit.name},
        description=_(
            "Supply requirement from the Shipyard: %(count)s backordered order(s) "
            "for %(fit)s."
        ) % {"count": len(orders), "fit": fit.name},
        objective_type=IndustryProject.Objective.BUILD,
        status=IndustryProject.Status.ACTIVE,
        target_location=locked.location,
        linked_doctrine=fit.doctrine,
        created_by=actor,
        due_at=locked.required_by,
        source=(
            IndustryProject.Source.STORE_ORDER if len(orders) == 1
            else IndustryProject.Source.STORE_GAP
        ),
        store_order_id=orders[0].pk if len(orders) == 1 else None,
    )
    IndustryProjectItem.objects.create(
        project=project, type_id=fit.ship_type_id, product_name=_ship_name(fit),
        quantity=max(locked.quantity_required, 1),
    )
    locked.industry_project = project
    locked.status = FitSupplyNeed.Status.IN_PROGRESS
    locked.save(update_fields=["industry_project", "status", "updated_at"])
    return project


@transaction.atomic
def create_build_job_for_need(need: FitSupplyNeed, *, actor):
    """Turn a need into an ERP build job (idempotent: returns the linked one)."""
    from apps.erp.messages import english_text
    from apps.erp.models import BuildJob
    from apps.stockpile.models import Stockpile

    locked = FitSupplyNeed.objects.select_for_update().get(pk=need.pk)
    if locked.build_job_id:
        return locked.build_job

    fit = locked.doctrine_fit
    deliver_to = None
    if locked.location_id:
        deliver_to = Stockpile.objects.filter(
            kind=Stockpile.Kind.CORP, location_id=locked.location_id
        ).order_by("pk").first()
    # Seam B: the board is read in every reader's locale — persist the key +
    # raw params, and the locale-independent English prose as the audit column.
    note_params = {"fit_name": fit.name}
    job = BuildJob.objects.create(
        output_type_id=fit.ship_type_id,
        quantity=max(locked.quantity_required, 1),
        status=BuildJob.Status.QUEUED,
        deliver_to=deliver_to,
        due_at=locked.required_by,
        created_by=actor,
        note=english_text("job.shipyard_restock", note_params),
        note_key="job.shipyard_restock",
        note_params=note_params,
    )
    locked.build_job = job
    locked.status = FitSupplyNeed.Status.IN_PROGRESS
    locked.save(update_fields=["build_job", "status", "updated_at"])
    return job


@transaction.atomic
def create_task_for_need(need: FitSupplyNeed, *, actor):
    """Turn a need into a claimable corp task (idempotent, same dedup convention
    as the doctrine supply fan-out: related_type/related_id)."""
    from apps.tasks.models import Task

    locked = FitSupplyNeed.objects.select_for_update().get(pk=need.pk)
    if locked.task_id:
        return locked.task

    fit = locked.doctrine_fit
    existing = Task.objects.filter(
        related_type="store_supply_need", related_id=str(locked.pk),
        status__in=[Task.Status.OPEN, Task.Status.CLAIMED, Task.Status.IN_PROGRESS],
    ).first()
    task = existing or Task.objects.create(
        type=Task.Type.BUILD,
        title=_("Supply %(count)s× %(fit)s for the Shipyard") % {
            "count": locked.quantity_required, "fit": fit.name,
        },
        is_open=True, status=Task.Status.OPEN, priority=8, created_by=actor,
        related_type="store_supply_need", related_id=str(locked.pk),
    )
    locked.task = task
    locked.status = FitSupplyNeed.Status.IN_PROGRESS
    locked.save(update_fields=["task", "status", "updated_at"])
    return task


def notify_supply_ready(need: FitSupplyNeed, *, vehicle: str) -> None:
    """Tell the officers a need's production vehicle finished: assemble + receipt.

    Best-effort (never breaks the caller); officer-routed via the industry_job
    category; idempotent per (need, vehicle)."""
    try:
        from apps.pingboard import services as pingboard
        from apps.pingboard.models import AlertCategory

        fit = need.doctrine_fit
        pingboard.emit_broadcast(
            category=AlertCategory.INDUSTRY_JOB,
            title="Shipyard restock built",
            body=(
                f"Production for {fit.name} finished. Assemble and fit the ships, "
                "then record the receipt on the Shipyard inventory console to "
                "release waiting backorders."
            ),
            template="store.supply_need.built",
            context={"fit_name": fit.name},
            source_service="store",
            source_object_id=f"supply_need:{need.pk}:{vehicle}",
            idempotency_key=f"store:supply_need_built:{need.pk}:{vehicle}",
        )
    except Exception:  # noqa: BLE001 — notification must never break the flow
        import logging

        logging.getLogger("forca.store").exception(
            "supply-need notification failed (need %s)", need.pk
        )


def on_vehicle_completed(*, build_job=None, industry_project=None) -> int:
    """Signal hook: a linked vehicle completed → flag needs + notify officers.

    Does NOT touch customer orders or stock — receipting the assembled ships is
    the deliberate human step that does. Returns needs notified."""
    q = Q()
    vehicle = ""
    if build_job is not None:
        q = Q(build_job=build_job)
        vehicle = "build_job"
    if industry_project is not None:
        q = q | Q(industry_project=industry_project)
        vehicle = vehicle or "industry_project"
    if not q:
        return 0
    count = 0
    for need in FitSupplyNeed.objects.filter(q, status__in=_LIVE).select_related(
        "doctrine_fit"
    ):
        notify_supply_ready(need, vehicle=vehicle)
        count += 1
    return count


def order_eta_datetime(policy, lead_days: int):
    """Now + lead days — the honest default estimate for a fresh backorder."""
    from datetime import timedelta

    return timezone.now() + timedelta(days=lead_days)


def notify_waitlist(fit) -> int:
    """Ping (then clear) the waitlist of a fit that just became orderable.

    Best-effort; each pilot is DM'd in their own locale via the scaffold. Returns
    pilots notified."""
    from .models import FitWaitlistEntry

    entries = list(FitWaitlistEntry.objects.filter(fit=fit).select_related("user"))
    if not entries:
        return 0
    ship = _ship_name(fit)
    notified = 0
    try:
        from apps.pingboard import services as pingboard
        from apps.pingboard.models import AlertCategory

        for entry in entries:
            pingboard.emit_broadcast(
                category=AlertCategory.ANNOUNCEMENT,
                title="Back in stock",
                body=f"{ship} can be ordered on the Shipyard again.",
                template="store.waitlist_available",
                context={"ship_name": ship},
                audience={"kind": "user", "id": entry.user_id},
                source_service="store",
                source_object_id=f"waitlist:{fit.pk}:{entry.user_id}",
                idempotency_key=f"store:waitlist:{fit.pk}:{entry.user_id}:{entry.pk}",
            )
            notified += 1
        FitWaitlistEntry.objects.filter(pk__in=[e.pk for e in entries]).delete()
    except Exception:  # noqa: BLE001 — notification must never break the flow
        import logging

        logging.getLogger("forca.store").exception("waitlist notify failed (fit %s)", fit.pk)
    return notified
