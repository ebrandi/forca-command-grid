"""P6 — Freight pipeline: batch lifecycle, allocation, receipts, MRP integration,
sweep, views. Uses the bundled SDE sample (587 Rifter = frigate, packaged 2,500 m³;
34 Tritanium as an always-buy component)."""
from __future__ import annotations

import threading
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.industry import mrp
from apps.industry.models import NetRequirement
from apps.logistics import freight
from apps.logistics.models import (
    CourierContract,
    FreightBatch,
    FreightBatchLine,
    FreightConfig,
    FreightReceipt,
)

pytestmark = pytest.mark.django_db

RIFTER, TRIT = 587, 34
JITA, OTITOH = 30000142, 30002053


# --------------------------------------------------------------------------- #
#  Fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _clear_jump_graph():
    from apps.logistics.jumps import clear_graph_cache

    clear_graph_cache()
    yield
    clear_graph_cache()


def _routable():
    """Give the two sample systems real coords within JF range so quote() prices."""
    from apps.logistics.jumps import clear_graph_cache
    from apps.sde.models import SdeSolarSystem

    SdeSolarSystem.objects.filter(system_id=JITA).update(x=1e15, y=0.0, z=0.0, security=0.9)
    SdeSolarSystem.objects.filter(system_id=OTITOH).update(x=1.4e15, y=0.0, z=0.0, security=0.3)
    clear_graph_cache()


def _hub(name="Jita Hub"):
    from apps.market.models import MarketLocation

    return MarketLocation.objects.create(
        name=name, location_type=MarketLocation.LocationType.STATION,
        system_id=JITA, is_price_reference=True,
    )


def _dest(name="Staging", system_id=OTITOH):
    from apps.market.models import MarketLocation

    return MarketLocation.objects.create(
        name=name, location_type=MarketLocation.LocationType.SYSTEM, system_id=system_id,
    )


def _corp_stockpile(location):
    from apps.stockpile.models import Stockpile

    return Stockpile.objects.create(name="Dest", kind=Stockpile.Kind.CORP, location=location)


def _officer(django_user_model, name="qm"):
    from apps.identity.models import RoleAssignment
    from apps.sso.services import ensure_role
    from core import rbac

    user = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_OFFICER))
    return user


def _member(django_user_model, name="pilot"):
    from apps.identity.models import RoleAssignment
    from apps.sso.services import ensure_role
    from core import rbac

    user = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    return user


# --------------------------------------------------------------------------- #
#  Batch core (WS3)
# --------------------------------------------------------------------------- #
def test_one_open_batch_per_lane(sde):
    hub, dest, other = _hub(), _dest(), _dest("Other", 30000144)
    b1 = freight.open_batch_for_lane(hub, dest, actor=None)
    b2 = freight.open_batch_for_lane(hub, dest, actor=None)
    assert b1.pk == b2.pk  # one OPEN row per lane
    b3 = freight.open_batch_for_lane(hub, other, actor=None)
    assert b3.pk != b1.pk  # a second lane gets its own


@pytest.mark.django_db(transaction=True)
def test_concurrent_open_batch_collapses_to_one(sde):
    hub, dest = _hub(), _dest()
    results, barrier = [], threading.Barrier(2)

    def worker():
        from django.db import connection

        barrier.wait()
        try:
            results.append(freight.open_batch_for_lane(hub, dest, actor=None).pk)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(results)) == 1
    assert FreightBatch.objects.filter(status=FreightBatch.Status.OPEN).count() == 1


def test_line_consolidation_merges_same_type(sde):
    batch = freight.open_batch_for_lane(_hub(), _dest(), actor=None)
    freight.add_line(batch, type_id=TRIT, quantity=100, actor=None)
    freight.add_line(batch, type_id=TRIT, quantity=50, actor=None)
    line = batch.lines.get()
    assert line.quantity == 150  # unique_together consolidates


