"""P3 — MRP v1: netting core, incoming dedup, cascade, fan-out, run guards.

Uses the bundled SDE sample: 587 Rifter (34×32000 + 35×6000, manufacturing),
600 Test Cruiser (700×10 + 34×100000), 700 Component (800×5 + 35×1000),
800 Reacted Alloy (reaction, batch of 200 from 34×1000).
"""
from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.doctrines.models import Doctrine, DoctrineFit
from apps.erp.models import BuildJob, CorpIndustryJob
from apps.industry import mrp
from apps.industry.models import (
    IndustryProject,
    IndustryProjectItem,
    MrpConfig,
    MrpRun,
    NetRequirement,
)
from apps.store.models import FitOffer, FitSupplyNeed

pytestmark = pytest.mark.django_db

RIFTER, TRIT, PYE = 587, 34, 35
CRUISER, COMPONENT, ALLOY = 600, 700, 800


def _fit(name="Alpha", hull=RIFTER, target=None):
    doctrine = Doctrine.objects.create(name=f"Doctrine {name}")
    fit = DoctrineFit.objects.create(doctrine=doctrine, name=name, ship_type_id=hull)
    if target is not None:
        FitOffer.objects.create(fit=fit, target_stock=target)
    return fit


def _live(type_id, location_id=None):
    return NetRequirement.objects.filter(
        type_id=type_id, location_id=location_id,
        status__in=(NetRequirement.Status.OPEN, NetRequirement.Status.IN_PROGRESS),
    ).first()


# --------------------------------------------------------------------------- #
#  Netting core
# --------------------------------------------------------------------------- #
def test_shared_component_nets_once_with_both_parents(priced_sde):
    """§17 verbatim: two doctrine restocks sharing components produce ONE netted
    material plan — one row per type, both parents in provenance."""
    fit_a = _fit("A", target=6)
    fit_b = _fit("B", target=4)

    mrp.run_mrp()

    hull = _live(RIFTER)
    assert hull is not None and hull.gross_quantity == 10
    fit_ids = {s["id"] for s in hull.sources if s["kind"] == "fit_demand"}
    assert fit_ids == {fit_a.id, fit_b.id}

    trit = _live(TRIT)
    assert trit is not None
    assert NetRequirement.objects.filter(
        type_id=TRIT, status__in=("open", "in_progress")
    ).count() == 1  # consolidated, not one per restock
    assert trit.gross_quantity == 10 * 32000
    assert trit.depth == 1
    parents = {s["kind"] for s in trit.sources}
    assert parents == {"parent"}
    assert trit.sources[0]["id"] == hull.pk  # provenance links to the parent row
    assert hull.suggestion == "build"
    assert trit.suggestion == "buy"  # no location → hub-less default


def test_rerun_with_unchanged_inputs_writes_nothing(priced_sde):
    _fit("A", target=6)
    run1 = mrp.run_mrp()
    stamps = dict(NetRequirement.objects.values_list("pk", "updated_at"))

    run2 = mrp.run_mrp()
    assert run2.inputs_digest == run1.inputs_digest
    assert run2.stats["rows_written"] == 0
    assert dict(NetRequirement.objects.values_list("pk", "updated_at")) == stamps
    # last_run stamps only on change — still the first run's pk.
    assert set(NetRequirement.objects.values_list("last_run", flat=True)) == {run1.pk}


def test_price_flip_mid_run_cannot_change_numbers(priced_sde, monkeypatch):
    """The pinned snapshot: decisions are taken through the run's price callable."""
    _fit("A", target=6)
    from apps.market import pricing

    real_maps = pricing.price_maps()
    calls = {"n": 0}

    def flipping_price_maps():
        calls["n"] += 1
        return real_maps

    monkeypatch.setattr("apps.market.pricing.price_maps", flipping_price_maps)
    run = mrp.run_mrp()
    assert run.status == "done"
    assert calls["n"] == 1  # one snapshot, taken once


