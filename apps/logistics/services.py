"""Freight service helpers: the active rate card, access control, contracts."""
from __future__ import annotations

import logging
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from apps.sde.models import SdeSolarSystem

from .models import Audience, CourierContract, RateCard
from .pricing import Quote

_AUDIENCE_CACHE_KEY = "logi:audience"


def active_rate_card() -> RateCard:
    """The rate card every quote uses. Seeds the default the first time."""
    card = RateCard.objects.filter(is_active=True).order_by("-updated_at").first()
    if card is None:
        card = RateCard.objects.create(name="Standard", is_active=True)
    return card


def current_audience() -> str:
    """The service audience (cached; refreshed when the rate card is saved).

    Read-only: never creates a row (it runs in a context processor on every
    page), falling back to the model default when no card exists yet.
    """
    cached = cache.get(_AUDIENCE_CACHE_KEY)
    if cached is None:
        cached = (
            RateCard.objects.filter(is_active=True)
            .order_by("-updated_at")
            .values_list("audience", flat=True)
            .first()
            or RateCard._meta.get_field("audience").default
        )
        cache.set(_AUDIENCE_CACHE_KEY, cached, 300)
    return cached


def invalidate_audience_cache() -> None:
    cache.delete(_AUDIENCE_CACHE_KEY)


def can_access(user) -> bool:
    """Whether ``user`` may use the freight service under the current audience."""
    audience = current_audience()
    if audience == Audience.PUBLIC:
        return True
    if audience == Audience.DISABLED:
        return False
    from core import rbac

    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False) or rbac.has_role(user, rbac.ROLE_MEMBER):
        return True
    if audience == Audience.ALLIANCE:
        from apps.corporation.access import is_service_alliance_pilot

        return is_service_alliance_pilot(user)
    return False


def service_enabled() -> bool:
    return current_audience() != Audience.DISABLED


def is_public() -> bool:
    return current_audience() == Audience.PUBLIC


def resolve_system(name: str) -> SdeSolarSystem | None:
    """Exact (case-insensitive) system lookup for the route pickers."""
    name = (name or "").strip()
    if not name:
        return None
    return (
        SdeSolarSystem.objects.filter(name__iexact=name).first()
        or SdeSolarSystem.objects.filter(name__istartswith=name).order_by("name").first()
    )


def create_contract_from_quote(
    *,
    quote: Quote,
    card: RateCard,
    origin: dict,
    dest: dict,
    ship_class: str,
    volume_m3: float,
    collateral,
    rush: bool,
    posted_as_kind: str = "character",
    posted_as_id: int | None = None,
    posted_as_name: str = "",
    created_by=None,
) -> CourierContract:
    """Persist a priced quote as an outstanding contract pilots can claim.

    ``origin``/``dest`` are resolved location dicts ({kind,id,name,system_id}).
    """
    bd = quote.breakdown
    contract = CourierContract.objects.create(
        origin_name=(origin.get("name") or "").strip(),
        origin_system_id=origin.get("system_id"),
        origin_location_kind=origin.get("kind") or "system",
        origin_location_id=origin.get("id"),
        dest_name=(dest.get("name") or "").strip(),
        dest_system_id=dest.get("system_id"),
        dest_location_kind=dest.get("kind") or "system",
        dest_location_id=dest.get("id"),
        jumps=bd.get("jumps", 0),
        lowsec_jumps=bd.get("lowsec_jumps", 0),
        sec_band=bd.get("sec_band", "highsec"),
        ship_class=ship_class,
        volume_m3=volume_m3,
        collateral=collateral or 0,
        rush=rush,
        reward=quote.reward,
        breakdown=bd,
        status=CourierContract.Status.OUTSTANDING,
        posted_as_kind=posted_as_kind,
        posted_as_id=posted_as_id,
        posted_as_name=(posted_as_name or "").strip(),
        deadline=timezone.now() + timedelta(days=card.contract_days),
        created_by=created_by,
    )
    from apps.pingboard import hooks

    # The contract's destination is ``dest_name`` — the old ``end_system_name`` attr
    # does not exist, so ``{destination_system}`` always rendered empty. Populate the
    # real origin/destination/reward/jumps so the seeded hauler alert is informative.
    hooks.fire(
        "logistics.new", source_object_id=contract.id, dedup_suffix="new",
        context={
            "destination_system": contract.dest_name or "",
            "origin_system": contract.origin_name or "",
            "reward": f"{int(contract.reward):,}",
            "jumps": contract.jumps,
        },
    )
    return contract


def poster_identity(user, kind: str) -> dict:
    """The name/id a contract is posted under: the user's main pilot or its corp.

    Returns ``{kind, id, name}``. ``kind`` is normalised to 'character' unless
    'corporation' is requested and the pilot's corp is known.
    """
    from apps.sso.models import EveCharacter

    main = (
        EveCharacter.objects.filter(user=user, is_main=True).select_related("corporation").first()
        or EveCharacter.objects.filter(user=user).select_related("corporation").first()
    )
    if not main:
        return {"kind": "character", "id": None, "name": ""}
    if kind == "corporation" and main.corporation_id:
        return {
            "kind": "corporation",
            "id": main.corporation_id,
            "name": _corp_name(main.corporation),
        }
    return {"kind": "character", "id": main.character_id, "name": main.name or ""}


