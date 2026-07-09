"""Operations: list/create (officer), detail with readiness + prep tasks."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_POST

from core import rbac
from core.rbac import perm_required, role_required

from . import services
from .models import (
    Operation,
    OperationAttendance,
    OperationCancellation,
    OperationCommitment,
    OperationDoctrine,
    OperationRsvp,
    OperationShipSlot,
    OperationTemplate,
    OperationTemplateSlot,
)


def _main_character(user):
    return next((c for c in user.characters.all() if c.is_main), user.characters.first())


def _visible_op_or_404(request: HttpRequest, pk: int) -> Operation:
    """Fetch an operation the requester is allowed to see, else 404.

    op_list hides DRAFT ops from non-officers, so guessing a draft's pk must 404 too —
    for reads AND writes (rsvp/commit), so a member can neither read an unpublished op
    nor pollute it with an RSVP/commitment nor use these endpoints as an existence oracle.
    """
    op = get_object_or_404(Operation, pk=pk)
    if op.status == Operation.Status.DRAFT and not rbac.has_perm(request.user, rbac.PERM_FLEET_MANAGE):
        raise Http404("No such operation.")
    return op


@login_required
@role_required(rbac.ROLE_MEMBER)
def op_list(request: HttpRequest) -> HttpResponse:
    # Members see scheduled/active ops; officers also see their own drafts.
    visible = [Operation.Status.PLANNED, Operation.Status.ACTIVE]
    is_officer = rbac.has_perm(request.user, rbac.PERM_FLEET_MANAGE)
    if is_officer:
        visible.append(Operation.Status.DRAFT)
    ops = Operation.objects.filter(status__in=visible)
    cards = []
    for op in ops:
        plan = services.fleet_plan(op)
        cards.append({
            "op": op,
            "readiness": services.operation_readiness(op),
            "plan": plan,
            "attend_count": op.attendance.count(),
        })
    return render(
        request,
        "operations/list.html",
        {
            "cards": cards,
            "leaderboard": services.participation_leaderboard(),
            "is_officer": is_officer,
        },
    )


@login_required
@role_required(rbac.ROLE_MEMBER)
def op_detail(request: HttpRequest, pk: int) -> HttpResponse:
    op = _visible_op_or_404(request, pk)
    character = _main_character(request.user)
    attendance = list(op.attendance.all())
    my_rsvp = next((r for r in op.rsvps.all() if r.user_id == request.user.id), None)
    commitments = list(op.commitments.select_related("slot").all())
    my_commitment = next((c for c in commitments if c.user_id == request.user.id), None)
    # Sign-up roster: firm "Coming" and tentative "Maybe", each with their chosen ship.
    coming = [c for c in commitments if c.response == OperationCommitment.Response.YES]
    maybe = [c for c in commitments if c.response == OperationCommitment.Response.MAYBE]
    cant_make_it = [r for r in op.rsvps.all() if r.response == OperationRsvp.Response.NO]
    is_officer = rbac.has_perm(request.user, rbac.PERM_FLEET_MANAGE)
    # 4.19: on-grid composition vs plan (AAR) — officers only, once the op has begun.
    composition = None
    if is_officer and op.target_at and op.target_at <= timezone.now():
        from .composition import reconcile_composition
        composition = reconcile_composition(op)
    return render(
        request,
        "operations/detail.html",
        {
            "op": op,
            "readiness": services.operation_readiness(op),
            "plan": services.fleet_plan(op),
            "my_prep": services.pilot_readiness(op, character) if character else [],
            "is_officer": is_officer,
            "composition": composition,
            "announce_channels": _announce_channels(),
            "attendance": attendance,
            "attend_count": len(attendance),
            "i_attended": any(a.user_id == request.user.id for a in attendance),
            "rsvp": services.rsvp_summary(op),
            "my_rsvp": my_rsvp.response if my_rsvp else None,
            "my_commitment": my_commitment,
            "coming_signups": coming,
            "maybe_signups": maybe,
            "cant_make_it": cant_make_it,
        },
    )


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def op_rsvp(request: HttpRequest, pk: int) -> HttpResponse:
    """A pilot marks availability. In the new flow this is the ship-less "Can't make
    it"; coming/maybe go through the ship sign-up (op_commit)."""
    op = _visible_op_or_404(request, pk)
    response = request.POST.get("response", "")
    char = _main_character(request.user)
    if services.set_rsvp(op, request.user, response, char) is None:
        messages.error(request, "Pick coming, maybe or can't make it.")
        return redirect("operations:detail", pk=op.pk)
    if response == OperationRsvp.Response.NO:
        # Can't make it → drop any ship commitment so they're no longer counted.
        services.release_commitment(op, request.user)
        messages.success(request, "Noted — you're marked as unavailable.")
    else:
        messages.success(request, "Thanks — your availability is recorded.")
    return redirect("operations:detail", pk=op.pk)


@login_required
@perm_required(rbac.PERM_FLEET_MANAGE)
def ship_search(request: HttpRequest) -> JsonResponse:
    """Autocomplete endpoint for the custom-ship hull picker (ships only)."""
    from apps.sde.search import search_ships

    return JsonResponse(search_ships(request.GET.get("q", ""), limit=20), safe=False)


