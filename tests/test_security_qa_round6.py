"""Security QA round 6 — regression tests for the multi-lane review fixes.

Covers: authenticated-page no-store caching (M2), absolute session cap (L2), and the
object-level authorization gaps closed in operations/store/industry (L7–L10). The
pingboard dispatch-floor (M1), Telegram webhook + verify-code TTL (L4/L5) and the
Director stale-affiliation co-check (L1) have dedicated tests in test_pingboard_* /
test_director_autogrant.
"""
from __future__ import annotations

import time

import pytest
from django.urls import reverse

from apps.identity.models import RoleAssignment
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac


def _user(dj, uid, role=rbac.ROLE_MEMBER):
    u = dj.objects.create(username=f"qa6-{uid}")
    RoleAssignment.objects.create(user=u, role=ensure_role(role))
    EveCharacter.objects.create(character_id=uid, user=u, name=f"P{uid}",
                                is_main=True, is_corp_member=True)
    return u


# --- M2: sensitive-data caching ---------------------------------------------
@pytest.mark.django_db
def test_authenticated_pages_are_no_store(client, django_user_model):
    client.force_login(_user(django_user_model, 6003))
    resp = client.get(reverse("pingboard:dashboard"))
    assert "no-store" in resp.headers.get("Cache-Control", "")


@pytest.mark.django_db
def test_anonymous_public_page_not_forced_no_store(client):
    # An anonymous request to a public page is not force-marked no-store (edge caching
    # of public pages still works); only authenticated responses get the directive.
    resp = client.get("/")
    assert "no-store" not in resp.headers.get("Cache-Control", "")


# --- L2: absolute session lifetime cap --------------------------------------
@pytest.mark.django_db
def test_absolute_session_cap_logs_out_stale_session(client, django_user_model, settings):
    settings.SESSION_ABSOLUTE_MAX_AGE = 3600  # 1h
    client.force_login(_user(django_user_model, 6001))
    s = client.session
    s["_auth_started_at"] = time.time() - 7200  # 2h ago → past the cap
    s.save()
    resp = client.get(reverse("pingboard:dashboard"))
    assert resp.status_code == 302 and settings.LOGIN_URL in resp["Location"]


@pytest.mark.django_db
def test_absolute_session_cap_allows_fresh_session(client, django_user_model, settings):
    settings.SESSION_ABSOLUTE_MAX_AGE = 3600
    client.force_login(_user(django_user_model, 6002))
    # First authenticated request stamps the start and is allowed through.
    assert client.get(reverse("pingboard:dashboard")).status_code == 200


# --- L7: draft operations are not readable by members via direct pk ----------
@pytest.mark.django_db
def test_member_cannot_read_draft_operation(client, django_user_model):
    from apps.operations.models import Operation

    op = Operation.objects.create(name="Secret", type=Operation.Type.HOME_DEFENCE,
                                  status=Operation.Status.DRAFT)
    client.force_login(_user(django_user_model, 6101))
    assert client.get(reverse("operations:detail", args=[op.pk])).status_code == 404


@pytest.mark.django_db
def test_officer_can_read_draft_operation(client, django_user_model):
    from apps.operations.models import Operation

    op = Operation.objects.create(name="Secret", type=Operation.Type.HOME_DEFENCE,
                                  status=Operation.Status.DRAFT)
    client.force_login(_user(django_user_model, 6102, rbac.ROLE_OFFICER))
    assert client.get(reverse("operations:detail", args=[op.pk])).status_code == 200


# --- L8: store order detail is object-scoped (no pk-enumeration IDOR) ---------
def _store_corp_audience():
    from apps.store.models import Audience, StoreConfig
    from apps.store.services import invalidate_audience_cache

    cfg = StoreConfig.get_solo() if hasattr(StoreConfig, "get_solo") else StoreConfig.objects.first()
    if cfg is None:
        cfg = StoreConfig.objects.create()
    cfg.audience = Audience.CORP
    cfg.save(update_fields=["audience"])
    invalidate_audience_cache()


