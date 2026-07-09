"""Background sync of the Ansiblex + cyno-beacon network from ESI."""
from __future__ import annotations

from celery import shared_task


@shared_task(name="navigation.sync_jump_network")
def sync_jump_network() -> dict:
    from .esi_sync import sync_jump_network as _sync

    return _sync()


@shared_task(name="navigation.warm_map_overlays")
def warm_map_overlays() -> str:
    """Pre-fetch the public ESI map overlays so a map view never makes ESI calls
    in-request on a cold cache. Each is cached ~1h; this runs under that TTL."""
    from .map_overlays import faction_warfare, sovereignty, system_jumps, system_kills

    jumps = len(system_jumps())
    kills = len(system_kills())
    sov = len(sovereignty())
    fw = len(faction_warfare())
    return f"jumps={jumps} kills={kills} sov={sov} fw={fw}"


@shared_task(name="navigation.scan_route_watches")
def scan_route_watches() -> dict:
    """4.5: DM the owner of a watched saved route when a camp/incursion appears on it.
    No-op unless the governance event is armed + a route has watch enabled."""
    from .route_watch import scan_route_watches as _scan

    return _scan()
