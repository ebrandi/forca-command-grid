"""KB-36 (WS-D2) — ship/weapon meta boards, mined from OUR killmail history.

Every board here answers a "meta" question — *what kills our Rifters, how does a hull we fly
perform, which weapons do our kills* — strictly from the home corp's own killmail history. The
board ingests nothing that does not touch the home corp, so "what kills a Rifter" always means
"what killed OUR Rifters / what we flew to kill theirs", never a universal claim (market-
leadership plan §1/§9). The pages state this in prose.

No new denormalised table (the CombatMetric decision, see the WS-D2 report): matchup boards are
inherently PAIRWISE (victim-hull × attacker-hull, victim-hull × weapon) and CombatMetric's flat
``(entity_type, entity_id, window)`` key cannot represent a pair without a synthetic composite
id — exactly the "forcing a schema that doesn't match" the brief warns against. Its ``SHIP``
entity type is declared but never written today. So, like ``adversary.py``, everything is a live
indexed query over the same ``Killmail`` / ``KillmailParticipant`` columns the leaderboards use,
memoised per ``(board, window[, hull])`` for a short TTL (the leaderboards cache pattern).

ISK figures use the at-kill valuation (``value_at_kill`` COALESCE ``total_value``) for the same
price-fairness the rankings use (KB-35 / WS-D1).
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, Max, Sum
from django.utils.translation import gettext_lazy as _

from core.i18n import i18n_cache_key

from .leaderboards import window_for
from .models import Killmail, KillmailParticipant
from .valuation import at_kill_value_expr

CACHE_VERSION = 1
CACHE_TTL = 300  # seconds — a meta board is analysis, tolerant of brief staleness
TOP_N = 10
HULL_PICK_LIMIT = 150  # hulls offered in the "what kills X" picker
POD_TYPE_ID = 670      # Capsule — not a fielded ship; excluded from hull boards

# The windows this surface offers. Narrower than the leaderboards' full set: a meta board is a
# trend read, so recent (30d/90d) and all-time are the useful lenses.
WINDOW_KEYS = ("30d", "90d", "all")
DEFAULT_WINDOW = "30d"

_ATTACKER = Killmail.HomeRole.ATTACKER
_VICTIM = Killmail.HomeRole.VICTIM
_ROLE_ATTACKER = KillmailParticipant.Role.ATTACKER


def _home() -> int:
    return int(getattr(settings, "FORCA_HOME_CORP_ID", 0) or 0)


def resolve_window(key: str) -> str:
    """Clamp an arbitrary query value to a supported window key (default 30d)."""
    return key if key in WINDOW_KEYS else DEFAULT_WINDOW


def window_choices() -> list[tuple[str, str]]:
    return [(k, window_for(k).label) for k in WINDOW_KEYS]


# --------------------------------------------------------------------------- #
#  Name resolution (bulk)
# --------------------------------------------------------------------------- #
def _type_names(ids) -> dict[int, str]:
    from apps.sde.models import SdeType

    wanted = [i for i in ids if i]
    if not wanted:
        return {}
    return dict(SdeType.objects.filter(type_id__in=wanted).values_list("type_id", "name"))


def _type_class_names(ids) -> dict[int, str]:
    """``{type_id: group_name}`` — the SDE group (ship class / weapon class) for a batch."""
    from apps.sde.models import SdeType

    wanted = [i for i in ids if i]
    if not wanted:
        return {}
    return dict(
        SdeType.objects.filter(type_id__in=wanted).values_list("type_id", "group__name")
    )


def _base(window_key: str):
    """Home-corp PvP killmails within the window (the shared board scope)."""
    window = window_for(window_key)
    qs = Killmail.objects.filter(involves_home_corp=True, is_npc=False)
    start = window.start
    end = window.end
    if start is not None:
        qs = qs.filter(killmail_time__gte=start)
    if end is not None:
        qs = qs.filter(killmail_time__lt=end)
    return qs


# --------------------------------------------------------------------------- #
#  Hull picker — hulls that appear in our history (as victim of either side)
# --------------------------------------------------------------------------- #
def hull_options(window_key: str = "all", *, use_cache: bool = True) -> list[dict]:
    """Hulls seen as a victim in our history (our losses + our kills), most-frequent first.

    This is the select for the "what kills X" board — every hull we have ever lost or killed,
    so the picker only ever offers a hull the board can actually answer for.
    """
    key = i18n_cache_key(f"kb:meta:hulls:{CACHE_VERSION}:{_home()}:{window_key}")
    if use_cache:
        cached = cache.get(key)
        if cached is not None:
            return cached
    rows = list(
        _base(window_key)
        .exclude(victim_ship_type_id=POD_TYPE_ID)
        .values("victim_ship_type_id")
        .annotate(n=Count("killmail_id"))
        .order_by("-n")[:HULL_PICK_LIMIT]
    )
    names = _type_names([r["victim_ship_type_id"] for r in rows])
    out = [
        {"ship_type_id": r["victim_ship_type_id"],
         "name": names.get(r["victim_ship_type_id"], f"Type {r['victim_ship_type_id']}"),
         "count": r["n"]}
        for r in rows
    ]
    if use_cache:
        cache.set(key, out, CACHE_TTL)
    return out


# --------------------------------------------------------------------------- #
#  "What kills <hull>" matchup board
# --------------------------------------------------------------------------- #
def _top_attacker_ships(loss_mail_subquery, *, home_only: bool, limit: int = TOP_N) -> list[dict]:
    qs = KillmailParticipant.objects.filter(
        role=_ROLE_ATTACKER, killmail_id__in=loss_mail_subquery
    ).exclude(ship_type_id__isnull=True).exclude(ship_type_id=POD_TYPE_ID)
    if home_only:
        qs = qs.filter(corporation_id=_home())
    rows = list(
        qs.values("ship_type_id").annotate(n=Count("id")).order_by("-n")[:limit]
    )
    names = _type_names([r["ship_type_id"] for r in rows])
    return [
        {"ship_type_id": r["ship_type_id"],
         "name": names.get(r["ship_type_id"], f"Type {r['ship_type_id']}"), "count": r["n"]}
        for r in rows
    ]


def _top_attacker_weapons(loss_mail_subquery, *, home_only: bool, limit: int = TOP_N) -> list[dict]:
    qs = KillmailParticipant.objects.filter(
        role=_ROLE_ATTACKER, killmail_id__in=loss_mail_subquery,
        weapon_type_id__isnull=False,
    ).exclude(weapon_type_id=0)
    if home_only:
        qs = qs.filter(corporation_id=_home())
    rows = list(
        qs.values("weapon_type_id").annotate(n=Count("id")).order_by("-n")[:limit]
    )
    names = _type_names([r["weapon_type_id"] for r in rows])
    return [
        {"weapon_type_id": r["weapon_type_id"],
         "name": names.get(r["weapon_type_id"], f"Type {r['weapon_type_id']}"), "count": r["n"]}
        for r in rows
    ]


def what_kills_hull(hull_type_id: int, window_key: str, *, use_cache: bool = True) -> dict:
    """Both matchup directions for one hull, from our history:

    * ``losses`` — what killed OUR ``hull``: the top attacker hulls + weapons on our losses
      of it (participation counts — a gang of five Vexors counts five).
    * ``kills`` — what WE flew to kill THEIR ``hull``: the top home-corp attacker hulls on our
      kills of it.
    """
    key = i18n_cache_key(
        f"kb:meta:matchup:{CACHE_VERSION}:{_home()}:{window_key}:{hull_type_id}"
    )
    if use_cache:
        cached = cache.get(key)
        if cached is not None:
            return cached

    base = _base(window_key)
    loss_mails = base.filter(home_corp_role=_VICTIM, victim_ship_type_id=hull_type_id).values(
        "killmail_id"
    )
    kill_mails = base.filter(home_corp_role=_ATTACKER, victim_ship_type_id=hull_type_id).values(
        "killmail_id"
    )
    payload = {
        "hull_type_id": hull_type_id,
        "losses": {
            "count": loss_mails.count(),
            "top_ships": _top_attacker_ships(loss_mails, home_only=False),
            "top_weapons": _top_attacker_weapons(loss_mails, home_only=False),
        },
        "kills": {
            "count": kill_mails.count(),
            "top_ships": _top_attacker_ships(kill_mails, home_only=True),
        },
    }
    if use_cache:
        cache.set(key, payload, CACHE_TTL)
    return payload


# --------------------------------------------------------------------------- #
#  Hull performance — per hull WE fly
# --------------------------------------------------------------------------- #
def hull_performance(window_key: str, limit: int = 25, *, use_cache: bool = True) -> list[dict]:
    """Per hull the home corp flies: kills-with vs losses-of, efficiency and ISK ratio.

    * ``kills`` — DISTINCT home-corp PvP kills a home pilot flew this hull on;
    * ``losses`` — our losses OF this hull;
    * ISK uses the at-kill value; ``kills``/``isk_destroyed`` are deduped per killmail so a kill
      with three same-hull pilots counts once (not three times). Efficiency is
      ``isk_destroyed / (isk_destroyed + isk_lost)``. Ranked by total engagements (kills+losses).

    Query budget: two grouped aggregates plus one dedup pass over (hull, mail) pairs; the
    result is cached, and the all-time window is the only heavy one.
    """
    key = i18n_cache_key(f"kb:meta:hullperf:{CACHE_VERSION}:{_home()}:{window_key}:{limit}")
    if use_cache:
        cached = cache.get(key)
        if cached is not None:
            return cached

    window = window_for(window_key)
    part = KillmailParticipant.objects.filter(
        role=_ROLE_ATTACKER, corporation_id=_home(),
        killmail__involves_home_corp=True, killmail__home_corp_role=_ATTACKER,
        killmail__is_npc=False,
    ).exclude(ship_type_id__isnull=True).exclude(ship_type_id=POD_TYPE_ID)
    if window.start is not None:
        part = part.filter(killmail__killmail_time__gte=window.start)
    if window.end is not None:
        part = part.filter(killmail__killmail_time__lt=window.end)
    # One row per (hull, mail) → the mail's value, so multiple same-hull pilots don't inflate.
    pairs = part.values("ship_type_id", "killmail_id").annotate(v=Max(at_kill_value_expr("killmail__")))
    kills_by_hull: dict[int, list] = defaultdict(lambda: [0, Decimal("0")])
    for p in pairs:
        e = kills_by_hull[p["ship_type_id"]]
        e[0] += 1
        e[1] += p["v"] or Decimal("0")

    loss_rows = (
        _base(window_key)
        .filter(home_corp_role=_VICTIM)
        .exclude(victim_ship_type_id=POD_TYPE_ID)
        .values("victim_ship_type_id")
        .annotate(losses=Count("killmail_id"), isk=Sum(at_kill_value_expr()))
    )
    losses_by_hull = {
        r["victim_ship_type_id"]: (r["losses"], r["isk"] or Decimal("0")) for r in loss_rows
    }

    hull_ids = set(kills_by_hull) | set(losses_by_hull)
    names = _type_names(hull_ids)
    out = []
    for hid in hull_ids:
        kills, isk_destroyed = kills_by_hull.get(hid, [0, Decimal("0")])
        losses, isk_lost = losses_by_hull.get(hid, (0, Decimal("0")))
        denom = isk_destroyed + isk_lost
        out.append({
            "ship_type_id": hid,
            "name": names.get(hid, f"Type {hid}"),
            "kills": kills,
            "losses": losses,
            "isk_destroyed": int(isk_destroyed),
            "isk_lost": int(isk_lost),
            "efficiency": float(isk_destroyed / denom * 100) if denom else 0.0,
        })
    out.sort(key=lambda r: (-(r["kills"] + r["losses"]), r["name"]))
    out = out[:limit]
    if use_cache:
        cache.set(key, out, CACHE_TTL)
    return out


# --------------------------------------------------------------------------- #
#  Weapon board — top weapons by our kills, with a weapon-class breakdown
# --------------------------------------------------------------------------- #
def weapon_board(window_key: str, limit: int = 25, *, use_cache: bool = True) -> dict:
    """Top weapons on OUR kills (home-corp attacker weapon ids), plus a class breakdown.

    Participation counts (a weapon used by five pilots on one mail counts five). ``classes``
    aggregates those counts by the weapon's SDE group name (Autocannon / Heavy Missile / …).
    """
    key = i18n_cache_key(f"kb:meta:weapons:{CACHE_VERSION}:{_home()}:{window_key}:{limit}")
    if use_cache:
        cached = cache.get(key)
        if cached is not None:
            return cached

    window = window_for(window_key)
    qs = KillmailParticipant.objects.filter(
        role=_ROLE_ATTACKER, corporation_id=_home(),
        killmail__involves_home_corp=True, killmail__home_corp_role=_ATTACKER,
        killmail__is_npc=False, weapon_type_id__isnull=False,
    ).exclude(weapon_type_id=0)
    if window.start is not None:
        qs = qs.filter(killmail__killmail_time__gte=window.start)
    if window.end is not None:
        qs = qs.filter(killmail__killmail_time__lt=window.end)
    rows = list(qs.values("weapon_type_id").annotate(n=Count("id")).order_by("-n")[:limit])

    ids = [r["weapon_type_id"] for r in rows]
    names = _type_names(ids)
    class_names = _type_class_names(ids)
    weapons = [
        {"weapon_type_id": r["weapon_type_id"],
         "name": names.get(r["weapon_type_id"], f"Type {r['weapon_type_id']}"),
         "weapon_class": class_names.get(r["weapon_type_id"]) or _("Other"),
         "count": r["n"]}
        for r in rows
    ]
    class_agg: dict[str, int] = defaultdict(int)
    for w in weapons:
        class_agg[w["weapon_class"]] += w["count"]
    classes = sorted(
        ({"name": k, "count": v} for k, v in class_agg.items()), key=lambda r: -r["count"]
    )
    payload = {"weapons": weapons, "classes": classes}
    if use_cache:
        cache.set(key, payload, CACHE_TTL)
    return payload


# --------------------------------------------------------------------------- #
#  Meta insights (readiness hint — informational, NOT an auto-recommendation)
# --------------------------------------------------------------------------- #
def meta_insights(window_key: str = "all", *, use_cache: bool = True) -> list[dict]:
    """Our three most-lost hulls and, for each, its top killers (attacker hulls on our losses).

    A light-touch readiness nudge: "these hulls die most, here's what kills them — go review the
    doctrine". Purely informational; the page links to doctrines, it does not auto-recommend.
    """
    key = i18n_cache_key(f"kb:meta:insights:{CACHE_VERSION}:{_home()}:{window_key}")
    if use_cache:
        cached = cache.get(key)
        if cached is not None:
            return cached

    base = _base(window_key)
    lost = list(
        base.filter(home_corp_role=_VICTIM)
        .exclude(victim_ship_type_id=POD_TYPE_ID)
        .values("victim_ship_type_id")
        .annotate(n=Count("killmail_id"))
        .order_by("-n")[:3]
    )
    names = _type_names([r["victim_ship_type_id"] for r in lost])
    out = []
    for r in lost:
        hid = r["victim_ship_type_id"]
        loss_mails = base.filter(home_corp_role=_VICTIM, victim_ship_type_id=hid).values("killmail_id")
        out.append({
            "ship_type_id": hid,
            "name": names.get(hid, f"Type {hid}"),
            "losses": r["n"],
            "top_killers": _top_attacker_ships(loss_mails, home_only=False, limit=3),
        })
    if use_cache:
        cache.set(key, out, CACHE_TTL)
    return out


# --------------------------------------------------------------------------- #
#  Assembled page payload
# --------------------------------------------------------------------------- #
def meta_page(window_key: str, hull_type_id: int | None = None) -> dict:
    """Everything the /killboard/meta/ page renders for a window and optional selected hull."""
    window_key = resolve_window(window_key)
    options = hull_options()
    selected = hull_type_id if any(o["ship_type_id"] == hull_type_id for o in options) else None
    name = ""
    if selected:
        name = next((o["name"] for o in options if o["ship_type_id"] == selected), "")
    return {
        "window": window_key,
        "windows": window_choices(),
        "hull_options": options,
        "selected_hull": selected,
        "selected_hull_name": name,
        "matchup": what_kills_hull(selected, window_key) if selected else None,
        "hull_performance": hull_performance(window_key),
        "weapons": weapon_board(window_key),
        "insights": meta_insights(),
    }