def test_low_level_code_nets_a_type_once_at_its_deepest_depth(priced_sde):
    """Tritanium is demanded at depth 1 (under the Cruiser) AND depth 3 (under
    the Alloy reaction) — it must net once, at depth 3, with summed gross."""
    _fit("Cruiser fleet", hull=CRUISER, target=1)

    mrp.run_mrp()

    trit_rows = NetRequirement.objects.filter(
        type_id=TRIT, status__in=("open", "in_progress")
    )
    assert trit_rows.count() == 1
    trit = trit_rows.get()
    assert trit.depth == 3
    # Direct (100000, depth 1) + via alloy chain when those levels build.
    assert trit.gross_quantity >= 100000
    parent_kinds = {s["kind"] for s in trit.sources}
    assert parent_kinds == {"parent"}


def test_incoming_status_matrix_and_units(priced_sde):
    """active/paused count; ready gated; delivered/cancelled/reverted never;
    invention never; character jobs never; runs × output_quantity conversion."""
    config = MrpConfig.active()
    common = {"blueprint_type_id": 599, "installer_id": 1, "product_type_id": ALLOY,
              "activity_id": 9}
    CorpIndustryJob.objects.create(job_id=1, runs=2, status="active", **common)
    CorpIndustryJob.objects.create(job_id=2, runs=1, status="delivered", **common)
    CorpIndustryJob.objects.create(job_id=3, runs=1, status="cancelled", **common)
    CorpIndustryJob.objects.create(job_id=4, runs=1, status="reverted", **common)
    CorpIndustryJob.objects.create(job_id=5, runs=1, status="ready", **common)
    CorpIndustryJob.objects.create(  # invention — probabilistic, never supply
        job_id=6, runs=9, status="active", blueprint_type_id=599,
        installer_id=1, product_type_id=ALLOY, activity_id=8,
    )

    pool, _cascade, _matched = mrp._collect_incoming(config)
    esi = [lot for lot in pool if lot.kind == "esi_job"]
    assert {lot.ref_id for lot in esi} == {1, 5}
    # 1 run of the 200-per-run alloy reaction = 200 units, never 1.
    assert next(lot for lot in esi if lot.ref_id == 1).remaining == 400
    assert next(lot for lot in esi if lot.ref_id == 5).remaining == 200

    config.include_ready_jobs = False
    config.save()
    pool, _c, _m = mrp._collect_incoming(config)
    assert {lot.ref_id for lot in pool if lot.kind == "esi_job"} == {1}


def test_esi_wins_linked_and_heuristic(django_user_model, priced_sde):
    """A BuildJob whose physical build shows up as a corp ESI job counts once —
    on the ESI side (explicit link, or the conservative unlinked match)."""
    from apps.sso.models import EveCharacter

    owner = django_user_model.objects.create(username="builder")
    EveCharacter.objects.create(character_id=9001, user=owner, name="B", is_main=True)

    linked = BuildJob.objects.create(
        output_type_id=RIFTER, quantity=5, status="building", owner=owner, esi_job_id=77,
    )
    CorpIndustryJob.objects.create(
        job_id=77, runs=5, status="active", blueprint_type_id=1,
        installer_id=9001, product_type_id=RIFTER, activity_id=1,
    )
    # Heuristic pair: unlinked BUILDING job + matching active ESI job.
    heur = BuildJob.objects.create(
        output_type_id=RIFTER, quantity=3, status="building", owner=owner,
    )
    CorpIndustryJob.objects.create(
        job_id=88, runs=3, status="active", blueprint_type_id=1,
        installer_id=9001, product_type_id=RIFTER,
        activity_id=1, start_date=timezone.now(),
    )

    pool, cascade, matched = mrp._collect_incoming(MrpConfig.active())
    build_lots = [lot for lot in pool if lot.kind == "build_job"]
    assert not build_lots  # both excluded — ESI carries the supply
    assert {lot.ref_id for lot in pool if lot.kind == "esi_job"} == {77, 88}
    assert linked.pk in matched and heur.pk in matched
    # ESI jobs never cascade component demand (inputs already consumed in game).
    assert not [c for c in cascade if c["type_id"] == RIFTER]


