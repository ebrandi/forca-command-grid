"""Impersonation ("view-as") request middleware.

Runs immediately after ``AuthenticationMiddleware``. When the director's session carries
an active, still-valid impersonation it swaps ``request.user`` for the target pilot, so
every downstream consumer — the ``core.context.roles`` context processor, the membership
and feature gates, RBAC decorators and every view — transparently sees the pilot with NO
per-view changes. The real director is preserved on ``request.impersonator`` (and
``request.real_user``) for the banner and the exit path.

The swap + re-validation happen in ``__call__`` so later middleware (the membership/feature
gates) already see the pilot. The VIEW-ONLY write block lives in ``process_view`` — it must
run after ``MessageMiddleware`` has initialised ``request._messages`` (that middleware sits
lower in the stack, so its setup only completes once ``get_response`` is entered).

Security invariants enforced here on EVERY request (any failure ends the session, audited,
and the request proceeds as the real director):

* only a current director/admin may be impersonating — re-checked live, so a mid-session
  demotion ends it at once;
* the target must still be strictly-lower-rank and not a superuser — a mid-session
  promotion ends it;
* the session is bound to the actor that created it (the session's actor-id must match the
  authenticated user);
* the session auto-expires after :func:`policy.max_duration`;
* impersonation is VIEW-ONLY — any unsafe HTTP method (POST/PUT/PATCH/DELETE) is refused
  (redirected + flashed + audited) except the exit/stop control surface and logout, so a
  director can never mutate a pilot's data or act on their behalf while viewing as them.
"""
from __future__ import annotations

import time
from urllib.parse import urlparse

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.shortcuts import redirect
from django.urls import reverse

from core import rbac
from core.audit import audit_log, client_ip

from . import policy, services

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# Identity / OAuth link flows that mutate the CURRENT account and therefore must NEVER run
# under a swapped identity — blocked even on GET while impersonating. These are GET-served
# (the provider redirects back with GET), so the method-based read-only guard alone would let
# them through: the EVE SSO callback resolves ``request.user`` (the impersonated pilot) as the
# login account and would link the director's own character + tokens to the PILOT's account;
# the Discord/recruitment OAuth callbacks would likewise bind an external identity to the
# pilot. Matched by resolved ``namespace:url_name`` so a URL-path change can't silently reopen
# the hole. (Read-only pages like ``sso:scopes`` stay viewable — only these mutation
# entrypoints are denied.)
_IDENTITY_MUTATION_VIEWS = frozenset({
    "sso:login", "sso:callback",
    "comms_access:discord_begin", "comms_access:discord_callback", "comms_access:discord_unlink",
    "recruitment:oauth_begin", "recruitment:oauth_callback",
})


class ImpersonationMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Defaults every downstream consumer can rely on, set before anything short-circuits.
        request.is_impersonating = False
        request.impersonator = None
        request.real_user = getattr(request, "user", None)
        request.impersonation = None

        if request.session.get(policy.SESSION_TARGET_KEY) is not None:
            self._apply(request)
        return self.get_response(request)

    def _apply(self, request) -> None:
        """Validate the session and, if it holds, swap ``request.user`` for the pilot.
        Any invariant failure ends the impersonation and leaves the real director in place."""
        real_user = getattr(request, "user", None)

        # Keys present but no authenticated user (cookie survived a logout / anon request).
        if real_user is None or not getattr(real_user, "is_authenticated", False):
            services.end(request, reason="actor_invalid", actor=None)
            return

        # Bind the session to the actor that created it: a different logged-in user must
        # never inherit a leftover impersonation.
        if request.session.get(policy.SESSION_ACTOR_KEY) != real_user.pk:
            services.end(request, reason="actor_invalid", actor=real_user)
            return

        # The actor must still be a director/admin right now.
        if not rbac.has_role(real_user, rbac.ROLE_DIRECTOR):
            services.end(request, reason="actor_invalid", actor=real_user)
            return

        # Auto-expire after the configured cap.
        started = request.session.get(policy.SESSION_STARTED_KEY) or 0
        if time.time() - started > policy.max_duration().total_seconds():
            services.end(request, reason="expired", actor=real_user)
            return

        # Load + re-validate the target under the same rule the entry gate used.
        target = (
            get_user_model().objects
            .filter(pk=request.session.get(policy.SESSION_TARGET_KEY))
            .prefetch_related("characters", "role_assignments__role")
            .first()
        )
        if target is None or not policy.can_impersonate(real_user, target):
            services.end(request, reason="target_invalid", actor=real_user)
            return

        # Arm the swap — from here on, request.user IS the pilot for all downstream code.
        request.user = target
        request.impersonator = real_user
        request.is_impersonating = True
        remaining = int(policy.max_duration().total_seconds() - (time.time() - started))
        request.impersonation = {
            "actor": real_user,
            "target": target,
            "started_at": started,
            "expires_in": max(0, remaining),
        }

    def process_view(self, request, view_func, view_args, view_kwargs):
        """VIEW-ONLY: refuse unsafe methods (and the GET-served identity/OAuth mutation
        flows) while impersonating, except the impersonation control surface + logout (so a
        director can always get out). Runs after MessageMiddleware has initialised
        request._messages, so the flash works, and after URL resolution, so
        request.resolver_match is available for the identity-view denylist."""
        if not getattr(request, "is_impersonating", False):
            return None
        match = getattr(request, "resolver_match", None)
        view_name = match.view_name if match else ""
        unsafe = request.method not in _SAFE_METHODS or view_name in _IDENTITY_MUTATION_VIEWS
        if not unsafe or self._is_control_path(request.path):
            return None
        target = request.user
        messages.warning(
            request,
            f"You're viewing as {getattr(target, 'display_name', 'this pilot')} (read-only). "
            "Exit impersonation to make changes as yourself.",
        )
        audit_log(
            request.impersonator, "impersonation.write_blocked", target_type="user",
            target_id=str(target.pk),
            metadata={"path": request.path[:200], "method": request.method},
            ip=client_ip(request),
        )
        return redirect(self._safe_back(request))

    @staticmethod
    def _is_control_path(path: str) -> bool:
        """The exit/stop endpoint (and logout) are always allowed while impersonating."""
        if path.startswith("/impersonation/"):
            return True
        try:
            return path == reverse("sso:logout")
        except Exception:  # noqa: BLE001 - URLconf not ready is not a reason to trap a director
            return False

    @staticmethod
    def _safe_back(request) -> str:
        """Bounce a blocked write back where it came from — but only to a same-site path,
        never an attacker-controlled open redirect."""
        ref = request.META.get("HTTP_REFERER", "")
        if ref:
            parsed = urlparse(ref)
            if (not parsed.netloc or parsed.netloc == request.get_host()) and parsed.path.startswith("/"):
                return parsed.path
        return "/dashboard/"