def test_officer_and_planned_share_coexist(sde):
    _hub()  # the price-reference origin must exist for fan-out
    dest = _dest()
    req = NetRequirement.objects.create(
        type_id=TRIT, location=dest, net_quantity=40, gross_quantity=40,
        suggestion="import", depth=1, sources=[{"kind": "parent", "id": 1, "qty": 40}],
    )
    line = freight.add_requirement_to_batch(req, actor=None)
    batch = line.batch
    freight.add_line(batch, type_id=TRIT, quantity=10, actor=None)  # officer adds 10
    line.refresh_from_db()
    assert line.quantity == 50 and line.planned_quantity == 40
    assert line.officer_quantity == 10  # quantity − planned_quantity


def test_edit_and_remove_only_while_open(sde):
    hub, dest = _hub(), _dest()
    batch = freight.open_batch_for_lane(hub, dest, actor=None)
    line = freight.add_line(batch, type_id=RIFTER, quantity=5, actor=None)
    freight.edit_line(line, quantity=6, unit_purchase_cost=Decimal("5"), actor=None)
    line.refresh_from_db()
    assert line.quantity == 6 and line.cost_source == "typed"

    _routable()
    freight.assign_batch(batch, ship_class="jf", actor=None)
    with pytest.raises(freight.FreightError):
        freight.add_line(batch, type_id=TRIT, quantity=1, actor=None)
    with pytest.raises(freight.FreightError):
        freight.edit_line(line, quantity=9, actor=None)


def test_capacity_fit_uses_packaged_volume_and_flags_over_cap(sde):
    hub, dest = _hub(), _dest()
    batch = freight.open_batch_for_lane(hub, dest, actor=None)
    freight.add_line(batch, type_id=RIFTER, quantity=10, actor=None)
    fit = freight.capacity_fit(batch, ship_class="jf")
    assert fit["volume_m3"] == pytest.approx(2500.0 * 10)  # packaged, not 27,289

    # 200 Rifters = 500,000 m³ > jf_max_m3 (360,000) → over cap, assignment refused.
    freight.add_line(batch, type_id=RIFTER, quantity=190, actor=None)
    over = freight.capacity_fit(batch, ship_class="jf")
    assert over["over_cap"] is True
    _routable()
    with pytest.raises(freight.FreightError):
        freight.assign_batch(batch, ship_class="jf", actor=None)


def test_assign_freezes_quote_and_creates_contract(sde):
    _routable()
    hub, dest = _hub(), _dest()
    batch = freight.open_batch_for_lane(hub, dest, actor=None)
    freight.add_line(batch, type_id=RIFTER, quantity=2, unit_purchase_cost=Decimal("1000000"), actor=None)
    freight.assign_batch(batch, ship_class="jf", actor=None)
    batch.refresh_from_db()
    assert batch.status == FreightBatch.Status.ASSIGNED
    assert batch.freight_cost > 0 and batch.freight_breakdown
    contract = batch.courier_contract
    assert contract is not None and contract.status == CourierContract.Status.OUTSTANDING
    assert "→" in contract.notes and "Freight batch" in contract.notes  # Seam-B note

    # A later rate-card edit never alters the frozen quote.
    from apps.logistics.services import active_rate_card

    card = active_rate_card()
    frozen = batch.freight_cost
    card.jf_base = card.jf_base * 3
    card.save()
    batch.refresh_from_db()
    assert batch.freight_cost == frozen


def test_freight_share_allocator_sums_exactly(sde, priced_sde):
    _routable()
    hub, dest = _hub(), _dest()
    batch = freight.open_batch_for_lane(hub, dest, actor=None)
    # RIFTER: dense-expensive (value-heavy); TRIT×20: bulky-cheap (volume-heavy).
    freight.add_line(batch, type_id=RIFTER, quantity=3, unit_purchase_cost=Decimal("50000000"), actor=None)
    freight.add_line(batch, type_id=TRIT, quantity=20, unit_purchase_cost=Decimal("5"), actor=None)
    freight.assign_batch(batch, ship_class="jf", actor=None)
    batch.refresh_from_db()
    shares = [ln.freight_share for ln in batch.lines.all()]
    assert sum(shares) == batch.freight_cost  # Σ shares == freight_cost exactly
    assert all(s >= 0 for s in shares)


