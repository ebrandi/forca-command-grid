"""Import the Ansiblex jump-bridge + cyno-beacon network from ESI.

Reads the home corporation's structures (``esi-corporations.read_structures.v1``,
which needs an in-game Director / Station Manager role) and upserts the navigable
ones: Ansiblex Jump Gates become bridges (the two linked systems parsed from the
structure name), Pharolux Cyno Beacons become beacons. Only the corp's *own*
structures are visible — there's no public ESI for another entity's bridges, so
manual entries remain for networks you can use but don't own.
"""
from __future__ import annotations

import logging
import re

from django.conf import settings

from apps.sde.models import SdeSolarSystem
from apps.sso.models import EveCharacter
from apps.sso.token_service import NoValidToken, get_valid_access_token
from core.esi.client import ESIClient, ESIError

from .models import AnsiblexBridge, CynoBeacon, Source

log = logging.getLogger("forca.navigation")

ANSIBLEX_TYPE_ID = 35841   # Ansiblex Jump Gate
PHAROLUX_TYPE_ID = 35840   # Pharolux Cyno Beacon
STRUCT_SCOPE = "esi-corporations.read_structures.v1"
UNIVERSE_SCOPE = "esi-universe.read_structures.v1"


def _token_character(corp_id: int) -> EveCharacter | None:
    """A corp character whose stored token carries the read-structures scope."""
    for character in EveCharacter.objects.filter(corporation_id=corp_id, is_corp_member=True):
        if character.tokens.filter(revoked_at__isnull=True).exists():
            try:
                get_valid_access_token(character, [STRUCT_SCOPE])
                return character
            except NoValidToken:
                continue
    return None


def _structure_name(structure: dict, client: ESIClient, token: str | None) -> str:
    if structure.get("name"):
        return structure["name"]
    sid = structure.get("structure_id")
    if not sid or not token:
        return ""
    try:
        resp = client.get(f"/universe/structures/{sid}/", token=token)
        return (resp.data or {}).get("name", "")
    except ESIError:
        return ""


def _parse_bridge_systems(name: str, own_system_id: int) -> tuple[int, int] | None:
    """Resolve the two linked systems from an Ansiblex name (e.g. "TAG A » B")."""
    found: list[int] = []
    for tok in re.findall(r"[A-Za-z0-9\-]+", name or ""):
        sid = (
            SdeSolarSystem.objects.filter(name__iexact=tok)
            .values_list("system_id", flat=True).first()
        )
        if sid and sid not in found:
            found.append(sid)
    if len(found) == 2:
        return found[0], found[1]
    if own_system_id in found:
        others = [s for s in found if s != own_system_id]
        if others:
            return own_system_id, others[0]
    return None


def _save_bridge(struct_id: int, a: int, b: int, names: dict, name: str) -> None:
    a, b = sorted((a, b))
    bridge = (
        AnsiblexBridge.objects.filter(structure_id=struct_id).first()
        or AnsiblexBridge.objects.filter(from_system_id=a, to_system_id=b).first()
        or AnsiblexBridge(from_system_id=a, to_system_id=b)
    )
    bridge.from_system_id, bridge.to_system_id = a, b
    bridge.from_system_name = names.get(a, str(a))
    bridge.to_system_name = names.get(b, str(b))
    bridge.structure_id, bridge.name = struct_id, name[:120]
    bridge.active, bridge.source = True, Source.ESI
    bridge.save()


def _save_beacon(struct_id: int, sys_id: int, names: dict, name: str) -> None:
    beacon = (
        CynoBeacon.objects.filter(structure_id=struct_id).first()
        or CynoBeacon.objects.filter(system_id=sys_id).first()
        or CynoBeacon(system_id=sys_id)
    )
    beacon.system_id, beacon.system_name = sys_id, names.get(sys_id, str(sys_id))
    beacon.structure_id, beacon.name = struct_id, name[:120]
    beacon.active, beacon.source = True, Source.ESI
    beacon.save()


def sync_jump_network(corp_id: int | None = None, client: ESIClient | None = None) -> dict:
    """Mirror the corp's Ansiblex + cyno-beacon structures into the registry.

    Never raises on a missing grant — returns a status dict the UI/task can act on.
    """
    corp_id = corp_id or settings.FORCA_HOME_CORP_ID
    if not corp_id:
        return {"status": "no_corp", "message": "No home corporation configured."}
    character = _token_character(corp_id)
    if character is None:
        return {"status": "no_scope", "message": (
            "No Director has granted structure access yet. A CEO/Director (or a pilot with "
            "the Station Manager role) must authorise with the jump-network scope.")}

    client = client or ESIClient()
    try:
        token = get_valid_access_token(character, [STRUCT_SCOPE])
        structures = client.get_paged(f"/corporations/{corp_id}/structures/", token=token)
    except (NoValidToken, ESIError) as exc:
        return {"status": "error", "message": f"Could not read corp structures: {exc}"}
    try:
        uni_token = get_valid_access_token(character, [UNIVERSE_SCOPE])
    except NoValidToken:
        uni_token = token

    names = dict(SdeSolarSystem.objects.values_list("system_id", "name"))
    bridges = beacons = 0
    unparsed: list[str] = []
    seen_bridges: set[int] = set()
    seen_beacons: set[int] = set()
    for s in structures:
        struct_id, sys_id = s.get("structure_id"), s.get("system_id")
        try:
            if s.get("type_id") == ANSIBLEX_TYPE_ID:
                name = _structure_name(s, client, uni_token)
                link = _parse_bridge_systems(name, sys_id)
                if not link:
                    if name:
                        unparsed.append(name)
                    continue
                _save_bridge(struct_id, link[0], link[1], names, name)
                bridges += 1
                seen_bridges.add(struct_id)
            elif s.get("type_id") == PHAROLUX_TYPE_ID:
                name = _structure_name(s, client, uni_token)
                _save_beacon(struct_id, sys_id, names, name)
                beacons += 1
                seen_beacons.add(struct_id)
        except Exception as exc:  # noqa: BLE001 - one odd structure shouldn't fail the sync
            log.warning("jump-network: skipped structure %s: %s", struct_id, exc)

    # Drop ESI-sourced records whose structure is gone (unanchored / destroyed).
    AnsiblexBridge.objects.filter(source=Source.ESI).exclude(structure_id__in=seen_bridges).delete()
    CynoBeacon.objects.filter(source=Source.ESI).exclude(structure_id__in=seen_beacons).delete()

    try:
        from apps.admin_audit.health import record_sync
        record_sync("jump_network", character=character.name,
                    character_id=character.character_id, bridges=bridges, beacons=beacons)
    except Exception:  # noqa: BLE001,S110 - health logging is best-effort
        pass

    msg = f"Imported {bridges} jump bridge(s) and {beacons} cyno beacon(s) from {character.name}."
    if unparsed:
        msg += f" {len(unparsed)} structure(s) couldn't be matched to systems by name."
    return {"status": "ok", "bridges": bridges, "beacons": beacons,
            "unparsed": unparsed, "character": character.name, "message": msg}