def _safe_url(value: str) -> str:
    """Keep only http(s) links; drop ``javascript:`` and other schemes (anti-XSS)."""
    url = (value or "").strip()
    if not url:
        return ""
    low = url.lower()
    if low.startswith(("http://", "https://")):
        return url[:500]
    return ""


def _aware(raw: str):
    """Parse a ``datetime-local`` value, treating a naive value as EVE/UTC."""
    import datetime as dt

    value = parse_datetime(raw or "")
    if value is None:
        return None
    if timezone.is_naive(value):
        value = value.replace(tzinfo=dt.UTC)
    return value


def _parse_slots(request) -> list[dict]:
    """Read the fleet-composition rows from the builder (skips blank rows).

    Each slot is one of two kinds:

    * ``doctrine`` — references an active ``DoctrineFit`` (``slot_fit_id``); the ship
      and EFT come from the doctrine, and pilots open the doctrine to see the fit.
    * ``custom`` — a one-off ship the organiser names (``slot_ship``) and optionally
      pastes an EFT for (``slot_eft``); the EFT, if present, is authoritative for the
      hull and is shown to pilots.
    """
    from apps.doctrines.fitparser import parse_eft
    from apps.doctrines.models import Doctrine, DoctrineFit
    from apps.sde.models import SdeType
    from apps.sde.search import resolve_type

    kinds = request.POST.getlist("slot_kind")
    fit_ids = request.POST.getlist("slot_fit_id")
    ships = request.POST.getlist("slot_ship")
    efts = request.POST.getlist("slot_eft")
    roles = request.POST.getlist("slot_role")
    mins = request.POST.getlist("slot_min")
    maxes = request.POST.getlist("slot_max")
    prios = request.POST.getlist("slot_priority")

    def _at(seq, i, default=""):
        return seq[i] if i < len(seq) else default

    def _int(value, default):
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return default

    # Validate the referenced doctrine fits in one query (only active doctrines).
    wanted = {f for f in fit_ids if f.isdigit()}
    fits = {
        str(f.id): f
        for f in DoctrineFit.objects.filter(
            id__in=wanted, doctrine__status=Doctrine.Status.ACTIVE
        ).select_related("doctrine")
    }
    fit_ship_names = dict(
        SdeType.objects.filter(type_id__in={f.ship_type_id for f in fits.values()})
        .values_list("type_id", "name")
    )

    n = max([len(kinds), len(ships), len(fit_ids), len(efts)])
    slots = []
    for i in range(n):
        kind = _at(kinds, i) or ("doctrine" if _at(fit_ids, i) else "custom")
        role = (_at(roles, i) or "").strip().lower()
        if role not in OperationShipSlot.Role.values:
            role = OperationShipSlot.Role.DPS
        max_raw = (_at(maxes, i) or "").strip()
        common = {
            "role": role,
            "min_pilots": max(1, _int(_at(mins, i), 1)),
            "max_pilots": (_int(max_raw, 0) or None) if max_raw else None,
            "priority": max(1, _int(_at(prios, i), i + 1)),
            "fitting_link": "",
        }
        if kind == "doctrine":
            fit = fits.get(_at(fit_ids, i))
            if fit is None:
                continue  # unknown / inactive doctrine fit — drop it
            slots.append({
                **common,
                "ship_name": fit_ship_names.get(fit.ship_type_id) or fit.name,
                "ship_type_id": fit.ship_type_id,
                "doctrine_fit": fit,
                "eft_text": "",
            })
        else:  # custom ship
            eft = (_at(efts, i) or "").strip()
            ship = (_at(ships, i) or "").strip()
            ship_name, ship_type_id = "", None
            if eft:
                try:
                    parsed = parse_eft(eft)
                except ValueError:
                    parsed = None
                if parsed and parsed["ship_type_id"]:
                    ship_name, ship_type_id = parsed["ship_name"], parsed["ship_type_id"]
            if not ship_type_id and ship:
                resolved = resolve_type(ship)
                ship_name = resolved.name if resolved else ship
                ship_type_id = resolved.type_id if resolved else None
            if not ship_name and not eft:
                continue  # genuinely empty row
            slots.append({
                **common,
                "ship_name": (ship_name or ship or "Custom ship")[:200],
                "ship_type_id": ship_type_id,
                "doctrine_fit": None,
                "eft_text": eft,
            })
    return slots


def _dt_local(value) -> str:
    """Format an aware datetime as a ``datetime-local`` string (EVE/UTC)."""
    import datetime as dt

    if not value:
        return ""
    return value.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M")


def _slot_to_data(s) -> dict:
    """Normalise a slot (a parsed dict or a saved model row) for the JS builder."""
    if isinstance(s, dict):
        fit = s.get("doctrine_fit")
        return {
            "kind": "doctrine" if fit else "custom",
            "fit_id": fit.id if fit else "",
            "doctrine": fit.doctrine.name if fit else "",
            "doctrine_id": fit.doctrine_id if fit else "",
            "ship_name": s.get("ship_name", ""),
            "ship_type_id": s.get("ship_type_id") or "",
            "role": s.get("role", "dps"),
            "eft": s.get("eft_text", ""),
            "priority": s.get("priority", 1),
            "min": s.get("min_pilots", 1),
            "max": s.get("max_pilots") or "",
        }
    return {
        "kind": "doctrine" if s.is_doctrine else "custom",
        "fit_id": s.doctrine_fit_id or "",
        "doctrine": s.doctrine_name,
        "doctrine_id": s.doctrine_id or "",
        "ship_name": s.ship_name,
        "ship_type_id": s.ship_type_id or "",
        "role": s.role,
        "eft": s.eft_text,
        "priority": s.priority,
        "min": s.min_pilots,
        "max": s.max_pilots or "",
    }