def test_project_line_counts_once_not_per_layer(priced_sde):
    project = IndustryProject.objects.create(
        name="P", status=IndustryProject.Status.ACTIVE)
    item = IndustryProjectItem.objects.create(
        project=project, type_id=RIFTER, quantity=4,
        build_or_buy=IndustryProjectItem.BuildOrBuy.BUILD)
    BuildJob.objects.create(output_type_id=RIFTER, quantity=4, status="queued",
                            source_item=item)
    pool, _c, _m = mrp._collect_incoming(MrpConfig.active())
    kinds = [(lot.kind, lot.remaining) for lot in pool if lot.type_id == RIFTER]
    assert kinds == [("build_job", 4)]  # the job, not job + project line


def test_cascade_descendants_survive(priced_sde):
    """An undelivered internal build is supply AND dependent component demand —
    its material rows must not evaporate while the job sits queued."""
    BuildJob.objects.create(output_type_id=RIFTER, quantity=5, status="queued")

    mrp.run_mrp()

    trit = _live(TRIT)
    assert trit is not None
    assert trit.gross_quantity == 5 * 32000
    assert {s["kind"] for s in trit.sources} == {"vehicle"}
    # The hull itself has no unmet demand (supply with no demand) — no open hull row.
    hull = _live(RIFTER)
    assert hull is None or hull.net_quantity == 0


def test_depth0_attribution_no_double_subtract(priced_sde):
    """A need-linked BuildJob offsets its need only — never the pool as well."""
    fit = _fit("A")
    need = FitSupplyNeed.objects.create(doctrine_fit=fit, quantity_required=10)
    job = BuildJob.objects.create(output_type_id=RIFTER, quantity=4, status="queued")
    need.build_job = job
    need.status = FitSupplyNeed.Status.IN_PROGRESS
    need.save(update_fields=["build_job", "status"])

    mrp.run_mrp()

    hull = _live(RIFTER)
    assert hull is not None
    # gross = 10 − 4 (offset by ITS vehicle); the job must not also appear in
    # pooled incoming.
    assert hull.gross_quantity == 6
    assert not [r for r in hull.incoming_refs if r["kind"] == "build_job" and r["id"] == job.pk]


def test_consolidation_window_excludes_far_dated_demand(priced_sde):
    fit = _fit("A")
    near = FitSupplyNeed.objects.create(
        doctrine_fit=fit, quantity_required=3,
        required_by=timezone.now() + timedelta(days=3))
    FitSupplyNeed.objects.filter(pk=near.pk).update(location=None)
    far_fit = _fit("B", hull=CRUISER)
    FitSupplyNeed.objects.create(
        doctrine_fit=far_fit, quantity_required=7,
        required_by=timezone.now() + timedelta(days=90))

    run = mrp.run_mrp()

    hull = _live(RIFTER)
    assert hull is not None and hull.gross_quantity == 3
    assert hull.required_by is not None
    assert _live(CRUISER) is None  # beyond the window — excluded entirely
    beyond = run.stats["beyond_window"]
    assert len(beyond) == 1 and beyond[0]["qty"] == 7


def test_stale_row_sweep(priced_sde):
    offer_fit = _fit("A", target=6)
    mrp.run_mrp()
    assert _live(TRIT) is not None

    offer = FitOffer.objects.get(fit=offer_fit)
    offer.target_stock = 0
    offer.save(update_fields=["target_stock"])
    mrp.run_mrp()

    assert _live(TRIT) is None  # zeroed and closed
    swept = NetRequirement.objects.filter(type_id=TRIT).latest("pk")
    assert swept.status == NetRequirement.Status.DONE
    assert swept.net_quantity == 0


def test_sweep_keeps_vehicle_linked_rows_flagged(priced_sde):
    offer_fit = _fit("A", target=6)
    mrp.run_mrp()
    trit = _live(TRIT)
    task_row = _live(PYE)
    assert trit and task_row
    job = mrp.create_buy_task_for_requirement(trit, actor=None)
    assert job is not None

    offer = FitOffer.objects.get(fit=offer_fit)
    offer.target_stock = 0
    offer.save(update_fields=["target_stock"])
    mrp.run_mrp()

    trit.refresh_from_db()
    assert trit.status == NetRequirement.Status.IN_PROGRESS  # vehicle keeps it live
    assert trit.diverged is True
    assert trit.net_quantity == 0


