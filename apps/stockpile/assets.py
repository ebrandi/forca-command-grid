"""Asset ingestion via ESI — corporation (Director token) and personal (pilot).

Both endpoints return flat asset lists with a ``location_id`` that may nest
through containers/ships; we roll each asset up to its real location, resolve a
name, aggregate quantity per (owner, location, type), and mirror it into the
``Asset`` table. Corp reads need a Director token with the corp-assets scope;
personal reads use the pilot's own token. Missing grants degrade gracefully.
"""
from __future__ import annotations

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.sso.models import EveCharacter
from apps.sso.token_service import NoValidToken, get_valid_access_token
from core.esi.client import ESIClient, ESIError
from core.mixins import Source

from .locations import resolve_location, roll_up_to_root
from .models import Asset, AssetLocation

CORP_ASSETS_SCOPE = "esi-assets.read_corporation_assets.v1"
CHAR_ASSETS_SCOPE = "esi-assets.read_assets.v1"
STRUCTURES_SCOPE = "esi-universe.read_structures.v1"


def find_director_token_character(corp_id: int) -> EveCharacter | None:
    """A corp character whose stored token carries the corp-assets scope."""
    for character in EveCharacter.objects.filter(corporation_id=corp_id, is_corp_member=True):
        if character.tokens.filter(revoked_at__isnull=True).exists():
            try:
                get_valid_access_token(character, [CORP_ASSETS_SCOPE])
                return character
            except NoValidToken:
                continue
    return None


def _aggregate_by_location(assets: list[dict], client: ESIClient, token: str | None) -> dict:
    """Roll assets up to their root location, resolve it, and sum per type.

    Returns ``{location_id_or_None: {"location": AssetLocation, "types": {type_id: qty}}}``.
    """
    roots = roll_up_to_root(assets)
    cache: dict[int, AssetLocation | None] = {}
    grouped: dict = {}
    for a in assets:
        tid, qty = a.get("type_id"), int(a.get("quantity", 0) or 0)
        if not tid:
            continue
        root_id, root_type = roots.get(a["item_id"], (a.get("location_id"), a.get("location_type", "other")))
        if root_id not in cache:
            cache[root_id] = resolve_location(root_id, root_type, client=client, token=token)
        location = cache[root_id]
        key = location.location_id if location else None
        bucket = grouped.setdefault(key, {"location": location, "types": {}})
        bucket["types"][tid] = bucket["types"].get(tid, 0) + qty
    return grouped


@transaction.atomic
def _store_assets(owner_type: str, owner_id: int, grouped: dict, source: str) -> int:
    """Replace an owner's Asset rows with a fresh aggregated snapshot."""
    Asset.objects.filter(owner_type=owner_type, owner_id=owner_id).delete()
    now = timezone.now()
    rows, type_count = [], 0
    for bucket in grouped.values():
        location = bucket["location"]
        for type_id, qty in bucket["types"].items():
            rows.append(Asset(
                owner_type=owner_type, owner_id=owner_id, location=location,
                type_id=type_id, quantity=qty, source=source, as_of=now,
            ))
            type_count += 1
    Asset.objects.bulk_create(rows, batch_size=1000)
    invalidate_assets_cache(owner_type, owner_id)
    return type_count


# Ship fitting slots in ESI's ``location_flag`` — a module in one of these is fitted to
# the hull whose item id equals the module's ``location_id`` (vs loose in a hangar/cargo).
_FIT_SLOT_PREFIXES = ("HiSlot", "MedSlot", "LoSlot", "RigSlot", "SubSystemSlot")


def extract_fitted_ships(assets: list[dict]) -> dict:
    """``{ship_item_id: {ship_type_id, location_id, modules: {str(type_id): count}}}``.

    Reconstructs what's actually *fitted* to each owned hull from the raw ESI assets, so
    a doctrine-fit completeness check is possible (the aggregated ``Asset`` mirror drops
    per-item slot info on purpose).
    """
    by_item = {a["item_id"]: a for a in assets if a.get("item_id")}
    fits: dict = {}
    for a in assets:
        flag = a.get("location_flag") or ""
        if not flag.startswith(_FIT_SLOT_PREFIXES):
            continue
        ship = by_item.get(a.get("location_id"))
        if not ship or not ship.get("type_id"):
            continue
        entry = fits.setdefault(ship["item_id"], {
            "ship_type_id": ship["type_id"],
            "location_id": ship.get("location_id"),
            "modules": {},
        })
        tid = str(a.get("type_id"))
        entry["modules"][tid] = entry["modules"].get(tid, 0) + int(a.get("quantity", 1) or 1)
    return fits


