"""KB-32 combat campaigns (WS-C2) — scope matcher + auto-aggregation service.

A :class:`~apps.killboard.models.CombatCampaign` is an ops-integrated, date-ranged,
scoped view over the home-corp board. This module owns two things:

* **Matching.** ``matched_queryset`` filters the killmail table down to the mails a
  campaign covers, entirely in SQL (index-friendly for the aggregate queries).
  ``campaign_matches`` is the pure, per-mail equivalent used by tests and any
  single-killmail check. Both share one rule: a killmail matches when it is inside
  the campaign window AND every *specified* scope dimension matches; an absent or
  empty dimension is a wildcard.
* **Aggregation.** ``campaign_stats`` rolls the matched mails into a scoreboard
  (kills/losses/ISK/efficiency, top pilots by-main, top ships, participation) plus
  the ops overlays: SRP spend against the budget (actual claims where they exist,
  eligibility estimate otherwise — labelled), doctrine-compliance % against the
  target, and the linked op. Cached per campaign with a short TTL and recomputed
  lazily on view, following the leaderboards cache pattern (never via beat).

Entity scope uses the same "vs-us" side semantics as the public feed's entity
filters: a campaign names the adversary corps/chars/alliances it is about, and
``entity_side`` decides whether they must be the victim (our kills), an attacker
(our losses / their gang), or either.
"""
from __future__ import annotations

from decimal import Decimal

from django.core.cache import cache
from django.db.models import Count, Q, Sum

from . import battle_sides
from .models import CombatCampaign, Killmail, KillmailParticipant

CACHE_VERSION = 1
CACHE_TTL = 120  # seconds — recompute lazily; a campaign board is not real-time
TOP_N = 10

_ATTACKER = KillmailParticipant.Role.ATTACKER


def _home() -> int:
    from django.conf import settings

    return int(getattr(settings, "FORCA_HOME_CORP_ID", 0) or 0)


def _int_set(values) -> set[int]:
    out: set[int] = set()
    for v in values or []:
        try:
            out.add(int(v))
        except (TypeError, ValueError):
            continue
    return out


def _entity_dims(scope: dict) -> tuple[set[int], set[int], set[int]]:
    """``(character_ids, corporation_ids, alliance_ids)`` from the scope dict."""
    return (
        _int_set(scope.get("character_ids")),
        _int_set(scope.get("corporation_ids")),
        _int_set(scope.get("alliance_ids")),
    )


# --------------------------------------------------------------------------- #
#  Pure matcher (single mail) — for tests and one-off checks
# --------------------------------------------------------------------------- #
def campaign_matches(km: Killmail, scope: dict, *, start, end, attacker_rows=None) -> bool:
    """True iff ``km`` falls in the window AND clears every specified scope dimension.

    ``attacker_rows`` is an optional pre-fetched list of the mail's attacker
    participants (each a dict/obj with ``character_id``/``corporation_id``/
    ``alliance_id``); when omitted and attacker-side entity matching is needed, it is
    queried from ``km``. Kept pure so the truth table can be tested without the ORM
    aggregate path.
    """
    if km.killmail_time < start:
        return False
    if end is not None and km.killmail_time > end:
        return False

    direction = scope.get("direction") or "both"
    if direction == "kills" and km.home_corp_role != Killmail.HomeRole.ATTACKER:
        return False
    if direction == "losses" and km.home_corp_role != Killmail.HomeRole.VICTIM:
        return False

    systems = _int_set(scope.get("system_ids"))
    if systems and km.solar_system_id not in systems:
        return False
    regions = _int_set(scope.get("region_ids"))
    if regions and km.region_id not in regions:
        return False
    bands = {b for b in (scope.get("sec_bands") or []) if b}
    if bands and km.sec_band not in bands:
        return False

    doctrines = _int_set(scope.get("doctrine_ids"))
    if doctrines:
        did = km.doctrine_fit.doctrine_id if km.doctrine_fit_id else None
        if did not in doctrines:
            return False

    return _entity_match(km, scope, attacker_rows)


def _entity_match(km: Killmail, scope: dict, attacker_rows) -> bool:
    chars, corps, alls = _entity_dims(scope)
    if not (chars or corps or alls):
        return True  # entity dimension unspecified → wildcard

    side = scope.get("entity_side") or "either"
    victim_hit = (
        (bool(chars) and km.victim_character_id in chars)
        or (bool(corps) and km.victim_corporation_id in corps)
        or (bool(alls) and km.victim_alliance_id in alls)
    )
    if side == "victim":
        return victim_hit
    if victim_hit and side == "either":
        return True

    if attacker_rows is None:
        attacker_rows = list(
            km.participants.filter(role=_ATTACKER).values(
                "character_id", "corporation_id", "alliance_id"
            )
        )

    def _row(r, key):
        return r.get(key) if isinstance(r, dict) else getattr(r, key, None)

    attacker_hit = any(
        (bool(chars) and _row(r, "character_id") in chars)
        or (bool(corps) and _row(r, "corporation_id") in corps)
        or (bool(alls) and _row(r, "alliance_id") in alls)
        for r in attacker_rows
    )
    if side == "attacker":
        return attacker_hit
    return victim_hit or attacker_hit


