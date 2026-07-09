"""RDY-2 (roadmap 2.6) — guided "activate readiness" wizard.

Acceptance: the wizard previews each disabled dimension's would-be score + recommended
weight, and a dimension can be enabled from the wizard in one click.
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.readiness import config
from apps.readiness.services import (
    activation_preview,
    enable_dimension,
    invalidate_activation_preview,
)
from core import rbac
from tests._raffle_utils import enrol_pilot

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _reset():
    config.reset("dimensions")
    invalidate_activation_preview()
    yield
    config.reset("dimensions")
    invalidate_activation_preview()


def test_preview_lists_only_disabled_dimensions(sde):
    rows = activation_preview()
    assert rows, "expected some net-new dimensions to ship disabled"
    keys = {r["key"] for r in rows}
    # default-enabled dimensions must not appear as activation candidates
    assert "doctrine" not in keys and "skill" not in keys
    for r in rows:
        assert "recommended_weight" in r and "score" in r and "label" in r


def test_enable_dimension_activates_and_is_idempotent(sde):
    key = activation_preview()[0]["key"]
    assert config.get("dimensions").get(key, {}).get("enabled", True) is False
    assert enable_dimension(key) is True
    dims = config.get("dimensions")
    assert dims[key]["enabled"] is True
    assert dims[key]["weight"] > 0
    assert enable_dimension(key) is False  # already enabled → no-op


def test_enable_unknown_dimension_returns_false():
    assert enable_dimension("not_a_real_dimension") is False


def test_enabling_removes_it_from_the_preview(sde):
    rows = activation_preview()
    key = rows[0]["key"]
    enable_dimension(key)
    assert key not in {r["key"] for r in activation_preview()}


def test_wizard_view_director_can_preview_and_enable(client, django_user_model, sde):
    key = activation_preview()[0]["key"]
    user, _ = enrol_pilot(django_user_model, 7700, roles=(rbac.ROLE_DIRECTOR,))
    client.force_login(user)
    url = reverse("admin_audit:readiness_wizard")
    assert client.get(url).status_code == 200
    resp = client.post(url, {"dimension": key})
    assert resp.status_code in (302, 200)
    assert config.get("dimensions")[key]["enabled"] is True


def test_wizard_forbidden_below_director(client, django_user_model):
    user, _ = enrol_pilot(django_user_model, 7701, roles=(rbac.ROLE_OFFICER,))
    client.force_login(user)
    assert client.get(reverse("admin_audit:readiness_wizard")).status_code in (302, 403, 404)
