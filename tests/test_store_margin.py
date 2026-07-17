"""Cost & profitability (cross-cutting): method stamping, settlement evidence,
quote drift, margin summary, the cost-basis registry, and erosion routing."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.doctrines.models import Doctrine, DoctrineFit
from apps.identity.models import RoleAssignment
from apps.market.models import MarketLocation, MarketPrice
from apps.sde.models import SdeCategory, SdeGroup, SdeType
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from apps.store import inventory as inv
from apps.store import margin
from apps.store.margin import (
    check_quote_drift,
    margin_summary,
    reconcile_settlements,
    record_contract_settlement,
)
from apps.store.models import (
    Audience,
    FulfilmentMethod,
    MarginConfig,
    OrderBasisDrift,
    OrderSettlement,
    PriceBasis,
    ShipyardPolicy,
    StoreOrder,
)
from apps.store.services import _stamp_fulfilment_method, active_config, place_fit_order, transition_order
from core import rbac

CAP_TYPE = 23911     # capital hull (BUILD basis)
FEROX = 16227        # cruiser hull (doctrine fit)
MODULE = 1234


def _member(django_user_model, char_id, name):
    user = django_user_model.objects.create(username=f"eve:{char_id}", first_name=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(
        character_id=char_id, user=user, name=name, is_main=True, is_corp_member=True
    )
    return user


def _officer(django_user_model, char_id, name):
    user = _member(django_user_model, char_id, name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_OFFICER))
    return user


def _director(django_user_model, char_id, name):
    user = _officer(django_user_model, char_id, name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_DIRECTOR))
    EveCharacter.objects.filter(user=user).update(is_corp_director=True)
    return user


@pytest.fixture
def env(db):
    ship_cat = SdeCategory.objects.create(category_id=6, name="Ship")
    mat_cat = SdeCategory.objects.create(category_id=4, name="Material")
    cruiser = SdeGroup.objects.create(group_id=26, category=ship_cat, name="Cruiser")
    carrier = SdeGroup.objects.create(group_id=547, category=ship_cat, name="Carrier")
    modgrp = SdeGroup.objects.create(group_id=60, category=mat_cat, name="Module")
    SdeType.objects.create(type_id=FEROX, group=cruiser, name="Ferox", volume=101000.0)
    SdeType.objects.create(type_id=CAP_TYPE, group=carrier, name="Thanatos", volume=1.3e7)
    SdeType.objects.create(type_id=MODULE, group=modgrp, name="Heavy Neutron Blaster", volume=5.0)
    for tid, price in [(FEROX, "39000000"), (MODULE, "1000000")]:
        MarketPrice.objects.create(
            type_id=tid, profile=MarketPrice.Profile.JITA_SELL, sell_min=Decimal(price)
        )
    doctrine = Doctrine.objects.create(name="Ferox Fleet")
    fit = DoctrineFit.objects.create(
        doctrine=doctrine, name="Ferox Railgun", ship_type_id=FEROX,
        modules=[{"type_id": MODULE, "quantity": 7, "slot": "high"}],
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
    return {"fit": fit, "home": home}


def _order(**kw) -> StoreOrder:
    defaults = dict(
        kind=StoreOrder.Kind.HULL, ship_type_id=CAP_TYPE, ship_name="Thanatos",
        quantity=1, unit_jita=Decimal("0"), unit_price=Decimal("2500000000"),
        total_price=Decimal("2500000000"), unit_cost=Decimal("2000000000"),
        price_basis=PriceBasis.BUILD, status=StoreOrder.Status.OPEN,
    )
    defaults.update(kw)
    return StoreOrder.objects.create(**defaults)


def _stock(fit, location, qty):
    policy = ShipyardPolicy.active()
    policy.auto_allocate_receipts = False
    policy.save(update_fields=["auto_allocate_receipts"])
    return inv.receive_stock(fit, location=location, quantity=qty, actor=None).stock


# --- method stamping --------------------------------------------------------


@pytest.mark.django_db
def test_full_consumption_stamps_stock(env, django_user_model):
    _stock(env["fit"], env["home"], 2)
    buyer = _member(django_user_model, 2001, "Buyer")
    order = place_fit_order(fit=env["fit"], quantity=2, buyer=buyer).order
    actor = _officer(django_user_model, 3001, "Off")
    for nxt in (StoreOrder.Status.CLAIMED, StoreOrder.Status.READY, StoreOrder.Status.DELIVERED):
        assert transition_order(order, nxt, actor=actor)
    order.refresh_from_db()
    assert order.fulfilment_method == FulfilmentMethod.STOCK


@pytest.mark.django_db
def test_expired_reservations_do_not_stamp_stock(env, django_user_model):
    from apps.store.inventory import release_order_reservations

    _stock(env["fit"], env["home"], 2)
    buyer = _member(django_user_model, 2002, "Buyer")
    order = place_fit_order(fit=env["fit"], quantity=2, buyer=buyer).order
    # Reservations expire before delivery: the frozen quantity_reserved stays 2 but zero
    # units are actually consumed — must NOT claim stock.
    release_order_reservations(order, expired=True)
    order.refresh_from_db()
    assert order.quantity_reserved == 2
    actor = _officer(django_user_model, 3002, "Off")
    for nxt in (StoreOrder.Status.CLAIMED, StoreOrder.Status.READY, StoreOrder.Status.DELIVERED):
        transition_order(order, nxt, actor=actor)
    order.refresh_from_db()
    assert order.fulfilment_method == ""


@pytest.mark.django_db
def test_stamp_is_write_once(env):
    order = _order(status=StoreOrder.Status.DELIVERED, fulfilment_method="build",
                   delivered_at=timezone.now())
    # A full-consumption stamp must not overwrite a non-blank method.
    _stamp_fulfilment_method(order, consumed=order.quantity, form_value="")
    order.refresh_from_db()
    assert order.fulfilment_method == "build"


@pytest.mark.django_db
def test_partial_consumption_uses_form_value(env):
    order = _order(status=StoreOrder.Status.DELIVERED, quantity=2, delivered_at=timezone.now())
    _stamp_fulfilment_method(order, consumed=1, form_value="import")
    order.refresh_from_db()
    assert order.fulfilment_method == "import"
    # unattended (no form value) leaves it blank
    order2 = _order(status=StoreOrder.Status.DELIVERED, quantity=2, delivered_at=timezone.now())
    _stamp_fulfilment_method(order2, consumed=1, form_value="")
    order2.refresh_from_db()
    assert order2.fulfilment_method == ""


@pytest.mark.django_db
def test_unrecorded_label():
    assert str(margin.method_label("")) == "Unrecorded"
    assert str(margin.method_label("build")) == str(FulfilmentMethod.BUILD.label)


# --- settlement evidence ----------------------------------------------------


def _arm_settlement():
    cfg = MarginConfig.active()
    cfg.settlement_reconcile_enabled = True
    cfg.save()
    return cfg


@pytest.mark.django_db
def test_settlement_disabled_is_noop():
    assert reconcile_settlements() == {"status": "disabled"}


@pytest.mark.django_db
def test_token_match_creates_settlement(env):
    from apps.corporation.models import CorpWalletJournalEntry

    _arm_settlement()
    order = _order(status=StoreOrder.Status.DELIVERED, total_price=Decimal("100000000"),
                   delivered_at=timezone.now())
    order.created_at = timezone.now() - timedelta(days=1)
    order.save(update_fields=["created_at"])
    CorpWalletJournalEntry.objects.create(
        entry_id=5001, division=1, ref_type="player_donation",
        date=order.created_at + timedelta(hours=1), amount=Decimal("100000000"),
        reason=f"thanks {order.payment_token} o7", second_party_id=999,
    )
    assert reconcile_settlements()["linked"] == 1
    s = OrderSettlement.objects.get(order=order)
    assert s.kind == "journal" and s.matched_by == "token"
    assert s.journal_entry_id == 5001 and s.occurred_at is not None
    assert "SO-" in s.note  # Seam-B pinned English note
    # second run: entry already used → no new settlement (partial-unique + used set)
    assert reconcile_settlements()["linked"] == 0


@pytest.mark.django_db
def test_token_delimiter_trap(env):
    from apps.corporation.models import CorpWalletJournalEntry

    _arm_settlement()
    # Force pks 5 and 50 so the delimiter guard matters.
    o5 = _order(status=StoreOrder.Status.DELIVERED, total_price=Decimal("10000000"),
                delivered_at=timezone.now())
    StoreOrder.objects.filter(pk=o5.pk).update(id=5)
    o50 = _order(status=StoreOrder.Status.DELIVERED, total_price=Decimal("10000000"),
                 delivered_at=timezone.now())
    StoreOrder.objects.filter(pk=o50.pk).update(id=50)
    o5 = StoreOrder.objects.get(pk=5)
    o50 = StoreOrder.objects.get(pk=50)
    for o in (o5, o50):
        StoreOrder.objects.filter(pk=o.pk).update(created_at=timezone.now() - timedelta(days=1))
    CorpWalletJournalEntry.objects.create(
        entry_id=6001, division=1, ref_type="player_donation",
        date=timezone.now(), amount=Decimal("10000000"),
        reason="pay SO-5- now", second_party_id=1,
    )
    reconcile_settlements()
    assert OrderSettlement.objects.filter(order_id=5).exists()
    assert not OrderSettlement.objects.filter(order_id=50).exists()


@pytest.mark.django_db
def test_money_out_never_matches(env):
    from apps.corporation.models import CorpWalletJournalEntry

    _arm_settlement()
    order = _order(status=StoreOrder.Status.DELIVERED, total_price=Decimal("100000000"),
                   delivered_at=timezone.now())
    StoreOrder.objects.filter(pk=order.pk).update(created_at=timezone.now() - timedelta(days=1))
    CorpWalletJournalEntry.objects.create(
        entry_id=7001, division=1, ref_type="player_donation",
        date=timezone.now(), amount=Decimal("-100000000"),  # money OUT
        reason=f"{order.payment_token}", second_party_id=1,
    )
    assert reconcile_settlements()["linked"] == 0


@pytest.mark.django_db
def test_contract_link_fills_on_completion(env, django_user_model):
    from apps.logistics.models import CorpContract

    _arm_settlement()
    officer = _officer(django_user_model, 3003, "Off")
    order = _order(status=StoreOrder.Status.READY)
    assert record_contract_settlement(order, contract_id=8001, actor=officer)
    s = OrderSettlement.objects.get(order=order)
    assert s.kind == "contract" and s.occurred_at is None and s.amount == 0
    # no completed contract yet → still pending
    reconcile_settlements()
    s.refresh_from_db()
    assert s.occurred_at is None
    # complete the contract → filled
    CorpContract.objects.create(contract_id=8001, price=Decimal("90000000"),
                                date_completed=timezone.now(), status="finished")
    reconcile_settlements()
    s.refresh_from_db()
    assert s.occurred_at is not None and s.amount == Decimal("90000000")


@pytest.mark.django_db
def test_contract_id_reuse_blocked(env, django_user_model):
    officer = _officer(django_user_model, 3004, "Off")
    a = _order(status=StoreOrder.Status.READY)
    b = _order(status=StoreOrder.Status.READY)
    assert record_contract_settlement(a, contract_id=8100, actor=officer)
    assert not record_contract_settlement(b, contract_id=8100, actor=officer)


@pytest.mark.django_db
def test_contract_fill_requires_finished_status(env, django_user_model):
    """A rejected/cancelled contract carries date_completed but must NOT fill revenue."""
    from apps.logistics.models import CorpContract

    _arm_settlement()
    officer = _officer(django_user_model, 3005, "Off")
    order = _order(status=StoreOrder.Status.READY)
    record_contract_settlement(order, contract_id=8200, actor=officer)
    CorpContract.objects.create(contract_id=8200, price=Decimal("90000000"),
                                date_completed=timezone.now(), status="rejected")
    reconcile_settlements()
    s = OrderSettlement.objects.get(order=order)
    assert s.occurred_at is None and s.amount == 0  # never-paid contract → still pending
    CorpContract.objects.filter(contract_id=8200).update(status="finished")
    reconcile_settlements()
    s.refresh_from_db()
    assert s.occurred_at is not None and s.amount == Decimal("90000000")


@pytest.mark.django_db
def test_contract_order_not_token_matched(env, django_user_model):
    """An order with a recorded contract lane must never also token-match a wallet line."""
    from apps.corporation.models import CorpWalletJournalEntry

    _arm_settlement()
    officer = _officer(django_user_model, 3006, "Off")
    order = _order(status=StoreOrder.Status.DELIVERED, total_price=Decimal("100000000"),
                   delivered_at=timezone.now())
    StoreOrder.objects.filter(pk=order.pk).update(created_at=timezone.now() - timedelta(days=1))
    record_contract_settlement(order, contract_id=8300, actor=officer)
    CorpWalletJournalEntry.objects.create(
        entry_id=9001, division=1, ref_type="player_donation", date=timezone.now(),
        amount=Decimal("100000000"), reason=order.payment_token, second_party_id=1,
    )
    reconcile_settlements()
    assert not OrderSettlement.objects.filter(order=order, kind="journal").exists()


# --- quote drift ------------------------------------------------------------


def _arm_drift():
    cfg = MarginConfig.active()
    cfg.drift_check_enabled = True
    cfg.save()
    return cfg


@pytest.mark.django_db
def test_drift_disabled_is_noop():
    assert check_quote_drift() == {"status": "disabled"}


@pytest.mark.django_db
def test_drift_build_basis_flags(env, monkeypatch):
    _arm_drift()
    order = _order(unit_cost=Decimal("2000000000"))
    monkeypatch.setattr("apps.store.pricing.production_cost_detail",
                        lambda tid: {"cost": Decimal("2400000000"), "source": "everef"})
    check_quote_drift()
    d = OrderBasisDrift.objects.get(order=order)
    assert d.flagged
    assert d.drift_pct == Decimal("0.2000")
    assert d.basis_source == "everef"
    order.refresh_from_db()
    assert order.unit_cost == Decimal("2000000000")  # frozen column untouched


@pytest.mark.django_db
def test_drift_needs_threshold_and_floor(env, monkeypatch):
    _arm_drift()
    # 20% drift but only 3M absolute → below the 5M floor → not flagged.
    order = _order(unit_cost=Decimal("15000000"), total_price=Decimal("18000000"))
    monkeypatch.setattr("apps.store.pricing.production_cost_detail",
                        lambda tid: {"cost": Decimal("18000000"), "source": "everef"})
    check_quote_drift()
    assert not OrderBasisDrift.objects.get(order=order).flagged


@pytest.mark.django_db
def test_drift_breaker_none_is_unknown(env, monkeypatch):
    _arm_drift()
    order = _order(unit_cost=Decimal("2000000000"))
    monkeypatch.setattr("apps.store.pricing.production_cost_detail", lambda tid: None)
    check_quote_drift()
    d = OrderBasisDrift.objects.get(order=order)
    assert not d.flagged and d.basis_source == "unknown" and d.current_value is None


@pytest.mark.django_db
def test_drift_open_order_monitored(env, monkeypatch):
    """An OPEN (unclaimed) BUILD order is drift-checked — no claim required."""
    _arm_drift()
    order = _order(status=StoreOrder.Status.OPEN, unit_cost=Decimal("1000000000"))
    monkeypatch.setattr("apps.store.pricing.production_cost_detail",
                        lambda tid: {"cost": Decimal("1300000000"), "source": "everef"})
    check_quote_drift()
    assert OrderBasisDrift.objects.get(order=order).flagged


@pytest.mark.django_db
def test_drift_idempotent(env, monkeypatch):
    _arm_drift()
    order = _order(unit_cost=Decimal("2000000000"))
    monkeypatch.setattr("apps.store.pricing.production_cost_detail",
                        lambda tid: {"cost": Decimal("2400000000"), "source": "everef"})
    check_quote_drift()
    d1 = OrderBasisDrift.objects.get(order=order)
    stamp = d1.checked_at
    check_quote_drift()  # unchanged inputs → no write, stable timestamp
    d2 = OrderBasisDrift.objects.get(order=order)
    assert d2.checked_at == stamp


@pytest.mark.django_db
def test_drift_ack_watermark(env, monkeypatch):
    from apps.store.margin import acknowledge_drift

    _arm_drift()
    order = _order(unit_cost=Decimal("2000000000"))
    monkeypatch.setattr("apps.store.pricing.production_cost_detail",
                        lambda tid: {"cost": Decimal("2400000000"), "source": "everef"})
    check_quote_drift()
    d = OrderBasisDrift.objects.get(order=order)
    assert acknowledge_drift(d, actor=None)
    d.refresh_from_db()
    assert not d.flagged and d.acknowledged_pct == Decimal("0.2000")
    # same drift → stays acknowledged (within the band)
    check_quote_drift()
    assert not OrderBasisDrift.objects.get(order=order).flagged
    # a bigger drift past ack+threshold (30%) re-flags
    monkeypatch.setattr("apps.store.pricing.production_cost_detail",
                        lambda tid: {"cost": Decimal("2800000000"), "source": "everef"})
    check_quote_drift()
    assert OrderBasisDrift.objects.get(order=order).flagged


# --- margin summary + cost-basis registry -----------------------------------


@pytest.mark.django_db
def test_margin_summary_groups(env, django_assert_max_num_queries):
    now = timezone.now()
    _order(status=StoreOrder.Status.DELIVERED, fulfilment_method="build",
           delivered_at=now, total_price=Decimal("100"), unit_cost=Decimal("60"))
    ev_order = _order(status=StoreOrder.Status.DELIVERED, fulfilment_method="build",
                      delivered_at=now, total_price=Decimal("100"), unit_cost=Decimal("60"))
    _order(status=StoreOrder.Status.DELIVERED, fulfilment_method="",
           delivered_at=now, total_price=Decimal("50"), unit_cost=Decimal("30"))
    OrderSettlement.objects.create(order=ev_order, kind="journal", matched_by="token",
                                   journal_entry_id=1, amount=Decimal("95"), occurred_at=now)
    with django_assert_max_num_queries(4):  # orders + settlements aggregate (no pricing)
        summary = margin_summary()
    methods = {m.method: m for m in summary["methods"]}
    assert "build" in methods and "" in methods
    assert methods["build"].unevidenced_count == 1
    assert methods["build"].evidenced_revenue == Decimal("95")
    assert str(methods[""].label) == "Unrecorded"


@pytest.mark.django_db
def test_cost_basis_registry(env):
    now = timezone.now()
    _order(status=StoreOrder.Status.DELIVERED, fulfilment_method="import",
           delivered_at=now, price_basis=PriceBasis.JITA,
           unit_jita=Decimal("500"), total_price=Decimal("600"))
    # v1: unregistered "import" falls back to the Jita reference.
    m = {x.method: x for x in margin_summary()["methods"]}["import"]
    assert m.estimate_cost == Decimal("500") and m.has_reference_cost
    # register an actual-cost provider for the lane
    margin.register_cost_basis("import", lambda o: {"unit_cost": Decimal("400"), "source": "landed"})
    try:
        with pytest.raises(ValueError):
            margin.register_cost_basis("import", lambda o: None)  # one owner per lane
        m2 = {x.method: x for x in margin_summary()["methods"]}["import"]
        assert m2.estimate_cost == Decimal("400")     # actual replaces the reference
        assert "landed" in m2.cost_sources and not m2.has_reference_cost
    finally:
        margin.COST_BASIS.pop("import", None)


# --- erosion routing --------------------------------------------------------


@pytest.mark.django_db
def test_margin_erosion_is_leadership_sensitive():
    from apps.pingboard.notifications import broadcast_classification, resolve

    r = resolve("store.margin_erosion")
    assert r["sensitive"] is True
    # audience "officer" → high_command classification → dropped by mass channels.
    assert broadcast_classification("store.margin_erosion") == "high_command"


@pytest.mark.django_db
def test_quote_drift_ping_gated(env, monkeypatch):
    """A drift ping never emits while the event is disabled."""
    from apps.store import margin as m

    calls = []
    monkeypatch.setattr("apps.pingboard.notifications.is_enabled", lambda k: False)
    monkeypatch.setattr("apps.pingboard.services.emit_broadcast",
                        lambda **kw: calls.append(kw))
    order = _order()
    m._emit_drift_ping(order)
    assert calls == []
