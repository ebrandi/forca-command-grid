"""KB-31 battle reports v2 — co-occurrence side detection, timeline, overlays.

The v1 report (``battle.py``) lists one side per corporation. v2 clusters those
corporations into the two (or more) *sides* that actually fought, the way
br.evetools.org does: entities that appear as attackers on the same killmail are
allies; the victim of a mail is an enemy of everyone who shot it.

Side detection (``detect_sides``) is a **pure, deterministic** function of the
killmail set:

  1. The unit of clustering is an *entity* — a corporation, or a bare character
     when the participant has no corporation (unaffiliated). Everyone with the
     same corporation is one entity.
  2. **Ally edges** union entities together with a union-find structure:
       * co-attack — every attacker entity on the same killmail is unioned, and
       * shared alliance — entities flying under the same alliance are unioned
         (allied corps that happened not to share a mail still land together).
  3. **Enemy edges** connect the resulting ally-components (``teams``): an
     attacker team is an enemy of the team its victim belongs to.
  4. The team graph is 2-**coloured** by breadth-first search. If it is bipartite
     (the usual red-vs-blue fight) the teams collapse into two sides; a victim
     shot by side A is coloured into side B exactly as the spec asks. A genuine
     three-way brawl is not 2-colourable, so we fall back to one side per team
     (an N-side partition).

Determinism comes from three rules: union-find always keeps the smallest entity
key as the representative; the BFS colouring visits teams in sorted-key order;
and the final sides are ordered *home side first, then by smallest member key*.
The home corporation is a fixed setting, so identical killmails always yield
identical side indexes and membership — recomputes are stable.

Manual reassignment (``move_entity``) never touches detection: it writes a
``BattleReportSideOverride`` keyed by entity, and ``recompute_sides`` re-reads
those overrides after detection and forces the listed entities onto their
recorded side index. Because indexes come from detection alone, an override index
keeps meaning as long as the killmail set is unchanged.
"""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import transaction

from .models import (
    BattleReport,
    BattleReportSide,
    BattleReportSideMember,
    BattleReportSideOverride,
    Killmail,
    KillmailParticipant,
)

# Entity kinds, ranked so a corporation is preferred over a bare character when we
# turn a participant into a clustering entity, and so keys sort deterministically.
_CORP = "corporation"
_CHAR = "character"
_KIND_RANK = {_CORP: 0, _CHAR: 1}


def _home_corp() -> int:
    return int(getattr(settings, "FORCA_HOME_CORP_ID", 0) or 0)


def _sort_key(entity: tuple[str, int]) -> tuple[int, int]:
    kind, eid = entity
    return (_KIND_RANK.get(kind, 9), eid)


def _entity_of(corp_id, char_id) -> tuple[str, int] | None:
    """The clustering entity for a participant: its corporation, else its character."""
    if corp_id:
        return (_CORP, int(corp_id))
    if char_id:
        return (_CHAR, int(char_id))
    return None


