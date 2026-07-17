"""Procurement surfaces (P4, WS7): role gating, audit trail, CSV, board math.

The lifecycle engines are tested elsewhere (WS1–WS6); this file covers only the
officer/Director web seams — that every page and mutation is role-gated, that a
mutation still writes its audit row, that the CSV keys stay machine-English, that
the board aggregates the numbers the Director reads, and that no template smuggles
an inline event handler past the CSP.
"""
from __future__ import annotations

import datetime
import pathlib
from decimal import Decimal

import pytest
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from apps.admin_audit.models import AppSetting, AuditLog
from apps.identity.models import RoleAssignment
from apps.procurement import metrics
from apps.procurement.models import (
    PurchaseOrder,
    PurchaseOrderLine,
    Supplier,
    SupplierItem,
    SupplyAgreement,
)
from apps.sso.services import ensure_role
from core import rbac

pytestmark = pytest.mark.django_db


def _user(django_user_model, name, *roles):
    user = django_user_model.objects.create(username=name)
    for role in roles:
        RoleAssignment.objects.create(user=user, role=ensure_role(role))
    return user


def _supplier(**kwargs):
    defaults = {
        "kind": Supplier.Kind.CORP,
        "display_name": "Acme Imports",
        "entity_id": 98000001,
    }
    defaults.update(kwargs)
    return Supplier.objects.create(**defaults)


# --- access control -----------------------------------------------------------

def _member_get_urls(supplier, agreement, po):
    return [
        reverse("procurement:suppliers"),
        reverse("procurement:supplier_new"),
        reverse("procurement:supplier_detail", args=[supplier.pk]),
        reverse("procurement:agreements"),
        reverse("procurement:agreement_new"),
        reverse("procurement:agreement_detail", args=[agreement.pk]),
        reverse("procurement:pos"),
        reverse("procurement:po_new"),
        reverse("procurement:po_detail", args=[po.pk]),
        reverse("procurement:board"),
    ]


def _member_post_urls(supplier, agreement, po):
    return [
        reverse("procurement:supplier_new"),
        reverse("procurement:supplier_item_add", args=[supplier.pk]),
        reverse("procurement:agreement_new"),
        reverse("procurement:agreement_line_add", args=[agreement.pk]),
        reverse("procurement:agreement_action", args=[agreement.pk]),
        reverse("procurement:po_new"),
        reverse("procurement:po_line_add", args=[po.pk]),
        reverse("procurement:po_action", args=[po.pk]),
    ]


def test_member_forbidden_on_every_page_and_mutation(client, django_user_model):
    member = _user(django_user_model, "member", rbac.ROLE_MEMBER)
    supplier = _supplier()
    agreement = SupplyAgreement.objects.create(supplier=supplier)
    po = PurchaseOrder.objects.create(supplier=supplier)

    client.force_login(member)
    for url in _member_get_urls(supplier, agreement, po):
        assert client.get(url).status_code == 403, url
    for url in _member_post_urls(supplier, agreement, po):
        assert client.post(url, {}).status_code == 403, url


def test_officer_can_load_lists_and_details(client, django_user_model):
    officer = _user(django_user_model, "officer", rbac.ROLE_OFFICER)
    supplier = _supplier()
    agreement = SupplyAgreement.objects.create(supplier=supplier)
    po = PurchaseOrder.objects.create(supplier=supplier)

    client.force_login(officer)
    for url in (
        reverse("procurement:suppliers"),
        reverse("procurement:supplier_new"),
        reverse("procurement:supplier_detail", args=[supplier.pk]),
        reverse("procurement:agreements"),
        reverse("procurement:agreement_new"),
        reverse("procurement:agreement_detail", args=[agreement.pk]),
        reverse("procurement:pos"),
        reverse("procurement:po_new"),
        reverse("procurement:po_detail", args=[po.pk]),
    ):
        assert client.get(url).status_code == 200, url


def test_ops_hub_shows_director_procurement_card(client, django_user_model, sde):
    director = _user(django_user_model, "director", rbac.ROLE_DIRECTOR)
    client.force_login(director)
    html = client.get("/ops/admin/").content.decode()
    assert reverse("procurement:board") in html


def test_board_is_director_only(client, django_user_model):
    officer = _user(django_user_model, "officer", rbac.ROLE_OFFICER)
    director = _user(django_user_model, "director", rbac.ROLE_DIRECTOR)
    url = reverse("procurement:board")

    client.force_login(officer)
    assert client.get(url).status_code == 403

    client.force_login(director)
    assert client.get(url).status_code == 200


# --- audit trail --------------------------------------------------------------

def test_submit_po_via_detail_post_writes_audit_log(client, django_user_model):
    officer = _user(django_user_model, "officer", rbac.ROLE_OFFICER)
    supplier = _supplier()
    SupplierItem.objects.create(supplier=supplier, type_id=587, moq=1, active=True)
    po = PurchaseOrder.objects.create(supplier=supplier, status=PurchaseOrder.Status.DRAFT)
    PurchaseOrderLine.objects.create(po=po, type_id=587, quantity_ordered=5)

    client.force_login(officer)
    resp = client.post(reverse("procurement:po_action", args=[po.pk]), {"action": "submit"})
    assert resp.status_code == 302

    po.refresh_from_db()
    assert po.status == PurchaseOrder.Status.SUBMITTED
    assert AuditLog.objects.filter(action="procurement.po_submitted",
                                   target_id=str(po.pk)).exists()


