"""Shipyard availability control (SHIP-1): ATP, reservations, backorders, policy.

Covers the authoritative availability service, the race-guarded placement path,
the reservation lifecycle, the officer inventory console, per-fit overrides vs
the corp-wide policy, the supply-need consolidation, and the DB constraints.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.doctrines.models import Doctrine, DoctrineFit
from apps.identity.models import RoleAssignment
from apps.market.models import MarketLocation, MarketPrice
from apps.sde.models import SdeCategory, SdeGroup, SdeType
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from apps.store import inventory as inv
from apps.store.availability import (
    availability_for_fit,
    availability_for_fits,
    manifest_hash,
)
from apps.store.models import (
    Audience,
    FitOffer,
    FitReservation,
    FitStock,
    FitStockEntry,
    FitSupplyNeed,
    FitWaitlistEntry,
    OfferState,
    OrderAvailability,
    ShipyardPolicy,
    StoreOrder,
)
from apps.store.services import (
    active_config,
    invalidate_audience_cache,
    place_fit_order,
    transition_order,
)
from apps.store.supply import recompute_supply_need
from core import rbac


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def shipyard(db):
    """A priced doctrine fit, a delivery location, and the default policy."""
    ship_cat = SdeCategory.objects.create(category_id=6, name="Ship")
    mat_cat = SdeCategory.objects.create(category_id=4, name="Material")
    cruiser = SdeGroup.objects.create(group_id=26, category=ship_cat, name="Cruiser")
    modgrp = SdeGroup.objects.create(group_id=60, category=mat_cat, name="Module")
    SdeType.objects.create(type_id=16227, group=cruiser, name="Ferox", volume=101000.0)
    SdeType.objects.create(type_id=1234, group=modgrp, name="Heavy Neutron Blaster", volume=5.0)
    for tid, price in [(16227, "39000000"), (1234, "1000000")]:
        MarketPrice.objects.create(
            type_id=tid, profile=MarketPrice.Profile.JITA_SELL, sell_min=Decimal(price)
        )
    doctrine = Doctrine.objects.create(name="Ferox Fleet")
    fit = DoctrineFit.objects.create(
        doctrine=doctrine, name="Ferox Railgun", ship_type_id=16227,
        modules=[{"type_id": 1234, "quantity": 7, "slot": "high"}],
    )
    home = MarketLocation.objects.create(
        name="Staging Keepstar", location_type=MarketLocation.LocationType.STRUCTURE,
        system_id=30000142,
    )
    away = MarketLocation.objects.create(
        name="Forward Fortizar", location_type=MarketLocation.LocationType.STRUCTURE,
        system_id=30000144,
    )
    policy = ShipyardPolicy.active()
    policy.default_location = home
    policy.save(update_fields=["default_location"])
    cfg = active_config()
    cfg.audience = Audience.CORP
    cfg.save(update_fields=["audience"])
    invalidate_audience_cache()
    return {"fit": fit, "doctrine": doctrine, "home": home, "away": away, "policy": policy}


def _member(django_user_model, char_id: int, name: str):
    user = django_user_model.objects.create(username=f"eve:{char_id}", first_name=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(
        character_id=char_id, user=user, name=name, is_main=True, is_corp_member=True
    )
    return user


def _officer(django_user_model, char_id: int, name: str):
    user = _member(django_user_model, char_id, name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_OFFICER))
    return user


def _stock(fit, location, qty: int, actor=None) -> FitStock:
    """Direct receipt with auto-allocation off (tests arm it explicitly)."""
    policy = ShipyardPolicy.active()
    saved = policy.auto_allocate_receipts
    policy.auto_allocate_receipts = False
    policy.save(update_fields=["auto_allocate_receipts"])
    try:
        return inv.receive_stock(fit, location=location, quantity=qty, actor=actor).stock
    finally:
        policy.auto_allocate_receipts = saved
        policy.save(update_fields=["auto_allocate_receipts"])


# --- manifest hash / fit revisions -------------------------------------------


@pytest.mark.django_db
def test_manifest_hash_is_stable_under_module_reordering(shipyard):
    fit = shipyard["fit"]
    h1 = manifest_hash(fit)
    fit.modules = list(reversed(fit.modules))
    assert manifest_hash(fit) == h1


@pytest.mark.django_db
def test_manifest_hash_changes_when_the_fit_changes(shipyard):
    fit = shipyard["fit"]
    h1 = manifest_hash(fit)
    fit.modules = [{"type_id": 1234, "quantity": 6, "slot": "high"}]
    assert manifest_hash(fit) != h1


@pytest.mark.django_db
def test_fit_edit_strands_stock_until_revalidated(shipyard, django_user_model):
    """A stocked ship built for an older revision must not silently satisfy the
    new fit — and revalidation moves only the free units back into play."""
    fit, home = shipyard["fit"], shipyard["home"]
    _stock(fit, home, 5)
    assert availability_for_fit(fit).atp == 5

    fit.modules = [{"type_id": 1234, "quantity": 6, "slot": "high"}]
    fit.save(update_fields=["modules"])
    a = availability_for_fit(fit)
    assert a.atp == 0
    assert a.stale_on_hand == 5
    assert a.state in (OfferState.BACKORDER, OfferState.UNAVAILABLE)

    officer = _officer(django_user_model, 9001, "Quartermaster")
    stale = FitStock.objects.get(doctrine_fit=fit)
    moved = inv.revalidate_stock(stale, actor=officer, reason="checked in hangar")
    assert moved == 5
    a = availability_for_fit(fit)
    assert a.atp == 5 and a.stale_on_hand == 0
    kinds = list(
        FitStockEntry.objects.filter(kind=FitStockEntry.Kind.REVALIDATION)
        .values_list("delta", flat=True)
    )
    assert sorted(kinds) == [-5, 5]


# --- availability states ------------------------------------------------------


@pytest.mark.django_db
def test_states_follow_stock_thresholds_and_policy(shipyard):
    fit, home, policy = shipyard["fit"], shipyard["home"], shipyard["policy"]
    # Nothing stocked, backorders on (default) → backorder with an ETA.
    a = availability_for_fit(fit)
    assert a.state == OfferState.BACKORDER
    assert a.eta is not None and a.lead_days == policy.default_lead_days

    _stock(fit, home, 2)  # at the limited threshold (default 2)
    assert availability_for_fit(fit).state == OfferState.LIMITED

    _stock(fit, home, 3)  # 5 > threshold
    assert availability_for_fit(fit).state == OfferState.READY

    policy.backorders_enabled = False
    policy.save(update_fields=["backorders_enabled"])
    FitStock.objects.all().delete()
    assert availability_for_fit(fit).state == OfferState.UNAVAILABLE


@pytest.mark.django_db
def test_not_offered_wins_over_everything(shipyard):
    fit, home = shipyard["fit"], shipyard["home"]
    _stock(fit, home, 10)
    FitOffer.objects.create(fit=fit, is_offered=False)
    a = availability_for_fit(fit)
    assert a.state == OfferState.NOT_OFFERED
    assert not a.can_order and a.max_orderable == 0


@pytest.mark.django_db
def test_availability_is_location_aware(shipyard):
    """Stock at another location never counts toward the configured location."""
    fit, away = shipyard["fit"], shipyard["away"]
    _stock(fit, away, 4)  # config points at `home`
    a = availability_for_fit(fit)
    assert a.on_hand == 0 and a.atp == 0

    FitOffer.objects.create(fit=fit, delivery_location=away)  # per-fit override
    a = availability_for_fit(fit)
    assert a.on_hand == 4 and a.location == away


@pytest.mark.django_db
def test_per_fit_overrides_inherit_when_null(shipyard):
    fit, policy = shipyard["fit"], shipyard["policy"]
    offer = FitOffer.objects.create(fit=fit)  # everything NULL → inherit
    a = availability_for_fit(fit)
    assert a.lead_days == policy.default_lead_days
    assert a.max_per_order == policy.max_order_quantity
    assert a.backorders_allowed is True

    offer.lead_days = 21
    offer.max_per_order = 3
    offer.backorders_allowed = False
    offer.save()
    a = availability_for_fit(fit)
    assert a.lead_days == 21 and a.max_per_order == 3 and a.backorders_allowed is False


@pytest.mark.django_db
def test_incoming_supply_is_never_counted_as_atp(shipyard):
    fit, home = shipyard["fit"], shipyard["home"]
    from apps.erp.models import BuildJob

    job = BuildJob.objects.create(output_type_id=fit.ship_type_id, quantity=5)
    FitSupplyNeed.objects.create(
        doctrine_fit=fit, location=home, quantity_required=5, build_job=job,
    )
    a = availability_for_fit(fit)
    assert a.incoming == 5
    assert a.atp == 0  # incoming informs, never promises
    assert a.state == OfferState.BACKORDER


@pytest.mark.django_db
def test_batched_availability_uses_constant_queries(shipyard, django_assert_max_num_queries):
    """The Shipyard page derives availability for ALL its cards in a handful of
    queries — never one (or more) per card."""
    doctrine = shipyard["doctrine"]
    fits = [shipyard["fit"]] + [
        DoctrineFit.objects.create(
            doctrine=doctrine, name=f"Variant {i}", ship_type_id=16227,
            modules=[{"type_id": 1234, "quantity": i}],
        )
        for i in range(1, 30)
    ]
    ShipyardPolicy.active()  # warm the singleton row
    with django_assert_max_num_queries(6):
        result = availability_for_fits(fits)
    assert len(result) == 30


# --- placement: split, freeze, caps -------------------------------------------


@pytest.mark.django_db
def test_fully_stocked_order_reserves_and_freezes(shipyard, django_user_model):
    fit, home = shipyard["fit"], shipyard["home"]
    _stock(fit, home, 5)
    buyer = _member(django_user_model, 9100, "Buyer")
    p = place_fit_order(fit=fit, quantity=2, buyer=buyer)
    order = p.order
    assert order is not None
    assert order.availability_state == OrderAvailability.READY
    assert order.quantity_reserved == 2 and order.quantity_backordered == 0
    assert order.delivery_location == home
    assert order.manifest_hash == manifest_hash(fit)
    assert order.promised_date is None  # ready stock carries no lead-time promise
    res = order.fit_reservations.get()
    assert res.quantity == 2 and res.status == FitReservation.Status.ACTIVE
    assert availability_for_fit(fit).atp == 3


@pytest.mark.django_db
def test_split_order_needs_acknowledgement_then_freezes_split(shipyard, django_user_model):
    fit, home = shipyard["fit"], shipyard["home"]
    _stock(fit, home, 2)
    buyer = _member(django_user_model, 9101, "Buyer")

    first = place_fit_order(fit=fit, quantity=5, buyer=buyer)
    assert first.order is None and first.needs_confirm
    assert first.ready_quantity == 2 and first.backordered_quantity == 3
    assert FitReservation.objects.count() == 0  # nothing held before confirmation

    p = place_fit_order(fit=fit, quantity=5, buyer=buyer, acknowledged=True)
    order = p.order
    assert order is not None
    assert order.availability_state == OrderAvailability.PARTIAL
    assert order.quantity_reserved == 2 and order.quantity_backordered == 3
    assert order.backorder_acknowledged is True
    assert order.promised_date is not None and order.current_eta == order.promised_date
    assert order.lead_days_assumed == shipyard["policy"].default_lead_days
    assert availability_for_fit(fit).atp == 0


@pytest.mark.django_db
def test_backorders_disabled_rejects_and_offers_ready_stock(shipyard, django_user_model):
    fit, home, policy = shipyard["fit"], shipyard["home"], shipyard["policy"]
    policy.backorders_enabled = False
    policy.save(update_fields=["backorders_enabled"])
    _stock(fit, home, 1)
    buyer = _member(django_user_model, 9102, "Buyer")

    p = place_fit_order(fit=fit, quantity=3, buyer=buyer, acknowledged=True)
    assert p.order is None and p.needs_confirm and p.atp == 1  # offer the 1 ready

    p = place_fit_order(fit=fit, quantity=1, buyer=buyer)
    assert p.order is not None and p.order.quantity_reserved == 1

    p = place_fit_order(fit=fit, quantity=1, buyer=buyer, acknowledged=True)
    assert p.order is None and not p.needs_confirm  # out of stock, backorders closed


@pytest.mark.django_db
def test_partial_disabled_forces_a_choice(shipyard, django_user_model):
    fit, home, policy = shipyard["fit"], shipyard["home"], shipyard["policy"]
    policy.allow_partial_fulfilment = False
    policy.save(update_fields=["allow_partial_fulfilment"])
    _stock(fit, home, 2)
    buyer = _member(django_user_model, 9103, "Buyer")

    p = place_fit_order(fit=fit, quantity=5, buyer=buyer, acknowledged=True)
    assert p.order is None and p.needs_confirm  # mixing is not allowed

    p = place_fit_order(fit=fit, quantity=5, buyer=buyer, acknowledged=True,
                        force_backorder=True)
    assert p.order is not None
    assert p.order.quantity_reserved == 0 and p.order.quantity_backordered == 5
    assert p.order.availability_state == OrderAvailability.BACKORDER
    assert availability_for_fit(fit).atp == 2  # the stock stays for someone else


@pytest.mark.django_db
def test_quantity_caps_are_enforced_server_side(shipyard, django_user_model):
    fit, home = shipyard["fit"], shipyard["home"]
    buyer = _member(django_user_model, 9104, "Buyer")

    p = place_fit_order(fit=fit, quantity=11, buyer=buyer)  # policy default max 10
    assert p.order is None and p.error

    FitOffer.objects.create(fit=fit, max_per_order=2, max_backorder_quantity=1)
    p = place_fit_order(fit=fit, quantity=3, buyer=buyer, acknowledged=True)
    assert p.order is None and p.error  # over max_per_order

    _stock(fit, home, 1)
    p = place_fit_order(fit=fit, quantity=2, buyer=buyer, acknowledged=True)
    assert p.order is not None  # 1 ready + 1 backorder ≤ caps

    p = place_fit_order(fit=fit, quantity=2, buyer=buyer, acknowledged=True)
    assert p.order is None and p.error  # 0 ready + 2 backorder > max_backorder 1


@pytest.mark.django_db
def test_invalid_quantities_rejected(shipyard, django_user_model):
    fit = shipyard["fit"]
    buyer = _member(django_user_model, 9105, "Buyer")
    for bad in (0, -3, "nonsense", None):
        p = place_fit_order(fit=fit, quantity=bad, buyer=buyer)
        assert p.order is None and p.error


# --- lifecycle: cancel releases, deliver consumes ------------------------------


@pytest.mark.django_db
def test_cancellation_releases_reservations(shipyard, django_user_model):
    fit, home = shipyard["fit"], shipyard["home"]
    _stock(fit, home, 3)
    buyer = _member(django_user_model, 9200, "Buyer")
    order = place_fit_order(fit=fit, quantity=2, buyer=buyer).order
    assert availability_for_fit(fit).atp == 1

    transition_order(order, StoreOrder.Status.CANCELLED, actor=buyer)
    assert availability_for_fit(fit).atp == 3
    res = order.fit_reservations.get()
    assert res.status == FitReservation.Status.RELEASED and res.released_at is not None
    # Stock never moved — release is not a balance change.
    assert FitStock.objects.get().quantity_on_hand == 3


@pytest.mark.django_db
def test_delivery_consumes_reservations_exactly_once(shipyard, django_user_model):
    fit, home = shipyard["fit"], shipyard["home"]
    _stock(fit, home, 3)
    buyer = _member(django_user_model, 9201, "Buyer")
    fulfiller = _member(django_user_model, 9202, "Builder")
    order = place_fit_order(fit=fit, quantity=2, buyer=buyer).order

    transition_order(order, StoreOrder.Status.DELIVERED, actor=fulfiller)
    stock = FitStock.objects.get()
    assert stock.quantity_on_hand == 1
    res = order.fit_reservations.get()
    assert res.status == FitReservation.Status.CONSUMED and res.consumed_at is not None
    assert order.delivered_at is not None
    entry = FitStockEntry.objects.get(kind=FitStockEntry.Kind.CONSUMED)
    assert entry.delta == -2 and entry.order_id == order.pk

    # A second consume (double-click, replay, crash retry) is a no-op.
    consumed_again = inv.consume_order_reservations(order, actor=fulfiller)
    assert consumed_again == 0
    assert FitStock.objects.get().quantity_on_hand == 1


@pytest.mark.django_db
def test_cancel_after_delivery_cannot_resurrect_stock(shipyard, django_user_model):
    fit, home = shipyard["fit"], shipyard["home"]
    _stock(fit, home, 1)
    buyer = _member(django_user_model, 9203, "Buyer")
    order = place_fit_order(fit=fit, quantity=1, buyer=buyer).order
    transition_order(order, StoreOrder.Status.DELIVERED, actor=buyer)
    # The view refuses cancel-after-delivery; even a direct release is a no-op
    # because the reservation is already CONSUMED.
    released = inv.release_order_reservations(order)
    assert released == 0
    assert FitStock.objects.get().quantity_on_hand == 0


# --- manual adjustments ---------------------------------------------------------


@pytest.mark.django_db
def test_adjustment_requires_reason_and_respects_reservations(shipyard, django_user_model):
    fit, home = shipyard["fit"], shipyard["home"]
    stock = _stock(fit, home, 5)
    officer = _officer(django_user_model, 9300, "Quartermaster")
    buyer = _member(django_user_model, 9301, "Buyer")
    place_fit_order(fit=fit, quantity=3, buyer=buyer)

    with pytest.raises(ValueError):
        inv.adjust_stock(stock, corrected_balance=4, actor=officer, reason="  ")
    with pytest.raises(ValueError, match="reserved"):
        inv.adjust_stock(stock, corrected_balance=2, actor=officer, reason="lost one")
    entry = inv.adjust_stock(stock, corrected_balance=4, actor=officer, reason="hangar count")
    assert entry.delta == -1 and entry.reason == "hangar count"
    stock.refresh_from_db()
    assert stock.quantity_on_hand == 4 and stock.last_reconciled_at is not None
    assert availability_for_fit(fit).atp == 1


# --- DB constraints --------------------------------------------------------------


@pytest.mark.django_db
def test_on_hand_can_never_go_negative(shipyard):
    stock = _stock(shipyard["fit"], shipyard["home"], 1)
    stock.quantity_on_hand = -1
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            stock.save(update_fields=["quantity_on_hand"])


@pytest.mark.django_db
def test_reservation_quantity_must_be_positive(shipyard, django_user_model):
    fit, home = shipyard["fit"], shipyard["home"]
    stock = _stock(fit, home, 1)
    buyer = _member(django_user_model, 9302, "Buyer")
    order = place_fit_order(fit=fit, quantity=1, buyer=buyer).order
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            FitReservation.objects.create(order=order, stock=stock, quantity=0)


@pytest.mark.django_db
def test_only_one_live_supply_need_per_fit_and_location(shipyard):
    fit, home = shipyard["fit"], shipyard["home"]
    FitSupplyNeed.objects.create(doctrine_fit=fit, location=home, quantity_required=1)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            FitSupplyNeed.objects.create(doctrine_fit=fit, location=home, quantity_required=2)


# --- receipts, allocation, waitlist ----------------------------------------------


@pytest.mark.django_db
def test_receipt_allocates_to_backorders_oldest_first(shipyard, django_user_model):
    fit, home = shipyard["fit"], shipyard["home"]
    early = _member(django_user_model, 9400, "Early Bird")
    late = _member(django_user_model, 9401, "Late Comer")
    o1 = place_fit_order(fit=fit, quantity=2, buyer=early, acknowledged=True).order
    o2 = place_fit_order(fit=fit, quantity=2, buyer=late, acknowledged=True).order
    assert o1.quantity_backordered == 2 and o2.quantity_backordered == 2

    result = inv.receive_stock(fit, location=home, quantity=3, actor=None)
    got = {a.order.pk: a.quantity for a in result.allocations}
    assert got == {o1.pk: 2, o2.pk: 1}  # oldest first, remainder to the next
    assert availability_for_fit(fit).atp == 0

    # Idempotent: a duplicate allocation pass finds nothing left to give.
    with transaction.atomic():
        again = inv.allocate_backorders(fit, location=home)
    assert again == []


@pytest.mark.django_db
def test_supply_need_consolidates_and_closes(shipyard, django_user_model):
    fit, home = shipyard["fit"], shipyard["home"]
    a_ = _member(django_user_model, 9402, "A")
    b_ = _member(django_user_model, 9403, "B")
    o1 = place_fit_order(fit=fit, quantity=2, buyer=a_, acknowledged=True).order
    place_fit_order(fit=fit, quantity=3, buyer=b_, acknowledged=True).order

    need = FitSupplyNeed.objects.get()  # ONE consolidated need, not two
    assert need.quantity_required == 5

    transition_order(o1, StoreOrder.Status.CANCELLED, actor=a_)
    need.refresh_from_db()
    assert need.quantity_required == 3

    inv.receive_stock(fit, location=home, quantity=3, actor=None)
    recompute_supply_need(fit, location=home)
    need.refresh_from_db()
    assert need.status == FitSupplyNeed.Status.DONE and need.quantity_required == 0


@pytest.mark.django_db
def test_vehicle_creation_is_idempotent_and_wires_the_links(shipyard, django_user_model):
    from apps.erp.models import BuildJob
    from apps.industry.models import IndustryProject

    fit, home = shipyard["fit"], shipyard["home"]
    officer = _officer(django_user_model, 9404, "Quartermaster")
    buyer = _member(django_user_model, 9405, "Buyer")
    place_fit_order(fit=fit, quantity=4, buyer=buyer, acknowledged=True)
    need = FitSupplyNeed.objects.get()

    from apps.store.supply import (
        create_build_job_for_need,
        create_industry_project_for_need,
        create_task_for_need,
    )

    project = create_industry_project_for_need(need, actor=officer)
    assert create_industry_project_for_need(need, actor=officer).pk == project.pk
    assert project.source == IndustryProject.Source.STORE_ORDER
    assert project.store_order_id is not None
    assert project.items.get().quantity == 4

    job = create_build_job_for_need(need, actor=officer)
    assert create_build_job_for_need(need, actor=officer).pk == job.pk
    assert job.note_key == "job.shipyard_restock"
    assert job.status == BuildJob.Status.QUEUED

    task = create_task_for_need(need, actor=officer)
    assert create_task_for_need(need, actor=officer).pk == task.pk
    need.refresh_from_db()
    assert need.status == FitSupplyNeed.Status.IN_PROGRESS


# --- reservation expiry -----------------------------------------------------------


@pytest.mark.django_db
def test_expiry_task_releases_unclaimed_holds(shipyard, django_user_model):
    from apps.store.tasks import expire_reservations

    fit, home, policy = shipyard["fit"], shipyard["home"], shipyard["policy"]
    _stock(fit, home, 2)
    buyer = _member(django_user_model, 9500, "Buyer")
    order = place_fit_order(fit=fit, quantity=2, buyer=buyer).order

    assert expire_reservations() == 0  # feature off by default (0 days)

    policy.reservation_expiry_days = 3
    policy.save(update_fields=["reservation_expiry_days"])
    StoreOrder.objects.filter(pk=order.pk).update(
        created_at=timezone.now() - timedelta(days=4)
    )
    assert expire_reservations() == 1
    res = order.fit_reservations.get()
    assert res.status == FitReservation.Status.EXPIRED
    assert availability_for_fit(fit).atp == 2
    # Frozen order-time promise stays; the live hold is what expired.
    order.refresh_from_db()
    assert order.quantity_reserved == 2 and order.status == StoreOrder.Status.OPEN
    # Demand reappears as a supply need.
    assert FitSupplyNeed.objects.filter(
        doctrine_fit=fit, status=FitSupplyNeed.Status.OPEN
    ).exists()
    # Idempotent re-run.
    assert expire_reservations() == 0


@pytest.mark.django_db
def test_expiry_never_touches_claimed_orders(shipyard, django_user_model):
    from apps.store.tasks import expire_reservations

    fit, home, policy = shipyard["fit"], shipyard["home"], shipyard["policy"]
    policy.reservation_expiry_days = 1
    policy.save(update_fields=["reservation_expiry_days"])
    _stock(fit, home, 1)
    buyer = _member(django_user_model, 9501, "Buyer")
    claimer = _member(django_user_model, 9502, "Builder")
    order = place_fit_order(fit=fit, quantity=1, buyer=buyer).order
    order.status = StoreOrder.Status.CLAIMED
    order.claimed_by = claimer
    order.save(update_fields=["status", "claimed_by"])
    StoreOrder.objects.filter(pk=order.pk).update(
        created_at=timezone.now() - timedelta(days=9)
    )
    assert expire_reservations() == 0
    assert order.fit_reservations.get().status == FitReservation.Status.ACTIVE


# --- views: buyer flow, permissions, tamper-resistance -----------------------------


def _login(client, user):
    client.force_login(user)


@pytest.mark.django_db
def test_order_flow_via_view_confirms_backorder(client, django_user_model, shipyard):
    fit, home = shipyard["fit"], shipyard["home"]
    _stock(fit, home, 1)
    buyer = _member(django_user_model, 9600, "Buyer")
    _login(client, buyer)

    # More than stock → confirm page, no order, nothing reserved.
    resp = client.post("/store/order/fit/", {"fit_id": fit.pk, "quantity": 3})
    assert resp.status_code == 200
    assert b"backorder" in resp.content.lower()
    assert StoreOrder.objects.count() == 0

    # Confirmed with acknowledgement → split order.
    resp = client.post("/store/order/fit/", {
        "fit_id": fit.pk, "quantity": 3, "acknowledge_backorder": "1",
    })
    assert resp.status_code == 302
    order = StoreOrder.objects.get()
    assert order.quantity_reserved == 1 and order.quantity_backordered == 2


@pytest.mark.django_db
def test_view_ignores_client_supplied_price_and_location(client, django_user_model, shipyard):
    """Hidden-field tampering: posted price/location/lead fields simply do not
    exist server-side — the order comes out with server-derived values."""
    fit, home = shipyard["fit"], shipyard["home"]
    _stock(fit, home, 2)
    buyer = _member(django_user_model, 9601, "Buyer")
    _login(client, buyer)
    resp = client.post("/store/order/fit/", {
        "fit_id": fit.pk, "quantity": 1,
        "unit_price": "1.00", "total_price": "1.00", "location_name": "Attacker's Den",
        "delivery_location": "999999", "lead_days_assumed": "0", "quantity_reserved": "999",
    })
    assert resp.status_code == 302
    order = StoreOrder.objects.get()
    assert order.unit_price == Decimal("50600000.00")  # (39M + 7×1M) × 1.10
    assert order.delivery_location == home
    assert order.location_name == str(home)
    assert order.quantity_reserved == 1


@pytest.mark.django_db
def test_inventory_console_is_officer_only(client, django_user_model, shipyard):
    member = _member(django_user_model, 9602, "Member")
    _login(client, member)
    for url in ("/store/inventory/", "/store/inventory/policy/",
                f"/store/inventory/fit/{shipyard['fit'].pk}/"):
        resp = client.get(url)
        assert resp.status_code == 403, url

    officer = _officer(django_user_model, 9603, "Officer")
    _login(client, officer)
    for url in ("/store/inventory/", "/store/inventory/policy/",
                f"/store/inventory/fit/{shipyard['fit'].pk}/"):
        resp = client.get(url)
        assert resp.status_code == 200, url


@pytest.mark.django_db
def test_inventory_mutations_require_officer(client, django_user_model, shipyard):
    fit, home = shipyard["fit"], shipyard["home"]
    stock = _stock(fit, home, 2)
    member = _member(django_user_model, 9604, "Member")
    _login(client, member)
    assert client.post(f"/store/inventory/fit/{fit.pk}/receipt/", {
        "location": home.pk, "quantity": 5,
    }).status_code == 403
    assert client.post(f"/store/inventory/stock/{stock.pk}/adjust/", {
        "corrected_balance": 0, "reason": "gone",
    }).status_code == 403
    assert FitStock.objects.get().quantity_on_hand == 2


@pytest.mark.django_db
def test_officer_receipt_via_view_allocates_and_audits(client, django_user_model, shipyard):
    fit, home = shipyard["fit"], shipyard["home"]
    buyer = _member(django_user_model, 9605, "Buyer")
    place_fit_order(fit=fit, quantity=1, buyer=buyer, acknowledged=True)
    officer = _officer(django_user_model, 9606, "Officer")
    _login(client, officer)
    resp = client.post(f"/store/inventory/fit/{fit.pk}/receipt/", {
        "location": home.pk, "quantity": 2, "reason": "fresh batch",
    })
    assert resp.status_code == 302
    from apps.admin_audit.models import AuditLog

    assert AuditLog.objects.filter(action="store.inventory_receipt").exists()
    order = StoreOrder.objects.get()
    assert order.fit_reservations.filter(status=FitReservation.Status.ACTIVE).count() == 1
    assert availability_for_fit(fit).atp == 1


@pytest.mark.django_db
def test_eta_revision_is_claimer_or_officer_only(client, django_user_model, shipyard):
    fit, home = shipyard["fit"], shipyard["home"]
    buyer = _member(django_user_model, 9607, "Buyer")
    order = place_fit_order(fit=fit, quantity=1, buyer=buyer, acknowledged=True).order
    stranger = _member(django_user_model, 9608, "Stranger")
    _login(client, stranger)
    eta = (timezone.now() + timedelta(days=9)).date().isoformat()
    assert client.post(f"/store/orders/{order.pk}/eta/", {
        "current_eta": eta, "delay_reason": "hostile camp",
    }).status_code == 403

    officer = _officer(django_user_model, 9609, "Officer")
    _login(client, officer)
    resp = client.post(f"/store/orders/{order.pk}/eta/", {
        "current_eta": eta, "delay_reason": "hostile camp",
    })
    assert resp.status_code == 302
    order.refresh_from_db()
    assert order.current_eta.date().isoformat() == eta
    assert order.eta_changed_by == officer and order.delay_reason == "hostile camp"
    assert order.promised_date != order.current_eta  # the original promise survives


@pytest.mark.django_db
def test_waitlist_join_requires_policy_and_pings_on_restock(client, django_user_model, shipyard):
    fit, home, policy = shipyard["fit"], shipyard["home"], shipyard["policy"]
    policy.backorders_enabled = False
    policy.save(update_fields=["backorders_enabled"])
    pilot = _member(django_user_model, 9610, "Pilot")
    _login(client, pilot)

    resp = client.post(f"/store/waitlist/{fit.pk}/")  # policy off → refused
    assert resp.status_code == 302
    assert FitWaitlistEntry.objects.count() == 0

    policy.waitlist_enabled = True
    policy.save(update_fields=["waitlist_enabled"])
    client.post(f"/store/waitlist/{fit.pk}/")
    assert FitWaitlistEntry.objects.filter(fit=fit, user=pilot).exists()

    officer = _officer(django_user_model, 9611, "Officer")
    _login(client, officer)
    client.post(f"/store/inventory/fit/{fit.pk}/receipt/", {
        "location": home.pk, "quantity": 1, "reason": "restock",
    })
    assert FitWaitlistEntry.objects.count() == 0  # notified + cleared


@pytest.mark.django_db
def test_shipyard_page_shows_states_and_hides_when_configured(
    client, django_user_model, shipyard
):
    fit, home, policy = shipyard["fit"], shipyard["home"], shipyard["policy"]
    pilot = _member(django_user_model, 9612, "Pilot")
    _login(client, pilot)

    html = client.get("/doctrines/ships/").content.decode()
    assert "Backorder available" in html  # default: no stock, backorders on

    _stock(fit, home, 1)
    html = client.get("/doctrines/ships/").content.decode()
    assert "Only 1 remaining" in html

    policy.backorders_enabled = False
    policy.save(update_fields=["backorders_enabled"])
    FitStock.objects.all().delete()
    html = client.get("/doctrines/ships/").content.decode()
    assert "Temporarily unavailable" in html

    policy.show_unavailable = False
    policy.save(update_fields=["show_unavailable"])
    html = client.get("/doctrines/ships/").content.decode()
    assert "Temporarily unavailable" not in html
    assert "No fits match these filters" in html
