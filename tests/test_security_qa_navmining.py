"""Regression tests for this round's adversarial-QA fixes: open redirects, input
bounds on mining payouts, and decompression-bomb caps on the EVE Ref importers."""
from __future__ import annotations

import datetime as dt
import io

import pytest
from django.test import RequestFactory
from django.urls import reverse

from apps.identity.models import RoleAssignment
from apps.mining.models import MiningPayout
from apps.sso.services import ensure_role
from core import rbac


# --- Open redirect ----------------------------------------------------------
def test_safe_next_rejects_offsite():
    from core.redirects import safe_next

    req = RequestFactory().get("/")  # default host "testserver" (in ALLOWED_HOSTS)
    assert safe_next(req, "https://evil.example/", "/fallback/") == "/fallback/"
    assert safe_next(req, "//evil.example", "/fallback/") == "/fallback/"   # protocol-relative
    assert safe_next(req, None, "/fallback/") == "/fallback/"
    assert safe_next(req, "/local/path/", "/fallback/") == "/local/path/"   # same-host OK


@pytest.mark.django_db
def test_toggle_recognition_ignores_external_next(client, django_user_model):
    user = django_user_model.objects.create(username="eve:secn1")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))  # pass the gate
    client.force_login(user)
    resp = client.post(reverse("pilots:toggle_recognition"), {"next": "https://evil.example/"})
    assert resp.status_code == 302 and "evil.example" not in resp.url
    resp2 = client.post(reverse("pilots:toggle_recognition"), {"next": "/some/local/path/"})
    assert resp2.url == "/some/local/path/"   # a same-host next is honoured verbatim


# --- Mining pool bounds + finalise guard ------------------------------------
def _officer(django_user_model, name):
    user = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_OFFICER))
    return user


@pytest.mark.django_db
def test_payout_create_rejects_bad_pool(client, django_user_model):
    client.force_login(_officer(django_user_model, "eve:secn2"))
    today = dt.date.today().isoformat()
    base = {"name": "X", "period_start": today, "period_end": today, "method": "by_value"}

    client.post("/mining/payouts/create/", {**base, "pool_isk": "-5"})       # negative
    client.post("/mining/payouts/create/", {**base, "pool_isk": "1e20"})     # over cap
    assert not MiningPayout.objects.exists()

    client.post("/mining/payouts/create/", {**base, "name": "OK", "pool_isk": "1000000"})
    assert MiningPayout.objects.filter(name="OK").exists()


@pytest.mark.django_db
def test_payout_finalise_is_idempotent(client, django_user_model):
    today = dt.date.today()
    payout = MiningPayout.objects.create(name="P", period_start=today, period_end=today,
                                         status=MiningPayout.Status.FINAL)
    client.force_login(_officer(django_user_model, "eve:secn3"))
    resp = client.post(f"/mining/payouts/{payout.pk}/finalise/")
    assert resp.status_code == 302
    payout.refresh_from_db()
    assert payout.status == MiningPayout.Status.FINAL


# --- Decompression-bomb caps ------------------------------------------------
class _FakeResp:
    def __init__(self, parts):
        self._parts = parts

    def iter_content(self, chunk_size=0):
        yield from self._parts


def test_download_to_buffer_caps():
    from core.netcap import DataTooLarge, download_to_buffer

    with pytest.raises(DataTooLarge):
        download_to_buffer(_FakeResp([b"x" * 100, b"y" * 100]), max_bytes=150)
    buf = download_to_buffer(_FakeResp([b"x" * 100, b"y" * 100]), max_bytes=1000)
    assert buf.read() == b"x" * 100 + b"y" * 100


def test_capped_text_caps():
    from core.netcap import DataTooLarge, capped_text

    text = capped_text(io.BytesIO(b"abcdef\n" * 1000), max_bytes=50)
    with pytest.raises(DataTooLarge):
        text.read()
    ok = capped_text(io.BytesIO(b"hello\n"), max_bytes=1000)
    assert ok.read() == "hello\n"
