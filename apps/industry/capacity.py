"""P5 — THE manufacturing-capacity authority (one truthful definition).

Every "how many slots does the corp have, how many are committed, and can this
build actually land by date X" question goes through this module — the MRP run
(to constrain feasible dates) and the leadership view (to display pools) share
one definition, exactly the way :mod:`apps.stockpile.availability` is the sole
per-type availability authority. Never compute capacity anywhere else.

The load-bearing rules (each guards a real double-count or a real leak):

* **Consent gate.** A pilot contributes *named* capacity only with an **active
  ``my_industry`` scope grant AND home-corp membership**. This EXTENDS
  ``job_tracker``'s ``has_my_industry`` lookup (``tools.py`` — which checks
  neither ``active`` nor corp membership because it only gates what a pilot sees
  about *themselves*); copying that filter verbatim would keep a revoked grant
  contributing named rows. Non-consenting pilots' work is an anonymous count.
* **Unknown ≠ zero.** No skills snapshot, a snapshot older than
  ``capacity_skill_stale_days``, or a non-consenting installer ⇒ *unknown*
  capacity: excluded from measured totals, surfaced as the ``unmeasured``
  bottleneck — never a 0 that deflates the corp's theoretical capacity.
* **One physical job, one slot.** Committed load is deduped by ``job_id`` across
  :class:`CorpIndustryJob` / :class:`CharacterIndustryJob` (corp row wins), and
  board ``BuildJob``s never occupy *committed* slots — the ESI row is the build.
* **Slots are pilot-global.** Pools are per (pilot, activity class), never per
  location — Mass Production slots follow the pilot, not the station. Remaining
  is summed per pilot ``max(0, slots − used)``, never pool-level ``Σslots − Σused``.
* **Determinism.** Every ordering (rows, pilots, slot free-times) is total, so a
  re-run on frozen inputs writes zero rows (the P3 idempotency contract).

The app only ever *plans*: it schedules promises, it never starts, pauses or
delivers an in-game job (no ESI write APIs exist).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as dt_time

from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.industry.bom import buildable_recipe
from apps.industry.models import ProductionResource
from apps.sso.models import EveScopeGrant

# --------------------------------------------------------------------------- #
#  Constants (documented, not magic)
# --------------------------------------------------------------------------- #
#: Activity classes we model as slot pools. Science is displayed but never
#: *scheduled* — P3 plans manufacturing + reactions only, and a science job
#: counts as load via its ESI end_date, needing no duration math.
ACTIVITY_CLASSES = ("manufacturing", "reaction", "science")

#: ESI industry ``activity_id`` → slot pool. manufacturing = {1}, reaction = {9},
#: science = {3 TE, 4 ME, 5 copy, 7 reverse-eng, 8 invention} — the full set from
#: ``CorpIndustryJob.ACTIVITY_LABELS`` so every occupying job maps to exactly one pool.
_ACTIVITY_ID_TO_CLASS = {
    1: "manufacturing",
    9: "reaction",
    3: "science", 4: "science", 5: "science", 7: "science", 8: "science",
}

#: ``Recipe.activity`` code → slot pool (the only two the scheduler books onto).
_RECIPE_ACTIVITY_TO_CLASS = {
    "manufacturing": "manufacturing",
    "reaction": "reaction",
}

#: Slot-granting skills per class, ``(base, advanced)``. Effective slots =
#: ``1 + trained(base) + trained(advanced)``. These type ids appear NOWHERE else
#: in the codebase and there is no dogma import, so they are a documented constant
#: with a guard test (``test_slot_skill_ids_match_sde``) that fails loudly if a
#: name ever drifts from the SDE — a wrong hardcode must never mis-count silently.
SLOT_SKILLS: dict[str, tuple[int, int]] = {
    "manufacturing": (3387, 24625),   # Mass Production, Advanced Mass Production
    "science": (3406, 24624),         # Laboratory Operation, Advanced Laboratory Operation
    "reaction": (45748, 45749),       # Mass Reactions, Advanced Mass Reactions
}

#: Expected ``SdeType.name`` per slot-skill id — the guard test's oracle.
SLOT_SKILL_NAMES: dict[int, str] = {
    3387: "Mass Production",
    24625: "Advanced Mass Production",
    3406: "Laboratory Operation",
    24624: "Advanced Laboratory Operation",
    45748: "Mass Reactions",
    45749: "Advanced Mass Reactions",
}

#: In-game statuses that occupy a slot. ``ready`` (finished, undelivered) STILL
#: holds its slot until delivered — deliberately independent of
#: ``MrpConfig.include_ready_jobs`` (that knob governs *supply* counting, a
#: different question). ``delivered`` / ``cancelled`` never occupy.
_OCCUPYING_JOB_STATUSES = ("active", "paused", "ready")

#: Machine bottleneck codes → translated labels (the FEASIBLE_SOURCE_LABELS
#: discipline: the code persists, the label renders in the reader's locale).
BOTTLENECK_LABELS: dict[str, object] = {
    "slots": _("slots"),
    "skills": _("skills"),
    "blueprint": _("blueprint"),
    "facility": _("facility"),
    "materials": _("materials"),
    "unmeasured": _("unmeasured"),
}


def bottleneck_label(code: str):
    """The human label for a bottleneck code (the code itself if unmapped/blank)."""
    return BOTTLENECK_LABELS.get(code, code)


# --------------------------------------------------------------------------- #
#  Day quantization (mirrors mrp._day — capacity dates are day-grained too)
# --------------------------------------------------------------------------- #
def _day(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    local = timezone.localtime(dt)
    return timezone.make_aware(datetime.combine(local.date(), dt_time.min))


def _week_start(d: date) -> date:
    """Monday of the ISO week containing ``d`` — the weekly-cap accounting key."""
    return d - timedelta(days=d.weekday())


# --------------------------------------------------------------------------- #
#  Consent gate
# --------------------------------------------------------------------------- #
def _consenting_character_ids() -> set[int]:
    """Characters that contribute NAMED capacity: an **active** ``my_industry``
    grant AND home-corp membership. Both revocation paths flip every grant to
    ``active=False`` together (``sso/linking.py``, ``sso/views.py``), so this set
    drops a revoked or departed pilot on the next read."""
    return set(
        EveScopeGrant.objects.filter(
            feature_key="my_industry", active=True, character__is_corp_member=True,
        ).values_list("character_id", flat=True)
    )


def _latest_snapshots(char_ids) -> dict[int, object]:
    from apps.characters.models import CharacterSkillSnapshot

    return {
        s.character_id: s
        for s in CharacterSkillSnapshot.objects.filter(
            character_id__in=list(char_ids), is_latest=True
        )
    }


# --------------------------------------------------------------------------- #
#  Slot derivation
# --------------------------------------------------------------------------- #
def derive_resources(config) -> int:
    """Upsert a :class:`ProductionResource` per (consenting pilot, activity class),
    deriving ``slots_total`` from the pilot's latest skills snapshot; delete rows
    for characters that no longer qualify. Returns the number of rows written.

    Officer overrides (``manual_slots_override``, ``max_weekly_output``, window,
    pause) are preserved — only ``slots_total`` and ``as_of`` are rewritten, and
    only when changed (the value-compare-before-write discipline). Idempotent: a
    re-run on unchanged skills writes nothing. The ONLY on-demand caller besides
    the run is the officer "Re-derive now" POST — a GET never derives.
    """
    from apps.sso.models import EveCharacter

    consenting = _consenting_character_ids()
    chars = {
        c.character_id: c
        for c in EveCharacter.objects.filter(character_id__in=consenting)
    }
    snaps = _latest_snapshots(consenting)
    stale_cutoff = timezone.now() - timedelta(days=int(config.capacity_skill_stale_days))
    now = timezone.now()

    written = 0
    existing = {
        (r.character_id, r.activity_class): r
        for r in ProductionResource.objects.filter(character_id__in=consenting)
    }
    for cid in sorted(consenting):
        if cid not in chars:
            continue
        snap = snaps.get(cid)
        snap_fresh = bool(snap and snap.as_of and snap.as_of >= stale_cutoff)
        snap_as_of = snap.as_of if snap else None
        for cls in ACTIVITY_CLASSES:
            base_id, adv_id = SLOT_SKILLS[cls]
            slots = (
                1 + snap.trained_level(base_id) + snap.trained_level(adv_id)
                if snap_fresh else None
            )
            resource = existing.get((cid, cls))
            if resource is None:
                ProductionResource.objects.create(
                    character_id=cid, activity_class=cls, slots_total=slots,
                    as_of=(snap_as_of or now), source="esi_char",
                )
                written += 1
                continue
            fields: list[str] = []
            if resource.slots_total != slots:
                resource.slots_total = slots
                fields.append("slots_total")
            # Only a REAL snapshot refreshes as_of — a snapshot-less pilot keeps its
            # stamp, so a re-run on unchanged skills writes nothing (idempotency).
            if snap_as_of is not None and resource.as_of != snap_as_of:
                resource.as_of = snap_as_of
                fields.append("as_of")
            if fields:
                resource.save(update_fields=fields)
                written += 1

    # Drop rows whose character no longer qualifies (grant revoked / left corp).
    stale = ProductionResource.objects.exclude(character_id__in=consenting)
    written += stale.count()
    stale.delete()
    return written


# --------------------------------------------------------------------------- #
#  Capacity state (the one read — measured pools, committed load, unmeasured)
# --------------------------------------------------------------------------- #
@dataclass
class PilotPool:
    """One consenting pilot's slots + committed load for one activity class."""

    character_id: int
    name: str
    activity_class: str
    effective_slots: int | None          # None = unknown (never bookable, never 0)
    slots_total: int | None
    manual_override: int | None
    is_paused: bool
    unavailable_from: datetime | None
    unavailable_until: datetime | None
    max_weekly_output: int | None
    committed_ends: list[datetime] = field(default_factory=list)  # occupying jobs' ends
    as_of: datetime | None = None         # when slots were derived from the snapshot
    resource_id: int | None = None        # ProductionResource pk (None = not derived yet)

    @property
    def used(self) -> int:
        return len(self.committed_ends)

    @property
    def remaining(self) -> int | None:
        """Free slots RIGHT NOW: ``max(0, slots − used)`` per pilot, never negative
        (over-commit clamps to 0, and never subsidises another pilot's free slots)."""
        if self.effective_slots is None:
            return None
        return max(0, self.effective_slots - self.used)

    @property
    def measured(self) -> bool:
        return self.effective_slots is not None and self.effective_slots > 0


