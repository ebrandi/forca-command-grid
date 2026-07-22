"""KB-27 (WS-D4) — character-intel inference over OUR killmail history.

A single, explainable classifier that turns the flat flags we already ingest (``is_solo`` /
``is_npc`` / ``is_awox``, participant hulls, fitted victim modules) into an inferred combat
profile for ANY character id. It is deliberately honest about scope and confidence:

* **Scope.** Every figure is computed from the home corp's own killmail history — the board
  never ingests a mail that does not touch the home corp — so this is "how this pilot fought
  *in the fights we were in*", never a universal record. That means it works for our own
  pilots (rich history) AND for an adversary or a recruitment applicant we have crossed swords
  with; an applicant we have never met yields an honest empty, low-confidence "unknown" rather
  than a fabricated read (see :func:`character_intel` ``has_history``).
* **Explainability (house rule).** Nothing here ships a bare score. Every label — the dominant
  playstyle, the FC-likelihood band, the role mix, the awox line — travels with the underlying
  counts it was derived from, so a recruiter or an FC can see *why* and disagree. The
  FC-likelihood in particular is a list of named, contributing signals, never an opaque number.

The five inferred dimensions, and the exact signal behind each:

1. **Playstyle** — solo / small-gang / fleet. For every mail where the character is an
   ATTACKER (a kill they were on), we bucket by the TOTAL attacker count on that mail: 1 = solo,
   2–5 = small-gang, 6+ = fleet. That count is the size of the gang the character was part of,
   so the buckets read straight as how they tend to fight. (Losses are excluded from playstyle:
   on a loss the attacker count is how many enemies ganged *them*, which is the enemy's fleet
   size, not the character's playstyle.) We output the three shares + the dominant label.

2. **FC-likelihood** — low / medium / high, from fleet-lead SIGNALS present in our data. Each
   is documented and additive; the output lists the ones that fired:
     * ``large_fleet_presence`` — the character is repeatedly on big-fleet mails (the fleet
       bucket). Leading a fleet requires being *in* fleets, so sustained large-fleet presence is
       a necessary (not sufficient) FC signal.
     * ``co_pilot_centrality`` — a simple degree centrality over the co-attacker graph: the
       number of DISTINCT other pilots the character has fought alongside. An FC/backbone flies
       with many different people over time; a lone-wolf or a fixed-duo does not. We use degree
       (distinct co-pilots) as the bounded proxy for the fuller "co-attackers who also fly with
       each other" centrality — it is O(1) queries and needs no graph materialisation.
   We deliberately DO NOT use final-blow-to-participation ratio as an FC signal: the final blow
   goes to whoever lands the last volley (often the highest-DPS or simply luckiest pilot on the
   mail), which is unrelated to who called the fight — using it would reward gank-DPS, not
   leadership. It is documented here as an explicit non-signal.

3. **Role usage** — capital / logi / tackle / ewar / links / dps shares, via
   :mod:`apps.killboard.roles` (WS-D2). A victim mail is read from the pilot's fitted modules
   (item-based, authoritative); an attacker appearance is the coarse hull approximation with
   WS-D2's documented limits (only dedicated logi hulls and capital hulls are inferable from a
   hull alone — everything else falls to dps). Each role carries its appearance count.

4. **Awox risk** — FACTUAL counts, never a speculative score:
     * ``events`` — times the character was an ATTACKER on a mail whose VICTIM shared the
       character's own corporation on that mail (shot a corpmate); the strongest awox signal we
       can read directly from participant rows.
     * ``flagged`` — times the character was an attacker on a mail the ingest already flagged
       ``is_awox``.
   Both are reported with recency; the label is "awox events", not a risk percentage.

5. **Confidence** — sample-size driven and ALWAYS shown: n = total engagements (attacker mails
   + loss mails). n < 10 → low, n < 50 → medium, else high. A low-confidence profile is a
   caveat the surfaces render verbatim, so a thin history is never read as a strong claim.

Caching mirrors the leaderboards/adversary pattern: memoised per character for a short TTL,
language-scoped (the payload embeds translated labels), and every query is bounded — the whole
build is a fixed, small number of indexed aggregates regardless of how large the pilot's history
is (asserted in the tests, ceiling :data:`QUERY_CEILING`).
"""
from __future__ import annotations

from django.core.cache import cache
from django.db.models import Count, F, IntegerField, Max, OuterRef, Q, Subquery
from django.utils.translation import gettext

from core.i18n import i18n_cache_key

from . import roles
from .models import Killmail, KillmailParticipant

