"""KB-34 D-scan / Local paste analyzer (WS-C4).

Paste the game's *Local* member list or a *Directional Scanner* dump and get back pre-fight
intel the public tools (PySpy, EveWho, dscan.info) give you — corp/alliance and ship-class
breakdowns, known-hostile flags — **enriched with what only we know**: our own engagement
history against the pasted entities (danger ratings from :mod:`apps.killboard.adversary`), our
Watchlists, and a counter-doctrine recommendation filtered to *current shipyard stock* ("undock
these — we have N in stock"). A pure killboard cannot do that last step; we can, because we hold
the doctrine + inventory context.

Privacy / statelessness (deliberate): a paste is analysed in-request and **never persisted** —
no model, no migration. The only writes are best-effort :class:`EveName` upserts for hostile
pilots we resolve, which are public-name cache rows, not the paste.

Two layers, kept separate so the fiddly bit is trivially unit-testable:

* **Parsing** (:func:`parse`, :func:`parse_local`, :func:`parse_dscan`, :func:`detect_kind`) —
  pure functions, no DB and no network. They turn raw pasted text into structured rows.
* **Analysis** (:func:`analyze_local`, :func:`analyze_dscan`, :func:`recommend_counter_doctrines`)
  — the DB/ESI layer, with every external dependency injectable so tests pass fakes.

Paste formats (documented because they vary by client version):

* **LOCAL** — one character name per line, as copied from the chat member list. No tabs.
* **D-SCAN** — tab-separated rows copied from the Directional Scanner. The column *order*
  differs across client versions (``itemID  name  typeName  distance`` is common, but some
  clients drop the id or the group), so the parser is deliberately tolerant: it splits on
  tabs, treats an all-digits cell as the item id, reads the distance from a trailing
  ``… km`` / ``… AU`` / ``… m`` / ``-`` cell, and hands every remaining text cell to the
  analyser as a *ship-type candidate*. The analyser then picks the candidate that matches a
  known SDE ship name — so we never depend on a fixed column position.
"""
from __future__ import annotations

import logging
import re

from django.core.cache import cache

_log = logging.getLogger("forca.killboard")

MAX_LINES = 2000
MAX_BYTES = 256 * 1024  # reject pastes larger than this before doing any work

# Directional-scanner distances: "14 km", "1,234 km", "1.2 AU", "950 m", or "-" (no range).
_DISTANCE_RE = re.compile(r"^-$|^[\d.,]+\s*(m|km|au)$", re.IGNORECASE)
_ALL_DIGITS_RE = re.compile(r"^\d+$")


class ScanKind:
    LOCAL = "local"
    DSCAN = "dscan"


class PasteTooLarge(ValueError):
    """Raised by :func:`parse` when the paste exceeds :data:`MAX_LINES` / :data:`MAX_BYTES`."""


# --------------------------------------------------------------------------- #
#  Ship-role classification (coarse, documented v1 constants)
# --------------------------------------------------------------------------- #
# Broad hull class comes from apps.doctrines.hulls; these finer SDE ship-group ids let us
# flag the *roles* that change how you engage a gang. Stable EVE static-data group ids.
LOGI_GROUPS = frozenset({832, 1527})  # Logistics Cruiser, Logistics Frigate
LINK_GROUPS = frozenset({540, 1534})  # Command Ship, Command Destroyer (fleet boosts)
# "Capital" as a hull class already covers carriers/dreads/FAX/supers/titans (hulls.py).


# --------------------------------------------------------------------------- #
#  Layer 1 — pure parsing (no DB, no network)
# --------------------------------------------------------------------------- #
def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]


def detect_kind(text: str) -> str:
    """Auto-detect LOCAL vs D-SCAN from the raw paste.

    D-scan rows are tab-separated; a Local member list is bare names. We call it D-scan when
    at least half the non-blank lines contain a tab, so a single stray tab in a long name
    list doesn't flip the whole paste — and a genuine d-scan (every row tabbed) always wins.
    """
    non_blank = [ln for ln in _lines(text) if ln]
    if not non_blank:
        return ScanKind.LOCAL
    tabbed = sum(1 for ln in non_blank if "\t" in ln)
    return ScanKind.DSCAN if tabbed * 2 >= len(non_blank) else ScanKind.LOCAL


