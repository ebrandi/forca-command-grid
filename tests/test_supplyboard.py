"""Supply Command board (cross-cutting): the provider contract, row correctness,
the P6 supersession hook, server-side section gating, caching, and the digest."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.utils import timezone

from apps.doctrines.models import Doctrine, DoctrineFit
from apps.identity.models import RoleAssignment
from apps.industry.models import MrpConfig, NetRequirement
from apps.market.models import MarketLocation, MarketPrice
from apps.sde.models import SdeCategory, SdeGroup, SdeType
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from apps.stockpile.models import HaulingTask
from apps.store import inventory as inv
from apps.store.models import FitOffer, ShipyardPolicy, StoreOrder
from apps.supplyboard import board, providers
from apps.supplyboard.models import BoardConfig
from core import rbac

FEROX = 16227
MODULE = 1234
CAP_TYPE = 23911


def _member(dum, char_id, name):
    user = dum.objects.create(username=f"eve:{char_id}", first_name=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(character_id=char_id, user=user, name=name,
                                is_main=True, is_corp_member=True)
    return user


def _officer(dum, char_id, name):
    user = _member(dum, char_id, name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_OFFICER))
    return user


def _director(dum, char_id, name):
    user = _officer(dum, char_id, name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_DIRECTOR))
    # The active-pilot ceiling (LP-4) caps a non-Director corp member at officer, so the
    # pilot must carry the in-game Director flag for the grant to be exercisable.
    EveCharacter.objects.filter(user=user).update(is_corp_director=True)
    return user


@pytest.fixture
def env(db):
    ship_cat = SdeCategory.objects.create(category_id=6, name="Ship")
    mat_cat = SdeCategory.objects.create(category_id=4, name="Material")
    cruiser = SdeGroup.objects.create(group_id=26, category=ship_cat, name="Cruiser")
    modgrp = SdeGroup.objects.create(group_id=60, category=mat_cat, name="Module")
    SdeType.objects.create(type_id=FEROX, group=cruiser, name="Ferox", volume=101000.0)
    SdeType.objects.create(type_id=CAP_TYPE, group=cruiser, name="Thanatos", volume=1.0e6)
    SdeType.objects.create(type_id=MODULE, group=modgrp, name="Heavy Neutron Blaster", volume=5.0)
    for tid, price in [(FEROX, "39000000"), (MODULE, "1000000")]:
        MarketPrice.objects.create(type_id=tid, profile=MarketPrice.Profile.JITA_SELL,
                                   sell_min=Decimal(price))
    doctrine = Doctrine.objects.create(name="Ferox Fleet")
    fit = DoctrineFit.objects.create(
        doctrine=doctrine, name="Ferox Railgun", ship_type_id=FEROX,
        modules=[{"type_id": MODULE, "quantity": 7, "slot": "high"}],
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
    BoardConfig.active()  # ensure a singleton exists
    return {"fit": fit, "doctrine": doctrine, "home": home, "away": away}


def _stock(fit, location, qty):
    policy = ShipyardPolicy.active()
    policy.auto_allocate_receipts = False
    policy.save(update_fields=["auto_allocate_receipts"])
    return inv.receive_stock(fit, location=location, quantity=qty, actor=None).stock


def _order(**kw):
    now = timezone.now()
    defaults = dict(
        kind=StoreOrder.Kind.DOCTRINE_FIT, ship_type_id=FEROX, ship_name="Ferox",
        fit_name="Ferox Railgun", quantity=1, unit_price=Decimal("42900000"),
        total_price=Decimal("42900000"), status=StoreOrder.Status.IN_PRODUCTION,
        current_eta=now + timedelta(days=1),
    )
    defaults.update(kw)
    return StoreOrder.objects.create(**defaults)


def _section(data, key):
    return next(s for s in data["sections"] if s.key == key)


# --- provider contract ------------------------------------------------------


@pytest.mark.django_db
def test_registry_has_nine_providers():
    assert len(providers.REGISTRY) == 9


@pytest.mark.django_db
def test_every_row_has_url_and_action(env):
    now = timezone.now()
    _stock(env["fit"], env["home"], 1)
    FitOffer.objects.create(fit=env["fit"], reorder_point=5)  # readiness breach
    _order(status=StoreOrder.Status.IN_PRODUCTION, current_eta=now - timedelta(days=1))  # overdue
    NetRequirement.objects.create(type_id=MODULE, status=NetRequirement.Status.OPEN,
                                  net_quantity=10, required_by=now - timedelta(days=1))
    HaulingTask.objects.create(status=HaulingTask.Status.OPEN, source_location=env["home"],
                               dest_location=env["away"])
    data = board.board_data(refresh=True)
    for section in data["sections"]:
        for row in section.rows:
            assert row.url, f"{section.key} row {row.key} has no url"
            assert row.action_key, f"{section.key} row {row.key} has no action_key"
            assert providers.render_action(row)  # resolves to a non-empty label


@pytest.mark.django_db
def test_duplicate_registration_raises():
    with pytest.raises(ValueError):
        providers.register("readiness", lambda c: None)


@pytest.mark.django_db
def test_broken_provider_yields_stub(env, monkeypatch, django_user_model):
    def boom(config):
        raise RuntimeError("provider exploded")

    monkeypatch.setitem(providers.REGISTRY, "orders", boom)
    data = board.board_data(refresh=True)
    assert _section(data, "orders").total == -1  # honest stub
    # the page still renders 200
    client = Client()
    client.force_login(_director(django_user_model, 4001, "Dir"))
    assert client.get("/supply-board/").status_code == 200


# --- row correctness --------------------------------------------------------


@pytest.mark.django_db
def test_overdue_order_row(env):
    now = timezone.now()
    order = _order(status=StoreOrder.Status.IN_PRODUCTION, current_eta=now - timedelta(days=1))
    section = _section(board.board_data(refresh=True), "orders")
    row = next(r for r in section.rows if r.key == f"order:{order.pk}")
    assert row.severity == "red"
    assert row.url == f"/store/orders/{order.pk}/"


@pytest.mark.django_db
def test_delivered_order_never_in_orders(env):
    """A DELIVERED order with a past ETA must never appear (explicit status set)."""
    now = timezone.now()
    _order(status=StoreOrder.Status.DELIVERED, current_eta=now - timedelta(days=1),
           delivered_at=now)
    section = _section(board.board_data(refresh=True), "orders")
    assert section.rows == []


@pytest.mark.django_db
def test_readiness_row_clears_when_knob_unbreaches(env):
    _stock(env["fit"], env["home"], 3)
    offer = FitOffer.objects.create(fit=env["fit"], reorder_point=5)  # atp 3 <= 5 → breach
    section = _section(board.board_data(refresh=True), "readiness")
    assert any(r.key == f"fit:{env['fit'].id}" and r.severity == "red" for r in section.rows)
    # lower the reorder point below atp → the alert vanishes
    offer.reorder_point = 1
    offer.save(update_fields=["reorder_point"])
    section = _section(board.board_data(refresh=True), "readiness")
    assert not any(r.key == f"fit:{env['fit'].id}" for r in section.rows)


@pytest.mark.django_db
def test_bottleneck_row(env):
    now = timezone.now()
    NetRequirement.objects.create(
        type_id=CAP_TYPE, status=NetRequirement.Status.OPEN, net_quantity=2,
        required_by=now + timedelta(days=1), feasible_at=now + timedelta(days=5),
    )
    section = _section(board.board_data(refresh=True), "bottlenecks")
    assert len(section.rows) == 1 and section.rows[0].severity == "red"


@pytest.mark.django_db
def test_discrepancy_stale_reconcile(env):
    _stock(env["fit"], env["home"], 2)  # last_reconciled_at is None → stale
    section = _section(board.board_data(refresh=True), "discrepancies")
    assert any(r.key == f"recon:{env['fit'].id}" for r in section.rows)


@pytest.mark.django_db
def test_obsolete_row(env):
    retired = Doctrine.objects.create(name="Retired Fleet", status=Doctrine.Status.RETIRED)
    fit2 = DoctrineFit.objects.create(doctrine=retired, name="Old Fit", ship_type_id=FEROX)
    _stock(fit2, env["home"], 1)
    section = _section(board.board_data(refresh=True), "obsolete")
    assert any(r.key == f"fit:{fit2.id}" for r in section.rows)


@pytest.mark.django_db
def test_in_transit_row(env):
    HaulingTask.objects.create(status=HaulingTask.Status.OPEN, source_location=env["home"],
                               dest_location=env["away"])
    section = _section(board.board_data(refresh=True), "in_transit")
    assert len(section.rows) == 1


@pytest.mark.django_db
def test_in_transit_supersession_hook(env):
    task = HaulingTask.objects.create(status=HaulingTask.Status.OPEN,
                                      source_location=env["home"], dest_location=env["away"])
    # v1: the hook is empty, so the haul shows.
    assert len(_section(board.board_data(refresh=True), "in_transit").rows) == 1
    # Populating the provider-local exclusion set (the P6 seam) empties the family.
    providers.HAUL_SUPPRESSION_SOURCES.append(lambda: {task.id})
    try:
        assert _section(board.board_data(refresh=True), "in_transit").rows == []
    finally:
        providers.HAUL_SUPPRESSION_SOURCES.pop()


# --- permission + server-side section gate ----------------------------------


@pytest.mark.django_db
def test_member_403(env, django_user_model):
    client = Client()
    client.force_login(_member(django_user_model, 5001, "Mem"))
    assert client.get("/supply-board/").status_code == 403
    assert client.post("/supply-board/refresh/").status_code == 403


@pytest.mark.django_db
def test_director_section_stripped_for_officer(env, django_user_model):
    client = Client()
    client.force_login(_officer(django_user_model, 5002, "Off"))
    officer_body = client.get("/supply-board/").content
    assert b"Margin erosion" not in officer_body  # director section stripped server-side
    client.force_login(_director(django_user_model, 5003, "Dir"))
    director_body = client.get("/supply-board/").content
    assert b"Margin erosion" in director_body


@pytest.mark.django_db
def test_margin_console_gate(env, django_user_model):
    client = Client()
    client.force_login(_officer(django_user_model, 5004, "Off"))
    assert client.get("/store/margin/").status_code == 403
    client.force_login(_director(django_user_model, 5005, "Dir"))
    assert client.get("/store/margin/").status_code == 200


@pytest.mark.django_db
def test_refresh_audits(env, django_user_model):
    from apps.admin_audit.models import AuditLog

    client = Client()
    client.force_login(_officer(django_user_model, 5006, "Off"))
    assert client.post("/supply-board/refresh/").status_code == 302
    assert AuditLog.objects.filter(action="supplyboard.refresh").exists()


# --- caching + query budget -------------------------------------------------


@pytest.mark.django_db
def test_second_read_is_cache_hit(env, django_assert_num_queries):
    board.board_data(refresh=True)  # build + cache
    with django_assert_num_queries(0):
        board.board_data()  # warm read — no DB


@pytest.mark.django_db
def test_disarmed_sweep_is_cheap(env, django_assert_max_num_queries):
    from apps.supplyboard.tasks import sweep

    with django_assert_max_num_queries(2):
        assert sweep() == {"status": "disabled"}


@pytest.mark.django_db
def test_cold_board_is_bounded(env, django_assert_max_num_queries):
    now = timezone.now()
    for _ in range(20):
        _order(status=StoreOrder.Status.IN_PRODUCTION, current_eta=now - timedelta(days=1))
    for i in range(15):
        NetRequirement.objects.create(type_id=MODULE + i + 1, status=NetRequirement.Status.OPEN,
                                      net_quantity=i + 1, required_by=now - timedelta(days=1))
    with django_assert_max_num_queries(80):
        board.board_data(refresh=True)


# --- digest -----------------------------------------------------------------


def _arm_board():
    cfg = BoardConfig.active()
    cfg.sweep_enabled = True
    cfg.save()
    return cfg


@pytest.mark.django_db
def test_digest_problem_keys(env, monkeypatch):
    from apps.supplyboard.tasks import sweep

    _arm_board()
    now = timezone.now()
    order = _order(status=StoreOrder.Status.IN_PRODUCTION, current_eta=now - timedelta(days=1))
    haul = HaulingTask.objects.create(status=HaulingTask.Status.OPEN, source_location=env["home"],
                                      dest_location=env["away"])
    HaulingTask.objects.filter(pk=haul.pk).update(created_at=now - timedelta(days=30))  # stalled
    captured = {}

    def fake_fire(**kw):
        captured.update(kw)
        return {"status": "alerted"}

    monkeypatch.setattr("apps.pingboard.dedup.fire_on_change", fake_fire)
    sweep()
    problems = captured["problems"]
    assert f"order:{order.pk}" in problems       # overdue order is a problem key
    assert f"haul:{haul.pk}" in problems          # stalled haul is a problem key (never suppressed)


@pytest.mark.django_db
def test_mrp_shortfall_exclusion(env, monkeypatch):
    from apps.supplyboard.tasks import sweep

    _arm_board()
    now = timezone.now()
    req = NetRequirement.objects.create(
        type_id=MODULE, status=NetRequirement.Status.OPEN, net_quantity=5,
        required_by=now - timedelta(days=1),  # overdue shortage → red
    )
    captured = {}
    monkeypatch.setattr("apps.pingboard.dedup.fire_on_change",
                        lambda **kw: captured.update(kw) or {"status": "alerted"})

    mrp = MrpConfig.active()
    mrp.auto_run_enabled = True   # MRP beat owns the officer shortfall ping
    mrp.save()
    sweep()
    assert not any(p.startswith("req:") for p in captured["problems"])  # count-only

    mrp.auto_run_enabled = False  # MRP beat disarmed → shortages are problem keys
    mrp.save()
    captured.clear()
    sweep()
    assert f"req:{req.pk}" in captured["problems"]


@pytest.mark.django_db
def test_idempotency_keys_bounded():
    day = "20260717"
    assert len(f"store:quote_drift:{999999999}:{day}") <= 80
    assert len(f"store:margin_erosion:{day}") <= 80
    # the digest key fire_on_change composes: service:prefix:sig16:stamp
    assert len(f"supplyboard:digest:{'0' * 16}:{10 ** 16}") <= 80


@pytest.mark.django_db
def test_digest_stable_across_same_state(env, monkeypatch):
    from apps.supplyboard.tasks import sweep

    _arm_board()
    now = timezone.now()
    _order(status=StoreOrder.Status.IN_PRODUCTION, current_eta=now - timedelta(days=1))
    seen = []
    monkeypatch.setattr("apps.pingboard.dedup.fire_on_change",
                        lambda **kw: seen.append(sorted(kw["problems"])) or {"status": "ok"})
    sweep()
    sweep()
    assert seen[0] == seen[1]  # identical problem set under an advancing clock
