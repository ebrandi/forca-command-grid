"""P1 — the unified per-type availability authority + reservation lifecycle.

Proves the census died: one definition (apps.stockpile.availability) with
ESI-wins-per-location, home-corp scoping, reservation netting and a zero floor;
wrappers return the same numbers; delivery consumes reservations exactly once;
closed plans hold no ACTIVE claims; BOM lines share a material pool; and the
lock design survives genuine thread races.
"""
from __future__ import annotations

import threading
from collections import namedtuple

import pytest
from django.conf import settings
from django.core.management import call_command
from django.db import connection

from apps.erp.models import BuildJob, Delivery
from apps.erp.services import deliver, job_materials
from apps.industry.models import (
    IndustryEconomyConfig,
    IndustryProject,
    IndustryProjectItem,
    MaterialRequirement,
)
from apps.industry.services import compute_project_bom, reserve_project_stock
from apps.market.models import MarketLocation
from apps.stockpile.availability import available, available_detail
from apps.stockpile.models import (
    Asset,
    AssetLocation,
    Stockpile,
    StockpileItem,
    StockReservation,
)
from apps.stockpile.services import (
    available_quantity,
    consume_reservation,
    reserve_for_project,
    reserve_for_project_bulk,
)

TRIT, PYE, RIFTER = 34, 35, 587
_Recipe = namedtuple("_Recipe", ["output_quantity"])


def _mock_bom(monkeypatch, per_run, *, output_quantity=1):
    monkeypatch.setattr("apps.industry.bom.buildable_recipe", lambda pid: _Recipe(output_quantity))
    monkeypatch.setattr(
        "apps.industry.bom.direct_materials",
        lambda pid, runs=1, me=0: {t: q * runs for t, q in per_run.items()},
    )


def _corp_sp(name="Home", location=None):
    return Stockpile.objects.create(name=name, kind=Stockpile.Kind.CORP, location=location)


def _stock(sp, type_id, qty, target=None):
    return StockpileItem.objects.create(
        stockpile=sp, type_id=type_id, quantity_current=qty, quantity_target=target
    )


def _corp_asset(loc, type_id, qty):
    return Asset.objects.create(
        owner_type=Asset.Owner.CORPORATION, owner_id=settings.FORCA_HOME_CORP_ID,
        location=loc, type_id=type_id, quantity=qty,
    )


def _market_loc(name, system_id):
    return MarketLocation.objects.create(
        name=name, location_type=MarketLocation.LocationType.SYSTEM, system_id=system_id
    )


def _enable_consumption():
    cfg = IndustryEconomyConfig.active()
    cfg.consume_materials_on_delivery = True
    cfg.save()


# ============================================================================
# WS1 — service semantics
# ============================================================================
@pytest.mark.django_db
def test_esi_wins_per_covered_location_counted_once():
    """Two stockpiles in one ESI-covered system: manual rows are ignored and the
    ESI stock counts ONCE, not once per stockpile."""
    loc = _market_loc("Staging", 30000001)
    asset_loc = AssetLocation.objects.create(location_id=1001, system_id=30000001)
    _corp_asset(asset_loc, TRIT, 40)
    _stock(_corp_sp("A", loc), TRIT, 100)
    _stock(_corp_sp("B", loc), TRIT, 999)

    detail = available_detail([TRIT])[TRIT]
    assert detail["esi"] == 40
    assert detail["manual"] == 0  # both covered → manual is a planning record only
    assert available([TRIT]) == {TRIT: 40}
    assert [s["covered"] for s in detail["sources"]] == [True, True]