@dataclass
class CapacityState:
    """Everything the scheduler and the board read from one derivation."""

    pools_by_class: dict[str, list[PilotPool]]
    unmeasured_jobs: int                  # jobs by pilots whose capacity is unknown
    unmatched_board_jobs: int             # BUILDING/BUILT board jobs not seen in ESI
    corp_jobs_as_of: datetime | None
    char_jobs_as_of: datetime | None

    def pools(self, activity_class: str) -> list[PilotPool]:
        return self.pools_by_class.get(activity_class, [])

    def theoretical(self, activity_class: str) -> int:
        return sum(p.effective_slots or 0 for p in self.pools(activity_class))

    def committed(self, activity_class: str) -> int:
        """Committed slots among MEASURED pilots (unknown-slot pilots' load is
        shown in their own row but not summed into a measured total)."""
        return sum(p.used for p in self.pools(activity_class) if p.measured)

    def remaining_total(self, activity_class: str) -> int:
        return sum((p.remaining or 0) for p in self.pools(activity_class) if p.measured)


def _committed_load(consenting_ids: set[int]):
    """Deduped occupying jobs → ``{(char_id, class): [end_date, …]}`` plus the
    unmeasured job count. Read ONCE (snapshot-replace tables must not be re-read).

    Dedup by ``job_id`` across both tables, corp row winning for metadata; the
    consent gate is applied at read time so a lingering ``CharacterIndustryJob``
    from a revoked pilot lands in the unmeasured bucket, never a named pool.
    """
    from apps.erp.models import CharacterIndustryJob, CorpIndustryJob

    jobs: dict[int, tuple[int, int, datetime | None]] = {}
    corp_as_of: datetime | None = None
    char_as_of: datetime | None = None
    for job_id, installer_id, activity_id, end_date, updated in CorpIndustryJob.objects.filter(
        status__in=_OCCUPYING_JOB_STATUSES
    ).values_list("job_id", "installer_id", "activity_id", "end_date", "updated_at"):
        jobs[job_id] = (installer_id, activity_id, end_date)
        if corp_as_of is None or (updated and updated > corp_as_of):
            corp_as_of = updated
    for job_id, char_id, activity_id, end_date, updated in CharacterIndustryJob.objects.filter(
        status__in=_OCCUPYING_JOB_STATUSES
    ).values_list("job_id", "character_id", "activity_id", "end_date", "updated_at"):
        jobs.setdefault(job_id, (char_id, activity_id, end_date))
        if char_as_of is None or (updated and updated > char_as_of):
            char_as_of = updated

    by_pilot_class: dict[tuple[int, str], list[datetime | None]] = defaultdict(list)
    unmeasured = 0
    for installer_id, activity_id, end_date in jobs.values():
        cls = _ACTIVITY_ID_TO_CLASS.get(activity_id)
        if cls is None:
            continue
        if installer_id in consenting_ids:
            by_pilot_class[(installer_id, cls)].append(end_date)
        else:
            unmeasured += 1
    return by_pilot_class, unmeasured, corp_as_of, char_as_of


