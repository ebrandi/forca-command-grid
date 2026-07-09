"""Safe post-action redirects.

``redirect(request.POST["next"])`` is an open-redirect: a crafted ``next`` of
``https://evil.example`` sends the victim off-site (phishing). Validate the target is a
local URL before trusting it.
"""
from __future__ import annotations

from django.utils.http import url_has_allowed_host_and_scheme


def safe_next(request, candidate: str | None, fallback: str) -> str:
    """Return ``candidate`` only if it's a safe same-host URL, else ``fallback``."""
    if candidate and url_has_allowed_host_and_scheme(
        candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return candidate
    return fallback
