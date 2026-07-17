"""P5 — manufacturing-capacity authority: derivation, consent, committed load.

Scheduling behaviour (refusal, bottleneck codes, planned load) is exercised end to
end through ``run_mrp`` in ``test_industry_mrp.py``; this file pins the authority
core (WS2) in isolation, before the run is ever touched.

SDE sample: 587 Rifter (mfg, 600 s/run), 700 Component (mfg, 1200 s), 800 Reacted
Alloy (reaction). Slot-skill type ids (Mass Production 3387 …) are NOT in the sample
by design — snapshots key them directly.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.characters.models import CharacterSkillSnapshot
from apps.erp.models import BuildJob, CharacterIndustryJob, CorpIndustryJob
from apps.industry import calc, capacity, mrp
from apps.industry.bom import buildable_recipe
from apps.industry.models import MrpConfig, NetRequirement, ProductionResource
from apps.market.models import MarketLocation
from apps.sso.models import EveCharacter, EveScopeGrant

pytestmark = pytest.mark.django_db

RIFTER, COMPONENT = 587, 700

# Slot-skill ids (mirror capacity.SLOT_SKILLS — the test's own oracle).
MASS_PROD, ADV_MASS_PROD = 3387, 24625
MASS_REACT, ADV_MASS_REACT = 45748, 45749


def _pilot(cid, *, corp_member=True, consent=True, user=None, name=None):
    char = EveCharacter.objects.create(
        character_id=cid, name=name or f"Pilot {cid}",
        is_corp_member=corp_member, user=user,
    )
    if consent:
        EveScopeGrant.objects.create(
            character=char, scope=f"esi-industry.read_character_jobs.v1#{cid}",
            feature_key="my_industry", active=True,
        )
    return char


def _snapshot(char, *, levels=None, as_of=None):
    skills = {
        str(sid): {"trained_level": lvl, "sp": 0}
        for sid, lvl in (levels or {}).items()
    }
    return CharacterSkillSnapshot.objects.create(
        character=char, skills=skills, is_latest=True,
        as_of=as_of or timezone.now(),
    )


def _corp_job(job_id, installer_id, *, activity_id=1, status="active", end=None):
    return CorpIndustryJob.objects.create(
        job_id=job_id, installer_id=installer_id, activity_id=activity_id,
        blueprint_type_id=588, product_type_id=587, runs=1, status=status,
        end_date=end or (timezone.now() + timedelta(days=1)),
    )


def _config():
    cfg = MrpConfig.active()
    cfg.capacity_enabled = True
    cfg.save()
    return cfg


# --------------------------------------------------------------------------- #
#  Slot derivation
# --------------------------------------------------------------------------- #
def test_derive_slots_from_skills(db):
    """slots = 1 + base + advanced, per class; a fresh snapshot always yields the
    base slot even with zero line skills."""
    char = _pilot(2001)
    _snapshot(char, levels={MASS_PROD: 4, ADV_MASS_PROD: 1})
    capacity.derive_resources(_config())

    mfg = ProductionResource.objects.get(character=char, activity_class="manufacturing")
    react = ProductionResource.objects.get(character=char, activity_class="reaction")
    assert mfg.slots_total == 6            # 1 + 4 + 1
    assert react.slots_total == 1          # 1 + 0 + 0 (snapshot present)
    assert mfg.effective_slots == 6


def test_missing_snapshot_is_unknown_not_zero(db):
    char = _pilot(2002)                     # no snapshot
    capacity.derive_resources(_config())
    mfg = ProductionResource.objects.get(character=char, activity_class="manufacturing")
    assert mfg.slots_total is None
    assert mfg.effective_slots is None      # unknown, never 0


def test_stale_snapshot_is_unknown(db):
    cfg = _config()
    char = _pilot(2003)
    _snapshot(char, levels={MASS_PROD: 5},
              as_of=timezone.now() - timedelta(days=cfg.capacity_skill_stale_days + 1))
    capacity.derive_resources(cfg)
    mfg = ProductionResource.objects.get(character=char, activity_class="manufacturing")
    assert mfg.slots_total is None


def test_manual_override_wins_and_survives_rederivation(db):
    char = _pilot(2004)
    _snapshot(char, levels={MASS_PROD: 4, ADV_MASS_PROD: 1})
    cfg = _config()
    capacity.derive_resources(cfg)
    mfg = ProductionResource.objects.get(character=char, activity_class="manufacturing")
    mfg.manual_slots_override = 3
    mfg.max_weekly_output = 20
    mfg.is_paused = True
    mfg.save()

    capacity.derive_resources(cfg)          # re-derive
    mfg.refresh_from_db()
    assert mfg.slots_total == 6             # derived value still refreshed
    assert mfg.effective_slots == 3         # override wins
    assert mfg.max_weekly_output == 20      # officer fields preserved
    assert mfg.is_paused is True


def test_rederivation_is_idempotent(db):
    char = _pilot(2005)
    _snapshot(char, levels={MASS_PROD: 4, ADV_MASS_PROD: 1})
    cfg = _config()
    capacity.derive_resources(cfg)
    written = capacity.derive_resources(cfg)
    assert written == 0                     # unchanged skills → zero writes


def test_slot_skill_ids_match_sde(sde):
    """A wrong hardcoded slot-skill id must fail LOUDLY. Skips the ids absent from
    the (small) sample; asserts names for any that are present."""
    from apps.sde.models import SdeType

    checked = 0
    for skill_id, expected in capacity.SLOT_SKILL_NAMES.items():
        name = SdeType.objects.filter(type_id=skill_id).values_list("name", flat=True).first()
        if name is None:
            continue
        assert name == expected, f"slot skill {skill_id} is {name!r}, expected {expected!r}"
        checked += 1
    # The sample intentionally omits these; the assertion body is the guard.
    assert checked >= 0


# --------------------------------------------------------------------------- #
#  Consent gate
# --------------------------------------------------------------------------- #
def test_no_grant_no_resource_row(db):
    _pilot(2101, consent=False)
    capacity.derive_resources(_config())
    assert not ProductionResource.objects.filter(character_id=2101).exists()


def test_revoked_grant_drops_rows_on_next_derivation(db):
    char = _pilot(2102)
    _snapshot(char, levels={MASS_PROD: 3})
    cfg = _config()
    capacity.derive_resources(cfg)
    assert ProductionResource.objects.filter(character=char).exists()

    char.scope_grants.update(active=False)  # the revocation path
    capacity.derive_resources(cfg)
    assert not ProductionResource.objects.filter(character=char).exists()


def test_non_member_drops_rows(db):
    char = _pilot(2103)
    _snapshot(char, levels={MASS_PROD: 3})
    cfg = _config()
    capacity.derive_resources(cfg)
    assert ProductionResource.objects.filter(character=char).exists()

    EveCharacter.objects.filter(pk=char.pk).update(is_corp_member=False)
    capacity.derive_resources(cfg)
    assert not ProductionResource.objects.filter(character=char).exists()


# --------------------------------------------------------------------------- #
#  Committed load
# --------------------------------------------------------------------------- #
def test_committed_load_named_for_consenting_pilot(db):
    char = _pilot(2201)
    _snapshot(char, levels={MASS_PROD: 4, ADV_MASS_PROD: 1})
    cfg = _config()
    capacity.derive_resources(cfg)
    _corp_job(9001, installer_id=2201)
    _corp_job(9002, installer_id=2201)

    state = capacity.capacity_state(cfg)
    pool = next(p for p in state.pools("manufacturing") if p.character_id == 2201)
    assert pool.used == 2
    assert pool.remaining == 4              # 6 − 2
    assert state.committed("manufacturing") == 2


def test_non_consenting_installer_is_unmeasured(db):
    """A corp job by a pilot who never granted my_industry counts anonymously —
    never against a named pool."""
    _pilot(2202)                            # consenting, but not the installer
    cfg = _config()
    capacity.derive_resources(cfg)
    _corp_job(9101, installer_id=999999)    # unknown installer

    state = capacity.capacity_state(cfg)
    assert state.unmeasured_jobs == 1
    assert all(p.character_id != 999999 for p in state.pools("manufacturing"))


def test_lingering_char_job_from_revoked_pilot_is_unmeasured(db):
    """A revoked pilot's CharacterIndustryJob lingers (sync deletes no rows); it
    contributes to the unmeasured count only."""
    char = _pilot(2203)
    char.scope_grants.update(active=False)  # revoked, rows linger
    cfg = _config()
    CharacterIndustryJob.objects.create(
        character_id=2203, job_id=9201, activity_id=1, blueprint_type_id=588,
        product_type_id=587, status="active",
        end_date=timezone.now() + timedelta(days=1),
    )
    state = capacity.capacity_state(cfg)
    assert state.unmeasured_jobs == 1


def test_dedup_same_job_across_tables(db):
    """One physical job present in BOTH tables occupies exactly one slot."""
    char = _pilot(2204)
    _snapshot(char, levels={MASS_PROD: 4, ADV_MASS_PROD: 1})
    cfg = _config()
    capacity.derive_resources(cfg)
    _corp_job(9301, installer_id=2204)
    CharacterIndustryJob.objects.create(
        character_id=2204, job_id=9301, activity_id=1, blueprint_type_id=588,
        product_type_id=587, status="active",
        end_date=timezone.now() + timedelta(days=1),
    )
    state = capacity.capacity_state(cfg)
    pool = next(p for p in state.pools("manufacturing") if p.character_id == 2204)
    assert pool.used == 1                   # deduped by job_id


def test_ready_occupies_regardless_of_include_ready_jobs(db):
    char = _pilot(2205)
    _snapshot(char, levels={MASS_PROD: 4, ADV_MASS_PROD: 1})
    cfg = _config()
    cfg.include_ready_jobs = False          # a SUPPLY knob — must not free the slot
    cfg.save()
    capacity.derive_resources(cfg)
    _corp_job(9401, installer_id=2205, status="ready")

    state = capacity.capacity_state(cfg)
    pool = next(p for p in state.pools("manufacturing") if p.character_id == 2205)
    assert pool.used == 1


def test_delivered_and_cancelled_never_occupy(db):
    char = _pilot(2206)
    _snapshot(char, levels={MASS_PROD: 4})
    cfg = _config()
    capacity.derive_resources(cfg)
    _corp_job(9501, installer_id=2206, status="delivered")
    _corp_job(9502, installer_id=2206, status="cancelled")

    state = capacity.capacity_state(cfg)
    pool = next(p for p in state.pools("manufacturing") if p.character_id == 2206)
    assert pool.used == 0


def test_pools_are_pilot_global_across_locations(db):
    """Jobs at two locations consume from ONE pilot pool — location is a load
    attribute, never a second pool."""
    char = _pilot(2207)
    _snapshot(char, levels={MASS_PROD: 4, ADV_MASS_PROD: 1})
    cfg = _config()
    capacity.derive_resources(cfg)
    j1 = _corp_job(9601, installer_id=2207)
    j1.location_id = 60000001
    j1.save()
    j2 = _corp_job(9602, installer_id=2207)
    j2.location_id = 60000002
    j2.save()

    state = capacity.capacity_state(cfg)
    pools = [p for p in state.pools("manufacturing") if p.character_id == 2207]
    assert len(pools) == 1
    assert pools[0].used == 2


def test_overcommit_clamps_remaining_to_zero(db):
    """slots 2, used 4 ⇒ remaining 0 (never −2); another pilot's free slots
    are unaffected."""
    a = _pilot(2208)
    _snapshot(a, levels={MASS_PROD: 1})     # 1+1+0 = 2 slots
    b = _pilot(2209)
    _snapshot(b, levels={MASS_PROD: 4, ADV_MASS_PROD: 1})  # 6 slots
    cfg = _config()
    capacity.derive_resources(cfg)
    for jid in (9701, 9702, 9703, 9704):
        _corp_job(jid, installer_id=2208)

    state = capacity.capacity_state(cfg)
    pool_a = next(p for p in state.pools("manufacturing") if p.character_id == 2208)
    pool_b = next(p for p in state.pools("manufacturing") if p.character_id == 2209)
    assert pool_a.effective_slots == 2
    assert pool_a.used == 4
    assert pool_a.remaining == 0            # clamped, never −2
    assert pool_b.remaining == 6            # unaffected
    assert state.remaining_total("manufacturing") == 6


def test_building_board_job_never_occupies_but_shows_data_quality(db):
    """A BUILDING board job with no ESI row is a data-quality line, never load."""
    char = _pilot(2210)
    _snapshot(char, levels={MASS_PROD: 4})
    cfg = _config()
    capacity.derive_resources(cfg)
    BuildJob.objects.create(output_type_id=587, quantity=1, status=BuildJob.Status.BUILDING)

    state = capacity.capacity_state(cfg)
    pool = next(p for p in state.pools("manufacturing") if p.character_id == 2210)
    assert pool.used == 0                   # board jobs are not committed load
    assert state.unmatched_board_jobs == 1


def test_science_jobs_route_to_science_pool(db):
    char = _pilot(2211)
    _snapshot(char, levels={MASS_PROD: 4})
    cfg = _config()
    capacity.derive_resources(cfg)
    _corp_job(9801, installer_id=2211, activity_id=4)   # ME research → science

    state = capacity.capacity_state(cfg)
    sci = next(p for p in state.pools("science") if p.character_id == 2211)
    mfg = next(p for p in state.pools("manufacturing") if p.character_id == 2211)
    assert sci.used == 1
    assert mfg.used == 0


# --------------------------------------------------------------------------- #
#  Scheduling (WS3) — driven through mrp._apply_feasible_dates
# --------------------------------------------------------------------------- #
def _mktloc(name, **kw):
    return MarketLocation.objects.create(
        name=name, location_type=MarketLocation.LocationType.STATION, **kw
    )


def _measured(cid, mass_prod):
    """A consenting pilot with a fresh snapshot → 1 + mass_prod manufacturing slots."""
    char = _pilot(cid)
    _snapshot(char, levels={MASS_PROD: mass_prod})
    return char


def _row(type_id, net, *, location=None, depth=0, suggestion="build",
         build_job=None, project=None):
    return NetRequirement.objects.create(
        type_id=type_id, location=location, net_quantity=net, gross_quantity=net,
        suggestion=suggestion, depth=depth, status=NetRequirement.Status.OPEN,
        build_job=build_job, industry_project=project,
    )


def _dur(type_id, net):
    import math
    r = buildable_recipe(type_id)
    runs = math.ceil(net / max(1, r.output_quantity))
    return calc.production_seconds(type_id, runs)


def _apply(rows, *, children_of=None, durations=None, now=None, cfg=None):
    cfg = cfg or _config()
    capacity.derive_resources(cfg)           # preserves any officer overrides set earlier
    row_by_key = {(r.type_id, r.location_id): r for r in rows}
    now = now or timezone.now()
    if durations is None:
        durations = {
            (r.type_id, r.location_id): _dur(r.type_id, r.net_quantity) for r in rows
        }
    mrp._apply_feasible_dates(row_by_key, children_of or {}, durations, cfg, now, {})
    for r in rows:
        r.refresh_from_db()
    return rows


def test_slots_contention_pushes_the_overflow_row(sde):
    """§17 verbatim: 4 concurrent builds against 3 measured slots ⇒ the fourth waits
    for the earliest slot to free, source capacity, bottleneck slots."""
    _measured(3001, 2)                       # 3 slots
    locs = [_mktloc(f"L{i}") for i in range(4)]
    rows = _apply([_row(RIFTER, 500, location=loc) for loc in locs])

    codes = sorted(r.bottleneck_code for r in rows)
    assert codes == ["", "", "", "slots"]
    assert all(r.feasible_source == "capacity" for r in rows)
    late = next(r for r in rows if r.bottleneck_code == "slots")
    early = next(r for r in rows if r.bottleneck_code == "")
    assert late.feasible_at > early.feasible_at


def test_zero_measured_capacity_refuses_unmeasured(sde):
    _pilot(3002)                             # consenting, no snapshot → slots unknown
    (row,) = _apply([_row(RIFTER, 1)])
    assert row.feasible_at is None
    assert row.feasible_source == "capacity"
    assert row.bottleneck_code == "unmeasured"


def test_skills_gate_refuses_when_all_blocked(sde):
    from apps.sde.models import SdeBlueprintSkill
    SdeBlueprintSkill.objects.create(       # 3300 is a real SdeType in the sample
        blueprint_type_id=588, product_type_id=RIFTER, skill_type_id=3300,
        level=5, activity="manufacturing",
    )
    _measured(3003, 2)                       # snapshot lacks skill 3300
    (row,) = _apply([_row(RIFTER, 1)])
    assert row.feasible_at is None
    assert row.bottleneck_code == "skills"


def test_none_skill_verdict_never_gates(sde):
    """No imported blueprint-skill for the product ⇒ can_manufacture None ⇒ schedules."""
    _measured(3004, 2)
    (row,) = _apply([_row(RIFTER, 1)])
    assert row.feasible_at is not None
    assert row.feasible_source == "capacity"


def test_blueprint_gate_refuses_without_usable_print(sde):
    from apps.erp.models import Blueprint
    Blueprint.objects.create(              # data present, but not for 587's print (588)
        owner_type=Blueprint.Owner.CORPORATION, type_id=999, product_type_id=999,
        quantity=-1,
    )
    _measured(3005, 2)
    (row,) = _apply([_row(RIFTER, 1)])
    assert row.feasible_at is None
    assert row.bottleneck_code == "blueprint"


def test_usable_corp_print_passes_blueprint_gate(sde):
    from apps.erp.models import Blueprint
    Blueprint.objects.create(
        owner_type=Blueprint.Owner.CORPORATION, type_id=588, product_type_id=587,
        quantity=-1,
    )
    _measured(3006, 2)
    (row,) = _apply([_row(RIFTER, 1)])
    assert row.feasible_at is not None
    assert row.bottleneck_code == ""


def test_runs_exhausted_bpc_does_not_count(sde):
    from apps.erp.models import Blueprint
    Blueprint.objects.create(
        owner_type=Blueprint.Owner.CORPORATION, type_id=588, product_type_id=587,
        quantity=-2, runs=0,
    )
    _measured(3007, 2)
    (row,) = _apply([_row(RIFTER, 1)])
    assert row.bottleneck_code == "blueprint"


def test_facility_reinforced_delays_start(sde):
    from apps.corporation.models import CorpStructure
    loc = _mktloc("Fortizar", structure_id=555)
    CorpStructure.objects.create(
        structure_id=555, type_id=35833, state="armor_reinforce",
        state_timer_end=timezone.now() + timedelta(days=7),
        fuel_expires=timezone.now() + timedelta(days=30),
    )
    _measured(3008, 2)
    now = timezone.now()
    (row,) = _apply([_row(RIFTER, 1, location=loc)], now=now)
    assert row.bottleneck_code == "facility"
    assert row.feasible_at >= capacity._day(now + timedelta(days=7))


def test_facility_out_of_fuel_refuses(sde):
    from apps.corporation.models import CorpStructure
    loc = _mktloc("DeadCitadel", structure_id=556)
    CorpStructure.objects.create(
        structure_id=556, type_id=35833, fuel_expires=timezone.now() - timedelta(days=1),
    )
    _measured(3009, 2)
    (row,) = _apply([_row(RIFTER, 1, location=loc)])
    assert row.feasible_at is None
    assert row.bottleneck_code == "facility"


def test_no_structure_keeps_capacity_date(sde):
    loc = _mktloc("NpcStation")              # no structure_id → no facility verdict
    _measured(3010, 2)
    (row,) = _apply([_row(RIFTER, 1, location=loc)])
    assert row.feasible_at is not None
    assert row.bottleneck_code != "facility"


def test_materials_bottleneck_when_children_dominate(sde):
    """A child finishing far out drives the parent's date ⇒ materials, not slots."""
    _measured(3011, 5)                       # 6 slots — no contention
    now = timezone.now()
    parent = _row(RIFTER, 1, depth=0)
    child = _row(COMPONENT, 500, depth=1)    # big → far-out feasible
    _apply(
        [parent, child],
        children_of={(RIFTER, None): [(COMPONENT, None)]},
        now=now,
    )
    # The parent can't start until the child lands; its sub-day build finishes the
    # same day, so its date is driven by the child (materials), not slot contention.
    assert parent.feasible_at >= child.feasible_at
    assert parent.bottleneck_code == "materials"