def test_min_reward_batch_splits_by_volume_only(sde):
    _routable()
    hub, dest = _hub(), _dest()
    batch = freight.open_batch_for_lane(hub, dest, actor=None)
    # Freighter, one short warp, tiny collateral → the quote clamps to card.min_reward,
    # so value_pool is 0 and the whole reward splits by packaged-m³ alone.
    freight.add_line(batch, type_id=RIFTER, quantity=1, unit_purchase_cost=Decimal("100"), actor=None)
    freight.add_line(batch, type_id=TRIT, quantity=1, unit_purchase_cost=Decimal("1"), actor=None)
    batch = freight.assign_batch(batch, ship_class="freighter", actor=None)
    assert batch.freight_breakdown.get("min_reward_applied") is True
    assert sum(ln.freight_share for ln in batch.lines.all()) == batch.freight_cost


def test_zero_cost_line_gets_share_but_null_landed(sde):
    _routable()
    hub, dest = _hub(), _dest()
    _corp_stockpile(dest)
    batch = freight.open_batch_for_lane(hub, dest, actor=None)
    line = freight.add_line(batch, type_id=RIFTER, quantity=1, actor=None)  # no cost
    freight.assign_batch(batch, ship_class="jf", actor=None)
    freight.mark_departed(batch, actor=None)
    receipt = freight.receive_line(line, 1, actor=None)
    assert receipt.unit_landed_cost is None  # null cost → null landed, never a fake 0


# --------------------------------------------------------------------------- #
#  Receipt (WS3)
# --------------------------------------------------------------------------- #
def _assigned_in_transit(cost=Decimal("100"), qty=10):
    _routable()
    hub, dest = _hub(), _dest()
    sp = _corp_stockpile(dest)
    batch = freight.open_batch_for_lane(hub, dest, actor=None)
    line = freight.add_line(batch, type_id=TRIT, quantity=qty, unit_purchase_cost=cost, actor=None)
    freight.assign_batch(batch, ship_class="jf", actor=None)
    freight.mark_departed(batch, actor=None)
    return batch, line, sp


def test_receipt_increments_stock_and_landed_cost(sde):
    from apps.stockpile.models import StockpileItem

    batch, line, sp = _assigned_in_transit(cost=Decimal("100"), qty=10)
    line.refresh_from_db()
    share = line.freight_share
    freight.receive_line(line, 10, actor=None)
    item = StockpileItem.objects.get(stockpile=sp, type_id=TRIT)
    assert item.quantity_current == 10  # F() blind increment
    receipt = FreightReceipt.objects.get(line=line)
    assert receipt.unit_landed_cost == (Decimal("100") + share / 10).quantize(Decimal("0.01"))
    batch.refresh_from_db()
    assert batch.status == FreightBatch.Status.CLOSED  # last line fully received


def test_partial_receipt_leaves_remainder_in_transit(sde):
    batch, line, sp = _assigned_in_transit(qty=10)
    freight.receive_line(line, 4, actor=None)
    line.refresh_from_db()
    assert line.quantity_received == 4 and line.remaining == 6
    batch.refresh_from_db()
    assert batch.status == FreightBatch.Status.IN_TRANSIT  # not closed yet
    lots = [lot for lot in freight.in_transit([TRIT]) if lot.kind == "in_transit"]
    assert lots and lots[0].remaining == 6


def test_double_receive_refused_by_remaining_guard(sde):
    batch, line, sp = _assigned_in_transit(qty=10)
    freight.receive_line(line, 10, actor=None)
    with pytest.raises(freight.FreightError):
        freight.receive_line(line, 1, actor=None)  # nothing left


def test_receive_without_stockpile_refused(sde):
    _routable()
    hub, dest = _hub(), _dest()  # no corp stockpile at dest
    batch = freight.open_batch_for_lane(hub, dest, actor=None)
    line = freight.add_line(batch, type_id=TRIT, quantity=5, actor=None)
    freight.assign_batch(batch, ship_class="jf", actor=None)
    freight.mark_departed(batch, actor=None)
    with pytest.raises(freight.FreightError):
        freight.receive_line(line, 5, actor=None)


