"""KB-33 adversary entity pages (WS-C3) — a profile of a non-home entity, RELATIVE TO US.

Every figure here is computed from OUR OWN killmail history (``involves_home_corp``): the
board only ever ingests mails that touch the home corp, so an external entity's record in our
DB *is* the record of its fights with us — nothing universal, nothing about the rest of EVE.
This is the deliberate framing of the market-leadership plan (§7 KB-33, §9 non-goals): we
extend coverage toward "anyone who fights us" without becoming a universal killboard.

Two engagement directions define "vs us" (and only these two — a third party who also shot
one of *our* kills is not fighting us):

* **Kills vs us** — the entity is an ATTACKER on one of OUR LOSSES (``home_corp_role`` VICTIM).
  They killed us. Their hulls, co-attackers and timezone are read from these mails.
* **Losses to us** — the entity is the VICTIM of one of OUR KILLS (``home_corp_role`` ATTACKER).
  We killed them.

The two are mutually exclusive per mail (a mail is home-VICTIM or home-ATTACKER), so the union
never double-counts.

The "danger to us" rating reuses ``leaderboards.danger_rating`` UNCHANGED — same thresholds,
same labels, same newbro softening — only the inputs are flipped to their-vs-us tallies:
``danger_rating(kills=kills_vs_us, losses=losses_to_us)``. The ratio is then "the share of our
mutual engagements they won": ≥0.8 (they beat us ~4:1) reads Dangerous, ≥0.5 Risky, below that
Snuggly/Learning. No invented magic constants — the whole scale is the existing rating's.

No new denormalised table: everything is a live indexed query over the same
``Killmail``/``KillmailParticipant`` columns the leaderboards use, memoised per entity for a
short TTL (the leaderboards cache pattern). An entity with zero history is an honest empty
profile (``has_history`` False), not an error — the URL space is our own history.
"""
from __future__ import annotations

from datetime import UTC

from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, Max, Min, Q, Sum
from django.db.models.functions import ExtractHour, ExtractIsoWeekDay

from core.i18n import i18n_cache_key

from .leaderboards import danger_rating
from .models import Killmail, KillmailParticipant

CACHE_VERSION = 1
CACHE_TTL = 300  # seconds — short; an adversary profile is intel, not real-time
TOP_N = 10
_EVE_TZ = UTC  # EVE plays on UTC; pin heatmap extraction to it

_ATTACKER = Killmail.HomeRole.ATTACKER
_VICTIM = Killmail.HomeRole.VICTIM
_ROLE_ATTACKER = KillmailParticipant.Role.ATTACKER

# The three supported entity kinds → (participant column, victim column on Killmail).
# A character is matched by its own id; a corp/alliance by the corresponding column.
ENTITY_KINDS = ("character", "corporation", "alliance")
_FIELDS = {
    "character": ("character_id", "victim_character_id"),
    "corporation": ("corporation_id", "victim_corporation_id"),
    "alliance": ("alliance_id", "victim_alliance_id"),
}


def _home() -> int:
    return int(getattr(settings, "FORCA_HOME_CORP_ID", 0) or 0)


def is_valid_kind(kind: str) -> bool:
    return kind in ENTITY_KINDS


# --------------------------------------------------------------------------- #
#  Engagement queryset (the "vs us" mail set) — shared by the profile + the feed
# --------------------------------------------------------------------------- #
def _attacker_subquery(part_field: str, entity_id: int):
    """Killmail ids where the entity appears as an ATTACKER (id-subquery, not a join —
    a mail with many matching attacker rows, e.g. a whole corp, is counted once)."""
    return (
        KillmailParticipant.objects.filter(role=_ROLE_ATTACKER, **{part_field: entity_id})
        .values("killmail_id")
    )