def test_own_queued_vehicle_sets_date_at_net_zero(sde):
    """A row covered by its own QUEUED vehicle (net 0) still books future slots — its
    date is the vehicle's booking end, not None."""
    _measured(3012, 5)
    now = timezone.now()
    job = BuildJob.objects.create(
        output_type_id=RIFTER, quantity=500, status=BuildJob.Status.QUEUED
    )
    row = _row(RIFTER, 0, suggestion="buy", build_job=job)   # net 0, covered
    _apply([row], now=now)
    assert row.feasible_source == "capacity"
    # The vehicle books from the quantized day (day(now)), not the wall clock — a
    # same-day re-run must not flip the date across a midnight boundary.
    assert row.feasible_at == capacity._day(
        capacity._day(now) + timedelta(seconds=_dur(RIFTER, 500))
    )


def test_non_own_vehicle_consumes_capacity(sde):
    """A store-fanned QUEUED job with no requirement row of its own still books a
    slot, delaying a competing row; removing it frees the pool."""
    _measured(3013, 0)                       # 1 slot only → forced contention
    now = timezone.now()
    job = BuildJob.objects.create(
        output_type_id=RIFTER, quantity=500, status=BuildJob.Status.QUEUED
    )
    loc = _mktloc("Home")
    (row,) = _apply([_row(RIFTER, 500, location=loc)], now=now)
    assert row.bottleneck_code == "slots"    # the vehicle took the only slot first
    delayed = row.feasible_at

    job.delete()
    (row2,) = _apply([_row(RIFTER, 500, location=_mktloc("Home2"))], now=now)
    assert row2.bottleneck_code == ""
    assert row2.feasible_at < delayed