CACHE_VERSION = 1
CACHE_TTL = 600  # seconds — intel, not real-time; the leaderboards cache pattern
QUERY_CEILING = 12  # a full cold build stays within this many queries (asserted in tests)

_ATTACKER = KillmailParticipant.Role.ATTACKER

# Playstyle buckets by total attacker count on a mail the character was an attacker on.
SOLO, SMALL, FLEET = "solo", "small_gang", "fleet"
_SMALL_MAX = 5  # 1 = solo, 2..5 = small-gang, 6+ = fleet
PLAYSTYLE_ORDER = [SOLO, SMALL, FLEET]
_PLAYSTYLE_LABELS = {
    SOLO: gettext("Solo"),
    SMALL: gettext("Small-gang"),
    FLEET: gettext("Fleet"),
}

# FC-likelihood bands + the signal thresholds (documented, tunable in ONE place).
FC_LOW, FC_MEDIUM, FC_HIGH = "low", "medium", "high"
_FC_LABELS = {
    FC_LOW: gettext("Low"),
    FC_MEDIUM: gettext("Medium"),
    FC_HIGH: gettext("High"),
}
# A meaningful large-fleet footprint: at least this many fleet-bucket mails AND that being a
# real share of their kills (not one incidental blob).
_FC_MIN_FLEET_MAILS = 5
_FC_MIN_FLEET_SHARE = 0.30
# Degree centrality: flying alongside at least this many DISTINCT pilots reads as a hub.
_FC_MIN_DISTINCT_CO_PILOTS = 15

# Confidence tiers by sample size (total engagements).
CONF_LOW, CONF_MEDIUM, CONF_HIGH = "low", "medium", "high"
_CONF_LABELS = {
    CONF_LOW: gettext("low"),
    CONF_MEDIUM: gettext("medium"),
    CONF_HIGH: gettext("high"),
}
_CONF_LOW_MAX = 10   # n < 10 → low
_CONF_MEDIUM_MAX = 50  # n < 50 → medium, else high


def confidence_for(n: int) -> str:
    if n < _CONF_LOW_MAX:
        return CONF_LOW
    if n < _CONF_MEDIUM_MAX:
        return CONF_MEDIUM
    return CONF_HIGH


def playstyle_gaps(intel: dict) -> list[dict]:
    """Playstyle buckets a pilot has NO experience in yet — the 'grow into' gaps.

    Used by the mentorship matching hint (KB-27): a mostly-fleet cadet with zero solo kills has
    a solo gap, so an officer can steer them to a solo-experienced mentor. Explainable and
    factual — each gap is a named bucket the pilot simply has no kills in, read from the same
    counts the profile shows. Returns ``[]`` for a pilot with no kills at all (no basis for a
    gap) so the hint stays quiet rather than implying a gap it cannot support.
    """
    playstyle = intel.get("playstyle") or {}
    buckets = playstyle.get("buckets") or {}
    if not playstyle.get("total"):
        return []
    return [
        {"code": bucket, "label": str(_PLAYSTYLE_LABELS[bucket])}
        for bucket in PLAYSTYLE_ORDER
        if not buckets.get(bucket)
    ]


# --------------------------------------------------------------------------- #
#  Component computations (each a bounded, indexed aggregate)
# --------------------------------------------------------------------------- #
def _playstyle(character_id: int) -> dict:
    """Solo / small-gang / fleet distribution over the character's ATTACKER mails.

    One aggregate: each of the character's attacker rows is annotated with the total attacker
    count on its mail (a correlated subquery), then the three buckets are counted with filtered
    Counts — so the whole distribution is a single row, independent of history size.
    """
    att_count = Subquery(
        KillmailParticipant.objects.filter(killmail_id=OuterRef("killmail_id"), role=_ATTACKER)
        .order_by()
        .values("killmail_id")
        .annotate(n=Count("id"))
        .values("n"),
        output_field=IntegerField(),
    )
    agg = (
        KillmailParticipant.objects.filter(character_id=character_id, role=_ATTACKER)
        .annotate(att_n=att_count)
        .aggregate(
            solo=Count("killmail_id", filter=Q(att_n=1), distinct=True),
            small=Count("killmail_id", filter=Q(att_n__gt=1, att_n__lte=_SMALL_MAX), distinct=True),
            fleet=Count("killmail_id", filter=Q(att_n__gt=_SMALL_MAX), distinct=True),
        )
    )
    buckets = {SOLO: agg["solo"] or 0, SMALL: agg["small"] or 0, FLEET: agg["fleet"] or 0}
    total = sum(buckets.values())
    shares = {k: (v / total if total else 0.0) for k, v in buckets.items()}
    dominant = max(PLAYSTYLE_ORDER, key=lambda k: buckets[k]) if total else None
    return {
        "buckets": buckets,
        "shares": shares,
        "total": total,
        "dominant": dominant,
        "dominant_label": str(_PLAYSTYLE_LABELS[dominant]) if dominant else "",
    }