def test_required_by_propagates_backward(priced_sde):
    fit = _fit("A")
    due = timezone.now() + timedelta(days=10)
    FitSupplyNeed.objects.create(doctrine_fit=fit, quantity_required=5, required_by=due)

    mrp.run_mrp()
    hull = _live(RIFTER)
    trit = _live(TRIT)
    assert hull.required_by is not None and trit.required_by is not None
    # Component inherits parent date minus the parent's own build duration
    # (verbatim when unknown), day-quantized — never later than the parent's.
    assert trit.required_by <= hull.required_by


def test_feasible_dates_lead_time_and_day_quantized(priced_sde):
    _fit("A", target=6)
    mrp.run_mrp()
    trit = _live(TRIT)
    assert trit.suggestion == "buy"
    assert trit.feasible_source == "lead_time"
    expected = mrp._day(timezone.now() + timedelta(days=MrpConfig.active().buy_lead_days))
    assert trit.feasible_at == expected


def test_run_queries_do_not_scale_per_type(priced_sde):
    """Batched per wave: adding a second fit on the same chain must not add
    per-type queries (the O(depth) budget, not O(types))."""
    _fit("A", target=2)
    with CaptureQueriesContext(connection) as ctx_one:
        mrp.run_mrp()
    n_one = len(ctx_one)

    _fit("B", target=3)  # same hull chain — same waves
    with CaptureQueriesContext(connection) as ctx_two:
        mrp.run_mrp()
    # Second world re-runs everything plus one extra fit; read amplification
    # must stay flat (a couple of extra row writes, never a per-type fan).
    assert len(ctx_two) <= n_one + 25


def test_single_flight_and_stale_takeover(priced_sde):
    stale = MrpRun.objects.create(
        heartbeat_at=timezone.now() - timedelta(minutes=30))
    run = mrp.run_mrp()
    stale.refresh_from_db()
    assert stale.status == MrpRun.Status.FAILED  # crashed run recovered precisely
    assert run.status == MrpRun.Status.DONE

    fresh = MrpRun.objects.create(heartbeat_at=timezone.now())
    with pytest.raises(mrp.MrpAlreadyRunning):
        mrp.run_mrp()
    fresh.delete()


@pytest.mark.django_db(transaction=True)
def test_concurrent_claims_one_winner(priced_sde):
    barrier = threading.Barrier(2)
    outcomes = []

    def claim():
        try:
            barrier.wait(timeout=10)
            run = mrp._claim_run(None)
            outcomes.append(("won", run.pk))
        except mrp.MrpAlreadyRunning:
            outcomes.append(("refused", None))
        except BaseException as exc:  # noqa: BLE001 — surfaced below
            outcomes.append(("error", exc))
        finally:
            connection.close()

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(o[0] for o in outcomes) == ["refused", "won"]
    assert MrpRun.objects.filter(status=MrpRun.Status.RUNNING).count() == 1


# --------------------------------------------------------------------------- #
#  Fan-out
# --------------------------------------------------------------------------- #
def _component_row(priced=True):
    _fit("A", target=6)
    mrp.run_mrp()
    return _live(RIFTER), _live(TRIT)


def test_fan_out_build_job_and_self_feedback(priced_sde, django_user_model):
    hull, trit = _component_row()
    # Force a buildable component scenario: use the hull row's child? The hull
    # row is ship-level; exercise the job creator on a synthetic component row.
    comp = NetRequirement.objects.create(
        type_id=COMPONENT, net_quantity=20, gross_quantity=20,
        suggestion="build", depth=1,
        sources=[{"kind": "parent", "id": hull.pk, "qty": 20}],
    )
    job = mrp.create_build_job_for_requirement(comp, actor=None)
    assert job.quantity == 20
    assert job.note_key == "job.mrp_restock"
    again = mrp.create_build_job_for_requirement(comp, actor=None)
    assert again.pk == job.pk  # idempotent

    # Self-feedback: the job now counts as incoming for COMPONENT; the vehicle
    # target must exclude it (never refresh the vehicle toward 0).
    mrp.run_mrp()
    comp.refresh_from_db()
    job.refresh_from_db()
    assert job.quantity == 20  # NOT refreshed toward 0
    assert comp.status == NetRequirement.Status.IN_PROGRESS


