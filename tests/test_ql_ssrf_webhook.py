"""KB-30 webhook delivery — the SSRF screen must bind the socket, not just the name.

CodeQL flags ``requests.post(sub.webhook_url, ...)`` as a full SSRF sink. The screen in front
of it (``resolve_webhook_target``: https-only, getaddrinfo over EVERY address the host maps to,
refuse if any is private/loopback/reserved, ``allow_redirects=False``) is strong, but it used
to be advisory: ``requests`` performed its own second DNS lookup at connect time, so a host
whose owner controls DNS could answer the check with a public address and the connection with
127.0.0.1 or 169.254.169.254 — classic DNS rebinding, and the one case the docstring wrongly
claimed was covered.

These tests simulate the rebind (a resolver that flips its answer after the first call) rather
than asserting the validator in isolation, and they pin the two properties the fix must keep
together: the socket goes to a *cleared* address, and the TLS identity is still the hostname
(SNI + certificate match), because trading SSRF for a certificate bypass would be worse.
"""
from __future__ import annotations

import io
import socket
from types import SimpleNamespace

import pytest
import requests
import urllib3.util.connection
from urllib3 import HTTPResponse

from apps.killboard import subscriptions as subs

HOST = "hook.rebind.example"
URL = f"https://{HOST}/deliver"
PUBLIC_IP = "93.184.216.34"
PRIVATE_IP = "127.0.0.1"
METADATA_IP = "169.254.169.254"

_real_getaddrinfo = socket.getaddrinfo


class _RebindingResolver:
    """``HOST`` answers ``first`` once, then ``then`` forever. Everything else resolves for real.

    This is exactly the attacker capability the TOCTOU needs: authority over one hostname's DNS,
    with a short/zero TTL. IP literals still resolve normally, so the code under test can dial a
    pinned address through this resolver just as it would in production.
    """

    def __init__(self, first: str, then: str):
        self.first = first
        self.then = then
        self.host_lookups = 0

    def __call__(self, host, port, *args, **kwargs):
        if host != HOST:
            return _real_getaddrinfo(host, port, *args, **kwargs)
        self.host_lookups += 1
        addr = self.first if self.host_lookups == 1 else self.then
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (addr, port))]


@pytest.fixture
def dialled(monkeypatch):
    """Record the address each connection attempt would actually land on, and connect to none.

    urllib3 asks ``util.connection.create_connection`` for the socket; whatever host it is given
    is resolved there. Resolving it here the same way urllib3 would is what makes the assertion
    about the *landing* address rather than about a string in a URL.
    """
    landed: list[str] = []

    def _spy(address, *args, **kwargs):
        host, port = address[0], address[1]
        landed.append(socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)[0][4][0])
        raise ConnectionRefusedError("no sockets in tests")

    monkeypatch.setattr(urllib3.util.connection, "create_connection", _spy)
    return landed


def _sub(url: str = URL):
    """The two attributes ``_deliver_webhook`` reads off a subscription — no DB needed."""
    return SimpleNamespace(webhook_url=url, id=1)


def _item():
    return SimpleNamespace(
        event_type="my_loss", title="t", summary="s", link="", killmail_id=None, payload={},
    )


# --------------------------------------------------------------------------- #
#  The residual the alert is really about: DNS rebinding between check and connect
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("rebind_to", [PRIVATE_IP, METADATA_IP, "10.0.0.7"])
def test_rebinding_after_the_screen_cannot_move_the_socket(monkeypatch, dialled, rebind_to):
    """The POST lands on the address the screen cleared, not on the rebound answer."""
    resolver = _RebindingResolver(first=PUBLIC_IP, then=rebind_to)
    monkeypatch.setattr(socket, "getaddrinfo", resolver)

    ok, err = subs._deliver_webhook(_sub(), _item())

    assert dialled, "no connection was attempted at all"
    assert set(dialled) == {PUBLIC_IP}, f"connection landed on {dialled}"
    assert rebind_to not in dialled
    assert ok is False and "request failed" in err  # refused socket → a normal delivery failure


def test_the_host_is_resolved_exactly_once_per_delivery(monkeypatch, dialled):
    """One lookup per delivery, retries included: every extra lookup is another chance to lie."""
    resolver = _RebindingResolver(first=PUBLIC_IP, then=PRIVATE_IP)
    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    monkeypatch.setattr(subs, "_webhook_retries", lambda: 1)

    subs._deliver_webhook(_sub(), _item())

    assert len(dialled) == 2, "the retry did not happen, so this proves nothing"
    assert resolver.host_lookups == 1