def parse_local(text: str) -> list[str]:
    """A Local paste → an ordered, de-duplicated list of pilot names (case-insensitive dedupe)."""
    out, seen = [], set()
    for ln in _lines(text):
        if not ln or "\t" in ln:
            continue
        low = ln.lower()
        if low not in seen:
            seen.add(low)
            out.append(ln)
    return out


def _distance_kind(cell: str) -> str | None:
    """Classify a d-scan distance cell. Coarse and clearly-labelled as approximate:

    * ``km`` / ``m`` → ``"on_grid"`` (near — likely on or close to grid)
    * ``AU`` → ``"off_grid"`` (far — elsewhere in system)
    * ``-`` → ``"off_grid"`` (no range reported — celestial/structure/unknown)
    """
    c = (cell or "").strip()
    if c == "-":
        return "off_grid"
    m = _DISTANCE_RE.match(c)
    if not m:
        return None
    unit = (m.group(1) or "").lower()
    return "on_grid" if unit in ("m", "km") else "off_grid"


def parse_dscan(text: str) -> list[dict]:
    """A D-scan paste → structured rows.

    Each row: ``{raw, distance_raw, distance_kind, type_candidates}`` where ``type_candidates``
    is every non-id, non-distance text cell — the analyser matches one of them to an SDE ship
    name. Rows with no text cell at all are skipped.
    """
    rows: list[dict] = []
    for ln in _lines(text):
        if not ln:
            continue
        cells = [c.strip() for c in ln.split("\t")]
        distance_raw = None
        distance_kind = None
        candidates: list[str] = []
        for cell in cells:
            if not cell:
                continue
            if distance_raw is None and _distance_kind(cell) is not None:
                distance_raw = cell
                distance_kind = _distance_kind(cell)
                continue
            if _ALL_DIGITS_RE.match(cell):
                continue  # itemID column
            candidates.append(cell)
        if not candidates:
            continue
        rows.append({
            "raw": ln,
            "distance_raw": distance_raw,
            "distance_kind": distance_kind,
            "type_candidates": candidates,
        })
    return rows


def parse(text: str) -> dict:
    """Detect kind and parse, enforcing the size cap.

    Returns ``{"kind": ScanKind.*, "names": [...]}`` for LOCAL or
    ``{"kind": ScanKind.*, "rows": [...]}`` for D-SCAN. Raises :class:`PasteTooLarge` when the
    paste is over the line/byte cap (the view turns that into a friendly error).
    """
    text = text or ""
    if len(text.encode("utf-8", "ignore")) > MAX_BYTES:
        raise PasteTooLarge("paste exceeds byte cap")
    if sum(1 for _ in text.split("\n")) > MAX_LINES:
        raise PasteTooLarge("paste exceeds line cap")
    kind = detect_kind(text)
    if kind == ScanKind.DSCAN:
        return {"kind": kind, "rows": parse_dscan(text)}
    return {"kind": kind, "names": parse_local(text)}


# --------------------------------------------------------------------------- #
#  Layer 2 — analysis (DB / ESI; every external dependency injectable)
# --------------------------------------------------------------------------- #
def _default_char_resolver(names):
    from core.esi.names import resolve_character_ids

    return resolve_character_ids(names)


def _default_affiliations(char_ids):
    from core.esi.names import character_affiliations

    return character_affiliations(char_ids)