def _unmatched_board_jobs() -> int:
    """BUILDING/BUILT board jobs with no ESI row yet (linked or heuristic-matched)
    — the data-quality line, never counted as load (their reality is the ESI row,
    landing within the 3 h corp-job cadence)."""
    from apps.erp.models import BuildJob
    from apps.erp.services import suggest_esi_matches

    jobs = list(
        BuildJob.objects.filter(
            status__in=(BuildJob.Status.BUILDING, BuildJob.Status.BUILT)
        ).select_related("owner")
    )
    linked = {j.pk for j in jobs if j.esi_job_id}
    heuristic = set(suggest_esi_matches([j for j in jobs if not j.esi_job_id]))
    matched = linked | heuristic
    return sum(1 for j in jobs if j.pk not in matched)


def capacity_state(config) -> CapacityState:
    """THE capacity read: measured pools with committed load, the unmeasured
    aggregate and the board-not-in-ESI data-quality count. Pure/read-only — safe
    for the leadership GET and reused by the scheduler."""
    from apps.sso.models import EveCharacter

    consenting = _consenting_character_ids()
    by_pc, unmeasured, corp_as_of, char_as_of = _committed_load(consenting)
    names = {
        c.character_id: (c.name or str(c.character_id))
        for c in EveCharacter.objects.filter(character_id__in=consenting)
    }
    resources = {
        (r.character_id, r.activity_class): r
        for r in ProductionResource.objects.filter(character_id__in=consenting)
    }

    pools_by_class: dict[str, list[PilotPool]] = {cls: [] for cls in ACTIVITY_CLASSES}
    seen: set[tuple[int, str]] = set()
    for (cid, cls), resource in sorted(resources.items()):
        pools_by_class.setdefault(cls, []).append(PilotPool(
            character_id=cid, name=names.get(cid, str(cid)), activity_class=cls,
            effective_slots=resource.effective_slots, slots_total=resource.slots_total,
            manual_override=resource.manual_slots_override, is_paused=resource.is_paused,
            unavailable_from=resource.unavailable_from,
            unavailable_until=resource.unavailable_until,
            max_weekly_output=resource.max_weekly_output,
            committed_ends=list(by_pc.get((cid, cls), [])),
            as_of=resource.as_of, resource_id=resource.pk,
        ))
        seen.add((cid, cls))
    # A consenting pilot with committed load but no derived row yet (derive hasn't
    # run since they consented) still shows their load — slots unknown, never dropped.
    for (cid, cls), ends in sorted(by_pc.items()):
        if (cid, cls) in seen:
            continue
        pools_by_class.setdefault(cls, []).append(PilotPool(
            character_id=cid, name=names.get(cid, str(cid)), activity_class=cls,
            effective_slots=None, slots_total=None, manual_override=None,
            is_paused=False, unavailable_from=None, unavailable_until=None,
            max_weekly_output=None, committed_ends=list(ends), as_of=None,
        ))

    for cls in pools_by_class:
        pools_by_class[cls].sort(key=lambda p: p.character_id)
    # Unmeasured = jobs by non-consenting installers (already counted) PLUS the load
    # of consenting pilots whose slots are *unknown* (no/stale snapshot, or consented
    # but not yet derived). Their capacity is unknown, so their jobs belong on the
    # unmeasured line too — otherwise they'd vanish from BOTH roll-ups (§2.6), leaving
    # ``committed`` + ``unmeasured`` under-counting real load.
    unmeasured_total = unmeasured + sum(
        p.used for pools in pools_by_class.values() for p in pools if not p.measured
    )
    return CapacityState(
        pools_by_class=pools_by_class, unmeasured_jobs=unmeasured_total,
        unmatched_board_jobs=_unmatched_board_jobs(),
        corp_jobs_as_of=corp_as_of, char_jobs_as_of=char_as_of,
    )