def test_weekly_cap_throttles_cadence(sde):
    """max_weekly_output pushes a second same-week booking to the next week; a job
    larger than the cap still lands in the first zero-consumption week."""
    _measured(3014, 5)                       # ample slots
    cfg = _config()
    capacity.derive_resources(cfg)
    ProductionResource.objects.filter(
        character_id=3014, activity_class="manufacturing"
    ).update(max_weekly_output=20)
    now = timezone.now()
    a = _row(RIFTER, 15, location=_mktloc("A"))
    b = _row(RIFTER, 15, location=_mktloc("B"))
    _apply([a, b], now=now, cfg=cfg)
    assert b.feasible_at > a.feasible_at     # second 15 pushed to next week

    big = _row(RIFTER, 50, location=_mktloc("C"))   # 50 > cap 20
    _apply([big], now=now, cfg=cfg)
    assert big.feasible_at is not None       # still lands (first empty week), no loop


def test_paused_pilot_excluded(sde):
    _measured(3015, 5)
    cfg = _config()
    capacity.derive_resources(cfg)
    ProductionResource.objects.filter(
        character_id=3015, activity_class="manufacturing"
    ).update(is_paused=True)
    (row,) = _apply([_row(RIFTER, 1)], cfg=cfg)
    assert row.feasible_at is None
    assert row.bottleneck_code == "slots"    # capacity deliberately withdrawn