def _default_name_lookup(ids):
    """Best-effort ``{id: name}`` for corp/alliance ids: fill EveName from ESI, then read it.

    Network is best-effort (``resolve_ids`` swallows transport errors); the DB read
    (``names_for``) always returns whatever names we do know. Injected as a fake in tests."""
    from core.esi import names as esi_names

    ids = [i for i in ids if i]
    if not ids:
        return {}
    try:
        esi_names.resolve_ids(ids)
    except Exception:  # noqa: BLE001 — a name fill must never sink a live intel analysis
        _log.warning("scan analyzer corp/alliance name fill failed", exc_info=True)
    return esi_names.names_for(ids)


def _home() -> int:
    from django.conf import settings

    return int(getattr(settings, "FORCA_HOME_CORP_ID", 0) or 0)


def _watched_ids() -> dict[str, dict[int, list[str]]]:
    """Every watchlisted entity → the watchlist names it sits on, grouped by kind."""
    from .models import WatchlistEntry

    out: dict[str, dict[int, list[str]]] = {"character": {}, "corporation": {}, "alliance": {}}
    for kind, eid, wl_name in (
        WatchlistEntry.objects.select_related("watchlist")
        .values_list("entity_type", "entity_id", "watchlist__name")
    ):
        bucket = out.get(kind)
        if bucket is not None:
            bucket.setdefault(eid, []).append(wl_name)
    return out


def _batch_engagement(char_ids: list[int]) -> dict[int, dict]:
    """Our engagement record vs each pasted pilot, in two indexed queries (not per-pilot).

    Mirrors :mod:`apps.killboard.adversary`: *kills vs us* = the pilot attacked one of our
    losses; *losses to us* = we killed the pilot. The danger rating reuses
    :func:`leaderboards.danger_rating` unchanged (their-vs-us tallies), so a threat read here
    matches the adversary page exactly.
    """
    from django.db.models import Count

    from .leaderboards import danger_rating
    from .models import Killmail, KillmailParticipant

    ids = [int(c) for c in char_ids if c]
    if not ids:
        return {}
    _A = Killmail.HomeRole.ATTACKER
    _V = Killmail.HomeRole.VICTIM

    kills_vs_us: dict[int, int] = {}
    for r in (
        KillmailParticipant.objects.filter(
            role=KillmailParticipant.Role.ATTACKER, character_id__in=ids,
            killmail__involves_home_corp=True, killmail__is_npc=False,
            killmail__home_corp_role=_V,
        )
        .values("character_id")
        .annotate(n=Count("killmail_id", distinct=True))
    ):
        kills_vs_us[r["character_id"]] = r["n"] or 0

    losses_to_us: dict[int, int] = {}
    for r in (
        Killmail.objects.filter(
            involves_home_corp=True, is_npc=False, home_corp_role=_A,
            victim_character_id__in=ids,
        )
        .values("victim_character_id")
        .annotate(n=Count("killmail_id"))
    ):
        losses_to_us[r["victim_character_id"]] = r["n"] or 0

    out: dict[int, dict] = {}
    for cid in ids:
        k = kills_vs_us.get(cid, 0)
        loss = losses_to_us.get(cid, 0)
        if k or loss:
            out[cid] = {
                "kills_vs_us": k,
                "losses_to_us": loss,
                "engagements": k + loss,
                "danger": danger_rating(kills=k, losses=loss),
            }
    return out


def _danger_sort_key(row: dict):
    """Sort a threat row: watched first, then most dangerous, then most-seen, then name."""
    hist = row.get("history")
    ratio = hist["danger"]["ratio"] if hist else -1.0
    engagements = hist["engagements"] if hist else 0
    return (not row["watched"], -ratio, -engagements, (row.get("name") or "").lower())