def test_a_host_that_screens_private_is_refused_before_any_socket(monkeypatch, dialled):
    """The first answer is the private one: nothing is dialled, and the reason is user-facing."""
    resolver = _RebindingResolver(first=METADATA_IP, then=PUBLIC_IP)
    monkeypatch.setattr(socket, "getaddrinfo", resolver)

    ok, err = subs._deliver_webhook(_sub(), _item())

    assert ok is False
    assert "private, loopback or reserved" in err
    assert dialled == []


# --------------------------------------------------------------------------- #
#  Pinning must not become a TLS bypass
# --------------------------------------------------------------------------- #
def _adapter(address: str = PUBLIC_IP, host: str = HOST, port: int = 443):
    target = subs.WebhookTarget(host=host, port=port, addresses=(address,))
    return subs._pinned_adapter_class()(target, address)


def test_tls_identity_is_the_hostname_never_the_pinned_address():
    """SNI and the certificate hostname check must still be made against the DNS name."""
    assert _adapter().poolmanager.connection_pool_kw["server_hostname"] == HOST


def test_tls_identity_survives_an_outbound_proxy():
    """Proxy managers do not inherit the pool kwargs, so the adapter must re-apply them."""
    manager = _adapter().proxy_manager_for("http://proxy.internal:3128")
    assert manager.connection_pool_kw["server_hostname"] == HOST


@pytest.mark.parametrize("url,expected_url,expected_host", [
    (URL, f"https://{PUBLIC_IP}/deliver", HOST),
    (f"https://{HOST}:8443/deliver", f"https://{PUBLIC_IP}:8443/deliver", f"{HOST}:8443"),
])
def test_only_the_socket_target_is_rewritten(monkeypatch, url, expected_url, expected_host):
    """The wire request keeps the original Host header; only the dialled authority changes."""
    seen = {}

    def _capture(self, request, **kwargs):
        seen["url"] = request.url
        seen["host"] = request.headers.get("Host")
        return SimpleNamespace(status_code=204)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _capture)
    _adapter().send(requests.Request("POST", url, json={}).prepare())

    assert seen["url"] == expected_url
    assert seen["host"] == expected_host


def test_redirects_are_never_followed(monkeypatch):
    """A 3xx must not be able to walk the POST off the cleared target onto an internal host."""
    seen = {}

    def _request(self, method, url, **kwargs):
        seen.update(method=method, url=url, allow_redirects=kwargs.get("allow_redirects"))
        resp = requests.Response()
        resp.status_code = 204
        resp.raw = HTTPResponse(body=io.BytesIO(b""), status=204, preload_content=False)
        return resp

    monkeypatch.setattr(requests.Session, "request", _request)
    target = subs.WebhookTarget(host=HOST, port=443, addresses=(PUBLIC_IP,))
    subs.post_to_webhook(URL, target, json={}, timeout=1)

    assert seen["allow_redirects"] is False
    assert seen["url"] == URL  # the adapter, not the caller, does the address swap


def test_the_adapter_refuses_a_host_it_was_not_cleared_for(monkeypatch):
    """A pinned connection can never be borrowed for another host (e.g. a followed redirect)."""
    monkeypatch.setattr(
        requests.adapters.HTTPAdapter, "send",
        lambda self, request, **kw: pytest.fail("an uncleared host reached the transport"),
    )
    with pytest.raises(requests.RequestException):
        _adapter().send(requests.Request("POST", "https://elsewhere.example/x").prepare())


# --------------------------------------------------------------------------- #
#  Legitimate multi-record webhooks must keep working
# --------------------------------------------------------------------------- #
def test_all_cleared_addresses_are_tried_before_the_delivery_fails(monkeypatch, dialled):
    """A dual-stack / multi-front-end host keeps urllib3's failover across cleared addresses."""
    second = "93.184.216.35"
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *a, **kw: (
        [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))
         for ip in (PUBLIC_IP, second)]
        if host == HOST else _real_getaddrinfo(host, port, *a, **kw)
    ))
    monkeypatch.setattr(subs, "_webhook_retries", lambda: 0)

    ok, _err = subs._deliver_webhook(_sub(), _item())

    assert ok is False
    assert dialled == [PUBLIC_IP, second]  # every cleared address got its turn


def test_a_reachable_address_ends_the_attempt(monkeypatch):
    """The first address that ANSWERS is the delivery — no further addresses are dialled."""
    target = subs.WebhookTarget(host=HOST, port=443, addresses=(PUBLIC_IP, "93.184.216.35"))
    sent = []

    def _capture(self, request, **kwargs):
        sent.append(request.url)
        resp = requests.Response()
        resp.status_code = 204
        resp.url = request.url
        resp.raw = HTTPResponse(body=io.BytesIO(b""), status=204, preload_content=False)
        return resp

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _capture)
    resp = subs.post_to_webhook(URL, target, json={"a": 1}, timeout=1)

    assert resp.status_code == 204
    assert sent == [f"https://{PUBLIC_IP}/deliver"]