def _op_form_context(request, op=None, *, values=None, errors=None, slots=None):
    """Shared context for the create/edit form, prefilled from an op or POST values."""
    v = values or {}

    def field(name, op_attr=None, default=""):
        # On a validation re-render, POST values win; otherwise read from the op.
        if values is not None:
            return v.get(name, default)
        if op is not None:
            return getattr(op, op_attr or name, default)
        return default

    if values is not None:
        rsvp_mode = v.get("rsvp_mode", "none")
        target_local = v.get("target_at", "")
        deadline_local = v.get("rsvp_deadline", "")
    elif op is not None:
        rsvp_mode = "relative" if op.rsvp_offset_minutes else ("absolute" if op.rsvp_deadline else "none")
        target_local = _dt_local(op.target_at)
        deadline_local = _dt_local(op.rsvp_deadline)
    else:
        rsvp_mode, target_local, deadline_local = "none", "", ""

    f = {
        "name": field("name"),
        "type": field("type", default=Operation.Type.PVP),
        "target_at": target_local,
        "duration_minutes": field("duration_minutes") or "",
        "formup": field("formup"),
        "destination": field("destination"),
        "comms": field("comms"),
        "link": field("link"),
        "notes": field("notes"),
        "min_pilots": field("min_pilots") or "",
        "rsvp_mode": rsvp_mode,
        "rsvp_deadline": deadline_local,
        "rsvp_offset_minutes": field("rsvp_offset_minutes") or "",
        "srp": field("srp"),
        "status": field("status", default=Operation.Status.PLANNED),
    }

    if slots is not None:
        slot_list = slots
    elif op is not None:
        slot_list = list(op.ship_slots.all())
    else:
        slot_list = []

    return {
        "op": op,
        "f": f,
        "errors": errors or {},
        "slots_data": [_slot_to_data(s) for s in slot_list],
        "catalogue": services.doctrine_fit_catalogue(),
        "types": Operation.Type.choices,
        "statuses": Operation.Status.choices,
        "roles": OperationShipSlot.Role.choices,
        "srp_choices": Operation.Srp.choices,
        "pvp_types": sorted(Operation.PVP_TYPES),
    }


def _apply_op_form(request, op, slots):
    """Validate + persist the operation fields and ship slots. Returns error dict."""
    errors: dict[str, str] = {}
    name = (request.POST.get("name") or "").strip()
    if not name:
        errors["name"] = "An operation needs a name."

    op_type = request.POST.get("type")
    if op_type not in Operation.Type.values:
        op_type = Operation.Type.PVP

    target_at = _aware(request.POST.get("target_at"))

    # RSVP deadline: either an absolute datetime or a relative "minutes before form-up".
    rsvp_mode = request.POST.get("rsvp_mode") or "none"
    rsvp_deadline = None
    rsvp_offset = None
    if rsvp_mode == "absolute":
        rsvp_deadline = _aware(request.POST.get("rsvp_deadline"))
    elif rsvp_mode == "relative":
        try:
            rsvp_offset = max(0, int(request.POST.get("rsvp_offset_minutes") or 0)) or None
        except (TypeError, ValueError):
            rsvp_offset = None
        if rsvp_offset and target_at:
            import datetime as dt

            rsvp_deadline = target_at - dt.timedelta(minutes=rsvp_offset)
    if rsvp_deadline and target_at and rsvp_deadline >= target_at:
        errors["rsvp_deadline"] = "The sign-up deadline must be before form-up."

    def _intval(field, default=0):
        try:
            return max(0, int(request.POST.get(field) or default))
        except (TypeError, ValueError):
            return default

    min_pilots = _intval("min_pilots", 0)

    # Composition sanity: the required ship slots should sum to the head-count.
    slot_min_total = sum(s["min_pilots"] for s in slots)
    mismatch = bool(slots) and min_pilots and slot_min_total != min_pilots
    if mismatch and request.POST.get("confirm_mismatch") != "1":
        errors["composition"] = (
            f"Your ship slots require {slot_min_total} pilots but the minimum is "
            f"{min_pilots}. Adjust them, or tick “proceed anyway”."
        )

    srp = request.POST.get("srp") or ""
    if srp not in dict(Operation.Srp.choices):
        srp = ""

    status = request.POST.get("status")
    if status not in Operation.Status.values:
        status = Operation.Status.PLANNED

    if errors:
        return errors

    op.name = name
    op.type = op_type
    op.target_at = target_at
    op.duration_minutes = _intval("duration_minutes", 0) or None
    op.formup = (request.POST.get("formup") or "").strip()[:200]
    op.destination = (request.POST.get("destination") or "").strip()[:200]
    op.comms = (request.POST.get("comms") or "").strip()[:200]
    op.link = _safe_url(request.POST.get("link"))
    op.notes = (request.POST.get("notes") or "").strip()
    op.min_pilots = min_pilots
    op.rsvp_deadline = rsvp_deadline
    op.rsvp_offset_minutes = rsvp_offset
    op.srp = srp
    op.status = status
    if op.created_by_id is None:
        op.created_by = request.user
    if op.fc_id is None:
        op.fc = request.user
    op.save()

    # Replace the ship-slot set wholesale (simplest correct behaviour on edit).
    op.ship_slots.all().delete()
    OperationShipSlot.objects.bulk_create([
        OperationShipSlot(operation=op, **s) for s in slots
    ])

    # Derive the readiness doctrines from the doctrine ships chosen in the fleet
    # composition above: each doctrine slot makes its doctrine "required", and the
    # pilots wanted on that doctrine is the sum of those slots' minimums. This
    # drives the corp- and pilot-readiness sections on the detail page with no
    # separate pick step (custom EFT ships carry no doctrine, so they're skipped).
    op.doctrines.all().delete()
    wanted: dict[int, int] = {}
    for s in slots:
        fit = s.get("doctrine_fit")
        if fit is not None:
            wanted[fit.doctrine_id] = wanted.get(fit.doctrine_id, 0) + s["min_pilots"]
    OperationDoctrine.objects.bulk_create([
        OperationDoctrine(operation=op, doctrine_id=did, target_count=target)
        for did, target in wanted.items()
    ])
    return {}


