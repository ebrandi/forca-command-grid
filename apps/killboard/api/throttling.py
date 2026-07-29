"""Rate limits for the killboard REST API (KB-28).

Two scopes, rates in ``settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]``:

* ``killboard_anon`` — anonymous callers (only reachable when KILLBOARD_API_PUBLIC_READ
  is on), keyed by IP. Protects the box when a corp opens the public-read subset.
* ``killboard_user`` — session/token users, keyed by user id (so one member's token traffic
  can't exhaust another's budget).

Both are attached to every killboard API view via the base class; DRF applies each only to
the request class it matches (anon-vs-authenticated), so listing both is correct.
"""
from __future__ import annotations

from rest_framework.throttling import AnonRateThrottle, UserRateThrottle

from core.audit import client_ip


class KillboardAnonThrottle(AnonRateThrottle):
    scope = "killboard_anon"

    def get_ident(self, request) -> str:
        """Key the bucket on the real client IP, not the raw X-Forwarded-For header.

        DRF's default ``get_ident`` returns the WHOLE ``X-Forwarded-For`` value unless
        ``NUM_PROXIES`` is configured, and it is not set here. Our nginx uses
        ``$proxy_add_x_forwarded_for``, which appends the peer to whatever the client sent,
        so a caller who varies that header gets a fresh bucket on every request and the
        anonymous budget never binds. ``core.audit.client_ip`` already encodes this
        project's trusted-proxy rule (right-most entry = the peer nginx actually saw), so
        defer to it and keep one client-IP authority in the codebase.
        """
        return client_ip(request)


class KillboardUserThrottle(UserRateThrottle):
    scope = "killboard_user"