@pytest.mark.django_db
def test_uncovered_manual_counts_and_mixed_corp_sums():
    """A wormhole (ESI-blind) stockpile keeps its manual truth; a mixed corp sums
    covered ESI + uncovered manual + ESI at locations with no stockpile at all."""
    covered_loc = _market_loc("Highsec", 30000001)
    wh_loc = _market_loc("J123456", 31000001)
    asset_loc = AssetLocation.objects.create(location_id=1001, system_id=30000001)
    stray_loc = AssetLocation.objects.create(location_id=1002, system_id=30000003)
    _corp_asset(asset_loc, TRIT, 40)
    _corp_asset(stray_loc, TRIT, 5)  # no stockpile anywhere near — still corp property
    _stock(_corp_sp("HS", covered_loc), TRIT, 100)  # covered → ignored
    _stock(_corp_sp("WH", wh_loc), TRIT, 70)        # uncovered → counts

    assert available([TRIT]) == {TRIT: 40 + 5 + 70}


@pytest.mark.django_db
def test_foreign_corp_and_character_assets_never_count():
    loc = AssetLocation.objects.create(location_id=1001, system_id=30000001)
    Asset.objects.create(owner_type=Asset.Owner.CORPORATION, owner_id=2002,
                         location=loc, type_id=TRIT, quantity=100000)
    Asset.objects.create(owner_type=Asset.Owner.CHARACTER, owner_id=1001,
                         location=loc, type_id=TRIT, quantity=5000)
    assert available([TRIT]) == {TRIT: 0}


@pytest.mark.django_db
def test_location_filter_restricts_all_three_scopes():
    """location=L restricts ESI assets, manual rows and reservations consistently."""
    loc_a = _market_loc("A", 30000001)
    loc_b = _market_loc("B", 30000002)
    asset_a = AssetLocation.objects.create(location_id=1001, system_id=30000001)
    _corp_asset(asset_a, TRIT, 40)
    _stock(_corp_sp("SA", loc_a), TRIT, 100)      # covered → ignored
    item_b = _stock(_corp_sp("SB", loc_b), TRIT, 70)  # uncovered → counts
    project = IndustryProject.objects.create(name="P")
    StockReservation.objects.create(stockpile_item=item_b, project=project, quantity_reserved=20)

    assert available([TRIT]) == {TRIT: 40 + 70 - 20}
    assert available([TRIT], location=loc_a) == {TRIT: 40}
    assert available([TRIT], location=loc_b) == {TRIT: 70 - 20}


@pytest.mark.django_db
def test_reservations_subtract_in_both_source_modes():
    # Covered mode: reservation against a covered stockpile still subtracts.
    loc = _market_loc("A", 30000001)
    asset_loc = AssetLocation.objects.create(location_id=1001, system_id=30000001)
    _corp_asset(asset_loc, TRIT, 100)
    covered_item = _stock(_corp_sp("SA", loc), TRIT, 999)
    project = IndustryProject.objects.create(name="P")
    StockReservation.objects.create(
        stockpile_item=covered_item, project=project, quantity_reserved=30
    )
    assert available([TRIT]) == {TRIT: 70}

    # Manual mode: uncovered stockpile, reservation subtracts from the manual count.
    item = _stock(_corp_sp("WH"), PYE, 50)
    StockReservation.objects.create(stockpile_item=item, project=project, quantity_reserved=20)
    assert available([PYE]) == {PYE: 30}


@pytest.mark.django_db
def test_floor_at_zero_and_over_reserved_surfaced():
    item = _stock(_corp_sp(), TRIT, 50)
    project = IndustryProject.objects.create(name="P")
    StockReservation.objects.create(stockpile_item=item, project=project, quantity_reserved=80)

    detail = available_detail([TRIT])[TRIT]
    assert detail["available"] == 0  # never negative
    assert detail["over_reserved"] == 30
    assert available([TRIT]) == {TRIT: 0}


