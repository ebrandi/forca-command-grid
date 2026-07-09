"""Shared outbound-HTTP helper for Pingboard providers.

Every provider that carries a bearer token / credential must POST only to its
allowlisted API host (SSRF guard) and must not follow redirects (a 3xx could bounce
the body + credential to an attacker host). This centralises that discipline.
"""
from __future__ import annotations

from urllib.parse import urlparse

import requests


def host_allowed(url: str, allowed_hosts) -> bool:
    p = urlparse(url or "")
    return p.scheme == "https" and p.hostname in set(allowed_hosts or [])


def post_json(url, allowed_hosts, *, json=None, headers=None, timeout=10):
    """Returns ``(response, error)``. On a blocked host / transport error, response is None."""
    if not host_allowed(url, allowed_hosts):
        return None, "outbound host not allowlisted"
    try:
        resp = requests.post(
            url, json=json, headers=headers or {}, timeout=timeout, allow_redirects=False
        )
        return resp, ""
    except requests.RequestException as exc:
        return None, f"request failed: {type(exc).__name__}"


def post_form(url, allowed_hosts, *, data=None, auth=None, timeout=10):
    if not host_allowed(url, allowed_hosts):
        return None, "outbound host not allowlisted"
    try:
        resp = requests.post(
            url, data=data, auth=auth, timeout=timeout, allow_redirects=False
        )
        return resp, ""
    except requests.RequestException as exc:
        return None, f"request failed: {type(exc).__name__}"


def _json_body(resp) -> dict:
    try:
        return resp.json() or {}
    except Exception:  # noqa: BLE001
        return {}