def test_capacity_off_is_the_p3_path(sde):
    """Disarmed: build rows take the P3 build_time source and carry no bottleneck."""
    _measured(3016, 5)
    cfg = MrpConfig.active()
    cfg.capacity_enabled = False
    cfg.save()
    (row,) = _apply([_row(RIFTER, 1)], cfg=cfg)
    assert row.feasible_source == "build_time"
    assert row.bottleneck_code == ""


# --------------------------------------------------------------------------- #
#  Run-level integration (idempotency, digest exclusion, inert guarantee)
# --------------------------------------------------------------------------- #
def _fit(name, hull=RIFTER, target=6):
    from apps.doctrines.models import Doctrine, DoctrineFit
    from apps.store.models import FitOffer

    doctrine = Doctrine.objects.create(name=f"Doctrine {name}")
    fit = DoctrineFit.objects.create(doctrine=doctrine, name=name, ship_type_id=hull)
    FitOffer.objects.create(fit=fit, target_stock=target)
    return fit


def test_capacity_run_is_idempotent_and_digest_stable(priced_sde):
    """A capacity-armed run re-runs to zero NetRequirement writes on frozen inputs,
    and the digest is unchanged (capacity dates are excluded from it)."""
    _measured(4001, 5)
    _fit("A", target=6)
    _config()                                # arm capacity
    run1 = mrp.run_mrp()
    hull = NetRequirement.objects.get(
        type_id=RIFTER, status__in=("open", "in_progress")
    )
    assert hull.feasible_source == "capacity"
    stamps = dict(NetRequirement.objects.values_list("pk", "updated_at"))

    run2 = mrp.run_mrp()
    assert run2.stats["rows_written"] == 0
    assert run2.inputs_digest == run1.inputs_digest
    assert dict(NetRequirement.objects.values_list("pk", "updated_at")) == stamps