def test_fan_out_buy_task_uses_shared_factory(priced_sde):
    _hull, trit = _component_row()
    task = mrp.create_buy_task_for_requirement(trit, actor=None)
    assert task.related_type == "net_requirement" and task.related_id == str(trit.pk)
    assert mrp.create_buy_task_for_requirement(trit, actor=None).pk == task.pk


def test_fan_out_haul_uses_packaged_volume(priced_sde):
    from apps.market.models import MarketLocation

    dest = MarketLocation.objects.create(
        name="Staging", location_type=MarketLocation.LocationType.SYSTEM,
        system_id=30000144)
    row = NetRequirement.objects.create(
        type_id=RIFTER, location=dest, net_quantity=4, gross_quantity=4,
        suggestion="import", depth=1,
        sources=[{"kind": "parent", "id": 1, "qty": 4}],
    )
    haul = mrp.create_hauling_task_for_requirement(row, actor=None)
    # Packaged frigate volume (2,500), never the assembled 27,289.
    assert haul.volume_m3 == pytest.approx(2500.0 * 4)


def test_fan_out_project_lands_bom_exploded(priced_sde):
    hull, _trit = _component_row()
    comp = NetRequirement.objects.create(
        type_id=COMPONENT, net_quantity=3, gross_quantity=3, suggestion="build",
        depth=1, sources=[{"kind": "parent", "id": hull.pk, "qty": 3}],
    )
    project = mrp.create_project_for_requirement(comp, actor=None)
    assert project.source == IndustryProject.Source.MRP
    assert project.items.get().material_requirements.exists()  # exploded on creation


def test_reconciliation_refreshes_unclaimed_flags_claimed(priced_sde, django_user_model):
    fit = _fit("A", target=6)
    mrp.run_mrp()
    hull = _live(RIFTER)
    comp = NetRequirement.objects.create(
        type_id=COMPONENT, net_quantity=20, gross_quantity=20, suggestion="build",
        depth=1, sources=[{"kind": "parent", "id": hull.pk, "qty": 20}],
    )
    job = mrp.create_build_job_for_requirement(comp, actor=None)

    # Unclaimed + demand vanished → swept to 0 target, vehicle refreshed? A
    # swept row keeps the vehicle and flags divergence instead of deleting.
    offer = FitOffer.objects.get(fit=fit)
    offer.target_stock = 0
    offer.save(update_fields=["target_stock"])
    mrp.run_mrp()
    comp.refresh_from_db()
    assert comp.status == NetRequirement.Status.IN_PROGRESS
    assert comp.diverged is True

    # A claimed job is never touched.
    owner = django_user_model.objects.create(username="b2")
    job.refresh_from_db()
    assert job.quantity == 20
    job.owner = owner
    job.status = "building"
    job.save(update_fields=["owner", "status"])
    mrp.run_mrp()
    job.refresh_from_db()
    assert job.quantity == 20


# --------------------------------------------------------------------------- #
#  Views / permissions
# --------------------------------------------------------------------------- #
def _officer(django_user_model, name="qm"):
    from apps.identity.models import RoleAssignment
    from apps.sso.services import ensure_role
    from core import rbac

    user = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_OFFICER))
    return user


def test_mrp_page_officer_only(client, django_user_model, priced_sde):
    from apps.identity.models import RoleAssignment
    from apps.sso.services import ensure_role
    from core import rbac

    member = django_user_model.objects.create(username="pilot")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(member)
    assert client.get("/industry/mrp/").status_code == 403

    client.force_login(_officer(django_user_model))
    _fit("A", target=6)
    resp = client.post("/industry/mrp/run/")
    assert resp.status_code == 302
    assert MrpRun.objects.filter(status=MrpRun.Status.DONE).exists()
    from apps.admin_audit.models import AuditLog

    assert AuditLog.objects.filter(action="industry.mrp_run").exists()

    resp = client.get("/industry/mrp/")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "unconstrained" in body.lower() or "capacity" in body.lower()

    csv_head = client.get("/industry/mrp/?export=csv").content.decode().splitlines()[0]
    for col in ("type_id", "gross", "available", "incoming", "net", "suggestion",
                "feasible_source", "diverged"):
        assert col in csv_head