def _distinct_co_pilots(character_id: int) -> int:
    """Degree centrality: how many DISTINCT other pilots the character has attacked alongside.

    One aggregate over the co-attacker rows on the character's own attacker mails (excluding the
    character itself). Bounded — it returns a single count.
    """
    own_mails = KillmailParticipant.objects.filter(
        character_id=character_id, role=_ATTACKER
    ).values("killmail_id")
    return (
        KillmailParticipant.objects.filter(role=_ATTACKER, killmail_id__in=own_mails)
        .exclude(character_id=character_id)
        .exclude(character_id__isnull=True)
        .values("character_id")
        .distinct()
        .count()
    )


def _fc_likelihood(playstyle: dict, distinct_co_pilots: int, confidence: str) -> dict:
    """Low / medium / high from additive, named fleet-lead signals (never a bare score).

    Each signal that fires is returned with the counts behind it, so the band is fully
    explainable. A thin history (low confidence) is capped at ``low`` — we do not infer
    leadership from a handful of mails.
    """
    fleet_mails = playstyle["buckets"][FLEET]
    fleet_share = playstyle["shares"][FLEET]
    signals: list[dict] = []

    if fleet_mails >= _FC_MIN_FLEET_MAILS and fleet_share >= _FC_MIN_FLEET_SHARE:
        signals.append({
            "key": "large_fleet_presence",
            "label": gettext("Regularly on large-fleet kills"),
            "detail": {"fleet_mails": fleet_mails, "fleet_share": round(fleet_share, 2)},
        })
    if distinct_co_pilots >= _FC_MIN_DISTINCT_CO_PILOTS:
        signals.append({
            "key": "co_pilot_centrality",
            "label": gettext("Flies with a wide, recurring set of pilots"),
            "detail": {"distinct_co_pilots": distinct_co_pilots},
        })

    if confidence == CONF_LOW:
        level = FC_LOW  # too little history to infer leadership
    elif len(signals) >= 2:
        level = FC_HIGH
    elif len(signals) == 1:
        level = FC_MEDIUM
    else:
        level = FC_LOW
    return {
        "level": level,
        "label": str(_FC_LABELS[level]),
        "signals": signals,
        "distinct_co_pilots": distinct_co_pilots,
        "fleet_mails": fleet_mails,
    }


def _role_usage(character_id: int) -> dict:
    """Per-role appearance shares via :mod:`apps.killboard.roles`.

    Two signals, combined and counted once per appearance:
      * VICTIM mails (their losses) — item-based, authoritative: the fitted high/med/low module
        groups classify the ship (charges excluded), else the hull's capital-or-dps fallback.
      * ATTACKER appearances (their kills) — the coarse hull approximation, taking the
        highest-precedence role across every hull they flew (WS-D2's documented limits).

    Bounded: the attacker side groups by ship_type_id (≤ distinct hulls flown); the victim side
    reads only the character's own loss mails and their items.
    """
    counts: dict[str, int] = dict.fromkeys(roles.ROLE_ORDER, 0)

    # --- Attacker appearances: group by hull, classify each hull once, weight by count. ---
    attacker_hulls = list(
        KillmailParticipant.objects.filter(character_id=character_id, role=_ATTACKER)
        .exclude(ship_type_id__isnull=True)
        .values("ship_type_id")
        .annotate(n=Count("killmail_id", distinct=True))
    )
    ship_meta = roles._ship_group_meta([r["ship_type_id"] for r in attacker_hulls])
    attacker_appearances = 0
    for r in attacker_hulls:
        gid, gname = ship_meta.get(r["ship_type_id"], (None, ""))
        role = roles.attacker_role(gid, gname)
        counts[role] += r["n"]
        attacker_appearances += r["n"]

    # --- Victim mails (losses): item-based role per mail. ---
    from .fitrender import slot_bucket
    from .models import KillmailItem

    losses = list(
        Killmail.objects.filter(victim_character_id=character_id)
        .values("killmail_id", "victim_ship_type_id")
    )
    loss_ids = [row["killmail_id"] for row in losses]
    victim_ship_meta = roles._ship_group_meta([row["victim_ship_type_id"] for row in losses])
    mods_by_km: dict[int, set[str]] = {kid: set() for kid in loss_ids}
    if loss_ids:
        item_rows = list(
            KillmailItem.objects.filter(killmail_id__in=loss_ids).values(
                "killmail_id", "item_type_id", "flag"
            )
        )
        item_meta = roles._item_group_meta({it["item_type_id"] for it in item_rows})
        for it in item_rows:
            gname, cat = item_meta.get(it["item_type_id"], ("", None))
            if cat == roles._CHARGE_CATEGORY or not gname:
                continue
            if slot_bucket(it["flag"]) in roles._MODULE_SLOTS:
                mods_by_km[it["killmail_id"]].add(gname)
    for row in losses:
        gid, _gname = victim_ship_meta.get(row["victim_ship_type_id"], (None, ""))
        role = roles.victim_role(mods_by_km.get(row["killmail_id"], set()), gid)
        counts[role] += 1

    total = attacker_appearances + len(losses)
    shares = {k: (v / total if total else 0.0) for k, v in counts.items()}
    ordered = [
        {
            "role": role,
            "label": str(roles.ROLE_LABELS[role]),
            "count": counts[role],
            "share": shares[role],
        }
        for role in roles.ROLE_ORDER
        if counts[role]
    ]
    dominant = max(roles.ROLE_ORDER, key=lambda k: counts[k]) if total else None
    return {
        "counts": counts,
        "shares": shares,
        "total": total,
        "attacker_appearances": attacker_appearances,
        "losses": len(losses),
        "ordered": ordered,
        "dominant": dominant,
        "dominant_label": str(roles.ROLE_LABELS[dominant]) if dominant else "",
    }


