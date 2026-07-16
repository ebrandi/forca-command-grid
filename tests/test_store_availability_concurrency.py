"""Genuine transactional concurrency proofs for Shipyard availability (SHIP-1).

These run with ``transaction=True`` (real commits, no test-wide rollback) and
race real threads on separate Postgres connections, proving the row-lock design
rather than assuming it: two buyers cannot oversell the final ship, delivery and
cancellation cannot double-spend a reservation, duplicate background passes are
no-ops, and concurrent workers collapse onto one supply need.
"""
from __future__ import annotations

import threading
from decimal import Decimal

import pytest
from django.db import connection

from apps.doctrines.models import Doctrine, DoctrineFit
from apps.identity.models import RoleAssignment
from apps.market.models import MarketLocation, MarketPrice
from apps.sde.models import SdeCategory, SdeGroup, SdeType
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from apps.store import inventory as inv
from apps.store.availability import availability_for_fit
from apps.store.models import (
    Audience,
    FitReservation,
    FitStock,
    FitSupplyNeed,
    ShipyardPolicy,
    StoreOrder,
)
from apps.store.services import (
    active_config,
    invalidate_audience_cache,
    place_fit_order,
    transition_order,
)
from core import rbac


@pytest.fixture
def rig(django_user_model, db):
    """Fit + location + two member buyers, committed for cross-connection reads."""
    ship_cat = SdeCategory.objects.create(category_id=6, name="Ship")
    cruiser = SdeGroup.objects.create(group_id=26, category=ship_cat, name="Cruiser")
    SdeType.objects.create(type_id=16227, group=cruiser, name="Ferox", volume=101000.0)
    MarketPrice.objects.create(
        type_id=16227, profile=MarketPrice.Profile.JITA_SELL, sell_min=Decimal("39000000")
    )
    doctrine = Doctrine.objects.create(name="Ferox Fleet")
    fit = DoctrineFit.objects.create(
        doctrine=doctrine, name="Ferox Railgun", ship_type_id=16227, modules=[]
    )
    home = MarketLocation.objects.create(
        name="Staging Keepstar", location_type=MarketLocation.LocationType.STRUCTURE,
        system_id=30000142,
    )
    policy = ShipyardPolicy.active()
    policy.default_location = home
    policy.save(update_fields=["default_location"])
    cfg = active_config()
    cfg.audience = Audience.CORP
    cfg.save(update_fields=["audience"])
    invalidate_audience_cache()

    buyers = []
    for i, name in enumerate(["Racer One", "Racer Two", "Racer Three"], start=1):
        user = django_user_model.objects.create(username=f"eve:88{i:02d}", first_name=name)
        RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
        EveCharacter.objects.create(
            character_id=8800 + i, user=user, name=name, is_main=True, is_corp_member=True
        )
        buyers.append(user)
    return {"fit": fit, "home": home, "policy": policy, "buyers": buyers}


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
def test_two_buyers_cannot_oversell_the_final_ship(rig):
    fit, home, buyers = rig["fit"], rig["home"], rig["buyers"]
    inv.receive_stock(fit, location=home, quantity=1, actor=None)
    results: dict[str, object] = {}

    def buy(user):
        def _run():
            results[user.username] = place_fit_order(fit=fit, quantity=1, buyer=user)
        return _run

    _race([buy(buyers[0]), buy(buyers[1])])

    placements = list(results.values())
    winners = [p for p in placements if p.order is not None and p.order.quantity_reserved == 1]
    losers = [p for p in placements if p.order is None]
    # Exactly one buyer got the ship; the other was paused on the honest
    # backorder confirmation (backorders are on by default), never oversold.
    assert len(winners) == 1 and len(losers) == 1
    assert losers[0].needs_confirm and losers[0].atp == 0
    active = FitReservation.objects.filter(status=FitReservation.Status.ACTIVE)
    assert sum(r.quantity for r in active) == 1
    assert FitStock.objects.get().quantity_on_hand == 1
    assert availability_for_fit(fit).atp == 0