# --------------------------------------------------------------------------- #
#  The finite-capacity scheduling pass
# --------------------------------------------------------------------------- #
@dataclass
class _Sched:
    """A bookable pilot's mutable scheduling state for one activity class."""

    pool: PilotPool
    snapshot: object | None
    slot_free: list[datetime]                       # one free-time per slot
    weekly: dict[date, int] = field(default_factory=dict)


@dataclass
class _Booking:
    """A booked work item — the end day it frees its slot, and whether contention
    (start pushed past ``day(now)``) delayed it (for slots-bottleneck attribution)."""

    start: datetime
    end: datetime
    character_id: int
    delayed: bool
    slot_free_pick: datetime            # the chosen slot's free-time before booking
    cap_delayed: bool = False           # the weekly cap pushed the start to a later week


def _initial_slot_free(pool: PilotPool, now_day: datetime) -> list[datetime]:
    """The free-times of a pilot's slots, seeded from committed load. The S
    latest-ending occupying jobs each hold a slot until their end (past ends clamp
    to now — the job should have finished); any remaining slots are free now."""
    slots = pool.effective_slots or 0
    ends = sorted(
        (max(now_day, _day(e)) for e in pool.committed_ends if e is not None),
        reverse=True,
    )
    frees = [ends[i] if i < len(ends) else now_day for i in range(slots)]
    return sorted(frees)


def _window_ok(pool: PilotPool, t: datetime) -> datetime | None:
    """Push ``t`` out of the pilot's one maintenance window; ``None`` if the pilot
    is unavailable indefinitely at ``t`` (open-ended window with no end)."""
    lo, hi = pool.unavailable_from, pool.unavailable_until
    if lo is None and hi is None:
        return t
    if lo is not None and hi is not None:
        return _day(hi) if _day(lo) <= t < _day(hi) else t
    if lo is not None:                       # open-ended: blocked forever from lo
        return None if t >= _day(lo) else t
    return _day(hi) if t < _day(hi) else t   # only an end: blocked until hi


def _resolve_start(sched: _Sched, base: datetime, units: int) -> datetime | None:
    """Earliest start ≥ ``base`` this pilot can take ``units`` at, honouring the
    maintenance window and the weekly cap. ``None`` if never (indefinite window).

    Weekly cap (§3.2.1): units count against the START week only; a week already
    carrying load rejects a booking that would exceed the cap (push to next week),
    but a **zero-consumption** week accepts any size — so a booking larger than the
    cap still lands, and the earliest-week search always terminates.
    """
    cap = sched.pool.max_weekly_output
    t = _window_ok(sched.pool, base)
    if t is None:
        return None
    if cap is None:
        return t
    for _guard in range(600):               # bounded: a future empty week always accepts
        wk = _week_start(t.date())
        consumed = sched.weekly.get(wk, 0)
        if consumed == 0 or consumed + units <= cap:
            return t
        nxt = timezone.make_aware(
            datetime.combine(wk + timedelta(days=7), dt_time.min)
        )
        t = _window_ok(sched.pool, nxt)
        if t is None:
            return None
    return None


