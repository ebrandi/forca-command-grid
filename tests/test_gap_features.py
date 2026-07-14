"""Member-facing features closing PRD gaps: fit export, killboard filters,
build opportunities, and project stock reservation."""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.utils import timezone

from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit
from apps.identity.models import RoleAssignment
from apps.industry.models import IndustryProject, IndustryProjectItem
from apps.killboard.models import Killmail
from apps.market.models import MarketLocation, MarketPrice
from apps.sso.services import ensure_role
from apps.stockpile.models import Stockpile, StockReservation
from apps.stockpile.services import record_manual_stock
from core import rbac


def _member(django_user_model, name="m", role=rbac.ROLE_MEMBER):
    user = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(role))
    return user


# --- Module B: fit EFT export -------------------------------------------------
@pytest.mark.django_db
def test_fit_export_eft_follows_doctrine_audience(client, django_user_model, sde):
    # EFT export is part of "Ships & doctrines" and follows its audience. Default corp:
    # only members export; anonymous / logged-in outsiders get the audience gate's 404.
    from django.core.cache import cache

    from core.features import set_feature_audiences

    cat = DoctrineCategory.objects.create(key="dps", label="DPS")
    doc = Doctrine.objects.create(name="Rifter Roam", category=cat)
    fit = DoctrineFit.objects.create(
        doctrine=doc, name="Rifter", ship_type_id=587,
        eft_text="[Rifter, Rifter]\n200mm AutoCannon I",
    )
    cache.clear()
    set_feature_audiences({"doctrines": "corp"})  # corp-only (the default)

    # Anonymous -> sent to log in (we do not know who they are yet).
    assert client.get(f"/doctrines/fits/{fit.id}/export/").status_code == 302
    # Logged-in outsider -> outside the corp audience -> 404, leaking nothing.
    client.force_login(django_user_model.objects.create(username="x"))
    assert client.get(f"/doctrines/fits/{fit.id}/export/").status_code == 404
    # Member -> the raw EFT text.
    client.force_login(_member(django_user_model))
    resp = client.get(f"/doctrines/fits/{fit.id}/export/")
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/plain")
    assert b"[Rifter, Rifter]" in resp.content


# --- Module A: killboard filters + drill-down ---------------------------------
@pytest.mark.django_db
def test_killboard_filters_by_entity_and_time(client, sde):
    now = timezone.now()
    old = now - timezone.timedelta(days=40)

    def km(kid, system, ship, char, corp, when, role):
        return Killmail.objects.create(
            killmail_id=kid, killmail_hash="h", killmail_time=when, solar_system_id=system,
            region_id=10000002, victim_ship_type_id=ship, victim_character_id=char,
            victim_corporation_id=corp, total_value=Decimal("1000000"),
            involves_home_corp=True, home_corp_role=role,
        )
    km(1, 30000142, 587, 100, 500, now, Killmail.HomeRole.VICTIM)
    km(2, 30002053, 588, 200, 600, now, Killmail.HomeRole.ATTACKER)
    km(3, 30000142, 587, 100, 500, old, Killmail.HomeRole.VICTIM)

    # Filter by victim character 100 -> km 1 and 3 only.
    body = client.get("/killboard/?character_id=100").content
    assert b"id/1/" not in body  # (rows link by killmail id; we assert via count below)
    page = client.get("/killboard/?character_id=100").context["page"]
    assert {k.killmail_id for k in page} == {1, 3}
    # Add a 30-day window -> drops the 40-day-old km 3.
    page = client.get("/killboard/?character_id=100&days=30").context["page"]
    assert {k.killmail_id for k in page} == {1}
    # System filter.
    page = client.get("/killboard/?system_id=30002053").context["page"]
    assert {k.killmail_id for k in page} == {2}
    # Active-filter chips are surfaced for removal.
    ctx = client.get("/killboard/?corporation_id=500").context
    assert ctx["active_filters"][0]["label"] == "Corp"


# --- Module F: profitable to build --------------------------------------------
@pytest.mark.django_db
def test_build_opportunities(priced_sde):
    from apps.market.services import build_opportunities
    loc = MarketLocation.objects.create(name="Jita", location_type="system", region_id=10000002)
    # Rifter (587) builds from 32000 Trit(5) + 6000 Pye(12) = 232000; sell it high.
    MarketPrice.objects.create(type_id=587, location=loc, sell_min=Decimal("1000000"),
                               profile=MarketPrice.Profile.JITA_SELL)
    ops = build_opportunities(min_profit=1, limit=10)
    assert any(o["type_id"] == 587 for o in ops)
    rifter = next(o for o in ops if o["type_id"] == 587)
    assert rifter["build_cost"] == Decimal("232000")
    assert rifter["profit"] == Decimal("768000")  # 1,000,000 - 232,000


# --- Module D/E: project stock reservation ------------------------------------
@pytest.mark.django_db
def test_reserve_and_release_project_stock(client, django_user_model, priced_sde):
    from apps.industry.services import compute_project_bom
    owner = _member(django_user_model, "lead")
    project = IndustryProject.objects.create(name="Build Rifters", created_by=owner, assigned_to=owner)
    IndustryProjectItem.objects.create(
        project=project, type_id=587, quantity=1,
        build_or_buy=IndustryProjectItem.BuildOrBuy.BUILD,
    )
    compute_project_bom(project)
    # Stock some Tritanium so there's something to reserve.
    stock = Stockpile.objects.create(name="Staging")
    record_manual_stock(stock, type_id=34, quantity_current=50000)

    client.force_login(owner)
    client.post(f"/industry/plans/{project.pk}/reserve/")
    active = StockReservation.objects.filter(project=project, status=StockReservation.Status.ACTIVE)
    assert active.exists()
    reserved_trit = sum(r.quantity_reserved for r in active)
    assert reserved_trit == 32000  # capped at the Rifter's Trit requirement

    # Release returns it all.
    client.post(f"/industry/plans/{project.pk}/release/")
    assert not StockReservation.objects.filter(
        project=project, status=StockReservation.Status.ACTIVE
    ).exists()


@pytest.mark.django_db
def test_reserve_requires_project_manager(client, django_user_model, sde):
    owner = _member(django_user_model, "owner")
    other = _member(django_user_model, "other")
    project = IndustryProject.objects.create(name="P", created_by=owner, assigned_to=owner)
    client.force_login(other)
    assert client.post(f"/industry/plans/{project.pk}/reserve/").status_code == 403