# --------------------------------------------------------------------------- #
#  State machine (WS3)
# --------------------------------------------------------------------------- #
def test_illegal_transitions_refused(sde):
    hub, dest = _hub(), _dest()
    batch = freight.open_batch_for_lane(hub, dest, actor=None)
    line = freight.add_line(batch, type_id=TRIT, quantity=5, actor=None)
    with pytest.raises(freight.FreightError):
        freight.receive_line(line, 1, actor=None)  # receive on OPEN
    with pytest.raises(freight.FreightError):
        freight.mark_departed(batch, actor=None)  # depart on OPEN


def test_unassign_returns_to_open_and_zeroes_shares(sde):
    _routable()
    hub, dest = _hub(), _dest()
    batch = freight.open_batch_for_lane(hub, dest, actor=None)
    line = freight.add_line(batch, type_id=RIFTER, quantity=2, unit_purchase_cost=Decimal("100"), actor=None)
    batch = freight.assign_batch(batch, ship_class="jf", actor=None)
    contract = batch.courier_contract
    freight.unassign_batch(batch, actor=None)
    batch.refresh_from_db()
    line.refresh_from_db()
    contract.refresh_from_db()
    assert batch.status == FreightBatch.Status.OPEN
    assert batch.freight_cost == 0 and batch.freight_breakdown == {}
    assert line.freight_share == 0 and line.unit_purchase_cost == Decimal("100")  # cost survives
    assert contract.status == CourierContract.Status.CANCELLED


def test_arrive_legal_from_assigned_without_depart(sde):
    _routable()
    hub, dest = _hub(), _dest()
    batch = freight.open_batch_for_lane(hub, dest, actor=None)
    freight.add_line(batch, type_id=TRIT, quantity=5, actor=None)
    freight.assign_batch(batch, ship_class="jf", actor=None)
    freight.mark_arrived(batch, actor=None)  # no depart clicked
    batch.refresh_from_db()
    assert batch.status == FreightBatch.Status.ARRIVED


def test_cancel_releases_nothing_into_stock(sde):
    from apps.stockpile.models import StockpileItem

    batch, line, sp = _assigned_in_transit(qty=5)
    freight.cancel_batch(batch, actor=None)
    batch.refresh_from_db()
    assert batch.status == FreightBatch.Status.CANCELLED
    assert not StockpileItem.objects.filter(stockpile=sp, type_id=TRIT).exists()


# --------------------------------------------------------------------------- #
#  in_transit reader + MRP integration (WS4)
# --------------------------------------------------------------------------- #
def _import_demand(dest, target=1, name="Alpha"):
    """A fit stocked at ``dest`` → run_mrp yields an import row for TRIT there."""
    from apps.doctrines.models import Doctrine, DoctrineFit
    from apps.store.models import FitOffer

    doctrine = Doctrine.objects.create(name=f"D-{name}")
    fit = DoctrineFit.objects.create(doctrine=doctrine, name=name, ship_type_id=RIFTER)
    FitOffer.objects.create(fit=fit, target_stock=target, delivery_location=dest)
    return fit


def _live_trit(dest):
    return NetRequirement.objects.filter(
        type_id=TRIT, location=dest, status__in=("open", "in_progress")
    ).first()


def test_in_transit_feeds_pool_but_never_available(sde, priced_sde):
    from apps.stockpile.availability import available

    _hub()  # the price-reference origin must exist for fan-out
    dest = _dest()
    _import_demand(dest, target=1)
    mrp.run_mrp()
    trit = NetRequirement.objects.filter(type_id=TRIT, location=dest, status__in=("open", "in_progress")).first()
    assert trit is not None and trit.net_quantity > 0

    # Fan it onto a freight batch; the goods are now incoming.
    freight.add_requirement_to_batch(trit, actor=None)
    assert available([TRIT], location=dest)[TRIT] == 0  # rule 7: never available

    mrp.run_mrp()
    trit.refresh_from_db()
    kinds = {r.get("kind") for r in trit.incoming_refs}
    assert "in_transit" in kinds
    # OPEN batch with no ETA → covered but no fabricated date (feasible stays undated).
    assert trit.net_quantity == 0


