"""Logging in lands you where you were going — and nowhere an attacker chose.

The feature gate and every ``@login_required`` view send a signed-out pilot to
``/auth/eve/login/?next=<where they were>``. The SSO flow used to ignore that entirely and
drop everyone on ``LOGIN_REDIRECT_URL``, so "log in to see this page" ended on the dashboard
and the pilot had to find their way back by hand.

``next`` is carried in the SESSION, never through EVE's OAuth parameters. Anything that
round-trips through the identity provider comes back under the client's control, and a
``next`` the client controls is an open redirect — a ready-made phishing lure that starts on
a real, trusted forca.club URL. It is validated going in and again coming out.
"""
from __future__ import annotations

import pytest
from django.urls import reverse

HOSTILE = [
    "https://evil.example/phish",       # absolute, foreign host
    "//evil.example/phish",             # scheme-relative — still leaves the site
    "http:/\\/\\evil.example",          # backslash trick some parsers mis-read
    "javascript:alert(1)",              # not even http
]


def _start_login(client, next_url=None):
    url = reverse("sso:login")
    return client.get(f"{url}?next={next_url}" if next_url else url)


@pytest.mark.django_db
def test_a_safe_next_is_remembered_for_the_callback(client):
    _start_login(client, "/doctrines/")
    assert client.session["eve_sso_next"] == "/doctrines/"


@pytest.mark.django_db
@pytest.mark.parametrize("hostile", HOSTILE)
def test_an_off_site_next_is_refused(client, hostile):
    """An open redirect off the back of a real login URL is a phishing primitive."""
    _start_login(client, hostile)
    assert "eve_sso_next" not in client.session, f"{hostile!r} must never be stored"


@pytest.mark.django_db
def test_a_stale_next_cannot_hijack_a_later_login(client):
    """Abandon a gated login, then log in normally: you belong on the dashboard, not on
    whatever page you bounced off ten minutes ago."""
    _start_login(client, "/doctrines/")
    assert client.session["eve_sso_next"] == "/doctrines/"

    _start_login(client)  # a plain login, no next
    assert "eve_sso_next" not in client.session


@pytest.mark.django_db
def test_the_gate_and_the_login_agree_on_the_parameter_name(client):
    """The redirect the feature gate emits must be one this login view actually understands —
    if the two ever disagree on the parameter name, the bounce silently stops working."""
    resp = client.get("/doctrines/")
    assert resp.status_code == 302
    assert resp.url == f"{reverse('sso:login')}?next=/doctrines/"

    client.get(resp.url)
    assert client.session["eve_sso_next"] == "/doctrines/"