def _corp_name(corp) -> str:
    """A display name for a corporation, resolving once via public ESI if unknown."""
    if corp is None:
        return ""
    if corp.name:
        return corp.name
    from core.esi.client import ESIClient, ESIError

    try:
        resp = ESIClient().get(f"/corporations/{corp.corporation_id}/")
        name = (resp.data or {}).get("name", "")
    except ESIError:
        name = ""
    if name:
        corp.name = name
        corp.save(update_fields=["name"])
        return name
    return corp.ticker or f"Corporation {corp.corporation_id}"


# --- LOG-1 (3.2): overdue-haul sweep & hauler nudges -------------------------
log = logging.getLogger(__name__)

_HAUL_EVENT_KEY = "logistics.haul_overdue"


def _haul_reminder_lead_minutes() -> int:
    from django.conf import settings

    try:
        return max(1, int(getattr(settings, "LOGISTICS_HAUL_REMINDER_LEAD_MINUTES", 360)))
    except (TypeError, ValueError):
        return 360


def sweep_overdue_hauls() -> dict:
    """Remind haulers before their deadline and auto-release overdue IN_PROGRESS hauls back to
    the pool, notifying the poster + former hauler (LOG-1 / 3.2).

    The auto-release is UNCONDITIONAL (self-healing freight); only the notifications are gated
    by the ``logistics.haul_overdue`` event. Mirrors the operations auto-cancel / form-up beat.
    """
    from apps.pingboard.notifications import is_enabled

    now = timezone.now()
    alerts_on = is_enabled(_HAUL_EVENT_KEY)
    reminded = released = 0

    # 1. Pre-deadline reminder, once per claim (guarded by reminder_sent_at).
    lead = timedelta(minutes=_haul_reminder_lead_minutes())
    for c in CourierContract.objects.filter(
        status=CourierContract.Status.IN_PROGRESS,
        deadline__gt=now, deadline__lte=now + lead,
        reminder_sent_at__isnull=True, assigned_user__isnull=False,
    ):
        if not alerts_on:
            continue  # don't stamp while the event is disabled, so arming it later still fires
        _remind_hauler(c, now)
        # Stamp regardless of send outcome so a broken channel never causes a retry storm.
        c.reminder_sent_at = now
        c.save(update_fields=["reminder_sent_at", "updated_at"])
        reminded += 1

    # 2. Overdue sweep: release past-deadline hauls back to the pool.
    for c in CourierContract.objects.filter(
        status=CourierContract.Status.IN_PROGRESS, deadline__lt=now,
    ):
        former_user_id = c.assigned_user_id
        stamp = int(c.deadline.timestamp())
        c.status = CourierContract.Status.OUTSTANDING
        c.assigned_hauler_character_id = None
        c.assigned_user = None
        c.reminder_sent_at = None
        c.save(update_fields=[
            "status", "assigned_hauler_character_id", "assigned_user",
            "reminder_sent_at", "updated_at",
        ])
        if alerts_on:
            _notify_haul_released(c, former_user_id, stamp)
        released += 1

    return {"reminded": reminded, "released": released}


def _emit_haul_dm(*, user_id, title, body, object_id, template=None, context=None):
    """One haul DM. ``template`` is a ``pingboard.messages.SCAFFOLDS`` key and ``context`` its raw
    values, so the line re-renders in the hauler's language; ``body`` stays the English audit column."""
    if not user_id:
        return
    try:
        from apps.pingboard import services as pingboard
        from apps.pingboard.models import AlertCategory

        pingboard.emit_broadcast(
            category=AlertCategory.LOGISTICS, title=title, body=body,
            template=template, context=context,
            audience={"kind": "user", "id": user_id},
            source_service="logistics", source_object_id=object_id,
            idempotency_key=f"logi:{object_id}",
        )
    except Exception:  # noqa: BLE001 — a notification must never break the sweep
        log.exception("haul DM failed (%s)", object_id)


def _remind_hauler(c, now) -> None:
    mins = int((c.deadline - now).total_seconds() // 60)
    _emit_haul_dm(
        user_id=c.assigned_user_id,
        title="Haul deadline approaching",
        body=(f"Your haul {c.origin_name} → {c.dest_name} is due in about {mins} min. "
              "Deliver it and mark the contract complete to get paid."),
        template="logistics.haul_reminder",
        context={"origin_system": c.origin_name, "destination_system": c.dest_name,
                 "minutes": mins},
        object_id=f"haul_reminder:{c.id}:{int(c.deadline.timestamp())}",
    )


def _notify_haul_released(c, former_user_id, stamp) -> None:
    route = f"{c.origin_name} → {c.dest_name}"
    ctx = {"origin_system": c.origin_name, "destination_system": c.dest_name}
    _emit_haul_dm(
        user_id=c.created_by_id,
        title="Haul overdue — released to the pool",
        body=f"The haul {route} passed its deadline and was returned to the pool for another hauler.",
        template="logistics.haul_released_poster", context=dict(ctx),
        object_id=f"haul_overdue_poster:{c.id}:{stamp}",
    )
    _emit_haul_dm(
        user_id=former_user_id,
        title="Your haul was released",
        body=f"Your haul {route} passed its deadline and was released back to the pool.",
        template="logistics.haul_released_hauler", context=dict(ctx),
        object_id=f"haul_overdue_hauler:{c.id}:{former_user_id}:{stamp}",
    )