def test_destination_pinning(sde):
    hub, a, b = _hub(), _dest("A", 30002053), _dest("B", 30000144)
    batch = freight.open_batch_for_lane(hub, a, actor=None)
    freight.add_line(batch, type_id=TRIT, quantity=1000, actor=None)
    lots_b = [lot for lot in freight.in_transit([TRIT], destination=b)]
    assert lots_b == []  # a lot to A never surfaces for B
    lots_a = [lot for lot in freight.in_transit([TRIT], destination=a)]
    assert lots_a and lots_a[0].destination_id == a.pk


def test_feasible_date_is_the_batch_eta(sde, priced_sde):
    _hub()  # the price-reference origin must exist for fan-out
    dest = _dest()
    _import_demand(dest, target=1)
    mrp.run_mrp()
    trit = NetRequirement.objects.filter(type_id=TRIT, location=dest, status__in=("open", "in_progress")).first()
    line = freight.add_requirement_to_batch(trit, actor=None)
    eta = mrp._day(timezone.now() + timedelta(days=9))
    FreightBatch.objects.filter(pk=line.batch_id).update(eta_planned=eta)
    mrp.run_mrp()
    trit.refresh_from_db()
    assert trit.feasible_source == "in_transit"
    assert trit.feasible_at == eta


def test_self_feedback_caps_at_planned_share(sde, priced_sde):
    _hub()  # the price-reference origin must exist for fan-out
    dest = _dest()
    _import_demand(dest, target=1)
    mrp.run_mrp()
    trit = NetRequirement.objects.filter(type_id=TRIT, location=dest, status__in=("open", "in_progress")).first()
    demand = trit.net_quantity
    line = freight.add_requirement_to_batch(trit, actor=None)
    # Officer piles extra units onto the same consolidated line.
    freight.add_line(line.batch, type_id=TRIT, quantity=5000, actor=None)
    mrp.run_mrp()
    trit.refresh_from_db()
    line.refresh_from_db()
    # Own output credited only up to the planned share → line never refreshed to 0,
    # net collapses toward 0, and the officer units are untouched.
    assert line.planned_quantity == demand
    assert line.officer_quantity == 5000
    assert trit.net_quantity == 0


def test_reconcile_unclaimed_refresh_keeps_officer_units(sde, priced_sde):
    _hub()  # the price-reference origin must exist for fan-out
    dest = _dest()
    _import_demand(dest, target=1, name="A")
    fit_b = _import_demand(dest, target=1, name="B")
    mrp.run_mrp()
    trit = _live_trit(dest)
    line = freight.add_requirement_to_batch(trit, actor=None)
    freight.add_line(line.batch, type_id=TRIT, quantity=7, actor=None)  # officer units
    planned0 = FreightBatchLine.objects.get(pk=line.pk).planned_quantity

    # Demand halves (one fit removed) → the unclaimed planned share refreshes; the
    # officer units survive the MRP re-run.
    fit_b.delete()
    mrp.run_mrp()
    line.refresh_from_db()
    assert line.planned_quantity < planned0
    assert line.officer_quantity == 7  # quantity − planned_quantity unchanged


def test_reconcile_claimed_line_flags_diverged(sde, priced_sde):
    _hub()  # the price-reference origin must exist for fan-out
    dest = _dest()
    _import_demand(dest, target=1, name="A")
    fit_b = _import_demand(dest, target=1, name="B")
    mrp.run_mrp()
    trit = _live_trit(dest)
    line = freight.add_requirement_to_batch(trit, actor=None)
    # Officer types a cost → the line is "claimed"; it must never auto-shrink.
    freight.edit_line(line, unit_purchase_cost=Decimal("5"), actor=None)
    qty_before = FreightBatchLine.objects.get(pk=line.pk).quantity

    fit_b.delete()
    mrp.run_mrp()
    line.refresh_from_db()
    trit.refresh_from_db()
    assert line.quantity == qty_before  # claimed → never shrunk
    assert trit.diverged is True