def analyze_local(
    names,
    *,
    char_resolver=None,
    affiliations_fn=None,
    name_lookup_fn=None,
) -> dict:
    """Analyse a parsed Local list into a threat table + corp/alliance breakdown.

    Resolves pasted names → character ids (bulk ESI), looks up affiliations, aggregates
    corp/alliance counts, cross-references Watchlists, and enriches each pilot with our own
    engagement history (danger rating). Unresolved names are returned honestly, not hidden.
    All three external calls are injectable for testing.
    """
    char_resolver = char_resolver or _default_char_resolver
    affiliations_fn = affiliations_fn or _default_affiliations
    name_lookup_fn = name_lookup_fn or _default_name_lookup

    names = list(names or [])
    resolved = char_resolver(names)  # {name_lower: (id, canonical_name)}
    resolved_lower = {k.lower(): v for k, v in (resolved or {}).items()}

    pilots: list[dict] = []
    unresolved: list[str] = []
    for nm in names:
        hit = resolved_lower.get(nm.lower())
        if hit:
            pilots.append({"input": nm, "character_id": hit[0], "name": hit[1]})
        else:
            unresolved.append(nm)

    char_ids = [p["character_id"] for p in pilots]
    affiliations = affiliations_fn(char_ids) if char_ids else {}
    engagement = _batch_engagement(char_ids)
    watched = _watched_ids()

    corp_ids: set[int] = set()
    alliance_ids: set[int] = set()
    for p in pilots:
        aff = affiliations.get(p["character_id"]) or {}
        p["corporation_id"] = aff.get("corporation_id")
        p["alliance_id"] = aff.get("alliance_id")
        if p["corporation_id"]:
            corp_ids.add(p["corporation_id"])
        if p["alliance_id"]:
            alliance_ids.add(p["alliance_id"])

    entity_names = name_lookup_fn(corp_ids | alliance_ids)

    corp_counts: dict[int, int] = {}
    alliance_counts: dict[int, int] = {}
    threat: list[dict] = []
    for p in pilots:
        cid = p["character_id"]
        corp = p["corporation_id"]
        alliance = p["alliance_id"]
        if corp:
            corp_counts[corp] = corp_counts.get(corp, 0) + 1
        if alliance:
            alliance_counts[alliance] = alliance_counts.get(alliance, 0) + 1
        wl = list(watched["character"].get(cid, []))
        if corp:
            wl += watched["corporation"].get(corp, [])
        if alliance:
            wl += watched["alliance"].get(alliance, [])
        threat.append({
            "character_id": cid,
            "name": p["name"],
            "corporation_id": corp,
            "corporation_name": entity_names.get(corp) if corp else None,
            "alliance_id": alliance,
            "alliance_name": entity_names.get(alliance) if alliance else None,
            "watched": bool(wl),
            "watchlists": sorted(set(wl)),
            "history": engagement.get(cid),
        })
    threat.sort(key=_danger_sort_key)

    def _breakdown(counts, kind):
        rows = [
            {
                "entity_id": eid,
                "name": entity_names.get(eid) or f"#{eid}",
                "count": n,
                "watched": bool(watched[kind].get(eid)),
                "watchlists": sorted(set(watched[kind].get(eid, []))),
            }
            for eid, n in counts.items()
        ]
        rows.sort(key=lambda r: (not r["watched"], -r["count"], r["name"].lower()))
        return rows

    watched_hits = [t for t in threat if t["watched"]]
    return {
        "kind": ScanKind.LOCAL,
        "pilot_count": len(pilots),
        "unresolved": unresolved,
        "threat": threat,
        "corporations": _breakdown(corp_counts, "corporation"),
        "alliances": _breakdown(alliance_counts, "alliance"),
        "watched_count": len(watched_hits),
        "danger_count": sum(
            1 for t in threat if t["history"] and t["history"]["danger"]["ratio"] >= 0.5
        ),
    }


def _match_ships(rows: list[dict]) -> dict[str, dict]:
    """``{name_lower: {type_id, name, group_id, group_name, hull_class}}`` for every d-scan
    ship-type candidate that matches a *published ship* in the SDE (category 6). One query."""
    from apps.doctrines.hulls import hull_class_for_group
    from apps.sde.models import SdeType

    candidates: set[str] = set()
    for r in rows:
        candidates.update(r.get("type_candidates") or [])
    if not candidates:
        return {}
    matches: dict[str, dict] = {}
    for type_id, name, group_id, group_name in (
        SdeType.objects.filter(name__in=candidates, group__category_id=6)
        .values_list("type_id", "name", "group_id", "group__name")
    ):
        matches[name.lower()] = {
            "type_id": type_id,
            "name": name,
            "group_id": group_id,
            "group_name": group_name or "",
            "hull_class": hull_class_for_group(group_id),
        }
    return matches