@pytest.mark.django_db
def test_query_budget(django_assert_max_num_queries):
    """≤5 queries regardless of the number of type ids."""
    loc = _market_loc("A", 30000001)
    asset_loc = AssetLocation.objects.create(location_id=1001, system_id=30000001)
    _corp_asset(asset_loc, TRIT, 40)
    item = _stock(_corp_sp("SA", loc), TRIT, 100)
    _stock(_corp_sp("WH"), PYE, 50)
    project = IndustryProject.objects.create(name="P")
    StockReservation.objects.create(stockpile_item=item, project=project, quantity_reserved=10)

    with django_assert_max_num_queries(5):
        available([TRIT, PYE, RIFTER, 1, 2, 3, 4, 5])


@pytest.mark.django_db
def test_wrappers_return_unified_numbers():
    """The census-death proof: every old entry point returns the authority's number."""
    from apps.doctrines.supply import corp_on_hand

    loc = _market_loc("A", 30000001)
    asset_loc = AssetLocation.objects.create(location_id=1001, system_id=30000001)
    _corp_asset(asset_loc, TRIT, 40)
    _stock(_corp_sp("SA", loc), TRIT, 100)          # covered → ignored (no double count)
    item = _stock(_corp_sp("WH"), TRIT, 70)          # uncovered → counts
    project = IndustryProject.objects.create(name="P")
    StockReservation.objects.create(stockpile_item=item, project=project, quantity_reserved=20)

    expected = 40 + 70 - 20
    assert available([TRIT])[TRIT] == expected
    assert available_quantity(TRIT) == expected
    assert corp_on_hand([TRIT])[TRIT] == expected


@pytest.mark.django_db
def test_job_materials_reads_unified_availability(monkeypatch):
    # job_materials binds direct_materials at import time — patch its module name.
    monkeypatch.setattr(
        "apps.erp.services.direct_materials", lambda pid, runs=1, me=0: {TRIT: 100 * runs}
    )
    loc = _market_loc("A", 30000001)
    asset_loc = AssetLocation.objects.create(location_id=1001, system_id=30000001)
    _corp_asset(asset_loc, TRIT, 40)
    _stock(_corp_sp("SA", loc), TRIT, 100)  # old double count would have said 140

    job = BuildJob.objects.create(output_type_id=RIFTER, quantity=1)
    mats = job_materials(job)
    line = next(ln for ln in mats["lines"] if ln["type_id"] == TRIT)
    assert line["have"] == 40
    assert line["short"] == 60
    assert mats["ready"] is False


# ============================================================================
# WS2 — reservation lifecycle
# ============================================================================
@pytest.mark.django_db
def test_deliver_consumes_reservations_exactly_once_then_done_releases(
    monkeypatch, django_user_model
):
    """Two-line plan: first delivery consumes claims first (splitting the surplus),
    stock is decremented once, and the final delivery flips the plan DONE with
    ZERO ACTIVE reservations left (§17 acceptance criterion)."""
    _mock_bom(monkeypatch, {TRIT: 100})
    _enable_consumption()
    sp = _corp_sp()
    _stock(sp, TRIT, 500)
    user = django_user_model.objects.create(username="builder")

    project = IndustryProject.objects.create(name="P")
    item1 = IndustryProjectItem.objects.create(
        project=project, type_id=RIFTER, quantity=1,
        build_or_buy=IndustryProjectItem.BuildOrBuy.BUILD)
    item2 = IndustryProjectItem.objects.create(
        project=project, type_id=RIFTER, quantity=1,
        build_or_buy=IndustryProjectItem.BuildOrBuy.BUILD)
    assert reserve_for_project(project, TRIT, 300) == 300

    job1 = BuildJob.objects.create(output_type_id=RIFTER, quantity=1, deliver_to=sp,
                                   status=BuildJob.Status.BUILT, source_item=item1)
    delivery = deliver(job1, user)
    # Re-read: JSONField keys are strings after the DB round-trip (shape unchanged).
    assert Delivery.objects.get(pk=delivery.pk).consumed == {str(TRIT): 100}
    trit = StockpileItem.objects.get(stockpile=sp, type_id=TRIT)
    assert trit.quantity_current == 400  # decremented ONCE, not twice

    consumed = StockReservation.objects.filter(
        project=project, status=StockReservation.Status.CONSUMED)
    active = StockReservation.objects.filter(
        project=project, status=StockReservation.Status.ACTIVE)
    assert sum(r.quantity_reserved for r in consumed) == 100
    assert sum(r.quantity_reserved for r in active) == 200  # split remainder survives
    assert available([TRIT]) == {TRIT: 400 - 200}

    # Second deliver of the same job is a no-op (status guard).
    assert deliver(job1, user) is None
    assert StockpileItem.objects.get(pk=trit.pk).quantity_current == 400

    # Final delivery completes the plan → DONE → zero ACTIVE reservations.
    job2 = BuildJob.objects.create(output_type_id=RIFTER, quantity=1, deliver_to=sp,
                                   status=BuildJob.Status.BUILT, source_item=item2)
    deliver(job2, user)
    project.refresh_from_db()
    assert project.status == IndustryProject.Status.DONE
    assert not StockReservation.objects.filter(
        project=project, status=StockReservation.Status.ACTIVE).exists()
    assert StockpileItem.objects.get(pk=trit.pk).quantity_current == 300


