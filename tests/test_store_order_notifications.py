"""MKT-5 (roadmap 3.18) — buyer order-status notifications.

A store buyer gets one non-spammy DM when their order is claimed / advanced / cancelled —
targeted at the buyer, never corp-wide, and never for a change the buyer made themselves.
"""
from __future__ import annotations

import pytest

from apps.identity.models import RoleAssignment
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from apps.store.models import Audience, StoreOrder
from apps.store.services import active_config, invalidate_audience_cache, notify_order_status
from core import rbac

pytestmark = pytest.mark.django_db

_EMIT = "apps.pingboard.services.emit_broadcast"


def _member(django_user_model, cid):
    u = django_user_model.objects.create(username=f"eve:{cid}")
    RoleAssignment.objects.create(user=u, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(character_id=cid, user=u, name=f"P{cid}", is_main=True,
                                is_corp_member=True)
    return u


def _order(django_user_model, *, status=StoreOrder.Status.CLAIMED, buyer=True):
    b = _member(django_user_model, 9400) if buyer else None
    return StoreOrder.objects.create(
        buyer=b, kind=StoreOrder.Kind.HULL, ship_type_id=670, ship_name="Rifter", status=status
    )


def test_notify_emits_to_buyer_on_status_change(django_user_model, monkeypatch):
    calls = []
    monkeypatch.setattr(_EMIT, lambda **kw: calls.append(kw))
    order = _order(django_user_model)
    notify_order_status(order, actor=django_user_model.objects.create(username="builder"))
    assert len(calls) == 1
    assert calls[0]["audience"] == {"kind": "user", "id": order.buyer_id}
    assert "claimed" in calls[0]["body"].lower()
    assert calls[0]["idempotency_key"] == f"store:order_status:{order.id}:claimed"


def test_self_triggered_change_does_not_ping(django_user_model, monkeypatch):
    calls = []
    monkeypatch.setattr(_EMIT, lambda **kw: calls.append(kw))
    order = _order(django_user_model, status=StoreOrder.Status.CANCELLED)
    notify_order_status(order, actor=order.buyer)  # the buyer cancelled their own order
    assert calls == []


def test_open_status_does_not_ping(django_user_model, monkeypatch):
    calls = []
    monkeypatch.setattr(_EMIT, lambda **kw: calls.append(kw))
    order = _order(django_user_model, status=StoreOrder.Status.OPEN)
    notify_order_status(order, actor=django_user_model.objects.create(username="x"))
    assert calls == []


def test_disabled_event_does_not_ping(django_user_model, monkeypatch):
    from apps.pingboard import config as pb_config

    calls = []
    monkeypatch.setattr(_EMIT, lambda **kw: calls.append(kw))
    doc = pb_config.get("notifications")
    doc["events"] = {**(doc.get("events") or {}), "store.order_status": {"enabled": False}}
    pb_config.set("notifications", doc)
    order = _order(django_user_model, status=StoreOrder.Status.DELIVERED)
    notify_order_status(order, actor=django_user_model.objects.create(username="y"))
    assert calls == []


def test_claim_view_pings_buyer(client, django_user_model, monkeypatch):
    calls = []
    monkeypatch.setattr(_EMIT, lambda **kw: calls.append(kw))
    cfg = active_config()
    cfg.audience = Audience.CORP
    cfg.save(update_fields=["audience"])
    invalidate_audience_cache()

    buyer = _member(django_user_model, 9500)
    order = StoreOrder.objects.create(
        buyer=buyer, kind=StoreOrder.Kind.HULL, ship_type_id=670, ship_name="Rifter",
        status=StoreOrder.Status.OPEN,
    )
    builder = _member(django_user_model, 9501)
    client.force_login(builder)
    client.post(f"/store/orders/{order.pk}/claim/")
    order.refresh_from_db()
    assert order.status == StoreOrder.Status.CLAIMED
    assert any(c["audience"] == {"kind": "user", "id": buyer.id} for c in calls)