def engagement_queryset(kind: str, entity_id: int):
    """Every home-corp killmail that is an engagement between the entity and us.

    Our kills of them (they are the victim of a home-ATTACKER mail) UNION our losses to
    them (they attacked a home-VICTIM mail). PvP only (``is_npc`` excluded), matching the
    leaderboards' definition so the counts reconcile with the rest of the board.
    """
    part_field, victim_field = _FIELDS[kind]
    attacker_sub = _attacker_subquery(part_field, entity_id)
    return Killmail.objects.filter(involves_home_corp=True, is_npc=False).filter(
        Q(home_corp_role=_ATTACKER, **{victim_field: entity_id})
        | Q(home_corp_role=_VICTIM, killmail_id__in=attacker_sub)
    )


# --------------------------------------------------------------------------- #
#  Name resolution (bulk, in-service — keeps the template query-free per row)
# --------------------------------------------------------------------------- #
def _entity_names(ids: list[int]) -> dict[int, str]:
    from apps.corporation.models import EveName

    wanted = [i for i in ids if i]
    if not wanted:
        return {}
    return dict(EveName.objects.filter(entity_id__in=wanted).values_list("entity_id", "name"))


def _type_names(ids: list[int]) -> dict[int, str]:
    from apps.sde.models import SdeType

    wanted = [i for i in ids if i]
    if not wanted:
        return {}
    return dict(SdeType.objects.filter(type_id__in=wanted).values_list("type_id", "name"))


def _system_names(ids: list[int]) -> dict[int, str]:
    from apps.sde.models import SdeSolarSystem

    wanted = [i for i in ids if i]
    if not wanted:
        return {}
    return dict(
        SdeSolarSystem.objects.filter(system_id__in=wanted).values_list("system_id", "name")
    )


def _region_names(ids: list[int]) -> dict[int, str]:
    from apps.sde.models import SdeRegion

    wanted = [i for i in ids if i]
    if not wanted:
        return {}
    return dict(SdeRegion.objects.filter(region_id__in=wanted).values_list("region_id", "name"))


# --------------------------------------------------------------------------- #
#  Component computations
# --------------------------------------------------------------------------- #
def _summary(engagements) -> dict:
    """Kills-vs-us / losses-to-us counts, ISK both ways, first/last seen — one query.

    ``home_corp_role`` VICTIM rows are mails they killed us on (their kills vs us);
    ATTACKER rows are mails we killed them on (their losses to us).
    """
    kills_vs_us = losses_to_us = 0
    isk_we_lost = isk_we_took = 0
    first_seen = last_seen = None
    for r in engagements.values("home_corp_role").annotate(
        n=Count("killmail_id"), isk=Sum("total_value"),
        first=Min("killmail_time"), last=Max("killmail_time"),
    ):
        if r["home_corp_role"] == _VICTIM:
            kills_vs_us = r["n"] or 0
            isk_we_lost = int(r["isk"] or 0)
        elif r["home_corp_role"] == _ATTACKER:
            losses_to_us = r["n"] or 0
            isk_we_took = int(r["isk"] or 0)
        for stamp in (r["first"], r["last"]):
            if stamp is not None:
                first_seen = stamp if first_seen is None else min(first_seen, stamp)
                last_seen = stamp if last_seen is None else max(last_seen, stamp)
    return {
        "kills_vs_us": kills_vs_us,
        "losses_to_us": losses_to_us,
        "isk_we_lost": isk_we_lost,
        "isk_we_took": isk_we_took,
        "engagements": kills_vs_us + losses_to_us,
        "first_seen": first_seen,
        "last_seen": last_seen,
        # The moat rating, computed over their-vs-us record (see module docstring).
        "danger": danger_rating(kills=kills_vs_us, losses=losses_to_us),
    }


def _hulls(kind: str, entity_id: int) -> list[dict]:
    """Top hulls the entity BRINGS against us — attacker ship types on our losses.

    Participation count (a corp fielding five Vexors on one mail = five), which reads as
    "what they undock against us" better than a per-mail distinct count would.
    """
    part_field, _victim = _FIELDS[kind]
    rows = list(
        KillmailParticipant.objects.filter(
            role=_ROLE_ATTACKER, **{part_field: entity_id},
            killmail__involves_home_corp=True, killmail__home_corp_role=_VICTIM,
            killmail__is_npc=False,
        )
        .exclude(ship_type_id__isnull=True)
        .values("ship_type_id")
        .annotate(n=Count("id"))
        .order_by("-n")[:TOP_N]
    )
    names = _type_names([r["ship_type_id"] for r in rows])
    return [
        {"ship_type_id": r["ship_type_id"],
         "name": names.get(r["ship_type_id"], f"Type {r['ship_type_id']}"), "count": r["n"]}
        for r in rows
    ]