@pytest.mark.django_db
def test_deliver_remainder_off_free_stock_respects_rival_claims(
    monkeypatch, django_user_model
):
    """Claims < needs: the remainder comes off free stock, but never off another
    plan's ACTIVE reservations (the old double-subtract)."""
    _mock_bom(monkeypatch, {TRIT: 100})
    _enable_consumption()
    sp = _corp_sp()
    item = _stock(sp, TRIT, 460)
    user = django_user_model.objects.create(username="builder")

    mine = IndustryProject.objects.create(name="Mine")
    line = IndustryProjectItem.objects.create(
        project=mine, type_id=RIFTER, quantity=1,
        build_or_buy=IndustryProjectItem.BuildOrBuy.BUILD)
    rival = IndustryProject.objects.create(name="Rival")
    assert reserve_for_project(mine, TRIT, 50) == 50
    StockReservation.objects.create(stockpile_item=item, project=rival, quantity_reserved=400)

    job = BuildJob.objects.create(output_type_id=RIFTER, quantity=1, deliver_to=sp,
                                  status=BuildJob.Status.BUILT, source_item=line)
    delivery = deliver(job, user)
    # 50 from my claim + only 10 free (410 on hand − 400 rival claim) = 60.
    assert Delivery.objects.get(pk=delivery.pk).consumed == {str(TRIT): 60}
    assert StockpileItem.objects.get(pk=item.pk).quantity_current == 400
    rival_active = StockReservation.objects.filter(
        project=rival, status=StockReservation.Status.ACTIVE)
    assert sum(r.quantity_reserved for r in rival_active) == 400  # untouched
    assert available([TRIT]) == {TRIT: 0}  # floored, never negative


@pytest.mark.django_db
def test_manual_status_close_releases_reservations(client, django_user_model):
    from apps.identity.models import RoleAssignment
    from apps.sso.services import ensure_role
    from core import rbac

    user = django_user_model.objects.create(username="lead")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    sp = _corp_sp()
    _stock(sp, TRIT, 100)
    project = IndustryProject.objects.create(name="P", created_by=user)
    assert reserve_for_project(project, TRIT, 80) == 80

    client.force_login(user)
    resp = client.post(f"/industry/plans/{project.pk}/status/", {"status": "cancelled"})
    assert resp.status_code == 302
    assert not StockReservation.objects.filter(
        project=project, status=StockReservation.Status.ACTIVE).exists()
    assert available([TRIT]) == {TRIT: 100}  # back to pre-reserve


