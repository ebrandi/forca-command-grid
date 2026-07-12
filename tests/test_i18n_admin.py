"""Localisation console (admin_audit): Director-gated i18n.config panel.

Covers officer/director gating, GET render, POST persistence via get_i18n_config,
invalid-locale rejection, and the always-on English invariant. Kept English-default
so the rest of the suite's English content-assertions stay green.
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.admin_audit.models import AuditLog
from apps.identity.models import RoleAssignment
from apps.sso.services import ensure_role
from core import rbac
from core.i18n import config


def _user(django_user_model, uid, role):
    u = django_user_model.objects.create(username=f"i18nadm-{uid}")
    RoleAssignment.objects.create(user=u, role=ensure_role(role))
    return u


def _base_post(**extra):
    """A minimal valid POST: English only, English default/broadcast, both toggles on."""
    post = {"locale": ["en"], "default": "en", "broadcast_locale": "en",
            "browser_detection": "on", "anon_selection": "on"}
    post.update(extra)
    return post


@pytest.mark.django_db
def test_officer_and_member_forbidden(client, django_user_model):
    url = reverse("admin_audit:i18n_settings")
    client.force_login(_user(django_user_model, 1, rbac.ROLE_OFFICER))
    assert client.get(url).status_code == 403
    client.force_login(_user(django_user_model, 2, rbac.ROLE_MEMBER))
    assert client.get(url).status_code == 403


@pytest.mark.django_db
def test_director_get_renders(client, django_user_model):
    client.force_login(_user(django_user_model, 3, rbac.ROLE_DIRECTOR))
    resp = client.get(reverse("admin_audit:i18n_settings"))
    assert resp.status_code == 200
    body = resp.content.decode()
    # Native labels, never English names, for the offered languages.
    assert "Deutsch" in body
    assert "Português (Brasil)" in body


@pytest.mark.django_db
def test_post_enables_locale_persists(client, django_user_model):
    client.force_login(_user(django_user_model, 4, rbac.ROLE_DIRECTOR))
    resp = client.post(reverse("admin_audit:i18n_settings"),
                       _base_post(locale=["en", "de"], default="de", broadcast_locale="de"))
    assert resp.status_code == 302
    cfg = config.get_i18n_config()
    assert cfg["locales"]["de"] is True
    assert cfg["default"] == "de" and cfg["broadcast_locale"] == "de"
    assert "de" in config.enabled_locales()
    assert AuditLog.objects.filter(action="i18n.config.update").exists()


@pytest.mark.django_db
def test_toggles_persist(client, django_user_model):
    client.force_login(_user(django_user_model, 5, rbac.ROLE_DIRECTOR))
    # Both unchecked → not present in POST → stored False.
    post = _base_post()
    del post["browser_detection"]
    del post["anon_selection"]
    client.post(reverse("admin_audit:i18n_settings"), post)
    cfg = config.get_i18n_config()
    assert cfg["browser_detection"] is False
    assert cfg["anon_selection"] is False


@pytest.mark.django_db
def test_invalid_locale_rejected(client, django_user_model):
    client.force_login(_user(django_user_model, 6, rbac.ROLE_DIRECTOR))
    resp = client.post(reverse("admin_audit:i18n_settings"),
                       _base_post(locale=["en", "klingon", "de"]))
    assert resp.status_code == 302  # never 500 — unknown code is dropped, not fatal
    cfg = config.get_i18n_config()
    assert "klingon" not in cfg["locales"]
    assert cfg["locales"]["de"] is True   # the valid one alongside it still took
    assert cfg["locales"]["en"] is True


@pytest.mark.django_db
def test_invalid_default_falls_back_to_english(client, django_user_model):
    client.force_login(_user(django_user_model, 7, rbac.ROLE_DIRECTOR))
    client.post(reverse("admin_audit:i18n_settings"),
                _base_post(default="klingon", broadcast_locale="klingon"))
    cfg = config.get_i18n_config()
    assert cfg["default"] == "en"
    assert cfg["broadcast_locale"] == "en"


@pytest.mark.django_db
def test_english_cannot_be_disabled(client, django_user_model):
    client.force_login(_user(django_user_model, 8, rbac.ROLE_DIRECTOR))
    # POST omits "en" entirely (as a tampered form might) — it must stay enabled.
    client.post(reverse("admin_audit:i18n_settings"),
                {"locale": ["de"], "default": "de", "broadcast_locale": "de"})
    cfg = config.get_i18n_config()
    assert cfg["locales"]["en"] is True
    assert "en" in config.enabled_locales()


@pytest.mark.django_db
def test_hub_links_i18n_for_director_only(client, django_user_model):
    client.force_login(_user(django_user_model, 9, rbac.ROLE_DIRECTOR))
    assert b"/ops/admin/i18n/" in client.get(reverse("admin_audit:console")).content
    client.force_login(_user(django_user_model, 10, rbac.ROLE_OFFICER))
    assert b"/ops/admin/i18n/" not in client.get(reverse("admin_audit:console")).content
