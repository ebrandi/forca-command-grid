"""ERP services: material readiness, job lifecycle, blueprint coverage."""
from __future__ import annotations

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils.translation import gettext as _

from apps.industry.bom import direct_materials
from apps.sde.models import SdeType

from .messages import english_text
from .models import BuildJob, Delivery


def job_materials(job: BuildJob) -> dict:
    """Materials a job needs, netted against corp availability, with a readiness flag.

    Availability comes from the unified authority (:mod:`apps.stockpile.availability`)
    — reservation-netted, ESI-wins per location, home corp only. Jobs may flip
    BLOCKED where the old manual+ESI double count hid a shortage: that is the
    truthful state arriving.
    """
    from apps.stockpile.availability import available

    materials = direct_materials(job.output_type_id, runs=job.quantity)
    if not materials:
        return {"buildable": False, "lines": [], "ready": False}
    on_hand = available(materials)
    names = dict(SdeType.objects.filter(type_id__in=list(materials)).values_list("type_id", "name"))
    lines = []
    ready = True
    for tid, need in sorted(materials.items(), key=lambda kv: names.get(kv[0], "")):
        have = int(on_hand.get(tid, 0))
        short = max(need - have, 0)
        if short:
            ready = False
        lines.append(
            {
                "type_id": tid,
                "name": names.get(tid, _("Type %(type_id)s") % {"type_id": tid}),
                "need": need,
                "have": have,
                "short": short,
            }
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
        # Seam B: persist the scaffold KEY + raw params (the material names are EVE game data —
        # they stay English and are never translated) alongside the English prose. The prose is
        # the audit record and the fallback; ``blocked_reason_i18n`` re-renders the sentence
        # under the reader's locale from the key. ``english_text`` pins the stored prose to
        # source English even when the caller happens to be a German officer's request.
        key = "job.blocked_short_truncated" if len(short) > 6 else "job.blocked_short"
        params = {"materials": ", ".join(short[:6])}
        reason = english_text(key, params)[:200]
        new_status = BuildJob.Status.BLOCKED
    else:
        key, params, reason = "", {}, ""
        new_status = BuildJob.Status.QUEUED
    if (
        job.status != new_status
        or job.blocked_reason != reason
        or job.blocked_reason_key != key
        or (job.blocked_reason_params or {}) != params
    ):
        job.status = new_status
        job.blocked_reason = reason
        job.blocked_reason_key = key
        job.blocked_reason_params = params
        job.save(
            update_fields=[
                "status", "blocked_reason", "blocked_reason_key", "blocked_reason_params",
                "updated_at",
            ]
        )
    return job


def plan_note_fields(project) -> dict:
    """The ``note`` columns for a BuildJob pushed from an industry plan (Seam B write side).

    Returns the ``BuildJob.objects.create(**…)`` kwargs for the note: the English prose exactly
    as before (the audit record + the fallback) PLUS the scaffold key and its raw params, so
    ``BuildJob.note_i18n`` can re-render the sentence under each reader's locale. The plan name
    is corp-authored content and is interpolated raw — never translated.

    Lives here (not in the ``apps.industry`` bridge that calls it) because the columns and the
    scaffold registry are the ERP's: the bridge only has to spread the result into ``create()``.
    """
    from apps.industry.models import IndustryProject

    # A non-corp plan's name is not surfaced on the corp-visible job board.
    if project.visibility == IndustryProject.Visibility.CORP:
        key, params = "job.from_plan", {"plan": project.name}
    else:
        key, params = "job.from_leadership_plan", {}
    return {"note": english_text(key, params)[:200], "note_key": key, "note_params": params}


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
    new_note = (note or locked.note or "").strip()[:200]
    if new_note != locked.note:
        # A pilot has replaced the note with free text, so the scaffold key the Plan→Job bridge
        # wrote no longer describes it. Drop the key (and its params) or ``note_i18n`` would keep
        # re-rendering the *old* plan sentence over the pilot's words. Human free text is never
        # translated — it renders verbatim from the prose column, in every locale.
        locked.note_key = ""
        locked.note_params = {}
    locked.note = new_note
    locked.save(update_fields=["quantity", "note", "note_key", "note_params", "updated_at"])
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

    # Global lock order: BuildJob → IndustryProject → StockpileItem → StockReservation.
    # A plan-linked delivery takes the plan lock up front so the reserve/release/close
    # paths (which also lock the plan first) fully serialize with the consumption and
    # the DONE-release below — a racing Reserve can never mint claims on a plan this
    # delivery is about to close.
    if locked.source_item_id:
        from apps.industry.models import IndustryProject

        IndustryProject.objects.select_for_update().get(pk=locked.source_item.project_id)

    from apps.stockpile.models import StockpileItem

    item, _created = StockpileItem.objects.get_or_create(
        stockpile=stockpile, type_id=locked.output_type_id
    )

    # IND-2 (3.4): decrement the inputs this build consumed so corp stock stays honest
    # (opt-in). Same atomic txn as the stock add. The output row's pk rides along so
    # every StockpileItem lock in this transaction is acquired in one pk-ascending
    # statement — the global lock order.
    consumed = (
        _consume_materials(locked, qty, extra_lock_pks=(item.pk,))
        if _consumption_enabled()
        else {}
    )

    # Blind increment — no read-modify-write; the row is freshly created or already
    # locked by the ordered acquisition above, and F() can't race a concurrent consume.
    StockpileItem.objects.filter(pk=item.pk).update(
        quantity_current=F("quantity_current") + qty
    )

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
        # Only what the kind cannot say. The leading verb ("Built") IS the kind — the ledger
        # already renders the translated ``get_kind_display`` chip next to this line, so
        # repeating it here would just freeze an English word into a row every locale reads.
        description=f"{qty}× {locked.output_type_id}",
        ref_type="build_job", ref_id=str(locked.pk),
    )

    # IND-1 (3.3): a plan-linked delivery flows back to the plan's status.
    if locked.source_item_id:
        _reconcile_project_from_delivery(locked.source_item, user)
    return delivery