def analyze_dscan(rows) -> dict:
    """Analyse parsed D-scan rows into a ship-class composition + notable-hull flags.

    Matches each row's candidates to an SDE ship; unmatched rows (drones, structures,
    celestials, containers) are counted separately, not folded into the ship totals. Reports
    counts by broad hull class, flags logi/links/capitals, and — where distances parse — an
    on-grid vs off-grid split.
    """
    from apps.doctrines.hulls import CLASS_ORDER

    rows = list(rows or [])
    matches = _match_ships(rows)

    class_counts: dict[str, int] = {}
    notable: dict[str, list[dict]] = {"capital": [], "logi": [], "links": []}
    notable_seen: dict[str, dict[int, int]] = {"capital": {}, "logi": {}, "links": {}}
    on_grid = off_grid = unknown_grid = 0
    ships = 0
    unmatched = 0

    for r in rows:
        ship = None
        for cand in r.get("type_candidates") or []:
            ship = matches.get(cand.lower())
            if ship:
                break
        if ship is None:
            unmatched += 1
            continue
        ships += 1
        hull_class = ship["hull_class"]
        class_counts[hull_class] = class_counts.get(hull_class, 0) + 1
        if r.get("distance_kind") == "on_grid":
            on_grid += 1
        elif r.get("distance_kind") == "off_grid":
            off_grid += 1
        else:
            unknown_grid += 1
        gid = ship["group_id"]
        role = None
        if hull_class == "Capital":
            role = "capital"
        elif gid in LOGI_GROUPS:
            role = "logi"
        elif gid in LINK_GROUPS:
            role = "links"
        if role:
            notable_seen[role][ship["type_id"]] = notable_seen[role].get(ship["type_id"], 0) + 1

    for role, by_type in notable_seen.items():
        for type_id, n in by_type.items():
            name = next((m["name"] for m in matches.values() if m["type_id"] == type_id), f"Type {type_id}")
            notable[role].append({"type_id": type_id, "name": name, "count": n})
        notable[role].sort(key=lambda x: (-x["count"], x["name"].lower()))

    composition = [
        {"hull_class": c, "count": class_counts[c]}
        for c in CLASS_ORDER
        if class_counts.get(c)
    ]
    return {
        "kind": ScanKind.DSCAN,
        "ship_count": ships,
        "unmatched": unmatched,
        "composition": composition,
        "class_counts": class_counts,
        "notable": notable,
        "has_capital": bool(notable["capital"]),
        "has_logi": bool(notable["logi"]),
        "has_links": bool(notable["links"]),
        "on_grid": on_grid,
        "off_grid": off_grid,
        "unknown_grid": unknown_grid,
    }


# --------------------------------------------------------------------------- #
#  Counter-doctrine recommendation (the unique kicker)
# --------------------------------------------------------------------------- #
# v1 suitability heuristic (deliberately coarse, and labelled as a *suggestion* in the UI):
# from the enemy's dominant hull class we prefer certain classes of our own, and count how
# many of a doctrine's fits fall in that preferred set. Ranking is stock-FIRST (the kicker a
# pure killboard can't do — "we have N in stock"), suitability only breaks ties.
_PREFERRED_BY_ENEMY_CLASS = {
    "Frigate": {"Frigate", "Destroyer", "Cruiser"},
    "Destroyer": {"Frigate", "Destroyer", "Cruiser"},
    "Cruiser": {"Cruiser", "Battlecruiser", "Battleship"},
    "Battlecruiser": {"Cruiser", "Battlecruiser", "Battleship"},
    "Battleship": {"Battleship", "Battlecruiser"},
    "Capital": {"Capital", "Battleship"},
    "Industrial": {"Frigate", "Cruiser"},
    "Freighter": {"Cruiser", "Battlecruiser"},
}