# --------------------------------------------------------------------------- #
#  SQL matcher (the aggregate set)
# --------------------------------------------------------------------------- #
def _victim_entity_q(chars, corps, alls) -> Q:
    q = Q()
    if chars:
        q |= Q(victim_character_id__in=chars)
    if corps:
        q |= Q(victim_corporation_id__in=corps)
    if alls:
        q |= Q(victim_alliance_id__in=alls)
    return q


def _attacker_entity_q(chars, corps, alls) -> Q:
    """A killmail_id-subquery (not a join) over attacker participants — one mail with
    many matching attackers isn't duplicated, mirroring the feed's side filter."""
    pq = Q()
    if chars:
        pq |= Q(character_id__in=chars)
    if corps:
        pq |= Q(corporation_id__in=corps)
    if alls:
        pq |= Q(alliance_id__in=alls)
    sub = KillmailParticipant.objects.filter(pq, role=_ATTACKER).values("killmail_id")
    return Q(killmail_id__in=sub)


def matched_queryset(campaign: CombatCampaign):
    """The home-corp killmails a campaign covers, filtered in SQL by window + scope."""
    scope = campaign.scope or {}
    qs = Killmail.objects.filter(
        involves_home_corp=True, killmail_time__gte=campaign.start_time
    )
    if campaign.end_time is not None:
        qs = qs.filter(killmail_time__lte=campaign.end_time)

    direction = scope.get("direction") or "both"
    if direction == "kills":
        qs = qs.filter(home_corp_role=Killmail.HomeRole.ATTACKER)
    elif direction == "losses":
        qs = qs.filter(home_corp_role=Killmail.HomeRole.VICTIM)

    systems = _int_set(scope.get("system_ids"))
    if systems:
        qs = qs.filter(solar_system_id__in=systems)
    regions = _int_set(scope.get("region_ids"))
    if regions:
        qs = qs.filter(region_id__in=regions)
    bands = [b for b in (scope.get("sec_bands") or []) if b]
    if bands:
        qs = qs.filter(sec_band__in=bands)
    doctrines = _int_set(scope.get("doctrine_ids"))
    if doctrines:
        qs = qs.filter(doctrine_fit__doctrine_id__in=doctrines)

    chars, corps, alls = _entity_dims(scope)
    if chars or corps or alls:
        side = scope.get("entity_side") or "either"
        if side == "victim":
            qs = qs.filter(_victim_entity_q(chars, corps, alls))
        elif side == "attacker":
            qs = qs.filter(_attacker_entity_q(chars, corps, alls))
        else:
            qs = qs.filter(_victim_entity_q(chars, corps, alls) | _attacker_entity_q(chars, corps, alls))
    return qs


# --------------------------------------------------------------------------- #
#  Aggregation
# --------------------------------------------------------------------------- #
def _sorted_pilots(pilots: list[dict]) -> list[dict]:
    """Top-N pilot rows, most engaged first (kills desc, then ISK destroyed)."""
    rows = sorted(
        pilots,
        key=lambda p: (p.get("kills", 0), float(p.get("isk_destroyed", 0) or 0)),
        reverse=True,
    )
    return rows[:TOP_N]


def _pilot_rows(matched, home: int) -> dict[int, dict]:
    """Per-character kills/losses/ISK inside the matched set, keyed by character_id.

    Kills credit every home-corp attacker on a matched kill mail (deduped per mail,
    like the leaderboards); losses credit the home victim. The dicts carry the field
    names ``leaderboards._rollup_by_main`` sums, so the by-main view reuses it as-is.
    """
    pilots: dict[int, dict] = {}
    kill_rows = (
        KillmailParticipant.objects.filter(
            role=_ATTACKER, corporation_id=home, character_id__isnull=False,
            killmail_id__in=matched.filter(
                home_corp_role=Killmail.HomeRole.ATTACKER
            ).values("killmail_id"),
        )
        .values("character_id")
        .annotate(
            kills=Count("killmail_id", distinct=True),
            isk_destroyed=Sum("killmail__total_value"),
        )
    )
    for r in kill_rows:
        pilots[r["character_id"]] = {
            "character_id": r["character_id"],
            "kills": r["kills"] or 0,
            "isk_destroyed": r["isk_destroyed"] or Decimal("0"),
            "losses": 0,
            "isk_lost": Decimal("0"),
        }
    loss_rows = (
        matched.filter(
            home_corp_role=Killmail.HomeRole.VICTIM, victim_character_id__isnull=False
        )
        .values("victim_character_id")
        .annotate(losses=Count("killmail_id"), isk_lost=Sum("total_value"))
    )
    for r in loss_rows:
        p = pilots.setdefault(
            r["victim_character_id"],
            {"character_id": r["victim_character_id"], "kills": 0,
             "isk_destroyed": Decimal("0")},
        )
        p["losses"] = r["losses"] or 0
        p["isk_lost"] = r["isk_lost"] or Decimal("0")
    return pilots