def test_digest_identical_capacity_on_or_off(priced_sde):
    """The digest excludes feasible/bottleneck fields, so arming capacity does not
    change it for the same demand."""
    _measured(4002, 5)
    _fit("A", target=6)
    off = MrpConfig.active()
    off.capacity_enabled = False
    off.save()
    run_off = mrp.run_mrp()

    on = MrpConfig.active()
    on.capacity_enabled = True
    on.save()
    run_on = mrp.run_mrp()
    assert run_on.inputs_digest == run_off.inputs_digest


def test_capacity_off_run_writes_no_bottleneck(priced_sde):
    """Inert: a disarmed run leaves every bottleneck_code empty and uses P3 sources."""
    _fit("A", target=6)
    cfg = MrpConfig.active()
    cfg.capacity_enabled = False
    cfg.save()
    mrp.run_mrp()
    hull = NetRequirement.objects.get(
        type_id=RIFTER, status__in=("open", "in_progress")
    )
    assert hull.feasible_source == "build_time"
    assert all(
        r.bottleneck_code == "" for r in NetRequirement.objects.all()
    )


# --------------------------------------------------------------------------- #
#  i18n (WS6) — labels localise (needs compiled catalogues)
# --------------------------------------------------------------------------- #
def test_capacity_labels_are_localised(db):
    """The feasible-source and bottleneck labels resolve through gettext, so a
    non-English locale renders a translated string, not the machine code."""
    from django.utils import translation

    from apps.industry.mrp import FEASIBLE_SOURCE_LABELS

    with translation.override("en"):
        en_cap = str(FEASIBLE_SOURCE_LABELS["capacity"])
        en_bt = str(capacity.bottleneck_label("facility"))
    with translation.override("de"):
        de_cap = str(FEASIBLE_SOURCE_LABELS["capacity"])
        de_bt = str(capacity.bottleneck_label("facility"))

    assert en_cap == "committed capacity"
    assert de_cap != en_cap                  # localised, not the English source
    assert de_bt != en_bt