def _suitability(dscan_analysis: dict) -> tuple[set[str], list[str]]:
    """(preferred hull classes, human notes) for the enemy composition — v1 coarse heuristic."""
    class_counts = dscan_analysis.get("class_counts") or {}
    notes: list[str] = []
    preferred: set[str] = set()
    if dscan_analysis.get("has_capital"):
        preferred |= _PREFERRED_BY_ENEMY_CLASS["Capital"]
        notes.append("capitals_on_field")
    dominant = max(class_counts, key=class_counts.get) if class_counts else None
    if dominant:
        preferred |= _PREFERRED_BY_ENEMY_CLASS.get(dominant, {"Cruiser"})
    if dscan_analysis.get("has_logi"):
        notes.append("logi_present")
    if dscan_analysis.get("has_links"):
        notes.append("links_present")
    return preferred or {"Cruiser"}, notes


def recommend_counter_doctrines(dscan_analysis: dict, *, availability_fn=None) -> dict:
    """Rank OUR doctrines against the scanned gang, stock-first.

    For every fit in every ACTIVE doctrine we read live shipyard availability
    (:func:`apps.store.availability.availability_for_fits` — the queryable per-fit stock seam)
    and its hull class. Doctrines are ranked by: **has stock now** → **total available-to-
    promise** ("we have N in stock") → coarse suitability vs the enemy composition → priority.
    If the shipyard has no offers/stock at all we say so honestly and rank by suitability +
    priority without counts (``stock_configured`` False).
    """
    from apps.doctrines.hulls import hull_meta
    from apps.doctrines.models import Doctrine, DoctrineFit

    if availability_fn is None:
        from apps.store.availability import availability_for_fits as availability_fn

    doctrines = list(
        Doctrine.objects.filter(status=Doctrine.Status.ACTIVE).order_by("-priority", "name")
    )
    fits = list(DoctrineFit.objects.filter(doctrine__in=doctrines).select_related("doctrine"))
    if not fits:
        return {
            "stock_configured": False, "dominant": None,
            "preferred_classes": [], "notes": [], "doctrines": [],
        }

    hull = hull_meta([f.ship_type_id for f in fits])
    try:
        availability = availability_fn(fits)
    except Exception:  # noqa: BLE001 — never let a shipyard hiccup sink the intel page
        availability = {}

    # "Configured" = the shipyard actually holds stock we can talk about. ``is_offered``
    # defaults True even for an untouched fit, so it can't stand in for real inventory — a
    # corp that never stocked anything must get the honest "no counts" gap note, not fake zeros.
    stock_configured = any(int(getattr(a, "on_hand", 0) or 0) > 0 for a in availability.values())
    preferred, notes = _suitability(dscan_analysis)
    class_counts = dscan_analysis.get("class_counts") or {}
    dominant = max(class_counts, key=class_counts.get) if class_counts else None

    by_doctrine: dict[int, dict] = {}
    for f in fits:
        meta = hull.get(f.ship_type_id, {})
        hull_class = meta.get("hull_class", "Other")
        avail = availability.get(f.id)
        atp = int(getattr(avail, "atp", 0) or 0)
        d = by_doctrine.setdefault(f.doctrine_id, {
            "doctrine_id": f.doctrine_id,
            "name": f.doctrine.name,
            "priority": f.doctrine.priority,
            "fits": [],
            "total_atp": 0,
            "suitability": 0,
        })
        d["fits"].append({
            "fit_id": f.id,
            "name": f.name,
            "ship_type_id": f.ship_type_id,
            "hull_class": hull_class,
            "atp": atp,
            "state": getattr(avail, "state", None),
            "preferred": hull_class in preferred,
        })
        d["total_atp"] += atp
        if hull_class in preferred:
            d["suitability"] += 1

    recs = list(by_doctrine.values())
    for d in recs:
        d["has_stock"] = d["total_atp"] > 0
        # Show the in-stock fits first, most-available first, within each doctrine card.
        d["fits"].sort(key=lambda x: (-x["atp"], not x["preferred"], x["name"].lower()))
    recs.sort(key=lambda d: (
        not d["has_stock"], -d["total_atp"], -d["suitability"], -d["priority"], d["name"].lower()
    ))
    return {
        "stock_configured": stock_configured,
        "dominant": dominant,
        "preferred_classes": sorted(preferred),
        "notes": notes,
        "doctrines": recs,
    }