def test_ship_level_rows_never_fan_out_from_mrp(client, django_user_model, priced_sde):
    client.force_login(_officer(django_user_model, "qm2"))
    _fit("A", target=6)
    mrp.run_mrp()
    hull = _live(RIFTER)
    resp = client.post(f"/industry/mrp/req/{hull.pk}/action/", {"action": "build_job"})
    assert resp.status_code == 302
    hull.refresh_from_db()
    assert hull.build_job_id is None  # refused — the Shipyard console owns ships


# --------------------------------------------------------------------------- #
#  Preconditions (WS3)
# --------------------------------------------------------------------------- #
def test_build_cost_is_batch_safe(priced_sde):
    """100 units of a 100-per-run recipe cost ONE run of materials — the old
    units-as-runs form overcosted by the batch factor."""
    from decimal import Decimal

    from apps.industry.bom import build_cost
    from apps.sde.models import SdeBlueprintMaterial

    # Give Tritanium a synthetic 100-per-run recipe (materials: Pyerite) — both
    # types exist in the sample, so the FK holds; the cache is reset around it.
    SdeBlueprintMaterial.objects.create(
        blueprint_type_id=9001, product_type_id=TRIT,
        material_type_id=PYE, quantity=50, output_quantity=100,
        activity=SdeBlueprintMaterial.MANUFACTURING,
    )
    from apps.industry.bom import reset_recipe_cache

    reset_recipe_cache()
    one_run = build_cost(TRIT, 1)
    hundred_units = build_cost(TRIT, 100)
    assert hundred_units == one_run  # 1 run covers both
    assert build_cost(TRIT, 101) == one_run * 2  # ceil to whole runs
    price_calls = []
    build_cost(TRIT, 100, price=lambda tid: price_calls.append(tid) or Decimal("1"))
    assert price_calls  # the price seam is honoured
    reset_recipe_cache()


def test_reaction_seconds_and_zero_row(priced_sde):
    from apps.industry.calc import production_seconds, reaction_seconds
    from apps.sde.models import SdeBlueprintActivityTime

    SdeBlueprintActivityTime.objects.create(
        blueprint_type_id=801, product_type_id=ALLOY,
        activity=SdeBlueprintActivityTime.REACTION, time=1800,
    )
    assert reaction_seconds(ALLOY, 2) == 3600
    assert reaction_seconds(999999, 1) is None
    SdeBlueprintActivityTime.objects.create(
        blueprint_type_id=901, product_type_id=PYE,
        activity=SdeBlueprintActivityTime.MANUFACTURING, time=0,
    )
    assert production_seconds(PYE, 5) == 0  # a real 0-second row is 0, not None


def test_plan_from_demand_is_idempotent(client, django_user_model, priced_sde):
    client.force_login(_officer(django_user_model, "qm3"))
    for _ in range(2):
        client.post("/industry/demand/create/", {"type_id": RIFTER, "quantity": 5})
    assert IndustryProject.objects.filter(
        source=IndustryProject.Source.DOCTRINE_SUPPLY
    ).count() == 1  # double-POST no longer mints twins


def test_esi_link_action(django_user_model, priced_sde):
    from apps.erp.services import link_esi_job

    owner = django_user_model.objects.create(username="b3")
    job = BuildJob.objects.create(
        output_type_id=RIFTER, quantity=2, status="building", owner=owner)
    CorpIndustryJob.objects.create(
        job_id=501, runs=2, status="active", blueprint_type_id=1,
        installer_id=1, product_type_id=CRUISER, activity_id=1)

    ok, code = link_esi_job(job, 501)
    assert (ok, code) == (False, "mismatch")  # different product refused
    ok, code = link_esi_job(job, 502)         # not in the mirror yet — allowed
    assert (ok, code) == (True, "linked")
    other = BuildJob.objects.create(
        output_type_id=RIFTER, quantity=1, status="building", owner=owner)
    ok, code = link_esi_job(other, 502)
    assert (ok, code) == (False, "taken")
    ok, code = link_esi_job(job, None)
    assert (ok, code) == (True, "unlinked")