# --- hub-pickup haul button ---------------------------------------------------

def test_create_haul_for_hub_pickup_po(client, django_user_model):
    from apps.stockpile.models import HaulingTask

    officer = _user(django_user_model, "officer", rbac.ROLE_OFFICER)
    supplier = _supplier()
    po = PurchaseOrder.objects.create(
        supplier=supplier, delivery_mode=PurchaseOrder.DeliveryMode.HUB_PICKUP,
        status=PurchaseOrder.Status.CONTRACT_AVAILABLE,
    )
    PurchaseOrderLine.objects.create(po=po, type_id=587, quantity_ordered=3)

    client.force_login(officer)
    resp = client.post(reverse("procurement:po_action", args=[po.pk]), {"action": "haul"})
    assert resp.status_code == 302
    assert HaulingTask.objects.filter(type_id=587, quantity=3).exists()
    assert AuditLog.objects.filter(action="procurement.po_haul_created",
                                   target_id=str(po.pk)).exists()


def test_haul_refused_for_supplier_delivers_po(client, django_user_model):
    from apps.stockpile.models import HaulingTask

    officer = _user(django_user_model, "officer", rbac.ROLE_OFFICER)
    supplier = _supplier()
    po = PurchaseOrder.objects.create(
        supplier=supplier, delivery_mode=PurchaseOrder.DeliveryMode.SUPPLIER_DELIVERS,
        status=PurchaseOrder.Status.CONTRACT_AVAILABLE,
    )
    PurchaseOrderLine.objects.create(po=po, type_id=587, quantity_ordered=3)

    client.force_login(officer)
    # The domain rule is surfaced as an error message + redirect, never a 500.
    resp = client.post(reverse("procurement:po_action", args=[po.pk]), {"action": "haul"})
    assert resp.status_code == 302
    assert not HaulingTask.objects.exists()


# --- CSV export ---------------------------------------------------------------

def test_pos_csv_export_has_machine_english_keys(client, django_user_model):
    officer = _user(django_user_model, "officer", rbac.ROLE_OFFICER)
    _supplier()

    client.force_login(officer)
    resp = client.get(reverse("procurement:pos") + "?format=csv")
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/csv")

    header = resp.content.decode().splitlines()[0]
    for key in ("po_id", "supplier_id", "status", "delivery_mode",
                "expected_total_isk", "contract_id"):
        assert key in header, key


# --- board math ---------------------------------------------------------------

def test_open_obligations_sums_outstanding_over_counted_pos():
    supplier = _supplier()
    # Counted PO #1: outstanding 8 units × 100 ISK = 800.
    po1 = PurchaseOrder.objects.create(supplier=supplier, status=PurchaseOrder.Status.APPROVED)
    PurchaseOrderLine.objects.create(po=po1, type_id=587, quantity_ordered=10,
                                     quantity_received=2, unit_price_isk=Decimal("100"))
    # Counted PO #2: outstanding 5 units × 50 ISK = 250.
    po2 = PurchaseOrder.objects.create(supplier=supplier, status=PurchaseOrder.Status.PARTIAL)
    PurchaseOrderLine.objects.create(po=po2, type_id=588, quantity_ordered=5,
                                     quantity_received=0, unit_price_isk=Decimal("50"))
    # Terminal PO must NOT count towards obligations.
    po3 = PurchaseOrder.objects.create(supplier=supplier, status=PurchaseOrder.Status.RECONCILED)
    PurchaseOrderLine.objects.create(po=po3, type_id=589, quantity_ordered=100,
                                     quantity_received=0, unit_price_isk=Decimal("999"))

    result = metrics.open_obligations()
    assert result["isk"] == Decimal("1050")
    assert result["units"] == 13
    assert result["count"] == 2


def test_board_freshness_is_never_silently_green():
    # No stamp yet → the feed must read stale, not green.
    fresh = metrics.board_freshness()
    assert fresh["corp_contracts"]["stale"] is True
    assert fresh["corp_contracts"]["at"] is None

    # A recent stamp clears it.
    from apps.admin_audit.health import record_sync

    record_sync("corp_contracts", count=5)
    assert metrics.board_freshness()["corp_contracts"]["stale"] is False

    # A stamp older than the threshold reads stale again.
    old = (timezone.now() - datetime.timedelta(hours=5)).isoformat()
    AppSetting.objects.update_or_create(
        key="sync:corp_contracts", defaults={"value": {"at": old, "count": 5}}
    )
    assert metrics.board_freshness()["corp_contracts"]["stale"] is True


# --- CSP: no inline event handlers -------------------------------------------

def test_templates_have_no_inline_event_handlers():
    template_dir = pathlib.Path(settings.BASE_DIR) / "templates" / "procurement"
    templates = list(template_dir.glob("*.html"))
    assert templates, "no procurement templates found"

    banned = ("onclick=", "onchange=", "onsubmit=", "oninput=", "onload=", "onkeyup=")
    for path in templates:
        text = path.read_text()
        for handler in banned:
            assert handler not in text, f"{path.name} contains inline {handler}"

    # Interactivity is Alpine (CSP-clean) — the PO detail drives its action forms
    # with @click, never an inline handler.
    assert "@click" in (template_dir / "po_detail.html").read_text()