@transaction.atomic
def _store_fitted_ships(character: EveCharacter, fits: dict) -> int:
    """Replace a character's fitted-ship snapshot."""
    from apps.characters.models import CharacterFittedShip

    CharacterFittedShip.objects.filter(character=character).delete()
    now = timezone.now()
    rows = [
        CharacterFittedShip(
            character=character, item_id=item_id, ship_type_id=f["ship_type_id"],
            location_id=f["location_id"], modules=f["modules"], is_latest=True,
            source=Source.ESI_CHAR, as_of=now, fetched_at=now,
        )
        for item_id, f in fits.items()
    ]
    CharacterFittedShip.objects.bulk_create(rows, batch_size=500)
    return len(rows)


def import_corporation_assets(corp_id: int | None = None, client: ESIClient | None = None) -> dict:
    """Mirror corp assets (per location) via a Director token. Never raises on a
    missing grant — returns a status dict the UI can act on."""
    corp_id = corp_id or settings.FORCA_HOME_CORP_ID
    if not corp_id:
        return {"status": "no_corp", "message": _("No home corporation configured.")}

    character = find_director_token_character(corp_id)
    if character is None:
        return {
            "status": "no_scope",
            "message": _(
                "No Director has granted corp-asset access yet. A CEO/Director must "
                "re-authorise with the corp-assets scope."
            ),
        }

    client = client or ESIClient()
    try:
        access = get_valid_access_token(character, [CORP_ASSETS_SCOPE])
        assets = client.get_paged(f"/corporations/{corp_id}/assets/", token=access)
    except (NoValidToken, ESIError) as exc:
        return {"status": "error", "message": _("Could not read corp assets: %(error)s") % {"error": exc}}

    struct_token = _structure_token(character)
    grouped = _aggregate_by_location(assets, client, struct_token)
    types = _store_assets(Asset.Owner.CORPORATION, corp_id, grouped, Source.ESI_CORP)

    from apps.admin_audit.health import record_sync

    record_sync("corp_assets", character=character.name, character_id=character.character_id,
                types=types, locations=len(grouped))
    return {
        "status": "ok",
        "message": _("Imported %(types)s asset stacks across %(locations)s locations from %(character)s.")
        % {"types": types, "locations": len(grouped), "character": character.name},
        "types": types, "locations": len(grouped), "character": character.name,
    }


def import_character_assets(character: EveCharacter, client: ESIClient | None = None) -> dict:
    """Mirror one pilot's personal assets (per location) via their own token."""
    client = client or ESIClient()
    try:
        access = get_valid_access_token(character, [CHAR_ASSETS_SCOPE])
    except NoValidToken:
        return {
            "status": "no_scope",
            "message": _("Grant personal-asset access first (ESI Scopes → Track my assets)."),
        }
    try:
        assets = client.get_paged(f"/characters/{character.character_id}/assets/", token=access)
    except ESIError as exc:
        return {"status": "error", "message": _("Could not read your assets: %(error)s") % {"error": exc}}

    struct_token = _structure_token(character)
    grouped = _aggregate_by_location(assets, client, struct_token)
    types = _store_assets(Asset.Owner.CHARACTER, character.character_id, grouped, Source.ESI_CHAR)
    _store_fitted_ships(character, extract_fitted_ships(assets))
    return {
        "status": "ok",
        "message": _("Imported %(types)s asset stacks across %(locations)s locations.")
        % {"types": types, "locations": len(grouped)},
        "types": types, "locations": len(grouped),
    }


def _structure_token(character: EveCharacter) -> str | None:
    """A token that can name private structures, if the character granted it."""
    try:
        return get_valid_access_token(character, [STRUCTURES_SCOPE])
    except NoValidToken:
        return None


def assets_by_location(owner_type: str, owner_id: int) -> dict:
    """Assets grouped by location with per-location ISK value (for logistics).

    Returns ``{"locations": [...], "total_value": Decimal, "as_of": dt|None}``,
    locations sorted by stored value descending so the richest staging shows
    first. Value is Jita sell price × quantity — indicative, not a market quote.
    """
    from decimal import Decimal

    from apps.market.pricing import price_for

    rows = (
        Asset.objects.filter(owner_type=owner_type, owner_id=owner_id)
        .select_related("location").order_by("location__name")
    )
    groups: dict = {}
    total_value = Decimal("0")
    as_of = None
    for a in rows:
        as_of = a.as_of if as_of is None else max(as_of, a.as_of)
        key = a.location_id
        g = groups.setdefault(key, {"location": a.location, "items": [], "value": Decimal("0"), "units": 0})
        value = price_for(a.type_id) * a.quantity
        g["items"].append({"type_id": a.type_id, "quantity": a.quantity, "value": value})
        g["value"] += value
        g["units"] += a.quantity
        total_value += value
    for g in groups.values():
        g["items"].sort(key=lambda i: i["value"], reverse=True)
    locations = sorted(groups.values(), key=lambda g: g["value"], reverse=True)
    return {"locations": locations, "total_value": total_value, "as_of": as_of}