# --------------------------------------------------------------------------- #
#  Rate limiting (simple cache cooldown — the app's house style, no new table)
# --------------------------------------------------------------------------- #
_RATE_LIMIT = 10          # analyses per user per window
_RATE_WINDOW = 60         # seconds


def rate_limit_ok(user_id) -> bool:
    """True if this user may run another analysis now; a sliding ~10/min per-user cap.

    Reuses the pingboard rate-limit idiom (``cache.add`` seeds the window, ``cache.incr``
    counts within it) so an analyse POST — which fans out to ESI — can't be spammed."""
    key = f"kb:scan:rate:{user_id}"
    if cache.add(key, 1, _RATE_WINDOW):
        return True
    try:
        return cache.incr(key) <= _RATE_LIMIT
    except ValueError:  # key expired between add and incr — treat as a fresh window
        cache.add(key, 1, _RATE_WINDOW)
        return True


# --------------------------------------------------------------------------- #
#  One-click Pingboard alert
# --------------------------------------------------------------------------- #
def build_alert_summary(analysis: dict, *, system: str = "") -> str:
    """A compact, corp-broadcastable intel line from an analysis (no pilot names dumped).

    Local: pilot count, top corp/alliance, watched/danger hits. D-scan: ship count, top
    classes, notable-role flags. Kept short — it rides a Pingboard alert body.
    """
    system = (system or "").strip()
    where = f" in {system}" if system else ""
    if analysis.get("kind") == ScanKind.DSCAN:
        top = ", ".join(f"{c['count']}× {c['hull_class']}" for c in analysis.get("composition", [])[:3])
        flags = []
        if analysis.get("has_capital"):
            flags.append("CAPITALS")
        if analysis.get("has_logi"):
            flags.append("logi")
        if analysis.get("has_links"):
            flags.append("links")
        flag_txt = f" [{', '.join(flags)}]" if flags else ""
        return (
            f"D-scan intel{where}: {analysis.get('ship_count', 0)} ships"
            f"{' — ' + top if top else ''}{flag_txt}."
        )
    # local
    parts = [f"Local intel{where}: {analysis.get('pilot_count', 0)} pilots"]
    corps = analysis.get("corporations", [])
    if corps:
        parts.append(f"top corp {corps[0]['name']} (×{corps[0]['count']})")
    if analysis.get("watched_count"):
        parts.append(f"{analysis['watched_count']} watchlisted")
    if analysis.get("danger_count"):
        parts.append(f"{analysis['danger_count']} known-dangerous")
    return " — ".join(parts) + "."


def emit_alert(analysis: dict, *, system: str = "", source_id: str = "") -> object | None:
    """Broadcast the compact intel summary through the existing Pingboard seam.

    Reuses :func:`apps.pingboard.services.emit_broadcast` exactly as the watchlist tripwire
    does — corp audience, ``ROAMING_GANG`` category (a scouted gang). Returns the created
    ``Alert`` (or ``None`` if suppressed / no channel armed). Best-effort by design.
    """
    from apps.pingboard import services as pingboard
    from apps.pingboard.models import AlertCategory

    summary = build_alert_summary(analysis, system=system)
    return pingboard.emit_broadcast(
        category=AlertCategory.ROAMING_GANG,
        title="Scout intel",
        body=summary,
        source_service="killboard",
        source_object_id=source_id,
        audience={"kind": "corp"},
    )
