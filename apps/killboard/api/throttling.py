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


class KillboardAnonThrottle(AnonRateThrottle):
    scope = "killboard_anon"


class KillboardUserThrottle(UserRateThrottle):
    scope = "killboard_user"