def _srp_spend(matched, home: int) -> dict:
    """SRP spend for OUR in-scope losses, using actual claims where they exist.

    For each home-corp loss: an existing ``SrpClaim`` contributes its actual payout
    (a denied claim contributes 0) and is labelled *actual*; a loss with no claim
    falls back to the SRP-eligibility *estimate*. ``basis`` records which mix was
    used so the UI can flag estimated figures. Sensitive (officer overlay).
    """
    from apps.srp import services as srp_services
    from apps.srp.models import SrpClaim

    our_losses = list(
        matched.filter(
            home_corp_role=Killmail.HomeRole.VICTIM, victim_corporation_id=home
        ).prefetch_related("items")
    )
    claims = {
        c.killmail_id: c
        for c in SrpClaim.objects.filter(killmail_id__in=[km.pk for km in our_losses])
    }
    spend = Decimal("0")
    actual = estimated = 0
    for km in our_losses:
        claim = claims.get(km.pk)
        if claim is not None:
            actual += 1
            if claim.status != SrpClaim.Status.DENIED:
                spend += claim.payout or Decimal("0")
        else:
            estimated += 1
            info = srp_services.eligibility(km)
            if info.get("eligible"):
                spend += info.get("payout") or Decimal("0")

    if actual and estimated:
        basis = "mixed"
    elif actual:
        basis = "actual"
    elif estimated:
        basis = "estimate"
    else:
        basis = None
    return {
        "spend": spend, "losses": len(our_losses),
        "actual_claims": actual, "estimated": estimated, "basis": basis,
    }


def _compute_stats(campaign: CombatCampaign) -> dict:
    home = _home()
    matched = matched_queryset(campaign)

    kills = losses = 0
    isk_destroyed = isk_lost = Decimal("0")
    for row in matched.values("home_corp_role").annotate(
        n=Count("killmail_id"), isk=Sum("total_value")
    ):
        if row["home_corp_role"] == Killmail.HomeRole.ATTACKER:
            kills = row["n"] or 0
            isk_destroyed = row["isk"] or Decimal("0")
        elif row["home_corp_role"] == Killmail.HomeRole.VICTIM:
            losses = row["n"] or 0
            isk_lost = row["isk"] or Decimal("0")

    denom = float(isk_destroyed) + float(isk_lost)
    efficiency = (float(isk_destroyed) / denom * 100.0) if denom else 0.0

    top_ships = [
        {"ship_type_id": r["victim_ship_type_id"], "count": r["n"]}
        for r in matched.filter(home_corp_role=Killmail.HomeRole.ATTACKER)
        .values("victim_ship_type_id")
        .annotate(n=Count("killmail_id"))
        .order_by("-n")[:TOP_N]
    ]

    pilots = _pilot_rows(matched, home)
    from .leaderboards import _rollup_by_main

    top_pilots = _sorted_pilots(list(pilots.values()))
    top_pilots_by_main = _sorted_pilots(_rollup_by_main(list(pilots.values())))

    # Overlays (sensitive; the view gates rendering to officers).
    srp = _srp_spend(matched, home)
    budget = campaign.srp_budget_isk
    srp["budget"] = budget
    srp["pct"] = float(srp["spend"] / budget * 100) if budget else None
    srp["over_budget"] = bool(budget is not None and srp["spend"] > budget)

    compliance = battle_sides.compliance_over_losses(
        matched.filter(home_corp_role=Killmail.HomeRole.VICTIM, victim_corporation_id=home)
    )
    target = campaign.doctrine_target_pct
    compliance_delta = (
        compliance["percent"] - target if (compliance and target is not None) else None
    )

    return {
        "kills": kills, "losses": losses,
        "isk_destroyed": isk_destroyed, "isk_lost": isk_lost,
        "efficiency": efficiency,
        "participants": len(pilots),
        "killmail_count": kills + losses,
        "top_pilots": top_pilots,
        "top_pilots_by_main": top_pilots_by_main,
        "top_ships": top_ships,
        "srp": srp,
        "compliance": compliance,
        "doctrine_target_pct": target,
        "compliance_delta": compliance_delta,
    }


def campaign_stats(campaign: CombatCampaign, *, use_cache: bool = True) -> dict:
    """Aggregate scoreboard + overlays for a campaign (memoised, short TTL).

    The cache key carries ``updated_at`` so editing the campaign's scope/window busts
    the entry immediately; otherwise it recomputes lazily every ``CACHE_TTL`` seconds
    on view. The payload is prose-free (code labels only), so no locale scoping.
    """
    if not use_cache:
        return _compute_stats(campaign)
    stamp = int(campaign.updated_at.timestamp()) if campaign.updated_at else 0
    key = f"kb:campaign:{CACHE_VERSION}:{_home()}:{campaign.pk}:{stamp}"
    payload = cache.get(key)
    if payload is None:
        payload = _compute_stats(campaign)
        cache.set(key, payload, CACHE_TTL)
    return payload


def recent_matches(campaign: CombatCampaign, limit: int = 30):
    """The most recent matching killmails, enriched for the ``_feed_row.html`` fragment."""
    from django.db.models import Prefetch

    ids = list(
        matched_queryset(campaign).order_by("-killmail_time").values_list("pk", flat=True)[:limit]
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
