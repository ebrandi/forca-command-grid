"""The set_language endpoint: persistence, cookie, and the open-redirect guard (D9, D18)."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory

from apps.identity.models import User
from core.i18n import config
from core.i18n.views import set_language

pytestmark = pytest.mark.django_db


def _post(data, *, user=None, secure=False):
    request = RequestFactory().post("/i18n/setlang/", data, secure=secure)
    request.user = user or AnonymousUser()
    request.real_user = request.user
    return request


def test_sets_cookie_and_redirects_to_safe_next():
    config.set_i18n_config(locales={"de": True})
    resp = set_language(_post({"language": "de", "next": "/dashboard/"}))
    assert resp.status_code == 302 and resp["Location"] == "/dashboard/"
    assert resp.cookies["forca_language"].value == "de"


def test_persists_authenticated_preference():
    config.set_i18n_config(locales={"de": True})
    user = User.objects.create(username="eve:1")
    set_language(_post({"language": "de", "next": "/"}, user=user))
    user.refresh_from_db()
    assert user.language == "de"


def test_open_redirect_is_rejected():
    config.set_i18n_config(locales={"de": True})
    resp = set_language(_post({"language": "de", "next": "https://evil.example/pwn"}))
    assert resp["Location"] == "/"
    # A same-origin next still works and the choice is still applied.
    assert resp.cookies["forca_language"].value == "de"


def test_invalid_locale_is_not_applied():
    config.set_i18n_config(locales={"de": True})
    user = User.objects.create(username="eve:1", language="")
    resp = set_language(_post({"language": "zz", "next": "/"}, user=user))
    user.refresh_from_db()
    assert user.language == ""
    assert "forca_language" not in resp.cookies


def test_disabled_locale_is_not_applied():
    # 'ja' is a valid LANGUAGES code but not enabled → must be refused.
    config.set_i18n_config(locales={"de": True})
    resp = set_language(_post({"language": "ja", "next": "/"}))
    assert "forca_language" not in resp.cookies


def test_get_not_allowed():
    request = RequestFactory().get("/i18n/setlang/")
    request.user = AnonymousUser()
    request.real_user = request.user
    assert set_language(request).status_code == 405