def _book(scheds: list[_Sched], units: int, duration_seconds: int | None,
          earliest_start: datetime, now_day: datetime,
          prefer_ids: set[int] | None = None) -> _Booking | None:
    """Book ``units`` (taking ``duration_seconds``) onto the eligible pilot+slot
    with the soonest start ≥ ``earliest_start``. Deterministic: pilots by
    ``character_id``, slots by free-time. Advances the chosen slot and the pilot's
    weekly tally. ``prefer_ids`` (a claimed job's owner) is tried first, then all.
    """
    duration = timedelta(seconds=max(0, int(duration_seconds or 0)))
    for candidate_ids in (prefer_ids, None):
        if candidate_ids is not None and not candidate_ids:
            continue
        best: tuple[datetime, int, int] | None = None   # (start, char_id, slot_idx)
        for sched in scheds:
            if candidate_ids is not None and sched.pool.character_id not in candidate_ids:
                continue
            if not sched.slot_free:
                continue
            slot_idx = min(range(len(sched.slot_free)), key=lambda i: sched.slot_free[i])
            base = max(sched.slot_free[slot_idx], earliest_start, now_day)
            start = _resolve_start(sched, base, units)
            if start is None:
                continue
            key = (start, sched.pool.character_id, slot_idx)
            if best is None or key < best:
                best = key
        if best is None:
            continue
        start, cid, slot_idx = best
        sched = next(s for s in scheds if s.pool.character_id == cid)
        slot_free_pick = sched.slot_free[slot_idx]
        end = _day(start + duration)
        sched.slot_free[slot_idx] = end
        wk = _week_start(start.date())
        sched.weekly[wk] = sched.weekly.get(wk, 0) + units
        # The weekly cap delayed the start iff it landed in a later week than the
        # slot/earliest-start would otherwise allow.
        base_week = _week_start(max(slot_free_pick, earliest_start, now_day).date())
        cap_delayed = wk > base_week
        return _Booking(start=start, end=end, character_id=cid,
                        delayed=start > now_day, slot_free_pick=slot_free_pick,
                        cap_delayed=cap_delayed)
    return None


def _planned_vehicles(matched_pks: set[int]):
    """Internal build vehicles the scheduler must book — promised-but-unstarted
    work that will need real slots. QUEUED/BLOCKED ``BuildJob``s minus ESI-matched,
    plus project BUILD items without a non-cancelled job. BUILDING/BUILT jobs are
    NEVER here (their reality is the ESI row / already done).

    Yields dicts ``{kind, id, type_id, quantity, owner_id, project_id}``, ordered
    deterministically by (kind, id).
    """
    from apps.erp.models import BuildJob
    from apps.industry.models import IndustryProject, IndustryProjectItem

    out: list[dict] = []
    for job in BuildJob.objects.filter(
        status__in=(BuildJob.Status.QUEUED, BuildJob.Status.BLOCKED)
    ).select_related("owner"):
        if job.pk in matched_pks:
            continue
        out.append({
            "kind": "build_job", "id": job.pk, "type_id": job.output_type_id,
            "quantity": int(job.quantity), "owner_id": job.owner_id, "project_id": None,
        })
    items_with_jobs = set(
        BuildJob.objects.filter(source_item__isnull=False)
        .exclude(status="cancelled").values_list("source_item_id", flat=True)
    )
    for item in IndustryProjectItem.objects.filter(
        project__is_archived=False,
        project__status__in=(IndustryProject.Status.ACTIVE, IndustryProject.Status.BLOCKED),
        build_or_buy=IndustryProjectItem.BuildOrBuy.BUILD,
    ).select_related("project"):
        if item.pk in items_with_jobs:
            continue
        out.append({
            "kind": "project_item", "id": item.pk, "type_id": item.type_id,
            "quantity": int(item.quantity), "owner_id": None, "project_id": item.project_id,
        })
    out.sort(key=lambda v: (v["kind"], v["id"]))
    return out


def _duration_seconds(type_id: int, units: int, recipe) -> int | None:
    """Whole-run duration to produce ``units`` of a type, from the single duration
    source (``calc``). Vehicle bookings occupy exactly the time their OWN quantity
    needs — never ``durations[key]``, whose ``net or gross`` cache is wrong for any
    other quantity."""
    import math

    from apps.industry import calc

    runs = math.ceil(max(1, units) / max(1, recipe.output_quantity))
    if recipe.activity == "reaction":
        return calc.reaction_seconds(type_id, runs)
    return calc.production_seconds(type_id, runs, te=0)


def _char_ids_for_user(user_id: int) -> set[int]:
    from apps.sso.models import EveCharacter

    return set(
        EveCharacter.objects.filter(user_id=user_id).values_list("character_id", flat=True)
    )