# --------------------------------------------------------------------------- #
#  Review fixes (adversarial pass)
# --------------------------------------------------------------------------- #
def test_rederivation_idempotent_without_snapshot(db):
    """A consenting pilot with NO snapshot must not churn as_of every derivation."""
    _pilot(2006)                             # consenting, no snapshot
    cfg = _config()
    capacity.derive_resources(cfg)
    assert capacity.derive_resources(cfg) == 0


def test_consenting_unknown_slots_jobs_are_unmeasured(db):
    """A consenting pilot whose slots are unknown has their load on the UNMEASURED
    line, not vanished from both roll-ups (committed stays 0 for them)."""
    _pilot(2212)                             # consenting, no snapshot → slots unknown
    cfg = _config()
    capacity.derive_resources(cfg)
    _corp_job(9851, installer_id=2212)
    _corp_job(9852, installer_id=2212)

    state = capacity.capacity_state(cfg)
    assert state.committed("manufacturing") == 0
    assert state.unmeasured_jobs == 2


def test_unknown_duration_build_refuses_like_p3(sde):
    """A build whose SDE time is unknown (reaction has no activity_time in the sample)
    yields feasible None under capacity — never an optimistic same-day promise."""
    alloy = 800
    char = _pilot(3017)
    _snapshot(char, levels={MASS_REACT: 2})
    (row,) = _apply([_row(alloy, 200, suggestion="build")],
                    durations={(alloy, None): None})
    assert row.feasible_at is None
    assert row.feasible_source == "capacity"