@pytest.mark.django_db
def test_store_order_detail_blocks_other_members(client, django_user_model):
    from apps.store.models import StoreOrder

    _store_corp_audience()
    buyer = _user(django_user_model, 6201)
    order = StoreOrder.objects.create(buyer=buyer, ship_type_id=587,
                                      status=StoreOrder.Status.CANCELLED)
    # A different corp member (passes the store audience gate) cannot read it.
    client.force_login(_user(django_user_model, 6202))
    assert client.get(reverse("store:order", args=[order.pk])).status_code == 403
    # The buyer still can (confirms the gate itself allows members through).
    client.force_login(buyer)
    assert client.get(reverse("store:order", args=[order.pk])).status_code == 200


@pytest.mark.django_db
def test_store_order_detail_denies_anonymous_under_public_audience(client, django_user_model):
    """Regression for the null-identity bypass: under a PUBLIC store audience an
    anonymous visitor passes the audience gate, and buyer/claimer can be NULL — the
    scope check must not let None==None grant access."""
    from apps.store.models import Audience, StoreConfig, StoreOrder
    from apps.store.services import invalidate_audience_cache

    cfg = StoreConfig.objects.first() or StoreConfig.objects.create()
    cfg.audience = Audience.PUBLIC
    cfg.save(update_fields=["audience"])
    invalidate_audience_cache()

    buyer = _user(django_user_model, 6211)
    open_order = StoreOrder.objects.create(buyer=buyer, ship_type_id=587,
                                           status=StoreOrder.Status.OPEN)
    orphan = StoreOrder.objects.create(buyer=None, ship_type_id=588,  # buyer FK SET_NULL
                                       status=StoreOrder.Status.CANCELLED)
    # Anonymous (no login) must not read either, despite PUBLIC letting them "shop".
    assert client.get(reverse("store:order", args=[open_order.pk])).status_code == 403
    assert client.get(reverse("store:order", args=[orphan.pk])).status_code == 403


# --- L7b: draft ops reject writes (rsvp/commit), not just reads --------------
@pytest.mark.django_db
def test_member_cannot_rsvp_or_commit_draft_op(client, django_user_model):
    from apps.operations.models import Operation

    op = Operation.objects.create(name="Secret", type=Operation.Type.HOME_DEFENCE,
                                  status=Operation.Status.DRAFT)
    client.force_login(_user(django_user_model, 6103))
    assert client.post(reverse("operations:rsvp", args=[op.pk]),
                       {"response": "yes"}).status_code == 404
    assert client.post(reverse("operations:commit", args=[op.pk]), {}).status_code == 404


# --- L9: corp-wide staging default is persisted only via POST (not GET/CSRF) --
@pytest.mark.django_db
def test_supply_forecast_persist_requires_post(client, django_user_model):
    from apps.admin_audit.models import AppSetting
    from apps.sde.models import SdeRegion, SdeSolarSystem

    r = SdeRegion.objects.create(region_id=10000002, name="The Forge")
    SdeSolarSystem.objects.create(system_id=30000142, region=r, name="Jita", security=0.9)
    client.force_login(_user(django_user_model, 6301, rbac.ROLE_OFFICER))
    # GET preview must NOT persist the corp-wide default (CSRF-on-GET closed).
    assert client.get(reverse("store:supply_forecast") + "?staging=Jita").status_code == 200
    assert not AppSetting.objects.filter(key="store.staging_system_id").exists()
    # An explicit (CSRF-protected) POST does persist it.
    client.post(reverse("store:supply_forecast"), {"staging": "Jita"})
    assert AppSetting.get("store.staging_system_id", {}).get("name") == "Jita"


# --- L10: claiming a project requires visibility of it -----------------------
@pytest.mark.django_db
def test_member_cannot_claim_hidden_project(client, django_user_model):
    from apps.industry.models import IndustryProject

    creator = _user(django_user_model, 6401)
    proj = IndustryProject.objects.create(
        name="secret", created_by=creator, visibility=IndustryProject.Visibility.LEADERSHIP)
    client.force_login(_user(django_user_model, 6402))
    assert client.post(reverse("industry:claim", args=[proj.pk])).status_code == 403
    # A CORP-visibility project stays claimable by any member.
    open_proj = IndustryProject.objects.create(
        name="shared", created_by=creator, visibility=IndustryProject.Visibility.CORP)
    assert client.post(reverse("industry:claim", args=[open_proj.pk])).status_code == 302