def _usable_blueprint_types(bp_type_ids: set[int], consenting_ids: set[int]) -> set[int]:
    """Blueprint type ids for which a USABLE print (BPO, or BPC with runs left) is
    owned by the corp or a consenting pilot — the blueprint gate, in one query."""
    from django.db.models import Q

    from apps.erp.models import Blueprint

    if not bp_type_ids:
        return set()
    rows = Blueprint.objects.filter(type_id__in=bp_type_ids).filter(
        Q(quantity=-1) | ~Q(runs=0)      # is_usable: original, or a copy with runs
    ).filter(
        Q(owner_type=Blueprint.Owner.CORPORATION)
        | Q(owner_type=Blueprint.Owner.CHARACTER, owner_id__in=consenting_ids)
    ).values_list("type_id", flat=True)
    return set(rows)


def _structures_for_locations(location_ids: set[int]) -> dict[int, object]:
    """``location_id → CorpStructure`` for the build rows' locations, resolved via
    ``MarketLocation.structure_id`` (NPC stations / SYSTEM rows resolve to nothing,
    which yields no facility verdict — never a guess)."""
    from apps.corporation.models import CorpStructure
    from apps.market.models import MarketLocation

    if not location_ids:
        return {}
    loc_struct = dict(
        MarketLocation.objects.filter(pk__in=location_ids, structure_id__isnull=False)
        .values_list("pk", "structure_id")
    )
    structs = {
        s.structure_id: s
        for s in CorpStructure.objects.filter(structure_id__in=set(loc_struct.values()))
    }
    return {loc_id: structs[sid] for loc_id, sid in loc_struct.items() if sid in structs}