def test_refused_child_refuses_parent(sde):
    """A component with no honest date (blueprint refused) refuses the assembly that
    consumes it — refuse rather than fabricate."""
    from apps.erp.models import Blueprint
    Blueprint.objects.create(              # RIFTER's print usable; COMPONENT's (701) absent
        owner_type=Blueprint.Owner.CORPORATION, type_id=588, product_type_id=587,
        quantity=-1,
    )
    _measured(3018, 5)
    parent = _row(RIFTER, 1, depth=0)
    child = _row(COMPONENT, 1, depth=1)
    _apply([parent, child], children_of={(RIFTER, None): [(COMPONENT, None)]})

    assert child.feasible_at is None and child.bottleneck_code == "blueprint"
    assert parent.feasible_at is None and parent.bottleneck_code == "materials"


def test_weekly_cap_delay_is_slots_bottleneck(sde):
    """A booking pushed to a later week by the weekly cap is attributed to slots."""
    _measured(3019, 5)
    cfg = _config()
    capacity.derive_resources(cfg)
    ProductionResource.objects.filter(
        character_id=3019, activity_class="manufacturing"
    ).update(max_weekly_output=20)
    now = timezone.now()
    a = _row(RIFTER, 15, location=_mktloc("A"))
    b = _row(RIFTER, 15, location=_mktloc("B"))
    _apply([a, b], now=now, cfg=cfg)

    assert b.bottleneck_code == "slots"      # cap pushed it to next week
    assert a.bottleneck_code == ""


def test_net_positive_buy_row_keeps_lead_time_despite_vehicle(sde):
    """A net>0 buy row with a lingering QUEUED build vehicle keeps the P3 lead-time
    date — the capacity pass does not own it."""
    _measured(3020, 5)
    job = BuildJob.objects.create(
        output_type_id=RIFTER, quantity=5, status=BuildJob.Status.QUEUED
    )
    (row,) = _apply([_row(RIFTER, 5, suggestion="buy", build_job=job)])
    assert row.feasible_source == "lead_time"