def _reconcile_project_from_delivery(item, user) -> None:
    """Mark the originating plan DONE once every BUILD line has a delivered job (IND-1 / 3.3).
    Stock + builder credit are already handled by ``deliver``; this closes the plan loop.
    A DONE plan must hold no ACTIVE reservations (P1) — the remainder is released here."""
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
        from apps.industry.services import release_project_stock
        from core.audit import audit_log

        released = release_project_stock(project)
        if released:
            audit_log(
                user, "industry.release_stock", target_type="industry_project",
                target_id=str(project.pk),
                metadata={"released": released, "reason": "plan_done"},
            )


def _consumption_enabled() -> bool:
    from apps.industry.models import IndustryEconomyConfig

    return IndustryEconomyConfig.active().consume_materials_on_delivery


def _consume_materials(job: BuildJob, qty: int, extra_lock_pks=()) -> dict:
    """Decrement a job's input materials from corp stockpiles on delivery (IND-2 / 3.4).

    Order of operations per input type (P1 / WS2):

    1. The plan's ACTIVE reservations for the type are consumed FIRST — the same
       decrement as before, now attributed to the claim that predicted it — up to
       the needed quantity. Transitions are status-guarded; a partially-needed
       claim is split so the remainder stays ACTIVE (and auditable) instead of
       silently dissolving.
    2. Only the unreserved remainder comes off free stock, and "free" respects
       every remaining ACTIVE claim — a delivery can no longer eat stock another
       plan reserved (the old double-subtract).

    Locking: every ``StockpileItem`` this delivery may touch (all input types plus
    the output row via ``extra_lock_pks``) is acquired in ONE pk-ascending
    ``select_for_update`` — the global lock order — and FIFO/priority is applied in
    Python afterwards. Only tracked corp rows are decremented, never the read-only
    ESI mirror; each take is capped so stock never goes negative. Returns
    ``{type_id: consumed}`` for the delivery's audit trail.
    """
    import math

    from django.db.models import Q, Sum

    from apps.industry.bom import buildable_recipe, direct_materials
    from apps.stockpile.models import Stockpile, StockpileItem, StockReservation

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
    project_id = job.source_item.project_id if job.source_item_id else None

    lock_q = Q(stockpile__kind=Stockpile.Kind.CORP, type_id__in=list(needs))
    if extra_lock_pks:
        lock_q |= Q(pk__in=list(extra_lock_pks))
    items = list(
        StockpileItem.objects.select_for_update(of=("self",))
        .select_related("stockpile")
        .filter(lock_q)
        .order_by("pk")
    )
    items_by_pk = {it.pk: it for it in items}
    corp_rows = [it for it in items if it.type_id in needs and it.stockpile.kind == Stockpile.Kind.CORP]

    # Live claims per row (all holders, ours included) — the free walk must not
    # take them. Stable under our row locks: reservations are only written by
    # transactions that hold the item lock.
    claims: dict[int, int] = {
        row["stockpile_item_id"]: int(row["s"] or 0)
        for row in (
            StockReservation.objects.filter(
                stockpile_item_id__in=list(items_by_pk),
                status=StockReservation.Status.ACTIVE,
            )
            .values("stockpile_item_id")
            .annotate(s=Sum("quantity_reserved"))
        )
    }

    # This plan's own claims, locked after the items (StockpileItem →
    # StockReservation order) in ascending pk — the one multi-row order for
    # reservation locks; FIFO (oldest first) is applied in Python afterwards.
    my_reservations: list[StockReservation] = []
    if project_id:
        my_reservations = list(
            StockReservation.objects.select_for_update(of=("self",))
            .filter(
                project_id=project_id,
                status=StockReservation.Status.ACTIVE,
                stockpile_item_id__in=[it.pk for it in corp_rows],
            )
            .order_by("pk")
        )
        my_reservations.sort(key=lambda r: (r.reserved_at, r.pk))
    my_res_by_type: dict[int, list[StockReservation]] = {}
    for res in my_reservations:
        my_res_by_type.setdefault(items_by_pk[res.stockpile_item_id].type_id, []).append(res)

    consumed: dict[int, int] = {}
    for type_id, need in needs.items():
        remaining = int(need)

        # (1) Consume the plan's claims first.
        for res in my_res_by_type.get(type_id, ()):
            if remaining <= 0:
                break
            row = items_by_pk[res.stockpile_item_id]
            take = min(int(res.quantity_reserved), remaining, max(0, int(row.quantity_current)))
            if take <= 0:
                continue
            leftover = int(res.quantity_reserved) - take
            flipped = StockReservation.objects.filter(
                pk=res.pk, status=StockReservation.Status.ACTIVE
            ).update(status=StockReservation.Status.CONSUMED, quantity_reserved=take)
            if not flipped:
                continue
            if leftover > 0:
                # Split: the unneeded remainder stays a live, auditable claim.
                StockReservation.objects.create(
                    stockpile_item=row, project_id=project_id, quantity_reserved=leftover
                )
            row.quantity_current = int(row.quantity_current) - take
            row.save(update_fields=["quantity_current"])
            claims[row.pk] = claims.get(row.pk, 0) - take
            remaining -= take

        # (2) The unreserved remainder off free stock — biggest rows first
        #     (priority applied after locking, never in the lock order).
        if remaining > 0:
            for row in sorted(corp_rows, key=lambda r: -int(r.quantity_current)):
                if remaining <= 0:
                    break
                if row.type_id != type_id:
                    continue
                free = int(row.quantity_current) - max(0, claims.get(row.pk, 0))
                take = min(free, remaining)
                if take <= 0:
                    continue
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