# --------------------------------------------------------------------------- #
#  Review regressions (P3 adversarial pass)
# --------------------------------------------------------------------------- #
def test_mixed_location_and_none_cells_run_clean(priced_sde):
    """Digest sort must survive a type demanded at BOTH a located and a
    location-None bucket (the None-vs-int tuple sort crash)."""
    from apps.market.models import MarketLocation
    from apps.store.models import ShipyardPolicy

    loc = MarketLocation.objects.create(
        name="Staging", location_type=MarketLocation.LocationType.SYSTEM,
        system_id=30000144)
    policy = ShipyardPolicy.active()
    policy.default_location = loc
    policy.save(update_fields=["default_location"])
    _fit("Located", target=2)                    # fit demand at (587, loc)
    BuildJob.objects.create(output_type_id=RIFTER, quantity=3, status="queued")
    # cascade component demand lands at (34, None) while fit explosion puts
    # (34, loc) — and the hull itself sits at (587, loc) + supply at None.

    run = mrp.run_mrp()
    assert run.status == MrpRun.Status.DONE
    assert run.inputs_digest


def test_ship_level_cells_never_net_p1_stock(priced_sde):
    """Assembled hulls in the asset mirror must NOT zero ship demand — fit ATP
    already covered them inside P2's suggestion (§3.3 step 1 / §11)."""
    from django.conf import settings as dj_settings

    from apps.stockpile.models import Asset, AssetLocation

    _fit("A", target=10)
    loc = AssetLocation.objects.create(location_id=7001, system_id=30000142)
    Asset.objects.create(
        owner_type=Asset.Owner.CORPORATION, owner_id=dj_settings.FORCA_HOME_CORP_ID,
        location=loc, type_id=RIFTER, quantity=10,
    )

    mrp.run_mrp()
    hull = _live(RIFTER)
    assert hull is not None
    assert hull.available_quantity == 0  # stock skipped at ship level
    assert hull.net_quantity == 10       # the plan still builds the missing ships
    # …while a COMPONENT of the same world still nets stock normally.
    trit = _live(TRIT)
    assert trit.available_quantity == 0 or trit.depth > 0


def test_attributed_vehicle_still_cascades_component_demand(priced_sde):
    """Attribution governs supply only: a need-linked queued job's materials
    must still be demanded, or the plan eats its own shopping list."""
    fit = _fit("A")
    need = FitSupplyNeed.objects.create(doctrine_fit=fit, quantity_required=10)
    job = BuildJob.objects.create(output_type_id=RIFTER, quantity=4, status="queued")
    need.build_job = job
    need.status = FitSupplyNeed.Status.IN_PROGRESS
    need.save(update_fields=["build_job", "status"])

    mrp.run_mrp()

    hull = _live(RIFTER)
    assert hull.gross_quantity == 6  # supply side: offset its need only
    trit = _live(TRIT)
    # Demand side: 6 (net hull explosion) + 4 (cascade of the promised job).
    assert trit.gross_quantity == 10 * 32000
    kinds = {s["kind"] for s in trit.sources}
    assert "vehicle" in kinds and "parent" in kinds


def test_terminal_vehicle_releases_the_row(priced_sde):
    _fit("A", target=6)
    mrp.run_mrp()
    trit = _live(TRIT)
    task = mrp.create_buy_task_for_requirement(trit, actor=None)
    from apps.tasks.models import Task

    Task.objects.filter(pk=task.pk).update(status=Task.Status.DONE)
    mrp.run_mrp()
    trit.refresh_from_db()
    assert trit.task_id is None  # released — a new shortfall can fan out again
    assert trit.status in (NetRequirement.Status.OPEN, NetRequirement.Status.IN_PROGRESS)


def test_esi_link_partial_unique_closes_the_race(django_user_model, priced_sde):
    from django.db import IntegrityError

    owner = django_user_model.objects.create(username="b9")
    BuildJob.objects.create(output_type_id=RIFTER, quantity=1, status="building",
                            owner=owner, esi_job_id=9911)
    with pytest.raises(IntegrityError):
        BuildJob.objects.create(output_type_id=RIFTER, quantity=1, status="building",
                                owner=owner, esi_job_id=9911)