def _systems(engagements) -> list[dict]:
    rows = list(
        engagements.values("solar_system_id")
        .annotate(n=Count("killmail_id"))
        .order_by("-n")[:TOP_N]
    )
    names = _system_names([r["solar_system_id"] for r in rows])
    return [
        {"system_id": r["solar_system_id"],
         "name": names.get(r["solar_system_id"], f"System {r['solar_system_id']}"), "count": r["n"]}
        for r in rows
    ]


def _regions(engagements) -> list[dict]:
    rows = list(
        engagements.filter(region_id__isnull=False)
        .values("region_id")
        .annotate(n=Count("killmail_id"))
        .order_by("-n")[:TOP_N]
    )
    names = _region_names([r["region_id"] for r in rows])
    return [
        {"region_id": r["region_id"],
         "name": names.get(r["region_id"], f"Region {r['region_id']}"), "count": r["n"]}
        for r in rows
    ]


def _heatmap(engagements) -> dict:
    """A 7x24 EVE-time (UTC) grid of when the entity engages us. Reuses the analytics
    heatmap cell builder so the template renders it identically to the pilot/corp pages."""
    from .analytics import _heatmap_cells

    grid = [[0] * 24 for _ in range(7)]
    rows = (
        engagements.annotate(
            dow=ExtractIsoWeekDay("killmail_time", tzinfo=_EVE_TZ),
            hour=ExtractHour("killmail_time", tzinfo=_EVE_TZ),
        )
        .values("dow", "hour")
        .annotate(n=Count("killmail_id"))
    )
    for r in rows:
        dow, hour, n = r["dow"], r["hour"], r["n"]
        if dow and 1 <= dow <= 7 and hour is not None and 0 <= hour <= 23:
            grid[dow - 1][hour] = n
    return _heatmap_cells(grid)


def _co_attackers(engagements, kind: str, entity_id: int) -> dict:
    """Who flies WITH the entity against us: the other attackers on our losses the entity
    was on, as a corp list and a character list, ranked by shared mails.

    Excludes the home corp (that's us) and the subject entity's own participants (a corp's
    own pilots aren't "who they fly with"), so both lists surface external allies.
    """
    part_field, _victim = _FIELDS[kind]
    home = _home()
    loss_mails = engagements.filter(home_corp_role=_VICTIM).values("killmail_id")
    base = KillmailParticipant.objects.filter(
        role=_ROLE_ATTACKER, killmail_id__in=loss_mails
    ).exclude(corporation_id=home).exclude(**{part_field: entity_id})

    corp_rows = list(
        base.exclude(corporation_id__isnull=True)
        .values("corporation_id")
        .annotate(n=Count("killmail_id", distinct=True))
        .order_by("-n")[:TOP_N]
    )
    char_rows = list(
        base.exclude(character_id__isnull=True)
        .values("character_id")
        .annotate(n=Count("killmail_id", distinct=True))
        .order_by("-n")[:TOP_N]
    )
    names = _entity_names(
        [r["corporation_id"] for r in corp_rows] + [r["character_id"] for r in char_rows]
    )
    corps = [
        {"kind": "corporation", "entity_id": r["corporation_id"],
         "name": names.get(r["corporation_id"], f"#{r['corporation_id']}"), "count": r["n"]}
        for r in corp_rows
    ]
    chars = [
        {"kind": "character", "entity_id": r["character_id"],
         "name": names.get(r["character_id"], f"#{r['character_id']}"), "count": r["n"]}
        for r in char_rows
    ]
    return {"corporations": corps, "characters": chars}