class CapacityScheduler:
    """The stateful finite-capacity pass, driven ROW-BY-ROW by
    :func:`mrp._apply_feasible_dates`'s single deepest-first sweep.

    Vehicle load is booked once at construction; each :meth:`schedule_row` call
    then books a row's residual net onto the shared, mutating pools — so a parent
    reads its children's already-written dates the same way the P3 pass does
    (including *buy/import* children the capacity module itself never sees). Never
    a batch pre-pass: that could not see a leaf's lead-time date.

    Determinism: pilots by ``character_id``, slots by free-time, vehicles by
    (kind, id). A re-run on frozen inputs reproduces every date exactly.
    """

    def __init__(self, config, now, row_by_key):
        from apps.erp.models import Blueprint as _Blueprint
        from apps.erp.models import BuildJob
        from apps.erp.services import suggest_esi_matches

        self.now_day = _day(now)
        self.state = capacity_state(config)
        self.consenting = _consenting_character_ids()
        snaps = _latest_snapshots(self.consenting)

        # Bookable pilots per class (slots known & > 0). Unknown-slot pilots are
        # never scheduled onto — unknown is not zero, not a slot we may promise.
        self.scheds_by_class: dict[str, list[_Sched]] = {}
        for cls, pools in self.state.pools_by_class.items():
            self.scheds_by_class[cls] = [
                _Sched(pool=p, snapshot=snaps.get(p.character_id),
                       slot_free=_initial_slot_free(p, self.now_day))
                for p in pools if p.measured
            ]

        # ---- Internal-vehicle bookings (booked FIRST; they hold slots too) ------
        live_jobs = list(
            BuildJob.objects.filter(
                status__in=(BuildJob.Status.QUEUED, BuildJob.Status.BLOCKED)
            )
        )
        linked = {j.pk for j in live_jobs if j.esi_job_id}
        matched_pks = linked | set(suggest_esi_matches(live_jobs))  # QUEUED/BLOCKED never heuristic-match
        self.own_build_end: dict[int, _Booking] = {}                # build_job pk → booking
        self.own_project_end: dict[tuple[int, int], _Booking] = {}  # (project_id, type_id) → latest
        for veh in _planned_vehicles(matched_pks):
            recipe = buildable_recipe(veh["type_id"])
            if recipe is None:
                continue
            cls = _RECIPE_ACTIVITY_TO_CLASS.get(recipe.activity)
            if cls is None:
                continue
            scheds = [s for s in self.scheds_by_class.get(cls, []) if not s.pool.is_paused]
            if not scheds:
                continue
            duration = _duration_seconds(veh["type_id"], veh["quantity"], recipe)
            prefer = _char_ids_for_user(veh["owner_id"]) if veh["owner_id"] else None
            booking = _book(scheds, veh["quantity"], duration, self.now_day, self.now_day,
                            prefer_ids=prefer)
            if booking is None:
                continue
            if veh["kind"] == "build_job":
                self.own_build_end[veh["id"]] = booking
            else:
                pkey = (veh["project_id"], veh["type_id"])
                prev = self.own_project_end.get(pkey)
                if prev is None or booking.end > prev.end:
                    self.own_project_end[pkey] = booking

        # ---- Row gates (blueprint, facility), precomputed in bulk ---------------
        build_rows = [
            (key, row) for key, row in row_by_key.items()
            if row.suggestion == "build" and int(row.net_quantity) > 0
        ]
        self.recipes = {row.type_id: buildable_recipe(row.type_id) for _k, row in build_rows}
        bp_type_ids = {r.blueprint_type_id for r in self.recipes.values() if r is not None}
        # Honest-data: the blueprint gate is active only when blueprint data has
        # been imported at all. Zero Blueprint rows corp-wide means *unknown*
        # ownership (no corp_industry grant), never "no print" — refusing every
        # build then would be the unknown-as-zero mistake §12 forbids.
        self.blueprint_data_present = _Blueprint.objects.exists()
        self.usable_bps = _usable_blueprint_types(bp_type_ids, self.consenting)
        loc_ids = {row.location_id for _k, row in build_rows if row.location_id}
        self.structures = _structures_for_locations(loc_ids)

    def owns_row(self, row) -> bool:
        """Whether the capacity pass sets this row's date: a build row (net > 0), or a
        COVERED (net 0) row whose own QUEUED/BLOCKED vehicle holds a slot. A net>0
        buy/import row keeps the P3 lead-time date even if a stale vehicle link lingers
        (reconcile releases it later) — the capacity pass never owns it."""
        if row.suggestion == "build" and int(row.net_quantity) > 0:
            return True
        return (
            int(row.net_quantity) <= 0
            and _row_own_vehicle(row, self.own_build_end, self.own_project_end) is not None
        )

    def schedule_row(self, row, child_max, duration, child_refused=False):
        """``(feasible_at, "capacity", bottleneck_code)`` for one capacity-owned row.
        ``child_max`` is the row's children's latest date (already written this sweep);
        ``duration`` is the run's cached residual duration; ``child_refused`` is True
        when a required component has no honest date (refused this sweep)."""
        is_build = row.suggestion == "build" and int(row.net_quantity) > 0
        own = _row_own_vehicle(row, self.own_build_end, self.own_project_end)

        # A net-0 row covered by its own QUEUED/BLOCKED vehicle: its date is that
        # vehicle's booking end; slots only if contention delayed the vehicle.
        if not is_build:
            if own is None:
                return (None, "capacity", "")
            ends = [own.end] + ([child_max] if child_max is not None else [])
            feasible = _day(max(ends))
            if child_max is not None and child_max > own.end:
                return (feasible, "capacity", "materials")
            return (feasible, "capacity", "slots" if own.delayed else "")

        # A component was refused (no honest date), so the assembly consuming it cannot
        # be honestly promised either — refuse rather than fabricate (§2.4). Nor can we
        # schedule a build whose duration is unknown (missing SDE time) — P3 leaves that
        # date None too, so armed capacity must not fabricate an optimistic same-day one.
        if child_refused:
            return (None, "capacity", "materials")
        if duration is None:
            return (None, "capacity", "")

        recipe = self.recipes.get(row.type_id) or buildable_recipe(row.type_id)
        cls = _RECIPE_ACTIVITY_TO_CLASS.get(recipe.activity) if recipe else None

        # Earliest start = latest of now, children's dates (materials) and a
        # reinforced structure timer (facility). Track which raised it.
        earliest = self.now_day
        start_driver = ""
        if child_max is not None and child_max > earliest:
            earliest, start_driver = child_max, "materials"

        struct = self.structures.get(row.location_id) if row.location_id else None
        if struct is not None:
            if struct.is_out_of_fuel:
                return (None, "capacity", "facility")
            if struct.is_reinforced and struct.state_timer_end:
                facility_floor = _day(struct.state_timer_end)
                if facility_floor > earliest:
                    earliest, start_driver = facility_floor, "facility"
                elif facility_floor == earliest and facility_floor > self.now_day:
                    start_driver = "facility"   # a structure block is the harder reason on a tie

        if (
            self.blueprint_data_present and recipe is not None
            and recipe.blueprint_type_id not in self.usable_bps
        ):
            return (None, "capacity", "blueprint")

        if cls is None:
            return (None, "capacity", "unmeasured")
        measured = self.scheds_by_class.get(cls, [])
        if not measured:
            return (None, "capacity", "unmeasured")
        skills_ok = [
            s for s in measured
            if _skills_verdict(s.snapshot, row.type_id, recipe.activity) is not False
        ]
        if not skills_ok:
            return (None, "capacity", "skills")
        available = [s for s in skills_ok if not s.pool.is_paused]
        if not available:
            return (None, "capacity", "slots")

        booking = _book(available, int(row.net_quantity), duration, earliest, self.now_day)
        if booking is None:
            # Every measured pilot is window-blocked indefinitely — capacity withdrawn.
            return (None, "capacity", "slots")

        final_end = booking.end
        if own is not None and own.end > final_end:
            final_end = own.end
        feasible = _day(final_end)

        # Attribution — the reason the date is later than building alone would allow:
        #   a busy slot or the weekly cap pushed the start → slots
        #   a reinforced facility raised the start         → facility
        #   the children's dates raised the start          → materials
        #   the row's own vehicle booking contended        → slots
        #   nothing bound it (== unconstrained)            → ""
        if booking.slot_free_pick > earliest or booking.cap_delayed:
            bottleneck = "slots"
        elif start_driver:
            bottleneck = start_driver
        elif own is not None and own.end > booking.end and own.delayed:
            bottleneck = "slots"
        else:
            bottleneck = ""
        return (feasible, "capacity", bottleneck)