def test_fan_out_idempotent_and_mutually_exclusive_with_task(sde, priced_sde):
    _hub()  # the price-reference origin must exist for fan-out
    dest = _dest()
    _import_demand(dest, target=1)
    mrp.run_mrp()
    trit = NetRequirement.objects.filter(type_id=TRIT, location=dest, status__in=("open", "in_progress")).first()
    line = freight.add_requirement_to_batch(trit, actor=None)
    again = freight.add_requirement_to_batch(trit, actor=None)
    assert again.pk == line.pk  # idempotent per requirement FK
    assert FreightBatchLine.objects.filter(batch=line.batch, type_id=TRIT).count() == 1


def test_covered_destination_received_unsynced_bridge(sde, priced_sde):
    """At an ESI-covered destination the receipt stays a received_unsynced lot until
    the mirror syncs — the requirement never reopens in the window."""
    from django.conf import settings

    from apps.admin_audit.health import record_sync
    from apps.stockpile.models import Asset, AssetLocation

    _hub()  # the price-reference origin must exist for fan-out
    dest = _dest()
    _corp_stockpile(dest)
    # Make the destination ESI-covered: a corp asset at an AssetLocation in its system.
    AssetLocation.objects.create(location_id=70001, system_id=OTITOH)
    Asset.objects.create(owner_type=Asset.Owner.CORPORATION, owner_id=settings.FORCA_HOME_CORP_ID,
                         location_id=70001, type_id=999, quantity=1)

    _import_demand(dest, target=1)
    mrp.run_mrp()
    trit = NetRequirement.objects.filter(type_id=TRIT, location=dest, status__in=("open", "in_progress")).first()
    line = freight.add_requirement_to_batch(trit, actor=None)
    batch = FreightBatch.objects.get(pk=line.batch_id)
    # Member haul (no rate-card cap on the big mineral load), then depart → in transit.
    freight.assign_to_haul_board(batch, actor=None)
    freight.mark_departed(batch, actor=None)

    # Stamp the mirror BEFORE the receipt, then receive → the receipt is "unsynced".
    record_sync("corp_assets", character="x")
    line.refresh_from_db()
    receipt = freight.receive_line(line, line.quantity, actor=None)
    FreightReceipt.objects.filter(pk=receipt.pk).update(
        created_at=timezone.now() + timedelta(minutes=5)  # strictly after the sync stamp
    )

    lots = freight.in_transit([TRIT])
    assert any(lot.kind == "received_unsynced" for lot in lots)
    mrp.run_mrp()
    trit.refresh_from_db()
    assert trit.net_quantity == 0  # requirement does NOT reopen in the sync window

    # Simulate the sync catching up → the bridge lot ages out.
    record_sync("corp_assets", character="x")
    FreightReceipt.objects.filter(pk=receipt.pk).update(created_at=timezone.now() - timedelta(minutes=5))
    lots2 = freight.in_transit([TRIT])
    assert not any(lot.kind == "received_unsynced" for lot in lots2)


# --------------------------------------------------------------------------- #
#  Sweep (WS6)
# --------------------------------------------------------------------------- #
def test_sweep_disarmed_is_noop(sde):
    from apps.logistics.tasks import sweep_freight_batches

    assert FreightConfig.active().eta_sweep_enabled is False
    assert sweep_freight_batches() == {"arrived": 0, "late": 0}


def _arm_sweep():
    cfg = FreightConfig.active()
    cfg.eta_sweep_enabled = True
    cfg.save()
    return cfg