@pytest.mark.django_db
def test_archive_releases_reservations(client, django_user_model):
    from apps.identity.models import RoleAssignment
    from apps.sso.services import ensure_role
    from core import rbac

    user = django_user_model.objects.create(username="lead")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    sp = _corp_sp()
    _stock(sp, TRIT, 100)
    project = IndustryProject.objects.create(name="P", created_by=user)
    reserve_for_project(project, TRIT, 40)

    client.force_login(user)
    client.post(f"/industry/plans/{project.pk}/archive/")
    assert not StockReservation.objects.filter(
        project=project, status=StockReservation.Status.ACTIVE).exists()


@pytest.mark.django_db
def test_consume_reservation_guards():
    sp = _corp_sp()
    item = _stock(sp, TRIT, 100)
    project = IndustryProject.objects.create(name="P")
    res = StockReservation.objects.create(
        stockpile_item=item, project=project, quantity_reserved=60)

    # A RELEASED row is a no-op — never applied twice, and never raising even
    # when its (dead) claim exceeds the stock.
    StockReservation.objects.filter(pk=res.pk).update(
        status=StockReservation.Status.RELEASED, quantity_reserved=500)
    res.refresh_from_db()
    consume_reservation(res)
    assert StockpileItem.objects.get(pk=item.pk).quantity_current == 100

    # Consuming beyond stock raises instead of clamping.
    over = StockReservation.objects.create(
        stockpile_item=item, project=project, quantity_reserved=150)
    with pytest.raises(ValueError):
        consume_reservation(over)
    assert StockpileItem.objects.get(pk=item.pk).quantity_current == 100
    over.refresh_from_db()
    assert over.status == StockReservation.Status.ACTIVE  # rolled back


# ============================================================================
# WS3 — BOM shared pool + idempotent reserve
# ============================================================================
@pytest.mark.django_db
def test_bom_shared_material_netted_once(priced_sde):
    """Two lines sharing a material with pool < combined need: the pool is
    allocated once and quantity_to_acquire covers the true remainder (§17)."""
    _stock(_corp_sp(), TRIT, 150)
    project = IndustryProject.objects.create(name="P")
    IndustryProjectItem.objects.create(
        project=project, type_id=TRIT, quantity=100,
        build_or_buy=IndustryProjectItem.BuildOrBuy.BUY)
    IndustryProjectItem.objects.create(
        project=project, type_id=TRIT, quantity=100,
        build_or_buy=IndustryProjectItem.BuildOrBuy.BUY)
    compute_project_bom(project)

    reqs = list(
        MaterialRequirement.objects.filter(project_item__project=project)
        .order_by("project_item__pk")
    )
    assert [r.quantity_available for r in reqs] == [100, 50]
    assert [r.quantity_to_acquire for r in reqs] == [0, 50]  # not [0, 0]


@pytest.mark.django_db
def test_reserve_project_stock_sums_and_is_idempotent(priced_sde):
    _stock(_corp_sp(), TRIT, 500)
    project = IndustryProject.objects.create(name="P")
    for _i in range(2):
        IndustryProjectItem.objects.create(
            project=project, type_id=TRIT, quantity=100,
            build_or_buy=IndustryProjectItem.BuildOrBuy.BUY)
    compute_project_bom(project)

    first = reserve_project_stock(project)
    assert (first["units"], first["types"]) == (200, 1)  # SUM across lines, not MAX

    second = reserve_project_stock(project)  # double-POST
    assert (second["units"], second["types"]) == (0, 0)
    active = StockReservation.objects.filter(
        project=project, status=StockReservation.Status.ACTIVE)
    assert sum(r.quantity_reserved for r in active) == 200  # nothing stacked