@pytest.mark.django_db(transaction=True)
def test_three_way_race_over_two_ships(rig):
    fit, home, buyers = rig["fit"], rig["home"], rig["buyers"]
    inv.receive_stock(fit, location=home, quantity=2, actor=None)

    def buy(user):
        def _run():
            place_fit_order(fit=fit, quantity=1, buyer=user)
        return _run

    _race([buy(b) for b in buyers])
    reserved = FitReservation.objects.filter(status=FitReservation.Status.ACTIVE)
    assert sum(r.quantity for r in reserved) == 2  # never 3
    assert availability_for_fit(fit).atp == 0


@pytest.mark.django_db(transaction=True)
def test_concurrent_cancel_and_deliver_cannot_double_spend(rig):
    """Whichever transition wins, the reservation ends in exactly ONE terminal
    state and stock is decremented at most once."""
    fit, home, buyers = rig["fit"], rig["home"], rig["buyers"]
    inv.receive_stock(fit, location=home, quantity=1, actor=None)
    order = place_fit_order(fit=fit, quantity=1, buyer=buyers[0]).order

    def deliver():
        transition_order(StoreOrder.objects.get(pk=order.pk),
                         StoreOrder.Status.DELIVERED, actor=buyers[1])

    def cancel():
        transition_order(StoreOrder.objects.get(pk=order.pk),
                         StoreOrder.Status.CANCELLED, actor=buyers[0])

    _race([deliver, cancel])

    res = FitReservation.objects.get()
    assert res.status in (FitReservation.Status.CONSUMED, FitReservation.Status.RELEASED)
    stock = FitStock.objects.get()
    if res.status == FitReservation.Status.CONSUMED:
        assert stock.quantity_on_hand == 0
    else:
        assert stock.quantity_on_hand == 1
    # Never negative, never double-decremented — the ledger agrees with the balance.
    entries = stock.entries.order_by("id")
    assert entries.last().balance_after == stock.quantity_on_hand


@pytest.mark.django_db(transaction=True)
def test_concurrent_receipt_allocation_and_new_order(rig):
    """A restock allocating to a waiting backorder racing a fresh buyer must
    never promise the same unit twice."""
    fit, home, buyers = rig["fit"], rig["home"], rig["buyers"]
    waiting = place_fit_order(fit=fit, quantity=1, buyer=buyers[0], acknowledged=True).order
    assert waiting.quantity_backordered == 1

    def restock():
        inv.receive_stock(fit, location=home, quantity=1, actor=None)

    def buy():
        place_fit_order(fit=fit, quantity=1, buyer=buyers[1])

    _race([restock, buy])
    active = FitReservation.objects.filter(status=FitReservation.Status.ACTIVE)
    assert sum(r.quantity for r in active) <= 1  # one unit exists, one promise max
    assert availability_for_fit(fit).atp >= 0


@pytest.mark.django_db(transaction=True)
def test_duplicate_expiry_runs_are_idempotent(rig):
    from datetime import timedelta

    from django.utils import timezone

    from apps.store.tasks import expire_reservations

    fit, home, policy, buyers = rig["fit"], rig["home"], rig["policy"], rig["buyers"]
    policy.reservation_expiry_days = 1
    policy.save(update_fields=["reservation_expiry_days"])
    inv.receive_stock(fit, location=home, quantity=1, actor=None)
    order = place_fit_order(fit=fit, quantity=1, buyer=buyers[0]).order
    StoreOrder.objects.filter(pk=order.pk).update(
        created_at=timezone.now() - timedelta(days=2)
    )

    counts: list[int] = []

    def run():
        counts.append(expire_reservations())

    _race([run, run])
    assert sum(counts) == 1  # one of the racing passes did the work, once
    assert FitReservation.objects.filter(status=FitReservation.Status.EXPIRED).count() == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_workers_collapse_onto_one_supply_need(rig):
    fit, home, buyers = rig["fit"], rig["home"], rig["buyers"]

    def order_backorder(user):
        def _run():
            place_fit_order(fit=fit, quantity=2, buyer=user, acknowledged=True)
        return _run

    _race([order_backorder(buyers[0]), order_backorder(buyers[1]), order_backorder(buyers[2])])
    live = FitSupplyNeed.objects.filter(
        status__in=(FitSupplyNeed.Status.OPEN, FitSupplyNeed.Status.IN_PROGRESS)
    )
    assert live.count() == 1  # the partial unique constraint collapsed the race
    assert live.get().quantity_required == 6
