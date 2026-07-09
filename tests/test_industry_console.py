"""Phase E: /erp/ redirect, Industry admin settings, job-tracker build queue."""
from __future__ import annotations

from decimal import Decimal

import pytest

from apps.erp.models import BuildJob
from apps.identity.models import RoleAssignment
from apps.industry.models import IndustryEconomyConfig
from apps.sso.services import ensure_role
from core import rbac

pytestmark = pytest.mark.django_db

RIFTER = 587


def _user(django_user_model, name, role):
    u = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=u, role=ensure_role(role))
    return u


def test_erp_redirects_into_job_tracker(client, django_user_model, sde):
    client.force_login(_user(django_user_model, "m", rbac.ROLE_MEMBER))
    resp = client.get("/erp/")
    assert resp.status_code == 302 and resp.headers["Location"] == "/industry/jobs/"


def test_erp_legacy_when_redirect_disabled(client, django_user_model, sde):
    cfg = IndustryEconomyConfig.active()
    cfg.erp_redirects = False
    cfg.save()
    client.force_login(_user(django_user_model, "m", rbac.ROLE_MEMBER))
    assert client.get("/erp/").status_code == 200  # legacy board still available


def test_job_tracker_shows_build_queue(client, django_user_model, priced_sde):
    BuildJob.objects.create(output_type_id=RIFTER, quantity=1, status=BuildJob.Status.QUEUED)
    client.force_login(_user(django_user_model, "m", rbac.ROLE_MEMBER))
    r = client.get("/industry/jobs/")
    assert r.status_code == 200
    assert [q["job"].output_type_id for q in r.context["queued"]] == [RIFTER]


def _config_post():
    cfg = IndustryEconomyConfig.active()
    return {
        "default_market_hub_system_id": cfg.default_market_hub_system_id,
        "default_system_cost_index": "0.0600",
        "default_facility_tax": cfg.default_facility_tax,
        "default_sales_tax": cfg.default_sales_tax,
        "default_broker_fee": cfg.default_broker_fee,
        "corp_buyback_modifier": cfg.corp_buyback_modifier,
        "hauling_cost_per_m3": cfg.hauling_cost_per_m3,
        "default_visibility": cfg.default_visibility,
        "stale_price_hours": cfg.stale_price_hours,
        "allow_pilot_plans": "on",
        # erp_redirects intentionally omitted -> unchecked -> False
    }


def test_admin_settings_officer_can_save(client, django_user_model, sde):
    client.force_login(_user(django_user_model, "off", rbac.ROLE_OFFICER))
    assert client.get("/ops/admin/industry/settings/").status_code == 200
    resp = client.post("/ops/admin/industry/settings/", _config_post())
    assert resp.status_code == 302
    cfg = IndustryEconomyConfig.active()
    assert cfg.default_system_cost_index == Decimal("0.0600")
    assert cfg.erp_redirects is False  # checkbox omitted


def test_admin_settings_blocks_members(client, django_user_model, sde):
    client.force_login(_user(django_user_model, "m", rbac.ROLE_MEMBER))
    assert client.get("/ops/admin/industry/settings/").status_code == 403


def test_console_hub_links_industry_settings(client, django_user_model, sde):
    client.force_login(_user(django_user_model, "off", rbac.ROLE_OFFICER))
    html = client.get("/ops/admin/").content
    assert b"/ops/admin/industry/settings/" in html
