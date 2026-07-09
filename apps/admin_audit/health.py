"""Integration health: ESI token status + per-feed sync freshness.

Surfaces, for directors, "is each ESI integration still working and how fresh is
its data" — the first place to look when corp assets, market data, or killmails
stop updating (usually an expired/revoked Director token or a missing scope).
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from .models import AppSetting

# Key scopes whose presence we surface explicitly on each token.
_KEY_SCOPES = {
    "corp_assets": "esi-assets.read_corporation_assets.v1",
    "corp_killmails": "esi-killmails.read_corporation_killmails.v1",
    "char_killmails": "esi-killmails.read_killmails.v1",
    "skills": "esi-skills.read_skills.v1",
}


def record_sync(key: str, **detail) -> None:
    """Stamp the last successful run of a sync (who/when/how many)."""
    detail["at"] = timezone.now().isoformat()
    AppSetting.objects.update_or_create(key=f"sync:{key}", defaults={"value": detail})


def _last_sync(key: str) -> dict | None:
    setting = AppSetting.objects.filter(key=f"sync:{key}").first()
    return setting.value if setting else None


def _status(last_dt, stale_after_hours: float) -> str:
    if last_dt is None:
        return "missing"
    age = timezone.now() - last_dt
    return "ok" if age <= timedelta(hours=stale_after_hours) else "stale"


def token_health() -> list[dict]:
    """Per non-revoked token: which character, key scopes, and freshness."""
    from apps.sso.models import AuthToken

    out = []
    for t in AuthToken.objects.filter(revoked_at__isnull=True).select_related("character"):
        scopes = set(t.scopes or [])
        out.append({
            "character_id": t.character_id,
            "name": t.character.name if t.character else str(t.character_id),
            "scopes": {label: (scope in scopes) for label, scope in _KEY_SCOPES.items()},
            "expired": t.access_expired,
            "revoked": t.revoked_at is not None,
            "last_refresh_ok_at": t.last_refresh_ok_at,
            "refresh_fail_count": t.refresh_fail_count,
            "healthy": t.is_valid and t.refresh_fail_count == 0,
        })
    # A token carrying the corp-assets scope is what powers asset ingestion.
    out.sort(key=lambda x: (not x["scopes"]["corp_assets"], x["name"]))
    return out


def _parse_dt(value):
    from django.utils.dateparse import parse_datetime

    return parse_datetime(value) if value else None


def feed_health() -> list[dict]:
    """Last-sync time, record count, and status for each ESI-fed dataset."""
    from apps.characters.models import CharacterSkillSnapshot
    from apps.killboard.models import Killmail
    from apps.market.models import MarketHistory, MarketPrice
    from apps.stockpile.models import Asset

    feeds = []

    # Corp assets (Director token) — authoritative run record + item count.
    rec = _last_sync("corp_assets")
    last = _parse_dt(rec.get("at")) if rec else None
    feeds.append({
        "key": "corp_assets", "label": "Corp assets",
        "last": last, "status": _status(last, 12),
        "count": Asset.objects.filter(owner_type=Asset.Owner.CORPORATION).count(),
        "by": rec.get("character") if rec else None,
        "note": "Director token · esi-assets.read_corporation_assets.v1",
    })

    # Personal assets — aggregate across all pilots who opted in (no private data).
    rec = _last_sync("personal_assets")
    last = _parse_dt(rec.get("at")) if rec else None
    pilots = (
        Asset.objects.filter(owner_type=Asset.Owner.CHARACTER)
        .values("owner_id").distinct().count()
    )
    feeds.append({
        "key": "personal_assets", "label": "Personal assets",
        "last": last, "status": _status(last, 12) if last else ("ok" if pilots else "missing"),
        "count": Asset.objects.filter(owner_type=Asset.Owner.CHARACTER).count(),
        "by": f"{pilots} pilot(s)", "note": "each pilot's own token",
    })

    # Market history (public) — run record, falling back to the data's own age.
    rec = _last_sync("market_history")
    last = _parse_dt(rec.get("at")) if rec else None
    if last is None:
        last = MarketHistory.objects.order_by("-as_of").values_list("as_of", flat=True).first()
    feeds.append({
        "key": "market_history", "label": "Market history",
        "last": last, "status": _status(last, 36),
        "count": MarketHistory.objects.count(),
        "by": "public ESI", "note": "no token required",
    })

    # Killmails — most recent fetch, falling back to ingest/record time.
    last_km = (
        Killmail.objects.exclude(fetched_at=None).order_by("-fetched_at")
        .values_list("fetched_at", flat=True).first()
        or Killmail.objects.order_by("-as_of").values_list("as_of", flat=True).first()
    )
    feeds.append({
        "key": "killmails", "label": "Killmails",
        "last": last_km, "status": _status(last_km, 6),
        "count": Killmail.objects.count(),
        "by": "char/corp tokens + zKill", "note": "discovery every 10m",
    })

    # Corp roster (Director member-tracking) — run record + member count.
    from apps.corporation.models import CorpMember

    rec = _last_sync("corp_members")
    last = _parse_dt(rec.get("at")) if rec else None
    feeds.append({
        "key": "corp_members", "label": "Member roster",
        "last": last, "status": _status(last, 12),
        "count": CorpMember.objects.count(),
        "by": rec.get("by") if rec else None,
        "note": "Director token · esi-corporations.track_members.v1",
    })

    # Member skills — latest snapshot freshness.
    last_skill = (
        CharacterSkillSnapshot.objects.filter(is_latest=True)
        .order_by("-as_of").values_list("as_of", flat=True).first()
    )
    feeds.append({
        "key": "skills", "label": "Member skills",
        "last": last_skill, "status": _status(last_skill, 36),
        "count": CharacterSkillSnapshot.objects.filter(is_latest=True).count(),
        "by": "character tokens", "note": "sync every 12h",
    })

    # Market prices (Jita aggregates) — last price refresh.
    last_price = MarketPrice.objects.order_by("-as_of").values_list("as_of", flat=True).first()
    feeds.append({
        "key": "market_prices", "label": "Market prices",
        "last": last_price, "status": _status(last_price, 72),
        "count": MarketPrice.objects.count(),
        "by": "public ESI", "note": "price import",
    })

    return feeds


# Friendly labels + expected max age (hours) for the recorded per-beat sync stamps.
# Unknown keys fall back to a humanised label and a generous default threshold, so a
# newly-added ``record_sync`` call appears on the panel automatically.
_SYNC_LABELS = {
    "corp_assets": ("Corp assets", 12),
    "personal_assets": ("Personal assets", 24),
    "corp_members": ("Member roster", 12),
    "market_history": ("Market history", 36),
    "market_adjusted_prices": ("Market adjusted prices", 36),
    "market_jita_prices": ("Market Jita prices", 36),
    "jump_network": ("Jump network", 200),
}
_DEFAULT_SYNC_STALE_H = 48


def sde_health() -> dict:
    """The loaded SDE static-data build and how long ago it was loaded.

    After a game patch new ships/items can render as raw numbers until the SDE is
    reloaded; surfacing the version + age tells a director whether a refresh is due.
    """
    setting = AppSetting.objects.filter(key="sde_version").first()
    if setting is None:
        return {"version": None, "loaded_at": None, "status": "missing"}
    version = (setting.value or {}).get("version")
    loaded_at = setting.updated_at
    # Advisory only: SDE changes at CCP's cadence, so ~45 days is a soft "refresh due".
    return {"version": version, "loaded_at": loaded_at, "status": _status(loaded_at, 24 * 45)}


def beat_health() -> list[dict]:
    """Every recorded ``sync:<key>`` stamp: task, last-success time and freshness.

    Complements ``feed_health`` (which shows dataset freshness + counts) by surfacing
    the raw per-beat last-success age for *all* syncs — including ones with no feed
    row (e.g. Jita/adjusted price refreshes, jump-network) — so a silently-stopped
    beat is visible in one place.
    """
    rows = []
    for setting in AppSetting.objects.filter(key__startswith="sync:").order_by("key"):
        key = setting.key[len("sync:"):]
        label, stale_h = _SYNC_LABELS.get(
            key, (key.replace("_", " ").capitalize(), _DEFAULT_SYNC_STALE_H)
        )
        value = setting.value or {}
        last = _parse_dt(value.get("at"))
        rows.append({
            "key": key,
            "label": label,
            "last": last,
            "status": _status(last, stale_h),
            "detail": {k: v for k, v in value.items() if k != "at"},
        })
    return rows


_HEALTH_CACHE_KEY = "admin:integration_health:v2"
_HEALTH_CACHE_TTL = 120  # seconds — cheap staleness in exchange for ~7 fewer COUNT(*)/render


def integration_health(*, use_cache: bool = True, refresh: bool = False) -> dict:
    """Token + feed health, briefly cached (called for every Director on /dashboard/).

    ``refresh=True`` recomputes and re-caches even on a hit.
    """
    from django.core.cache import cache

    if use_cache and not refresh:
        cached = cache.get(_HEALTH_CACHE_KEY)
        if cached is not None:
            return cached
    tokens = token_health()
    feeds = feed_health()
    payload = {
        "tokens": tokens,
        "feeds": feeds,
        "sde": sde_health(),
        "beats": beat_health(),
        "ok": all(f["status"] == "ok" for f in feeds) and all(t["healthy"] for t in tokens),
        "has_asset_token": any(t["scopes"]["corp_assets"] and t["healthy"] for t in tokens),
    }
    if use_cache:
        cache.set(_HEALTH_CACHE_KEY, payload, _HEALTH_CACHE_TTL)
    return payload