@pytest.mark.django_db
def test_reserve_refused_on_closed_or_archived_plan(priced_sde):
    """A DONE/CANCELLED/archived plan reserves nothing — a claim minted there
    would be stranded forever (§2.2's invariant, forward-looking)."""
    _stock(_corp_sp(), TRIT, 500)
    project = IndustryProject.objects.create(name="P")
    item = IndustryProjectItem.objects.create(
        project=project, type_id=TRIT, quantity=100,
        build_or_buy=IndustryProjectItem.BuildOrBuy.BUY)
    compute_project_bom(project)

    for closer in ("done", "cancelled", "archived"):
        if closer == "archived":
            project.status = IndustryProject.Status.ACTIVE
            project.is_archived = True
        else:
            project.is_archived = False
            project.status = closer
        project.save()
        result = reserve_project_stock(project)
        assert result["closed"] is True and result["units"] == 0
    assert not StockReservation.objects.filter(project=project).exists()
    assert item.material_requirements.exists()  # the demand existed; refusal was the guard


@pytest.mark.django_db
def test_reserve_capped_at_truthful_availability():
    """A stale-high manual count at an ESI-covered location can't mint claims
    beyond the effective stock (which would zero available() corp-wide)."""
    loc = _market_loc("A", 30000001)
    asset_loc = AssetLocation.objects.create(location_id=1001, system_id=30000001)
    _corp_asset(asset_loc, TRIT, 40)                # the truth
    _stock(_corp_sp("SA", loc), TRIT, 999)          # stale manual record
    project = IndustryProject.objects.create(name="P")

    assert reserve_for_project(project, TRIT, 300) == 40  # capped at effective
    detail = available_detail([TRIT])[TRIT]
    assert detail["available"] == 0
    assert detail["over_reserved"] == 0  # the cap prevented an over-claim


# ============================================================================
# WS4 — integrity: constraints + the audit command
# ============================================================================
@pytest.mark.django_db
def test_negative_stock_rejected_at_service_and_db():
    from django.db import IntegrityError
    from django.db import transaction as dj_transaction

    from apps.stockpile.services import record_manual_stock

    sp = _corp_sp()
    with pytest.raises(ValueError):
        record_manual_stock(sp, TRIT, quantity_current=-5)
    with pytest.raises(IntegrityError), dj_transaction.atomic():
        StockpileItem.objects.create(stockpile=sp, type_id=TRIT, quantity_current=-1)


@pytest.mark.django_db
def test_record_manual_stock_preserves_target_unless_passed():
    from apps.stockpile.services import record_manual_stock

    sp = _corp_sp()
    record_manual_stock(sp, TRIT, quantity_current=10, quantity_target=50)
    # A count-only update must not wipe the target (old last-writer-wins bug).
    item = record_manual_stock(sp, TRIT, quantity_current=20)
    assert item.quantity_target == 50
    item = record_manual_stock(sp, TRIT, quantity_current=20, quantity_target=None)
    assert item.quantity_target is None  # explicit clear still works


@pytest.mark.django_db
def test_audit_command_reports_stranded_reservations(capsys):
    sp = _corp_sp()
    item = _stock(sp, TRIT, 100)
    done = IndustryProject.objects.create(name="Old", status=IndustryProject.Status.DONE)
    StockReservation.objects.create(stockpile_item=item, project=done, quantity_reserved=10)

    call_command("audit_stock_integrity")
    out = capsys.readouterr().out
    assert "stranded_active_on_closed_projects=1" in out
    assert "OK" in out  # stranded rows don't block the constraint migration


# ============================================================================
# Concurrency — the lock design under real thread races (pk order, no deadlock)
# ============================================================================
def _race(workers):
    """Run callables truly concurrently: a barrier lines them up, each closes its
    thread-local DB connection afterwards. Re-raises the first worker error."""
    barrier = threading.Barrier(len(workers))
    errors: list[BaseException] = []

    def runner(fn):
        try:
            barrier.wait(timeout=10)
            fn()
        except BaseException as exc:  # noqa: BLE001 — surfaced to the test below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=runner, args=(fn,)) for fn in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    if errors:
        raise errors[0]


