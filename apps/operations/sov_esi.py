"""Sovereignty ADM tracking (public ESI sovereignty systems).

No token needed. CCP consolidated the old ``/sovereignty/structures/`` +
``/sovereignty/map/`` endpoints (both now 404) into ``/sovereignty/systems/``,
which carries, per claimed system, the holder, the sovereignty hub + its
vulnerability window, and a ``development`` block with the **Activity Defense
Multiplier**. We keep only the systems our own alliance holds, so leadership can
spot soft systems. Empty unless the corp's alliance holds sov; snapshot-replaced.
"""
from __future__ import annotations

from django.conf import settings
from django.utils.dateparse import parse_datetime


def _home_alliance_id() -> int | None:
    from apps.corporation.models import EveCorporation

    corp = EveCorporation.objects.filter(
        corporation_id=getattr(settings, "FORCA_HOME_CORP_ID", 0)
    ).first()
    return corp.alliance_id if corp and corp.alliance_id else None


def sync_sovereignty(alliance_id: int | None = None, client=None) -> dict:
    """Snapshot our alliance's held systems (ADM + vulnerability window)."""
    from apps.sde.models import SdeSolarSystem
    from core.esi.client import ESIClient, ESIError

    from .models import SovStructure

    alliance_id = alliance_id or _home_alliance_id()
    if not alliance_id:
        return {"status": "no_alliance", "count": 0}

    client = client or ESIClient()
    try:
        data = client.get("/sovereignty/systems/").data or {}
    except ESIError:
        return {"status": "error", "count": 0}

    # ESI returns {"solar_systems": [...]}; tolerate a bare list too.
    rows = data.get("solar_systems", []) if isinstance(data, dict) else (data or [])

    ours = []
    for r in rows:
        claim = (r.get("claim") or {}).get("alliance") or {}
        if claim.get("alliance_id") != alliance_id:
            continue
        hub = claim.get("sovereignty_hub") or {}
        structure_id = hub.get("id")
        if not structure_id:
            continue  # claimed but no sovereignty hub yet — nothing to track
        window = hub.get("vulnerability_window") or {}
        ours.append({
            "structure_id": structure_id,
            "solar_system_id": r.get("solar_system_id"),
            "adm": (claim.get("development") or {}).get("activity_defense_multiplier"),
            "v_start": window.get("start"),
            "v_end": window.get("end"),
        })

    system_ids = {r["solar_system_id"] for r in ours if r["solar_system_id"]}
    system_names = dict(
        SdeSolarSystem.objects.filter(system_id__in=system_ids).values_list("system_id", "name")
    )

    objs = [
        SovStructure(
            structure_id=r["structure_id"], alliance_id=alliance_id,
            solar_system_id=r["solar_system_id"] or 0,
            system_name=system_names.get(r["solar_system_id"], ""),
            structure_type_id=0,  # the new single "Sovereignty Hub"
            adm=float(r["adm"]) if r["adm"] is not None else 1.0,
            vulnerable_start=parse_datetime(r["v_start"] or "") or None,
            vulnerable_end=parse_datetime(r["v_end"] or "") or None,
        )
        for r in ours
    ]

    # Snapshot replace: the public endpoint is the full current picture.
    SovStructure.objects.all().delete()
    SovStructure.objects.bulk_create(objs)
    return {"status": "ok", "count": len(objs)}