@login_required
@perm_required(rbac.PERM_FLEET_MANAGE)
def op_create(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        slots = _parse_slots(request)
        op = Operation()
        errors = _apply_op_form(request, op, slots)
        if errors:
            for msg in errors.values():
                messages.error(request, msg)
            return render(request, "operations/form.html",
                          _op_form_context(request, values=request.POST, errors=errors, slots=slots))
        if request.POST.get("announce") == "1" and op.status != Operation.Status.DRAFT:
            if not _announce_op(request, op):
                messages.warning(request, "No Discord webhook configured — operation not announced.")
        messages.success(request, f"Operation created: {op.name}")
        return redirect("operations:detail", pk=op.pk)
    return render(request, "operations/form.html", _op_form_context(request))


@login_required
@perm_required(rbac.PERM_FLEET_MANAGE)
def op_edit(request: HttpRequest, pk: int) -> HttpResponse:
    op = get_object_or_404(Operation, pk=pk)
    if request.method == "POST":
        slots = _parse_slots(request)
        errors = _apply_op_form(request, op, slots)
        if errors:
            for msg in errors.values():
                messages.error(request, msg)
            return render(request, "operations/form.html",
                          _op_form_context(request, op=op, values=request.POST, errors=errors, slots=slots))
        messages.success(request, "Operation updated.")
        return redirect("operations:detail", pk=op.pk)
    return render(request, "operations/form.html", _op_form_context(request, op=op))


@login_required
@perm_required(rbac.PERM_FLEET_MANAGE)
@require_POST
def op_generate_tasks(request: HttpRequest, pk: int) -> HttpResponse:
    """Turn an operation's coverage gaps into open 'prepare' tasks (idempotent)."""
    from apps.tasks.models import Task

    op = get_object_or_404(Operation, pk=pk)
    readiness = services.operation_readiness(op)
    created = 0
    for gap in readiness["gaps"]:
        related_id = f"{op.pk}:{gap['doctrine_id']}"
        if Task.objects.filter(
            related_type="operation", related_id=related_id,
            status__in=[Task.Status.OPEN, Task.Status.CLAIMED, Task.Status.IN_PROGRESS],
        ).exists():
            continue
        Task.objects.create(
            type=Task.Type.PREPARE, title=f"Prep {gap['label']} for {op.name}",
            is_open=True, status=Task.Status.OPEN, priority=12, created_by=request.user,
            related_type="operation", related_id=related_id,
        )
        created += 1
    messages.success(request, f"Generated {created} prep task{'' if created == 1 else 's'}.")
    return redirect("operations:detail", pk=op.pk)


@login_required
@role_required(rbac.ROLE_MEMBER)
def timer_board(request: HttpRequest) -> HttpResponse:
    """Upcoming structure / sov timers with live countdowns."""
    import datetime as dt

    from .models import StructureTimer

    cutoff = timezone.now() - dt.timedelta(hours=3)  # keep just-passed timers briefly
    timers = StructureTimer.objects.filter(exits_at__gte=cutoff)
    return render(request, "operations/timers.html", {
        "timers": timers,
        "is_officer": rbac.has_perm(request.user, rbac.PERM_FLEET_MANAGE),
        "timer_types": StructureTimer.TimerType.choices,
        "sides": StructureTimer.Side.choices,
        "announce_channels": _announce_channels(),
    })


@login_required
@perm_required(rbac.PERM_FLEET_MANAGE)
def sov_board(request: HttpRequest) -> HttpResponse:
    """Our alliance's sov structures with ADM and vulnerability windows."""
    from .models import SovStructure

    if request.method == "POST":
        from .sov_esi import sync_sovereignty

        result = sync_sovereignty()
        if result["status"] == "ok":
            messages.success(request, f"Sovereignty synced — {result['count']} structure(s).")
        elif result["status"] == "no_alliance":
            messages.warning(request, "The home corp isn't in an alliance, so it holds no sov.")
        else:
            messages.error(request, "Sovereignty sync failed; try again later.")
        return redirect("operations:sov")

    rows = list(SovStructure.objects.all())
    return render(request, "operations/sov.html", {
        "structures": rows,
        "soft": sum(1 for s in rows if s.is_soft),
        "is_officer": rbac.has_perm(request.user, rbac.PERM_FLEET_MANAGE),
    })


@login_required
@perm_required(rbac.PERM_FLEET_MANAGE)
@require_POST
def timer_add(request: HttpRequest) -> HttpResponse:
    from .services import add_structure_timer

    name = (request.POST.get("name") or "").strip()
    exits_at = parse_datetime(request.POST.get("exits_at") or "")
    if not name or exits_at is None:
        messages.error(request, "A timer needs a name and an exit time.")
        return redirect("operations:timers")
    channels = _selected_channels(request)
    announce = request.POST.get("announce") == "1"
    # An explicit empty selection (picker shown, every channel un-ticked) means "don't
    # announce" — never fall back to fanning out to every armed channel. channels is None
    # only when no picker was offered, where None ⇒ every armed channel is the intent.
    if announce and channels is not None and not channels:
        announce = False
    # One entry point: creates the StructureTimer, mirrors it onto the Pingboard calendar,
    # and (if announced) fans the alert across the ticked armed channels — not Discord only.
    add_structure_timer(
        name=name, exits_at=exits_at,
        timer_type=request.POST.get("timer_type") or "",
        side=request.POST.get("side") or "",
        system_name=request.POST.get("system_name") or "",
        structure_type=request.POST.get("structure_type") or "",
        notes=request.POST.get("notes") or "",
        announce=announce, channels=channels,
        created_by=request.user,
    )
    messages.success(request, "Timer added.")
    return redirect("operations:timers")


@login_required
@perm_required(rbac.PERM_FLEET_MANAGE)
@require_POST
def timer_remove(request: HttpRequest, pk: int) -> HttpResponse:
    from .models import StructureTimer
    from .services import unpublish_structure_timer

    StructureTimer.objects.filter(pk=pk).delete()
    unpublish_structure_timer(pk)  # retire its calendar mirror too
    messages.success(request, "Timer removed.")
    return redirect("operations:timers")


def _op_ping_text(op, url: str) -> str:
    """A markdown announcement for an operation (doubles as Discord/plain text)."""
    lines = [f"📣 **{op.name}** — {op.get_type_display()}"]
    if op.target_at:
        lines.append(f"🕒 {op.target_at:%a %d %b · %H:%M} EVE")
    docs = [od.doctrine.name for od in op.doctrines.all()]
    if docs:
        lines.append("🚀 " + ", ".join(docs))
    if op.notes:
        lines.append(op.notes)
    lines.append(url)
    return "\n".join(lines)


def _announce_channels():
    """(kind, label) for every armed channel, for the announce picker. Best-effort."""
    try:
        from apps.pingboard.services import enabled_channel_kinds

        return enabled_channel_kinds()
    except Exception:  # noqa: BLE001 - never 500 the detail page over channel discovery
        return []


def _selected_channels(request):
    """Channels the officer ticked in the announce picker.

    Returns the ticked kinds as a list (possibly empty — an explicit "send to nothing")
    when a picker was shown, or ``None`` only when no picker was offered at all (so the
    caller falls back to every armed channel). Only known-enabled kinds are honoured.
    """
    offered = [k for k, _ in _announce_channels()]
    if not offered:
        return None
    return [k for k in offered if request.POST.get(f"channel_{k}") == "on"]


def _announce_op(request, op, channels=None):
    url = request.build_absolute_uri(reverse("operations:detail", args=[op.pk]))
    return services.notify_operation(
        op, source_suffix="announce", title=op.name, body=_op_ping_text(op, url),
        channels=channels, created_by=request.user,
    )


@login_required
@perm_required(rbac.PERM_FLEET_MANAGE)
@require_POST
def op_announce(request: HttpRequest, pk: int) -> HttpResponse:
    """Officer broadcasts this operation across the corp's armed channels (Discord,
    in-app, EVE-mail, Telegram, WhatsApp, Slack) via Pingboard."""
    op = get_object_or_404(Operation, pk=pk)
    channels = _selected_channels(request)
    if channels is not None and not channels:
        messages.warning(request, "Pick at least one channel to announce to.")
        return redirect("operations:detail", pk=op.pk)
    alert = _announce_op(request, op, channels=channels)
    if alert is None:
        messages.warning(
            request,
            "Announcement not sent — alerting is disabled, no channel is armed, or it was "
            "suppressed as a duplicate. Arm a channel in the Admin Console → Pingboard.",
        )
    else:
        labels = ", ".join(dict(_announce_channels()).get(k, k) for k in alert.channels) or "no channel"
        messages.success(request, f"Announcement queued to: {labels}.")
    return redirect("operations:detail", pk=op.pk)


def _credit_fleet(user, op) -> None:
    from apps.pilots.models import ContributionEvent
    from apps.pilots.services import record_contribution

    record_contribution(
        user, ContributionEvent.Kind.FLEET, 1, "fleets",
        description=op.name, ref_type="operation", ref_id=f"{op.pk}:{user.pk}",
        occurred_at=op.target_at or timezone.now(),
    )


def _uncredit_fleet(user, op) -> None:
    from apps.pilots.models import ContributionEvent

    ContributionEvent.objects.filter(
        kind=ContributionEvent.Kind.FLEET, ref_type="operation",
        ref_id=f"{op.pk}:{user.pk}",
    ).delete()


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def op_attend(request: HttpRequest, pk: int) -> HttpResponse:
    """A pilot self-reports attending this operation (PAP)."""
    op = _visible_op_or_404(request, pk)  # a member must not self-credit on a hidden DRAFT op
    char = next((c for c in request.user.characters.all() if c.is_main),
                request.user.characters.first())
    OperationAttendance.objects.update_or_create(
        operation=op, user=request.user,
        defaults={
            "character_id": char.character_id if char else None,
            "character_name": char.name if char else request.user.get_username(),
        },
    )
    # OPS-2 (3.1): a self-reported PAP no longer credits the recognition ledger or raffle
    # tickets on its own — credit follows FC/officer confirmation (or the ESI fleet-pull),
    # so the leaderboard and raffle stay fair. Crediting happens in op_attendance_action.
    messages.success(
        request, "You're on the participation roster — an officer will confirm attendance."
    )
    return redirect("operations:detail", pk=op.pk)


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def op_unattend(request: HttpRequest, pk: int) -> HttpResponse:
    op = _visible_op_or_404(request, pk)
    OperationAttendance.objects.filter(operation=op, user=request.user).delete()
    _uncredit_fleet(request.user, op)
    messages.success(request, "Removed you from the roster.")
    return redirect("operations:detail", pk=op.pk)


@login_required
@perm_required(rbac.PERM_FLEET_MANAGE)
@require_POST
def op_pull_fleet(request: HttpRequest, pk: int) -> HttpResponse:
    """FC pulls the live fleet roster → auto-records attendance for linked members."""
    from .fleet_esi import pull_fleet_attendance

    op = get_object_or_404(Operation, pk=pk)
    char = next((c for c in request.user.characters.all() if c.is_main),
                request.user.characters.first())
    if char is None:
        messages.error(request, "Link a character first.")
        return redirect("operations:detail", pk=op.pk)

    result = pull_fleet_attendance(op, char)
    status = result["status"]
    if status == "ok":
        messages.success(
            request,
            f"Recorded {result['recorded']} of {result['fleet_size']} fleet members "
            "(only those with a linked account).",
        )
    elif status == "no_token":
        messages.warning(request, "Grant the fleet-tracking scope on the ESI Scopes page first.")
    elif status == "not_in_fleet":
        messages.warning(request, "You're not in a fleet right now.")
    elif status == "not_boss":
        messages.warning(request, "Only the fleet boss can read the fleet roster.")
    else:
        messages.error(request, "Couldn't read the fleet; try again.")
    return redirect("operations:detail", pk=op.pk)


@login_required
@perm_required(rbac.PERM_FLEET_MANAGE)
@require_POST
def op_attendance_action(request: HttpRequest, pk: int) -> HttpResponse:
    """FC / officer confirms, unconfirms or removes a roster entry."""
    op = get_object_or_404(Operation, pk=pk)
    att = get_object_or_404(OperationAttendance, pk=request.POST.get("att_id"), operation=op)
    action = request.POST.get("action")
    if action == "confirm":
        att.confirmed = True
        att.save(update_fields=["confirmed", "updated_at"])
        # OPS-2 (3.1): credit follows confirmation. record_contribution is idempotent on the
        # (operation, pilot) ref, so re-confirming an ESI-pulled row won't double-credit.
        _credit_fleet(att.user, op)
    elif action == "unconfirm":
        att.confirmed = False
        att.save(update_fields=["confirmed", "updated_at"])
        _uncredit_fleet(att.user, op)  # pull the credit back if it was confirmed
    elif action == "remove":
        _uncredit_fleet(att.user, op)
        att.delete()
    return redirect("operations:detail", pk=op.pk)


@login_required
@perm_required(rbac.PERM_FLEET_MANAGE)
@require_POST
def op_status(request: HttpRequest, pk: int) -> HttpResponse:
    op = get_object_or_404(Operation, pk=pk)
    to = request.POST.get("status")
    if to in Operation.Status.values:
        # A move into manual-cancelled is recorded for analytics, like the auto job.
        if to == Operation.Status.CANCELLED and not op.is_cancelled:
            from .models import OperationCancellation

            services.record_cancellation(op, OperationCancellation.Reason.MANUAL)
        op.status = to
        op.save(update_fields=["status", "updated_at"])
        messages.success(request, f"Operation marked {op.get_status_display()}.")
    return redirect("operations:detail", pk=op.pk)


@login_required
@perm_required(rbac.PERM_FLEET_MANAGE)
def op_cancellation_analytics(request: HttpRequest) -> HttpResponse:
    """OPS-8: in-app trends over OperationCancellation snapshots (why ops fall
    through) — previously visible only in Django admin."""
    import datetime as dt

    from django.db.models import Count
    from django.db.models.functions import TruncWeek

    ranges = {"30d": 30, "90d": 90, "180d": 180, "1y": 365}
    rkey = request.GET.get("range", "90d")
    days = ranges.get(rkey, 90)
    since = timezone.now() - dt.timedelta(days=days)

    qs = OperationCancellation.objects.filter(created_at__gte=since)
    reason_labels = dict(OperationCancellation.Reason.choices)

    # Weekly buckets split by reason -> a stacked bar of cancellations over time.
    buckets = (
        qs.annotate(week=TruncWeek("created_at"))
        .values("week", "reason")
        .annotate(n=Count("id"))
        .order_by("week")
    )
    weeks = sorted({b["week"] for b in buckets})
    week_index = {w: i for i, w in enumerate(weeks)}
    per_reason = {key: [0] * len(weeks) for key in reason_labels}
    for b in buckets:
        per_reason.setdefault(b["reason"], [0] * len(weeks))[week_index[b["week"]]] += b["n"]
    chart = {
        "labels": [w.strftime("%d %b") for w in weeks],
        "series": [{"label": reason_labels[key], "data": per_reason[key]} for key in reason_labels],
    }

    # Summary cards: totals by reason, by op type, and the average pilot shortfall.
    total = qs.count()
    by_reason = [{"label": reason_labels[key], "count": sum(per_reason[key])} for key in reason_labels]
    # operation_type is stored as a raw code (no choices on the snapshot field), so
    # map it to the Operation.Type label for display — matching the other op views.
    type_labels = dict(Operation.Type.choices)
    by_type = [
        {"label": type_labels.get(r["operation_type"], r["operation_type"] or "—"), "n": r["n"]}
        for r in qs.values("operation_type").annotate(n=Count("id")).order_by("-n")[:8]
    ]
    shortfalls = [
        max(0, (c.min_pilots or 0) - (c.confirmed_at_deadline or 0))
        for c in qs.only("min_pilots", "confirmed_at_deadline")
    ]
    avg_shortfall = round(sum(shortfalls) / len(shortfalls), 1) if shortfalls else 0

    return render(request, "operations/cancellations.html", {
        "chart": chart,
        "ranges": list(ranges),
        "active_range": rkey,
        "enough": total > 0,
        "total": total,
        "by_reason": by_reason,
        "by_type": by_type,
        "avg_shortfall": avg_shortfall,
    })


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def op_commit(request: HttpRequest, pk: int) -> HttpResponse:
    """A pilot signs up for a requested ship, saying if they're coming or maybe.

    This is the single confirmation flow: choosing a ship *is* how a pilot says
    they'll attend (or might). Race-safe and switch-safe.
    """
    op = _visible_op_or_404(request, pk)
    char = _main_character(request.user)
    response = request.POST.get("response") or OperationCommitment.Response.YES
    outcome = services.claim_slot(op, request.user, request.POST.get("slot_id"), char, response)
    if outcome == services.CLAIM_OK:
        # Signing up clears any earlier "can't make it".
        op.rsvps.filter(user=request.user, response=OperationRsvp.Response.NO).delete()
        coming = response != OperationCommitment.Response.MAYBE
        messages.success(
            request,
            "You're down as coming — see you on the fleet." if coming
            else "Marked as a maybe — let the FC know if you firm up.",
        )
    elif outcome == services.CLAIM_FULL:
        messages.warning(request, "That ship is already full — pick another from the list.")
    elif outcome == services.CLAIM_CLOSED:
        messages.warning(request, "Sign-ups for this operation are closed.")
    else:
        messages.error(request, "Pick one of the requested ships first.")
    return redirect("operations:detail", pk=op.pk)


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def op_uncommit(request: HttpRequest, pk: int) -> HttpResponse:
    op = _visible_op_or_404(request, pk)
    if not op.is_open_for_signup and op.status in Operation.CLOSED_STATUSES:
        messages.warning(request, "This operation is closed; your commitment stands.")
        return redirect("operations:detail", pk=op.pk)
    services.release_commitment(op, request.user)
    messages.success(request, "You've withdrawn from this operation.")
    return redirect("operations:detail", pk=op.pk)


@login_required
@perm_required(rbac.PERM_FLEET_MANAGE)
@require_POST
def op_override(request: HttpRequest, pk: int) -> HttpResponse:
    """Organiser forces the op viable (or clears the override) despite unmet minimums."""
    op = get_object_or_404(Operation, pk=pk)
    op.requirements_overridden = request.POST.get("override") == "1"
    op.override_note = (request.POST.get("override_note") or "").strip()[:200]
    op.save(update_fields=["requirements_overridden", "override_note", "updated_at"])
    if op.requirements_overridden:
        messages.success(request, "Override set — this op will run regardless of the minimum.")
    else:
        messages.success(request, "Override cleared.")
    return redirect("operations:detail", pk=op.pk)


# --------------------------------------------------------------------------- #
#  OPS-4 (3.12): recurring op templates (officer CRUD)
# --------------------------------------------------------------------------- #
def _parse_template_slots(request) -> list[dict]:
    """Read composition rows from the template form (skips blank ship rows)."""
    from apps.sde.search import resolve_type

    ships = request.POST.getlist("slot_ship")
    roles = request.POST.getlist("slot_role")
    mins = request.POST.getlist("slot_min")
    maxes = request.POST.getlist("slot_max")
    prios = request.POST.getlist("slot_priority")
    out = []
    for i, ship in enumerate(ships[:50]):  # cap rows — a crafted POST shouldn't fan out inserts
        ship = (ship or "").strip()
        if not ship:
            continue
        def _int(seq, idx, default):
            try:
                return max(0, int(seq[idx]))
            except (IndexError, ValueError):
                return default
        type_id = None
        match = resolve_type(ship)
        if match:
            type_id, ship = match[0], match[1] or ship
        out.append({
            "ship_name": ship[:200], "ship_type_id": type_id,
            "role": roles[i] if i < len(roles) and roles[i] in
                    dict(OperationShipSlot.Role.choices) else OperationShipSlot.Role.DPS,
            "min_pilots": _int(mins, i, 1), "max_pilots": _int(maxes, i, 0),
            "priority": _int(prios, i, 1),
        })
    return out


def _apply_template(request, template) -> dict:
    """Apply the template form to ``template`` (unsaved or existing); returns errors, else {}."""
    p = request.POST
    errors: dict[str, str] = {}
    name = (p.get("name") or "").strip()
    if not name:
        errors["name"] = "Give the template a name."

    def _clamp(field, lo, hi, default):
        try:
            return min(hi, max(lo, int(p.get(field, default))))
        except (TypeError, ValueError):
            return default

    if errors:
        return errors
    template.name = name[:200]
    template.type = p.get("type") if p.get("type") in dict(Operation.Type.choices) else Operation.Type.PVP
    template.srp = p.get("srp") if p.get("srp") in dict(Operation.Srp.choices) else ""
    template.formup = (p.get("formup") or "")[:200]
    template.destination = (p.get("destination") or "")[:200]
    template.comms = (p.get("comms") or "")[:200]
    template.link = _safe_url(p.get("link"))  # drop javascript: etc. like the op form
    template.notes = p.get("notes") or ""
    template.duration_minutes = _clamp("duration_minutes", 0, 100000, 60)
    template.min_pilots = _clamp("min_pilots", 0, 100000, 0)
    template.rsvp_offset_minutes = _clamp("rsvp_offset_minutes", 0, 100000, 0)
    template.weekday = _clamp("weekday", 0, 6, 5)
    template.hour = _clamp("hour", 0, 23, 20)
    template.minute = _clamp("minute", 0, 59, 0)
    template.lead_days = _clamp("lead_days", 1, 60, 10)
    template.active = p.get("active") == "1"
    if template.created_by_id is None:
        template.created_by = request.user
    template.save()
    slots = _parse_template_slots(request)
    template.slots.all().delete()
    for s in slots:
        OperationTemplateSlot.objects.create(template=template, **s)
    return {}


@login_required
@perm_required(rbac.PERM_FLEET_MANAGE)
def op_templates(request: HttpRequest) -> HttpResponse:
    from django.db.models import Count

    templates = list(OperationTemplate.objects.prefetch_related("slots")
                     .annotate(n_instances=Count("instances")))
    return render(request, "operations/templates.html", {"templates": templates,
                  "weekdays": _WEEKDAYS})


@login_required
@perm_required(rbac.PERM_FLEET_MANAGE)
def op_template_create(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        template = OperationTemplate()
        errors = _apply_template(request, template)
        if errors:
            for msg in errors.values():
                messages.error(request, msg)
            return render(request, "operations/template_form.html",
                          _template_form_context(request, values=request.POST))
        messages.success(request, f"Template created: {template.name}")
        return redirect("operations:templates")
    return render(request, "operations/template_form.html", _template_form_context(request))


@login_required
@perm_required(rbac.PERM_FLEET_MANAGE)
def op_template_edit(request: HttpRequest, pk: int) -> HttpResponse:
    template = get_object_or_404(OperationTemplate, pk=pk)
    if request.method == "POST":
        errors = _apply_template(request, template)
        if errors:
            for msg in errors.values():
                messages.error(request, msg)
            return render(request, "operations/template_form.html",
                          _template_form_context(request, template=template, values=request.POST))
        messages.success(request, "Template updated.")
        return redirect("operations:templates")
    return render(request, "operations/template_form.html",
                  _template_form_context(request, template=template))


@login_required
@perm_required(rbac.PERM_FLEET_MANAGE)
@require_POST
def op_template_toggle(request: HttpRequest, pk: int) -> HttpResponse:
    template = get_object_or_404(OperationTemplate, pk=pk)
    template.active = not template.active
    template.save(update_fields=["active", "updated_at"])
    messages.success(request, f"Template {'activated' if template.active else 'paused'}.")
    return redirect("operations:templates")


@login_required
@perm_required(rbac.PERM_FLEET_MANAGE)
@require_POST
def op_template_delete(request: HttpRequest, pk: int) -> HttpResponse:
    template = get_object_or_404(OperationTemplate, pk=pk)
    template.delete()  # spawned ops keep running (recurring_template SET_NULL)
    messages.success(request, "Template deleted. Already-spawned operations are unaffected.")
    return redirect("operations:templates")


@login_required
@perm_required(rbac.PERM_FLEET_MANAGE)
@require_POST
def op_template_run(request: HttpRequest) -> HttpResponse:
    """Materialise upcoming instances now, rather than waiting for the beat."""
    from .services import materialize_recurring_ops

    n = materialize_recurring_ops()["created"]
    messages.success(request, f"Materialised {n} upcoming operation instance(s).")
    return redirect("operations:templates")


_WEEKDAYS = [(0, "Monday"), (1, "Tuesday"), (2, "Wednesday"), (3, "Thursday"),
             (4, "Friday"), (5, "Saturday"), (6, "Sunday")]


def _template_form_context(request, template=None, *, values=None):
    return {
        "template": template, "values": values,
        "types": Operation.Type.choices, "srps": Operation.Srp.choices,
        "roles": OperationShipSlot.Role.choices, "weekdays": _WEEKDAYS,
        "hours": range(24), "slots": list(template.slots.all()) if template else [],
    }
