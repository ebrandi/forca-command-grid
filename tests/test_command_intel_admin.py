"""Command Intelligence admin console pages (Director-gated config; doc 13 §3).

The provider page proves the shared write contract end to end: a Director can read
it and save a valid change (audited, version-bumped), a non-Director is refused, and
an invalid value is rejected with a surfaced error and no write (the validator raises
``ConfigError`` *before* any persist — no partial writes).
"""
from __future__ import annotations

import pytest
from django.core.cache import cache
from django.urls import reverse

from apps.admin_audit.models import AuditLog
from apps.command_intel import config
from apps.identity.models import RoleAssignment
from apps.sso.services import ensure_role
from core import rbac


def _user(django_user_model, role, username):
    user, _ = django_user_model.objects.get_or_create(username=username)
    RoleAssignment.objects.get_or_create(user=user, role=ensure_role(role))
    return user


def _director(django_user_model):
    return _user(django_user_model, rbac.ROLE_DIRECTOR, "ci-director")


@pytest.fixture(autouse=True)
def _clear_config_cache():
    # config.get caches the merged document process-wide; clear so each test reads
    # the shipped defaults rather than a value cached by a sibling test.
    cache.clear()
    yield
    cache.clear()


@pytest.mark.django_db
def test_director_can_view_provider_page(client, django_user_model):
    client.force_login(_director(django_user_model))
    resp = client.get(reverse("admin_audit:command_intel_provider"))
    assert resp.status_code == 200
    # The page shows the configured model but never offers a field for the secret key.
    assert b"MiniMax-M2.7" in resp.content
    lower = resp.content.lower()
    assert b'name="api_key"' not in lower and b'name="llm_api_key"' not in lower
    assert config.get("provider")["model"] not in (None, "")


@pytest.mark.django_db
def test_director_saves_valid_model_change(client, django_user_model):
    client.force_login(_director(django_user_model))
    resp = client.post(reverse("admin_audit:command_intel_provider"), {"model": "MiniMax-M3"})
    assert resp.status_code == 302  # PRG redirect on success
    assert config.get("provider")["model"] == "MiniMax-M3"
    assert AuditLog.objects.filter(action="command_intel.config.update",
                                   target_id="provider").exists()


@pytest.mark.django_db
def test_non_director_is_forbidden(client, django_user_model):
    client.force_login(_user(django_user_model, rbac.ROLE_MEMBER, "ci-member"))
    resp = client.get(reverse("admin_audit:command_intel_provider"))
    assert resp.status_code == 403


@pytest.mark.django_db
def test_invalid_value_is_rejected_with_no_write(client, django_user_model):
    client.force_login(_director(django_user_model))
    before = config.get("provider")["temperature"]
    resp = client.post(reverse("admin_audit:command_intel_provider"),
                       {"model": "MiniMax-M3", "temperature": "5"}, follow=True)
    assert resp.status_code == 200
    # No partial write — neither the bad field nor the otherwise-valid model persisted.
    assert config.get("provider")["temperature"] == before
    assert config.get("provider")["model"] != "MiniMax-M3"
    msgs = list(resp.context["messages"])
    assert any(m.level_tag == "error" for m in msgs)
    assert not AuditLog.objects.filter(action="command_intel.config.update").exists()


@pytest.mark.django_db
def test_constraints_and_classification_pages_render(client, django_user_model):
    client.force_login(_director(django_user_model))
    assert client.get(reverse("admin_audit:command_intel_constraints")).status_code == 200
    assert client.get(reverse("admin_audit:command_intel_classification")).status_code == 200


@pytest.mark.django_db
def test_classification_floor_is_enforced(client, django_user_model):
    # The server-side floor rejects loosening a tier below its design minimum.
    client.force_login(_director(django_user_model))
    resp = client.post(reverse("admin_audit:command_intel_classification"),
                       {"default": "high_command", "tier_director_eyes_only": "member"},
                       follow=True)
    assert resp.status_code == 200
    assert config.get("classification")["tier_min_rank"]["director_eyes_only"] == "director"
    assert any(m.level_tag == "error" for m in resp.context["messages"])