def _row_own_vehicle(row, own_build_end, own_project_end) -> _Booking | None:
    """The row's OWN QUEUED/BLOCKED vehicle booking, if any (the ``_own_vehicle_output``
    linkage restricted to booked, unstarted vehicles)."""
    candidates = []
    if row.build_job_id and row.build_job_id in own_build_end:
        candidates.append(own_build_end[row.build_job_id])
    if row.industry_project_id:
        booking = own_project_end.get((row.industry_project_id, row.type_id))
        if booking is not None:
            candidates.append(booking)
    if not candidates:
        return None
    return max(candidates, key=lambda b: b.end)


def _skills_verdict(snapshot, type_id: int, activity: str):
    """``can_manufacture`` with the recipe's activity passed EXPLICITLY (the default
    is manufacturing, so a reaction row checked with the default would read
    manufacturing skills, find no requirement and never gate). ``None`` never gates."""
    from apps.industry import capability

    return capability.can_manufacture(snapshot, type_id, activity=activity)


# --------------------------------------------------------------------------- #
#  Board helpers (leadership view + member strip) — read-only over the authority
# --------------------------------------------------------------------------- #
def committed_by_location() -> list[dict]:
    """Deduped occupying jobs grouped by resolved location — where the corp's
    production actually sits. Location/facility ids resolve to names via the
    AssetLocation cache (an unreadable structure keeps its id)."""
    from apps.erp.models import CharacterIndustryJob, CorpIndustryJob
    from apps.stockpile.models import AssetLocation

    jobs: dict[int, int | None] = {}
    for job_id, loc in CorpIndustryJob.objects.filter(
        status__in=_OCCUPYING_JOB_STATUSES
    ).values_list("job_id", "location_id"):
        jobs[job_id] = loc
    for job_id, loc in CharacterIndustryJob.objects.filter(
        status__in=_OCCUPYING_JOB_STATUSES
    ).values_list("job_id", "location_id"):
        jobs.setdefault(job_id, loc)

    counts: dict[int | None, int] = defaultdict(int)
    for loc in jobs.values():
        counts[loc] += 1
    names = dict(
        AssetLocation.objects.filter(
            location_id__in=[lid for lid in counts if lid is not None]
        ).values_list("location_id", "name")
    )
    out = []
    for loc, n in counts.items():
        if loc is None:
            label = _("Unknown location")
        else:
            label = names.get(loc) or _("Location %(id)s") % {"id": loc}
        out.append({"location_id": loc, "name": label, "jobs": n})
    out.sort(key=lambda d: (-d["jobs"], str(d["name"])))
    return out


def blocked_requirements() -> list[dict]:
    """Live requirements carrying a bottleneck, grouped by code — the board's
    blocked-work panel. Each row deep-links to its Material Plan entry."""
    from apps.industry.models import NetRequirement
    from apps.sde.models import SdeType

    rows = list(
        NetRequirement.objects.filter(
            status__in=(NetRequirement.Status.OPEN, NetRequirement.Status.IN_PROGRESS)
        ).exclude(bottleneck_code="").order_by("bottleneck_code", "type_id")
    )
    names = dict(
        SdeType.objects.filter(type_id__in={r.type_id for r in rows})
        .values_list("type_id", "name")
    )
    grouped: dict[str, list] = {}
    for r in rows:
        grouped.setdefault(r.bottleneck_code, []).append(
            {"row": r, "name": names.get(r.type_id, str(r.type_id))}
        )
    return [
        {"code": code, "label": bottleneck_label(code), "rows": items}
        for code, items in sorted(grouped.items())
    ]


def my_capacity(user, config) -> list[dict]:
    """The viewing pilot's OWN pools (slots/used/free/next-free per class) — the job
    tracker's member strip. Restricted to the user's own consenting characters; a
    second pilot's capacity never leaks in. Empty when none of them consented."""
    char_ids = set(user.characters.values_list("character_id", flat=True)) if user else set()
    if not char_ids:
        return []
    state = capacity_state(config)
    now_day = _day(timezone.now())
    out = []
    for cls in ACTIVITY_CLASSES:
        for pool in state.pools(cls):
            if pool.character_id not in char_ids:
                continue
            frees = _initial_slot_free(pool, now_day)
            out.append({
                "character_id": pool.character_id, "name": pool.name,
                "activity_class": cls, "activity_label": _activity_label(cls),
                "slots": pool.effective_slots, "used": pool.used,
                "remaining": pool.remaining,
                "next_free_at": min(frees) if frees else None,
                "as_of": pool.as_of,
            })
    return out


def _activity_label(cls: str):
    """The translated label for an activity-class code (choices-driven)."""
    return dict(ProductionResource.ActivityClass.choices).get(cls, cls)