@pytest.mark.django_db(transaction=True)
def test_concurrent_deliver_vs_bulk_reserve_no_deadlock(monkeypatch, django_user_model):
    """Delivery consumption (multi-type + output row) races a multi-type bulk
    reserve on overlapping items: pk-ordered acquisition means no deadlock, and
    the constraint keeps stock non-negative under load."""
    _mock_bom(monkeypatch, {TRIT: 100, PYE: 50})
    _enable_consumption()
    sp_a = _corp_sp("A")
    sp_b = _corp_sp("B")
    _stock(sp_a, TRIT, 80)
    _stock(sp_b, TRIT, 80)
    _stock(sp_a, PYE, 60)
    user = django_user_model.objects.create(username="builder")
    job = BuildJob.objects.create(
        output_type_id=RIFTER, quantity=1, deliver_to=sp_b, status=BuildJob.Status.BUILT)
    project = IndustryProject.objects.create(name="P")

    _race([
        lambda: deliver(job, user),
        lambda: reserve_for_project_bulk(project, {PYE: 40, TRIT: 120}),
    ])

    assert not StockpileItem.objects.filter(quantity_current__lt=0).exists()
    for tid, qty in available([TRIT, PYE]).items():
        assert qty >= 0, (tid, qty)


@pytest.mark.django_db(transaction=True)
def test_concurrent_double_deliver_consumes_reservations_once(monkeypatch, django_user_model):
    """Two racing delivers of one plan-linked job: one wins, the plan's claims
    are consumed exactly once (the §7 spec, WITH reservations in play)."""
    _mock_bom(monkeypatch, {TRIT: 100})
    _enable_consumption()
    sp = _corp_sp()
    _stock(sp, TRIT, 500)
    user = django_user_model.objects.create(username="builder")
    project = IndustryProject.objects.create(name="P")
    line1 = IndustryProjectItem.objects.create(
        project=project, type_id=RIFTER, quantity=1,
        build_or_buy=IndustryProjectItem.BuildOrBuy.BUILD)
    IndustryProjectItem.objects.create(  # second line keeps the plan open
        project=project, type_id=RIFTER, quantity=1,
        build_or_buy=IndustryProjectItem.BuildOrBuy.BUILD)
    reserve_for_project(project, TRIT, 300)
    job = BuildJob.objects.create(
        output_type_id=RIFTER, quantity=1, deliver_to=sp,
        status=BuildJob.Status.BUILT, source_item=line1)

    _race([lambda: deliver(job, user), lambda: deliver(job, user)])

    assert Delivery.objects.filter(job=job).count() == 1  # one winner
    assert StockpileItem.objects.get(stockpile=sp, type_id=TRIT).quantity_current == 400
    assert StockpileItem.objects.get(stockpile=sp, type_id=RIFTER).quantity_current == 1
    consumed = StockReservation.objects.filter(
        project=project, status=StockReservation.Status.CONSUMED)
    active = StockReservation.objects.filter(
        project=project, status=StockReservation.Status.ACTIVE)
    assert sum(r.quantity_reserved for r in consumed) == 100  # exactly once
    assert sum(r.quantity_reserved for r in active) == 200


@pytest.mark.django_db(transaction=True)
def test_concurrent_double_reserve_does_not_stack():
    """Two racing Reserve POSTs (a real double-click): the netting runs under the
    item locks, so the loser reserves nothing and claims never exceed demand."""
    _stock(_corp_sp(), TRIT, 500)
    project = IndustryProject.objects.create(name="P")
    IndustryProjectItem.objects.create(
        project=project, type_id=TRIT, quantity=200,
        build_or_buy=IndustryProjectItem.BuildOrBuy.BUY)
    compute_project_bom(project)

    _race([lambda: reserve_project_stock(project), lambda: reserve_project_stock(project)])

    active = StockReservation.objects.filter(
        project=project, status=StockReservation.Status.ACTIVE)
    assert sum(r.quantity_reserved for r in active) == 200  # not 400