def _their_pilots(engagements, kind: str, entity_id: int) -> list[dict]:
    """The distinct pilots of a corp/alliance we have seen vs us, ranked by engagements.

    Only meaningful for corp/alliance pages (a character *is* one pilot). Combines the
    entity's attacker rows on our losses with its victim rows on our kills.
    """
    if kind == "character":
        return []
    part_field, victim_field = _FIELDS[kind]
    counts: dict[int, int] = {}
    # Attacker side — their pilots who shot our losses.
    loss_mails = engagements.filter(home_corp_role=_VICTIM).values("killmail_id")
    for r in (
        KillmailParticipant.objects.filter(
            role=_ROLE_ATTACKER, killmail_id__in=loss_mails, **{part_field: entity_id}
        )
        .exclude(character_id__isnull=True)
        .values("character_id")
        .annotate(n=Count("killmail_id", distinct=True))
    ):
        counts[r["character_id"]] = counts.get(r["character_id"], 0) + (r["n"] or 0)
    # Victim side — their pilots we killed.
    for r in (
        engagements.filter(home_corp_role=_ATTACKER, **{victim_field: entity_id})
        .exclude(victim_character_id__isnull=True)
        .values("victim_character_id")
        .annotate(n=Count("killmail_id", distinct=True))
    ):
        cid = r["victim_character_id"]
        counts[cid] = counts.get(cid, 0) + (r["n"] or 0)
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:TOP_N]
    names = _entity_names([cid for cid, _ in top])
    return [
        {"character_id": cid, "name": names.get(cid, f"#{cid}"), "count": n} for cid, n in top
    ]


# --------------------------------------------------------------------------- #
#  Assembled profile (cached)
# --------------------------------------------------------------------------- #
def _build(kind: str, entity_id: int) -> dict:
    engagements = engagement_queryset(kind, entity_id)
    summary = _summary(engagements)
    has_history = summary["engagements"] > 0
    payload = {
        "kind": kind,
        "entity_id": entity_id,
        "has_history": has_history,
        "summary": summary,
        "hulls": _hulls(kind, entity_id) if has_history else [],
        "systems": _systems(engagements) if has_history else [],
        "regions": _regions(engagements) if has_history else [],
        "heatmap": _heatmap(engagements) if has_history else None,
        "co_attackers": (
            _co_attackers(engagements, kind, entity_id)
            if has_history else {"corporations": [], "characters": []}
        ),
        "pilots": _their_pilots(engagements, kind, entity_id) if has_history else [],
    }
    return payload


def adversary_profile(kind: str, entity_id: int, *, use_cache: bool = True) -> dict:
    """The full adversary profile for one entity, memoised per entity (short TTL).

    Language-scoped key: the payload embeds the translated danger label (and the heatmap
    day names), so it must not be shared across locales — the leaderboards cache pattern.
    """
    if not use_cache:
        return _build(kind, entity_id)
    key = i18n_cache_key(f"kb:adv:{CACHE_VERSION}:{_home()}:{kind}:{entity_id}")
    payload = cache.get(key)
    if payload is None:
        payload = _build(kind, entity_id)
        cache.set(key, payload, CACHE_TTL)
    return payload


def recent_engagements(kind: str, entity_id: int, limit: int = 20):
    """The most recent mails between the entity and us, enriched for ``_feed_row.html``."""
    from django.db.models import Prefetch

    ids = list(
        engagement_queryset(kind, entity_id)
        .order_by("-killmail_time")
        .values_list("pk", flat=True)[:limit]
    )
    enriched = {
        k.pk: k
        for k in Killmail.objects.filter(pk__in=ids)
        .annotate(attacker_count=Count("participants", filter=Q(participants__role="attacker")))
        .prefetch_related(Prefetch(
            "participants",
            queryset=KillmailParticipant.objects.filter(role="attacker", final_blow=True),
            to_attr="final_blowers",
        ))
    }
    return [enriched[i] for i in ids if i in enriched]
