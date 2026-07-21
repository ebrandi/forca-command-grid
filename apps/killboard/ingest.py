"""Killmail ingestion: fetch body, enrich, value, persist (idempotent)."""
from __future__ import annotations

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils.dateparse import parse_datetime

from apps.sde.models import SdeSolarSystem
from core.esi.client import ESIClient
from core.mixins import Source

from .doctrine_tag import compute_fit_deviation, tag_doctrine_fit
from .models import Killmail, KillmailItem, KillmailParticipant, SecBand
from .valuation import apply_valuation, stamp_value_at_kill

POCHVEN_REGION_ID = 10000070


def classify_sec_band(system_id: int, security: float | None, region_id: int | None = None) -> str:
    if region_id == POCHVEN_REGION_ID:
        return SecBand.POCHVEN
    if system_id and 31000000 <= system_id < 32000000:
        return SecBand.WORMHOLE
    if system_id and 32000000 <= system_id < 33000000:
        return SecBand.ABYSSAL
    if security is None:
        return SecBand.UNKNOWN
    # EVE bands on the displayed (one-decimal-rounded) security status.
    rounded = round(security, 1)
    if rounded >= 0.5:
        return SecBand.HIGHSEC
    if rounded >= 0.1:
        return SecBand.LOWSEC
    return SecBand.NULLSEC


def _home_role(victim_corp, attacker_corps) -> tuple[bool, str]:
    home = settings.FORCA_HOME_CORP_ID
    if home and victim_corp == home:
        return True, Killmail.HomeRole.VICTIM
    if home and home in attacker_corps:
        return True, Killmail.HomeRole.ATTACKER
    return False, Killmail.HomeRole.NONE


def ingest_killmail(
    killmail_id: int,
    killmail_hash: str,
    *,
    source: str = Source.ESI_CORP,
    client: ESIClient | None = None,
    body: dict | None = None,
) -> Killmail:
    """Ingest one killmail by (id, hash). Idempotent on killmail_id."""
    existing = Killmail.objects.filter(killmail_id=killmail_id).first()
    if existing:
        return existing

    if body is None:
        client = client or ESIClient()
        resp = client.get(f"/killmails/{killmail_id}/{killmail_hash}/", essential=True)
        body = resp.data or {}

    victim = body.get("victim", {})
    attackers = body.get("attackers", [])
    system_id = body.get("solar_system_id")

    killmail_time = parse_datetime(body.get("killmail_time") or "")
    if killmail_time is None:
        raise ValueError(f"killmail {killmail_id} has a missing/invalid killmail_time")

    sde_system = SdeSolarSystem.objects.filter(system_id=system_id).first()
    region_id = sde_system.region_id if sde_system else None
    security = sde_system.security if sde_system else None

    attacker_corps = [a.get("corporation_id") for a in attackers if a.get("corporation_id")]
    involves_home, home_role = _home_role(victim.get("corporation_id"), attacker_corps)

    # "Player" attackers are those with a character_id; NPC/structure entities
    # carry a faction_id / NPC corp instead. Solo and NPC are judged on players.
    player_attackers = [a for a in attackers if a.get("character_id")]
    is_npc = bool(attackers) and len(player_attackers) == 0
    is_solo = len(player_attackers) == 1
    victim_corp = victim.get("corporation_id")
    is_awox = bool(victim_corp) and any(
        a.get("corporation_id") == victim_corp for a in player_attackers
    )

    try:
        with transaction.atomic():
            km = _create_killmail(
                killmail_id,
                killmail_hash,
                killmail_time,
                system_id,
                region_id,
                security,
                victim,
                attackers,
                source,
                involves_home,
                home_role,
                is_npc,
                is_solo,
                is_awox,
                moon_id=body.get("moon_id"),
                war_id=body.get("war_id"),
            )
    except IntegrityError:
        # Lost a concurrent race; the other writer created it.
        return Killmail.objects.get(killmail_id=killmail_id)

    apply_valuation(km)
    # KB-35: a fresh kill's at-kill value ≈ its live value, so stamp it cheaply now (no
    # network). The value_at_kill backfill upgrades OLD mails to true period prices later.
    stamp_value_at_kill(km, historical=False)
    tag_doctrine_fit(km)
    compute_fit_deviation(km)
    # KB-29: append an outbound-stream event. This is the single post-ingest seam every path
    # funnels through (ESI corp/char, zKill query poll, R2Z2 killstream, EVE Ref / zKill
    # backfills), so one call covers them all. It self-limits to home-corp mails within the
    # freshness window and never raises, so a stream hiccup can't break ingestion.
    from .stream import emit_stream_event

    emit_stream_event(km)
    return km


def _create_killmail(
    killmail_id,
    killmail_hash,
    killmail_time,
    system_id,
    region_id,
    security,
    victim,
    attackers,
    source,
    involves_home,
    home_role,
    is_npc,
    is_solo,
    is_awox,
    moon_id=None,
    war_id=None,
) -> Killmail:
    km = Killmail.objects.create(
        killmail_id=killmail_id,
        killmail_hash=killmail_hash,
        killmail_time=killmail_time,
        solar_system_id=system_id,
        region_id=region_id,
        moon_id=moon_id,
        war_id=war_id,
        victim_character_id=victim.get("character_id"),
        victim_corporation_id=victim.get("corporation_id"),
        victim_alliance_id=victim.get("alliance_id"),
        victim_faction_id=victim.get("faction_id"),
        victim_ship_type_id=victim.get("ship_type_id"),
        damage_taken=victim.get("damage_taken", 0),
        is_solo=is_solo,
        is_npc=is_npc,
        is_awox=is_awox,
        sec_band=classify_sec_band(system_id, security, region_id),
        involves_home_corp=involves_home,
        home_corp_role=home_role,
        source=source,
    )

    # victim participant
    KillmailParticipant.objects.create(
        killmail=km,
        role=KillmailParticipant.Role.VICTIM,
        seq=0,
        character_id=victim.get("character_id"),
        corporation_id=victim.get("corporation_id"),
        alliance_id=victim.get("alliance_id"),
        faction_id=victim.get("faction_id"),
        ship_type_id=victim.get("ship_type_id"),
        damage_done=0,
    )
    for i, a in enumerate(attackers):
        KillmailParticipant.objects.create(
            killmail=km,
            role=KillmailParticipant.Role.ATTACKER,
            seq=i,
            character_id=a.get("character_id"),
            corporation_id=a.get("corporation_id"),
            alliance_id=a.get("alliance_id"),
            faction_id=a.get("faction_id"),
            ship_type_id=a.get("ship_type_id"),
            weapon_type_id=a.get("weapon_type_id"),
            damage_done=a.get("damage_done", 0),
            final_blow=a.get("final_blow", False),
            security_status=a.get("security_status"),
        )

    _ingest_items(km, victim.get("items", []))
    return km


def _ingest_items(km: Killmail, items: list, parent_idx: int | None = None, start: int = 0) -> int:
    idx = start
    for item in items:
        KillmailItem.objects.create(
            killmail=km,
            idx=idx,
            parent_idx=parent_idx,
            item_type_id=item.get("item_type_id"),
            flag=item.get("flag", 0),
            singleton=item.get("singleton", 0),
            quantity_dropped=item.get("quantity_dropped"),
            quantity_destroyed=item.get("quantity_destroyed"),
        )
        this_idx = idx
        idx += 1
        nested = item.get("items")
        if nested:
            idx = _ingest_items(km, nested, parent_idx=this_idx, start=idx)
    return idx