# --- P3: BuildJob ↔ ESI-job linking (the ESI-wins dedup) ----------------------
def suggest_esi_matches(jobs) -> dict:
    """Best-guess ``CorpIndustryJob`` per claimed board job, in ONE query.

    A match is the same product, installed by one of the job owner's characters,
    started after the board job was created, still active-ish, and not already
    linked to another BuildJob. Advisory only — the builder confirms; MRP applies
    the same heuristic conservatively at read time until they do.
    """
    from .models import CorpIndustryJob

    candidates = [
        j for j in jobs
        if j.owner_id and not j.esi_job_id
        and j.status in (BuildJob.Status.BUILDING, BuildJob.Status.BUILT)
    ]
    if not candidates:
        return {}
    owner_chars: dict[int, set[int]] = {}
    for job in candidates:
        if job.owner_id not in owner_chars:
            owner_chars[job.owner_id] = set(
                job.owner.characters.values_list("character_id", flat=True)
            )
    linked_ids = set(
        BuildJob.objects.filter(esi_job_id__isnull=False)
        .values_list("esi_job_id", flat=True)
    )
    esi_rows = list(
        CorpIndustryJob.objects.filter(
            product_type_id__in={j.output_type_id for j in candidates},
            status__in=("active", "paused", "ready"),
            activity_id__in=(1, 9),
        ).order_by("start_date")
    )
    out: dict[int, CorpIndustryJob] = {}
    for job in candidates:
        for row in esi_rows:
            if row.job_id in linked_ids or row.job_id in {
                r.job_id for r in out.values()
            }:
                continue
            if row.product_type_id != job.output_type_id:
                continue
            if row.installer_id not in owner_chars[job.owner_id]:
                continue
            if row.start_date and row.start_date < job.created_at:
                continue
            out[job.pk] = row
            break
    return out


@transaction.atomic
def link_esi_job(job: BuildJob, esi_job_id: int | None) -> tuple[bool, str]:
    """Link (or unlink, ``None``) a board job to the in-game ESI job it became.

    A linked job is excluded from MRP incoming — the ESI row carries the supply
    and the real end date (one physical build, one count). Returns (ok, code):
    codes ``linked``/``unlinked``/``bad_status``/``mismatch``/``taken``.
    """
    from .models import CorpIndustryJob

    locked = BuildJob.objects.select_for_update().get(pk=job.pk)
    if locked.status not in (BuildJob.Status.BUILDING, BuildJob.Status.BUILT):
        return False, "bad_status"
    if esi_job_id is None:
        locked.esi_job_id = None
        locked.save(update_fields=["esi_job_id", "updated_at"])
        job.esi_job_id = None
        return True, "unlinked"
    esi_row = CorpIndustryJob.objects.filter(job_id=esi_job_id).first()
    if esi_row and esi_row.product_type_id and esi_row.product_type_id != locked.output_type_id:
        return False, "mismatch"
    if BuildJob.objects.filter(esi_job_id=esi_job_id).exclude(pk=locked.pk).exists():
        return False, "taken"
    locked.esi_job_id = esi_job_id
    try:
        # The partial unique (uniq_buildjob_esi_job_id) closes the race the
        # pre-check above cannot — two links to one in-game job would exclude
        # both board jobs from incoming while only one ESI lot supplies.
        with transaction.atomic():
            locked.save(update_fields=["esi_job_id", "updated_at"])
    except IntegrityError:
        return False, "taken"
    job.esi_job_id = esi_job_id
    return True, "linked"