def _awox(character_id: int) -> dict:
    """Factual awox counts: same-corp-victim events + is_awox-flagged mails, with recency."""
    # Shot a corpmate: attacker on a mail whose victim shares the character's own corp on that row.
    events = (
        KillmailParticipant.objects.filter(
            character_id=character_id, role=_ATTACKER, corporation_id__isnull=False
        )
        .filter(killmail__victim_corporation_id=F("corporation_id"))
        .aggregate(n=Count("killmail_id", distinct=True), last=Max("killmail__killmail_time"))
    )
    # Attacker on an ingest-flagged awox mail.
    flagged = (
        KillmailParticipant.objects.filter(character_id=character_id, role=_ATTACKER)
        .filter(killmail__is_awox=True)
        .aggregate(n=Count("killmail_id", distinct=True), last=Max("killmail__killmail_time"))
    )
    n_events = events["n"] or 0
    n_flagged = flagged["n"] or 0
    last = max(
        (s for s in (events["last"], flagged["last"]) if s is not None), default=None
    )
    return {
        "events": n_events,
        "flagged": n_flagged,
        "last": last,
        "has_risk": bool(n_events or n_flagged),
    }


# --------------------------------------------------------------------------- #
#  Assembled profile (cached)
# --------------------------------------------------------------------------- #
def _build(character_id: int) -> dict:
    playstyle = _playstyle(character_id)
    role_usage = _role_usage(character_id)
    awox = _awox(character_id)
    # Total engagements = attacker mails + loss mails. ``role_usage.total`` already sums the
    # attacker appearances (the same attacker-mail set as ``playstyle.total``) and the losses.
    n = role_usage["total"]
    confidence = confidence_for(n)
    distinct_co_pilots = _distinct_co_pilots(character_id)
    fc = _fc_likelihood(playstyle, distinct_co_pilots, confidence)
    return {
        "character_id": character_id,
        "has_history": n > 0,
        "engagements": n,
        "loss_mails": role_usage["losses"],
        "confidence": {
            "level": confidence,
            "label": str(_CONF_LABELS[confidence]),
            "n": n,
        },
        "playstyle": playstyle,
        "fc": fc,
        "roles": role_usage,
        "awox": awox,
    }


def character_intel(character_id: int, *, use_cache: bool = True) -> dict:
    """The full inferred intel profile for one character, memoised per character (short TTL).

    Language-scoped key: the payload embeds translated labels (playstyle, FC band, role labels,
    confidence), so it must not be shared across locales — the leaderboards cache pattern. An
    unknown/empty character is a valid, honest ``has_history=False`` payload (low confidence),
    never an error — the classifier is defined over ANY id in our history space.
    """
    character_id = int(character_id or 0)
    if not character_id:
        return _build(0)
    if not use_cache:
        return _build(character_id)
    key = i18n_cache_key(f"kb:intel:{CACHE_VERSION}:{character_id}")
    payload = cache.get(key)
    if payload is None:
        payload = _build(character_id)
        cache.set(key, payload, CACHE_TTL)
    return payload
