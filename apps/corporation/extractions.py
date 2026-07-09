"""Moon-extraction schedule sync (ESI corporation mining extractions).

Pulls upcoming moon extractions from a Station-Manager/Director token and stores them
for a member-facing extraction calendar. Moon names come from the SDE celestials we
already load — no extra ESI call.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.utils.dateparse import parse_datetime

_log = logging.getLogger("forca.corporation")

MINING_SCOPE = "esi-industry.read_corporation_mining.v1"


def _token_character(corp_id: int):
    from apps.sso.models import EveCharacter
    from apps.sso.token_service import NoValidToken, get_valid_access_token

    for character in EveCharacter.objects.filter(is_corp_member=True):
        try:
            if get_valid_access_token(character, [MINING_SCOPE]):
                return character
        except NoValidToken:
            continue
    return None


def sync_moon_extractions(corp_id: int | None = None, client=None) -> dict:
    """Refresh the corp's scheduled moon extractions."""
    from apps.sde.models import SdeCelestial
    from core.esi.client import ESIClient, ESIError

    from .models import MoonExtraction

    corp_id = corp_id or getattr(settings, "FORCA_HOME_CORP_ID", 0)
    character = _token_character(corp_id)
    if character is None:
        return {"status": "no_token", "count": 0}

    from apps.sso.token_service import get_valid_access_token

    token = get_valid_access_token(character, [MINING_SCOPE])
    client = client or ESIClient()
    try:
        rows = client.get(
            f"/corporation/{corp_id}/mining/extractions/", token=token,
        ).data or []
    except ESIError:
        return {"status": "error", "count": 0}

    moon_ids = {r.get("moon_id") for r in rows if r.get("moon_id")}
    moon_names = dict(
        SdeCelestial.objects.filter(item_id__in=moon_ids).values_list("item_id", "name")
    )
    count = 0
    for r in rows:
        arrival = parse_datetime(r.get("chunk_arrival_time") or "")
        if arrival is None:
            continue
        MoonExtraction.objects.update_or_create(
            structure_id=r.get("structure_id"), chunk_arrival=arrival,
            defaults={
                "moon_id": r.get("moon_id"),
                "moon_name": moon_names.get(r.get("moon_id"), ""),
                "extraction_start": parse_datetime(r.get("extraction_start_time") or "") or None,
                "natural_decay": parse_datetime(r.get("natural_decay_time") or "") or None,
            },
        )
        count += 1
    return {"status": "ok", "count": count}


# --------------------------------------------------------------------------- #
#  MIN-3 (3.13): opt-in chunk-arrival reminder ping
# --------------------------------------------------------------------------- #
_CHUNK_EVENT_KEY = "mining.chunk_arrival"
_DEFAULT_OFFSETS = [24, 1]


def _emit_chunk_reminder(ext, hours: int) -> None:
    from apps.pingboard import services as pingboard
    from apps.pingboard.models import AlertCategory

    where = ext.moon_name or ext.structure_name or f"structure {ext.structure_id}"
    body = (f"The moon chunk at {where} is ready to fracture in about {hours}h — form up to "
            f"mine it before it decays.")
    try:
        pingboard.emit_broadcast(
            category=AlertCategory.MOON_EXTRACTION, title="Moon chunk arriving soon", body=body,
            # Explicit corp audience — this is a corp-wide "form up to mine" rally, not the
            # MOON_EXTRACTION category's default officer routing (which would also be gated
            # off corp chat channels by the high_command classification).
            audience={"kind": "corp"},
            source_service="corporation", source_object_id=f"chunk:{ext.id}:{hours}",
            idempotency_key=f"chunk:{ext.id}:{hours}",
        )
    except Exception:  # noqa: BLE001 — a reminder must never break the sweep
        _log.exception("chunk reminder emit failed for extraction %s", ext.id)


def sweep_chunk_reminders() -> int:
    """Fire opt-in corp reminders ahead of each upcoming ``chunk_arrival`` at the configured
    offsets (default 24h + 1h), at most once per (extraction, offset).

    Opt-in by construction — routes through the ``mining.chunk_arrival`` event (inert until
    leadership arms channels). Future-only: an offset whose window is already stale (the
    extraction synced late) is marked without firing, so there's no burst of past-due pings.
    """
    from datetime import timedelta

    from django.utils import timezone

    from apps.admin_audit.models import AppSetting
    from apps.pingboard.notifications import is_enabled

    from .models import MoonExtraction

    now = timezone.now()
    raw = AppSetting.get("mining.chunk_reminder_offsets_hours", _DEFAULT_OFFSETS)
    if not isinstance(raw, list):  # a misconfigured (non-list) value must not crash the sweep
        raw = _DEFAULT_OFFSETS
    offsets = []
    for o in raw:
        try:
            offsets.append(int(o))
        except (TypeError, ValueError):
            continue
    enabled = is_enabled(_CHUNK_EVENT_KEY)
    grace = timedelta(hours=2)
    fired = 0
    for ext in MoonExtraction.objects.filter(chunk_arrival__gt=now - grace):
        try:
            sent = set(ext.reminders_sent or [])
            changed = False
            for off in offsets:
                if off in sent:
                    continue
                fire_time = ext.chunk_arrival - timedelta(hours=off)
                if now < fire_time:
                    continue  # not due yet
                if not enabled:
                    continue  # leave unmarked → arming the event later still fires fresh ones
                if (now - fire_time) <= grace and now < ext.chunk_arrival:
                    _emit_chunk_reminder(ext, off)
                    fired += 1
                sent.add(off)  # fired OR stale — never reconsider this offset again
                changed = True
            if changed:
                ext.reminders_sent = sorted(sent)
                ext.save(update_fields=["reminders_sent"])
        except Exception:  # noqa: BLE001 — one extraction must not abort the whole sweep
            _log.exception("chunk-reminder sweep failed for extraction %s", ext.id)
    return fired