def test_sweep_verified_contract_arrives_once(sde):
    from apps.logistics.tasks import sweep_freight_batches

    _routable()
    hub, dest = _hub(), _dest()
    batch = freight.open_batch_for_lane(hub, dest, actor=None)
    freight.add_line(batch, type_id=TRIT, quantity=5, actor=None)
    batch = freight.assign_batch(batch, ship_class="jf", actor=None)  # ASSIGNED, no depart
    batch.courier_contract.verification_state = CourierContract.Verification.VERIFIED
    batch.courier_contract.save(update_fields=["verification_state"])
    _arm_sweep()
    assert sweep_freight_batches()["arrived"] == 1
    batch.refresh_from_db()
    assert batch.status == FreightBatch.Status.ARRIVED
    assert sweep_freight_batches()["arrived"] == 0  # idempotent


def test_sweep_flags_late_once_and_refires_after_eta_change(sde):
    from apps.logistics.tasks import sweep_freight_batches

    _routable()
    hub, dest = _hub(), _dest()
    batch = freight.open_batch_for_lane(hub, dest, actor=None)
    freight.add_line(batch, type_id=TRIT, quantity=5, actor=None)
    freight.assign_batch(batch, ship_class="jf", actor=None)
    freight.mark_departed(batch, actor=None)
    past = timezone.now() - timedelta(days=1)
    FreightBatch.objects.filter(pk=batch.pk).update(eta_planned=past)
    _arm_sweep()
    assert sweep_freight_batches()["late"] == 1
    assert sweep_freight_batches()["late"] == 0  # flagged once per ETA
    # A new (still-past) ETA clears the flag → can flag again.
    freight.update_eta(batch, eta=timezone.now() - timedelta(hours=12), actor=None)
    assert sweep_freight_batches()["late"] == 1


# --------------------------------------------------------------------------- #
#  Views / permissions (WS5)
# --------------------------------------------------------------------------- #
def test_member_forbidden_officer_ok(client, django_user_model, sde):
    client.force_login(_member(django_user_model))
    assert client.get("/freight/pipeline/").status_code == 403
    assert client.post("/freight/pipeline/open/").status_code == 403

    client.force_login(_officer(django_user_model))
    assert client.get("/freight/pipeline/").status_code == 200
    assert client.get("/freight/pipeline/?tab=intransit").status_code == 200


def test_csv_machine_keys(client, django_user_model, sde):
    client.force_login(_officer(django_user_model))
    head = client.get("/freight/pipeline/?export=csv").content.decode().splitlines()[0]
    for col in ("batch_id", "origin", "destination", "type_id", "quantity",
                "planned_quantity", "freight_share", "eta_planned"):
        assert col in head


def test_detail_and_audit(client, django_user_model, sde):
    from apps.admin_audit.models import AuditLog

    user = _officer(django_user_model)
    client.force_login(user)
    batch = freight.open_batch_for_lane(_hub(), _dest(), actor=user)
    resp = client.post(f"/freight/pipeline/{batch.pk}/line/",
                       {"type_id": str(TRIT), "quantity": "10"})
    assert resp.status_code == 302
    assert batch.lines.filter(type_id=TRIT).exists()
    assert AuditLog.objects.filter(action="freight.line_add").exists()
    assert client.get(f"/freight/pipeline/{batch.pk}/").status_code == 200


def test_material_plan_freight_action_and_chip(client, django_user_model, sde, priced_sde):
    user = _officer(django_user_model)
    client.force_login(user)
    _hub()  # the price-reference origin must exist for fan-out
    dest = _dest()
    _import_demand(dest, target=1)
    mrp.run_mrp()
    trit = NetRequirement.objects.filter(type_id=TRIT, location=dest, status__in=("open", "in_progress")).first()
    resp = client.post(f"/industry/mrp/req/{trit.pk}/action/", {"action": "freight"})
    assert resp.status_code == 302
    trit.refresh_from_db()
    assert trit.freight_line_id is not None
    # A BUY task now refuses (mutually exclusive with the freight batch).
    client.post(f"/industry/mrp/req/{trit.pk}/action/", {"action": "buy_task"})
    trit.refresh_from_db()
    assert trit.task_id is None