# --- assets page: cached per-location summary + on-demand item detail ---------
# The assets page renders one accordion per location and only shows a location's items
# when it is expanded. Rather than render every item row up front (a corp/pilot can hold
# thousands — a ~2 MB page + heavy recompute on every load), the page renders only the
# per-location SUMMARY (value / item count / units, cached) and lazily loads a location's
# items when the pilot expands it. Assets change only at sync, so the summary is cached and
# busted in _store_assets.
_ASSETS_SUMMARY_TTL = 900  # 15 min; also explicitly invalidated on each sync


def _summary_key(owner_type: str, owner_id: int) -> str:
    return f"stockpile:assets:summary:{owner_type}:{owner_id}"


def invalidate_assets_cache(owner_type: str, owner_id: int) -> None:
    """Drop the cached asset summary for an owner (called after a sync writes new rows)."""
    from django.core.cache import cache

    cache.delete(_summary_key(owner_type, owner_id))


def assets_summary(owner_type: str, owner_id: int) -> dict:
    """Per-location value/count/units for an owner (NO item detail), cached per owner.

    Returns ``{"locations": [{location_id, name, system_id, kind_display, value, item_count,
    units}], "total_value", "as_of"}``, locations sorted by value desc. Item detail is
    fetched separately by :func:`location_items` when a location is expanded."""
    from django.core.cache import cache

    key = _summary_key(owner_type, owner_id)
    cached = cache.get(key)
    if cached is None:
        cached = _build_assets_summary(owner_type, owner_id)
        cache.set(key, cached, _ASSETS_SUMMARY_TTL)
    return cached


def _build_assets_summary(owner_type: str, owner_id: int) -> dict:
    from decimal import Decimal

    from apps.market.pricing import price_for

    agg: dict = {}
    total_value = Decimal("0")
    as_of = None
    for loc_id, type_id, qty, a_as_of in Asset.objects.filter(
        owner_type=owner_type, owner_id=owner_id
    ).values_list("location_id", "type_id", "quantity", "as_of"):
        as_of = a_as_of if as_of is None else max(as_of, a_as_of)
        value = price_for(type_id) * qty
        g = agg.setdefault(loc_id, {"value": Decimal("0"), "item_count": 0, "units": 0})
        g["value"] += value
        g["item_count"] += 1
        g["units"] += qty
        total_value += value
    # Resolve location display names in one query (cache-safe: plain dicts, no model instances).
    known_ids = [lid for lid in agg if lid is not None]
    locs = {loc.location_id: loc for loc in AssetLocation.objects.filter(location_id__in=known_ids)}
    locations = []
    for loc_id, g in agg.items():
        loc = locs.get(loc_id)
        if loc is not None:
            name, system_id, kind_display = str(loc), loc.system_id, loc.get_kind_display()
        elif loc_id is None:
            # Assets whose location couldn't be resolved (an unreadable structure) sit in the
            # null bucket. Use 0 as the id in the lazy-load URL — asset_location_items maps 0
            # back to a location__isnull filter so expanding this card still shows its items.
            name, system_id, kind_display = "Unknown location", None, ""
        else:
            name, system_id, kind_display = f"Location {loc_id}", None, ""
        locations.append({
            "location_id": loc_id or 0,
            "name": name,
            "system_id": system_id,
            "kind_display": kind_display,
            "value": g["value"],
            "item_count": g["item_count"],
            "units": g["units"],
        })
    locations.sort(key=lambda x: x["value"], reverse=True)
    return {"locations": locations, "total_value": total_value, "as_of": as_of}


def location_items(owner_type: str, owner_id: int, location_id: int) -> list[dict]:
    """Item detail (type, quantity, value) for ONE location — the lazy-load target.

    A bounded query over a single location's assets, priced from the in-process snapshot;
    fast even for a location holding many types. ``location_id == 0`` is the sentinel for the
    unresolved (null) location bucket the summary emits."""
    from apps.market.pricing import price_for

    qs = Asset.objects.filter(owner_type=owner_type, owner_id=owner_id)
    qs = qs.filter(location__isnull=True) if not location_id else qs.filter(location_id=location_id)
    items = [
        {"type_id": type_id, "quantity": qty, "value": price_for(type_id) * qty}
        for type_id, qty in qs.values_list("type_id", "quantity")
    ]
    items.sort(key=lambda i: i["value"], reverse=True)
    return items