class _UnionFind:
    """Union-find whose representative is always the smallest entity key, so the
    component a caller reads back does not depend on union order."""

    def __init__(self) -> None:
        self.parent: dict[tuple[str, int], tuple[str, int]] = {}

    def add(self, x) -> None:
        self.parent.setdefault(x, x)

    def find(self, x):
        self.add(x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:  # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        lo, hi = (ra, rb) if _sort_key(ra) <= _sort_key(rb) else (rb, ra)
        self.parent[hi] = lo


def _load_battle(report: BattleReport):
    """Killmails (id-ordered) + their attacker/victim participants for detection."""
    kms = list(report.killmails.all().order_by("killmail_time", "killmail_id"))
    km_ids = [k.killmail_id for k in kms]
    parts = list(
        KillmailParticipant.objects.filter(killmail_id__in=km_ids).values(
            "killmail_id", "role", "character_id", "corporation_id", "alliance_id"
        )
    )
    attackers_by_km: dict[int, list[dict]] = {kid: [] for kid in km_ids}
    for p in parts:
        if p["role"] == KillmailParticipant.Role.ATTACKER:
            attackers_by_km.setdefault(p["killmail_id"], []).append(p)
    return kms, attackers_by_km


def detect_sides(report: BattleReport) -> list[list[tuple[str, int]]]:
    """Deterministic co-occurrence partition of the report's entities into sides.

    Returns a list of sides, each a sorted list of ``(entity_type, entity_id)``
    keys, ordered *home side first, then by smallest member key*.
    """
    kms, attackers_by_km = _load_battle(report)
    uf = _UnionFind()
    alliance_rep: dict[int, tuple[str, int]] = {}  # alliance_id -> first entity seen
    all_entities: set[tuple[str, int]] = set()

    def _note_alliance(entity, alliance_id) -> None:
        if not alliance_id:
            return
        rep = alliance_rep.setdefault(int(alliance_id), entity)
        uf.union(entity, rep)  # allied corps cluster even without a shared mail

    # Ally edges: co-attackers on a mail, plus shared-alliance links.
    for km in kms:
        victim = _entity_of(km.victim_corporation_id, km.victim_character_id)
        if victim:
            all_entities.add(victim)
            uf.add(victim)
            _note_alliance(victim, km.victim_alliance_id)
        attacker_entities: list[tuple[str, int]] = []
        for p in attackers_by_km.get(km.killmail_id, []):
            ent = _entity_of(p["corporation_id"], p["character_id"])
            if ent is None:
                continue
            all_entities.add(ent)
            attacker_entities.append(ent)
            _note_alliance(ent, p["alliance_id"])
        for ent in attacker_entities[1:]:
            uf.union(attacker_entities[0], ent)

    if not all_entities:
        return []

    # Enemy edges between ally-components (teams): attacker team vs victim team.
    team_enemies: dict[tuple[str, int], set[tuple[str, int]]] = {}

    def _enemy(a, b) -> None:
        ta, tb = uf.find(a), uf.find(b)
        if ta == tb:
            return
        team_enemies.setdefault(ta, set()).add(tb)
        team_enemies.setdefault(tb, set()).add(ta)

    for km in kms:
        victim = _entity_of(km.victim_corporation_id, km.victim_character_id)
        if victim is None:
            continue
        for p in attackers_by_km.get(km.killmail_id, []):
            ent = _entity_of(p["corporation_id"], p["character_id"])
            if ent is not None:
                _enemy(ent, victim)

    teams = sorted({uf.find(e) for e in all_entities}, key=_sort_key)

    # 2-colour the team graph (BFS, deterministic team order). A monochromatic
    # enemy edge means the graph is not bipartite → fall back to one side per team.
    colour: dict[tuple[str, int], int] = {}
    bipartite = True
    for start in teams:
        if start in colour:
            continue
        colour[start] = 0
        queue = [start]
        while queue:
            node = queue.pop(0)
            for nb in sorted(team_enemies.get(node, set()), key=_sort_key):
                if nb not in colour:
                    colour[nb] = 1 - colour[node]
                    queue.append(nb)
                elif colour[nb] == colour[node]:
                    bipartite = False

    # Group entities into their side buckets.
    if bipartite:
        side_of_team = {t: colour.get(t, 0) for t in teams}
    else:
        side_of_team = {t: i for i, t in enumerate(teams)}  # N sides, one per team

    buckets: dict[int, list[tuple[str, int]]] = {}
    for ent in all_entities:
        sid = side_of_team[uf.find(ent)]
        buckets.setdefault(sid, []).append(ent)
    sides = [sorted(members, key=_sort_key) for members in buckets.values()]

    # Order: home side first, then by smallest member key ascending.
    home = (_CORP, _home_corp())

    def _side_order(members):
        return (0 if home in members else 1, _sort_key(members[0]))

    return sorted(sides, key=_side_order)


def _apply_overrides(sides, overrides):
    """Relocate overridden entities into their recorded side index (in place).

    ``sides`` is the detected partition (list of member-key lists); ``overrides``
    maps ``(entity_type, entity_id) -> side_index``. Returns ``(new_sides,
    manual_keys)`` where empty detected sides are dropped but surviving sides keep
    their detection index, and ``manual_keys`` marks which entities were moved.
    """
    n = len(sides)
    indexed = {i: set(members) for i, members in enumerate(sides)}
    manual: set[tuple[str, int]] = set()
    for entity, target in overrides.items():
        if not (0 <= target < n):
            continue  # stale override (killmail set changed) — ignore
        moved = False
        for members in indexed.values():
            if entity in members:
                members.discard(entity)
                moved = True
        # Also honour an override for an entity detection didn't place anywhere.
        indexed[target].add(entity)
        manual.add(entity)
        if not moved:
            manual.add(entity)
    # Keep detection indexes; drop sides emptied by the moves.
    surviving = [(i, indexed[i]) for i in range(n) if indexed[i]]
    return surviving, manual


def _tally(report, sides_by_index):
    """Aggregate per-side and per-member kill/loss/ISK/pilot counts over the mails.

    ``sides_by_index`` maps a detection index to its set of member entity keys.
    Returns ``(side_totals, member_totals, side_pilots)``.
    """
    kms, attackers_by_km = _load_battle(report)
    side_of_entity: dict[tuple[str, int], int] = {}
    for idx, members in sides_by_index.items():
        for m in members:
            side_of_entity[m] = idx

    side_totals = {idx: {"kills": 0, "losses": 0, "isk_destroyed": Decimal("0"),
                         "isk_lost": Decimal("0")} for idx in sides_by_index}
    member_totals: dict[tuple[str, int], dict] = {}
    for members in sides_by_index.values():
        for m in members:
            member_totals[m] = {"kills": 0, "losses": 0, "isk_lost": Decimal("0")}
    side_pilots: dict[int, set[int]] = {idx: set() for idx in sides_by_index}
    member_pilots: dict[tuple[str, int], set[int]] = {m: set() for m in member_totals}

    for km in kms:
        value = km.total_value or Decimal("0")
        victim = _entity_of(km.victim_corporation_id, km.victim_character_id)
        v_side = side_of_entity.get(victim) if victim else None
        if v_side is not None:
            side_totals[v_side]["losses"] += 1
            side_totals[v_side]["isk_lost"] += value
            member_totals[victim]["losses"] += 1
            member_totals[victim]["isk_lost"] += value
            if km.victim_character_id:
                side_pilots[v_side].add(int(km.victim_character_id))
                member_pilots[victim].add(int(km.victim_character_id))
        # Attribute the kill to every OTHER side that had a shooter on this mail,
        # deduped per (side/member, killmail) so a 50-pilot gang counts once.
        killing_sides: set[int] = set()
        killing_members: set[tuple[str, int]] = set()
        for p in attackers_by_km.get(km.killmail_id, []):
            ent = _entity_of(p["corporation_id"], p["character_id"])
            side = side_of_entity.get(ent) if ent else None
            if side is None:
                continue
            if p["character_id"]:
                side_pilots[side].add(int(p["character_id"]))
                member_pilots[ent].add(int(p["character_id"]))
            if side != v_side:
                killing_sides.add(side)
                killing_members.add(ent)
        for side in killing_sides:
            side_totals[side]["kills"] += 1
            side_totals[side]["isk_destroyed"] += value
        for ent in killing_members:
            member_totals[ent]["kills"] += 1

    return side_totals, member_totals, side_pilots, member_pilots


@transaction.atomic
def recompute_sides(report: BattleReport) -> list[BattleReportSide]:
    """Run detection, apply the persisted overrides, and rebuild the side tables.

    Deletes and recreates ``BattleReportSide``/``BattleReportSideMember`` for the
    report; ``BattleReportSideOverride`` rows are read but never modified, so a
    prior officer reassignment survives every recompute.
    """
    report.detected_sides.all().delete()

    detected = detect_sides(report)
    overrides = {
        (o.entity_type, o.entity_id): o.side_index
        for o in report.side_overrides.all()
    }
    surviving, manual_keys = _apply_overrides(detected, overrides)
    sides_by_index = {idx: members for idx, members in surviving}

    side_totals, member_totals, side_pilots, member_pilots = _tally(report, sides_by_index)
    home = (_CORP, _home_corp())

    created: list[BattleReportSide] = []
    # Renumber to a dense 0..k label order for display while preserving detection order.
    for label_pos, (idx, members) in enumerate(surviving):
        st = side_totals[idx]
        side = BattleReportSide.objects.create(
            report=report,
            index=idx,
            label=_side_label(label_pos),
            is_home_side=home in members,
            kills=st["kills"],
            losses=st["losses"],
            isk_destroyed=st["isk_destroyed"],
            isk_lost=st["isk_lost"],
            pilot_count=len(side_pilots[idx]),
        )
        BattleReportSideMember.objects.bulk_create([
            BattleReportSideMember(
                side=side,
                entity_type=m[0],
                entity_id=m[1],
                is_manual=m in manual_keys,
                kills=member_totals[m]["kills"],
                losses=member_totals[m]["losses"],
                isk_lost=member_totals[m]["isk_lost"],
                pilot_count=len(member_pilots.get(m, set())),
            )
            for m in sorted(members, key=lambda k: (-member_totals[k]["isk_lost"], _sort_key(k)))
        ])
        created.append(side)
    return created


def _side_label(pos: int) -> str:
    """Side A, Side B, … (positional; the home side sorts first so it is Side A)."""
    if pos < 26:
        return f"Side {chr(ord('A') + pos)}"
    return f"Side {pos + 1}"


@transaction.atomic
def move_entity(report: BattleReport, entity_type: str, entity_id: int,
                side_index: int, *, actor=None) -> bool:
    """Officer action: force an entity onto ``side_index`` and recompute.

    Persists (or updates) a ``BattleReportSideOverride`` so the placement survives
    later recomputes, then rebuilds the derived side tables. Returns ``True`` when
    the target index is a real detected side.
    """
    if entity_type not in dict(BattleReportSideMember.EntityType.choices):
        return False
    detected = detect_sides(report)
    if not (0 <= side_index < len(detected)):
        return False
    BattleReportSideOverride.objects.update_or_create(
        report=report, entity_type=entity_type, entity_id=int(entity_id),
        defaults={"side_index": side_index, "created_by": actor},
    )
    recompute_sides(report)
    return True


# --------------------------------------------------------------------------- #
#  Timeline (scrubbable kill sequence with a cumulative ISK-swing sparkline)
# --------------------------------------------------------------------------- #
def battle_timeline(report: BattleReport, reference_side: BattleReportSide | None) -> dict:
    """Ordered kills with a cumulative ISK swing from ``reference_side``'s view.

    Each mail adds ``+value`` when the reference side destroyed a ship and
    ``-value`` when it lost one; ``swing`` is the running total after that mail.
    Also returns an SVG polyline (with a zero baseline) for a server-rendered
    sparkline. When no reference side is given (e.g. a report with no home side),
    the first side is used so the line still tells a consistent story.
    """
    sides = list(report.detected_sides.prefetch_related("members"))
    if reference_side is None:
        reference_side = sides[0] if sides else None
    ref_members: set[tuple[str, int]] = set()
    if reference_side is not None:
        ref_members = {(m.entity_type, m.entity_id) for m in reference_side.members.all()}

    kms = list(report.killmails.all().order_by("killmail_time", "killmail_id"))
    km_ids = [k.killmail_id for k in kms]
    attacker_ents: dict[int, set[tuple[str, int]]] = {kid: set() for kid in km_ids}
    for p in KillmailParticipant.objects.filter(
        killmail_id__in=km_ids, role=KillmailParticipant.Role.ATTACKER
    ).values("killmail_id", "character_id", "corporation_id"):
        ent = _entity_of(p["corporation_id"], p["character_id"])
        if ent is not None:
            attacker_ents[p["killmail_id"]].add(ent)

    rows: list[dict] = []
    swing = Decimal("0")
    for km in kms:
        value = km.total_value or Decimal("0")
        victim = _entity_of(km.victim_corporation_id, km.victim_character_id)
        ref_lost = victim in ref_members
        ref_killed = (not ref_lost) and bool(ref_members & attacker_ents.get(km.killmail_id, set()))
        if ref_lost:
            swing -= value
        elif ref_killed:
            swing += value
        rows.append({
            "killmail_id": km.killmail_id,
            "time": km.killmail_time,
            "victim_ship_type_id": km.victim_ship_type_id,
            "value": value,
            "ref_lost": ref_lost,
            "ref_killed": ref_killed,
            "swing": swing,
        })
    return {
        "rows": rows,
        "polyline": _swing_polyline([r["swing"] for r in rows]),
        "final_swing": swing,
    }


def _swing_polyline(values: list[Decimal], w: int = 240, h: int = 48) -> str:
    """SVG polyline ``points`` for a signed series, scaled around a zero baseline."""
    if not values:
        return ""
    floats = [float(v) for v in values]
    peak = max((abs(v) for v in floats), default=0.0) or 1.0
    mid = h / 2
    n = len(floats)
    step = w / (n - 1) if n > 1 else 0.0
    pts = []
    for i, v in enumerate(floats):
        x = round(i * step, 1)
        y = round(mid - (v / peak) * (mid - 2), 1)
        pts.append(f"{x},{y}")
    return " ".join(pts)


def swing_baseline_y(h: int = 48) -> float:
    """The y of the zero line for the swing sparkline (its vertical midpoint)."""
    return h / 2


# --------------------------------------------------------------------------- #
#  br.evetools export
# --------------------------------------------------------------------------- #
def brevetools_url(report: BattleReport) -> str:
    """A br.evetools.org related-battle URL for this report's first system + start.

    Format ``https://br.evetools.org/related/<systemID>/<YYYYMMDDHHMM>`` — the
    zKillboard ``/related/<systemID>/<datetime>/`` shape br.evetools ingests
    (market-leadership plan §11.1). Empty when the report spans no system.
    """
    systems = report.system_ids or []
    if not systems:
        return ""
    stamp = report.start_time.strftime("%Y%m%d%H%M")
    return f"https://br.evetools.org/related/{systems[0]}/{stamp}"


# --------------------------------------------------------------------------- #
#  Overlays (SRP liability, doctrine compliance, fleet-readiness context)
# --------------------------------------------------------------------------- #
def srp_liability(report: BattleReport, side: BattleReportSide) -> dict:
    """Open SRP liability for OUR losses on ``side`` (officer overlay).

    Sums the eligible payout (via ``apps.srp.services.eligibility`` — never a
    second valuation path) over the home-corp losses that fall on this side.
    Returns totals plus a covered/eligible count. Meaningful only for the home
    side; other sides return zeros.
    """
    from apps.srp import services as srp_services

    home = _home_corp()
    member_ids = {m.entity_id for m in side.members.all()
                  if m.entity_type == _CORP}
    if home not in member_ids:
        return {"total": Decimal("0"), "eligible": 0, "losses": 0}
    total = Decimal("0")
    eligible = 0
    losses = 0
    for km in report.killmails.filter(
        involves_home_corp=True, home_corp_role=Killmail.HomeRole.VICTIM,
        victim_corporation_id=home,
    ).prefetch_related("items"):
        losses += 1
        info = srp_services.eligibility(km)
        if info.get("eligible"):
            eligible += 1
            total += info.get("payout") or Decimal("0")
    return {"total": total, "eligible": eligible, "losses": losses}


def doctrine_compliance(report: BattleReport, side: BattleReportSide) -> dict | None:
    """Doctrine-compliance % for the home side's tagged losses (officer overlay).

    Compliant = a doctrine-tagged loss whose fit did not deviate; the ratio is
    over TAGGED losses only (an untagged loss isn't a doctrine ship, so it can't be
    off-doctrine). Returns ``None`` when this isn't the home side or nothing was
    doctrine-tagged. Derived from FitDeviation, which is officer/owner-sensitive,
    so the caller gates this to officers.
    """
    home = _home_corp()
    member_ids = {m.entity_id for m in side.members.all() if m.entity_type == _CORP}
    if home not in member_ids:
        return None
    tagged = 0
    clean = 0
    for km in report.killmails.filter(
        involves_home_corp=True, home_corp_role=Killmail.HomeRole.VICTIM,
        victim_corporation_id=home, doctrine_fit__isnull=False,
    ).select_related("fit_deviation"):
        tagged += 1
        dev = getattr(km, "fit_deviation", None)
        if dev is None or dev.is_clean:
            clean += 1
    if not tagged:
        return None
    return {"tagged": tagged, "clean": clean, "percent": round(clean / tagged * 100)}


def op_overlap(report: BattleReport):
    """A scheduled operation whose window overlaps the battle, or ``None``.

    A readiness-context chip: was this fight during a sanctioned op? Matches on the
    op's [target_at, target_at+duration] window intersecting the report window.
    Not sensitive (ops are member-visible), so the caller may show it to members.
    """
    from datetime import timedelta

    from django.db.models import Max

    from apps.operations.models import Operation

    max_dur = (
        Operation.objects.filter(duration_minutes__isnull=False)
        .aggregate(m=Max("duration_minutes"))["m"]
    )
    longest = timedelta(minutes=max_dur) if max_dur else timedelta(hours=24)
    longest = max(longest, timedelta(hours=24))
    candidates = (
        Operation.objects.filter(
            target_at__isnull=False,
            target_at__lte=report.end_time,
            target_at__gte=report.start_time - longest,
        )
        .exclude(status__in=[
            Operation.Status.DRAFT, Operation.Status.CANCELLED, Operation.Status.CANCELLED_AUTO,
        ])
        .order_by("-target_at")
    )
    for op in candidates:
        dur = timedelta(minutes=op.duration_minutes) if op.duration_minutes else timedelta(hours=1)
        op_end = op.target_at + dur
        if op.target_at <= report.end_time and op_end >= report.start_time:
            return op
    return None
