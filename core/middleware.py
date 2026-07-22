"""Security response headers and the corp-membership access gate."""
from __future__ import annotations

import secrets
import time

from django.shortcuts import redirect

from core import rbac

# Path prefixes an authenticated NON-member (a pilot whose character is not in
# the home corporation, so they hold no `member` role — see
# apps.sso.services.sync_roles_for_user) is allowed to reach. Such a user is a
# prospective recruit: they may see the recruitment/onboarding surface and
# manage their own account, and nothing internal.
_RECRUIT_ALLOWED_PREFIXES = (
    "/onboarding",   # the recruitment / New Player surface (their home)
    "/killboard/stats",  # combat stats dashboard — offered to registered alliance
                         # pilots too (the view enforces member-or-alliance); the
                         # public killfeed/rankings/detail keep their own access
    "/killboard/pilot",  # per-pilot analytics — same member-or-alliance gate
    "/killboard/meta",   # KB-36 meta boards — same member-or-alliance intel gate (the view's
                         # _can_view_stats enforces it; the boards are read-only analytics)
    "/killboard/compare",  # pilot comparison — same member-or-alliance gate
    "/killboard/adversary",  # KB-33 adversary intel pages — same member-or-alliance gate
                             # (the view's _can_view_stats enforces it; the officer-only
                             # add-to-watchlist action keeps its own role decorator)
    "/killboard/scan",   # KB-34 D-scan/Local analyzer — same member-or-alliance intel gate
                         # (the view's _can_view_stats enforces it; the corp-broadcast alert
                         # inside it keeps its own member check)
    "/killboard/trophies",  # KB-37 trophy hall — same member-or-alliance gate (the view's
                            # _can_view_stats enforces it; the catalogue is read-only)
    "/killboard/seasons",   # KB-37 seasonal ladders — same member-or-alliance gate
    "/killboard/kotw",      # KB-37 Kill-of-the-Week hall — same member-or-alliance gate (the
                            # officer override POST under it keeps its own role decorator)
    "/auth/",        # EVE SSO: login, callback, logout, ESI scopes, disconnect
    "/privacy",      # the pilot's own data rights (view + delete)
    "/recruitment/oauth",  # candidate-facing live ESI consent (begin + callback);
                     # the recruitment DESK under /recruitment keeps its officer gate
    "/freight",      # public freight rate calculator (external-facing; the
                     # board/rates under it keep their own role decorators)
    "/buyback",      # buyback & appraisal (audience enforced in the view; the
                     # board/settings under it keep their own gates)
    "/store",        # corp store (audience enforced in the view; the fulfilment
                     # board/settings under it keep their own role gates)
    "/doctrines",    # Ships & doctrines is an audience-controlled feature (public / corp /
                     # corp+alliance / disabled); the FeatureGate audience check is the
                     # single enforcement point, so the membership gate lets the whole
                     # namespace through and defers to it.
    "/tools",        # Navigation & maps — also an audience-controlled feature (defaults
                     # public); same deal, the FeatureGate audience check enforces it.
    "/kb",           # Knowledge base — the PUBLIC tier is a recruiting surface; the
                     # kb views' own visibility gate restricts a non-member to public
                     # pages, so let the namespace through and defer to it.
    "/features",     # public features showcase — a recruiting/marketing tour; a
                     # logged-in recruit should see it too, so allowlist it past the gate.
    "/impersonation",  # director "view-as" control surface (start/stop/log); the stop
                       # endpoint must stay reachable even while viewing as a NON-member
                       # recruit, whom this gate would otherwise confine to onboarding.
    "/pilot",        # Linked Pilots: the selector, the management page and the switch/link/
                     # unlink controls. Authority is computed from the ACTIVE pilot (LP-4), so
                     # a member who switches to an alt in another corporation correctly stops
                     # being a member — and this gate would then strand them in onboarding with
                     # no way back. The switch control must stay reachable from outside the
                     # corp, exactly as the impersonation stop control must. Every route under
                     # it is @login_required and resolves pilots through the caller's own
                     # account, so nothing internal is exposed by allowlisting the prefix.
    "/s",            # Combat Signatures public banner PNGs (/s/<token>.png): a login-free,
                     # token-addressed asset. Anonymous fetches never hit this gate, but a
                     # logged-in EX-member must still be able to load their old forum-signature
                     # URL, so allowlist the prefix (whole-segment match keeps /store etc. out).
    "/healthz",
    "/static/",
    "/favicon.ico",
)


def _path_allowed(path: str) -> bool:
    """Whole-segment prefix match: ``/store`` allows ``/store`` and ``/store/x``
    but not ``/storeadmin``. Prevents a future root-mounted URL from being
    silently allowlisted just because it shares a textual prefix."""
    for p in _RECRUIT_ALLOWED_PREFIXES:
        base = p.rstrip("/")
        if path == base or path.startswith(base + "/"):
            return True
    return False


class MembershipGateMiddleware:
    """Restrict authenticated non-members to the recruitment surface only.

    Enforced centrally (not per-view) so no internal page — dashboard, killboard,
    doctrines, industry, readiness, briefings, … — can leak to a logged-in pilot
    who is not in the corporation. Anonymous visitors are unaffected (the public
    pages keep their existing access); superusers bypass the gate.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (
            user is not None
            and user.is_authenticated
            and not user.is_superuser
            and not rbac.has_role(user, rbac.ROLE_MEMBER)
        ):
            if not _path_allowed(request.path):
                return redirect("onboarding:dashboard")
        return self.get_response(request)


class AbsoluteSessionTimeoutMiddleware:
    """Cap the *total* session lifetime on top of the sliding idle timeout.

    ``SESSION_COOKIE_AGE`` + ``SESSION_SAVE_EVERY_REQUEST`` give a sliding idle
    timeout: every request pushes the window forward, so an actively-replayed stolen
    cookie never ages out on its own. This adds an absolute ceiling — the first
    authenticated request stamps ``_auth_started_at`` (the session key is freshly
    cycled at login, so this is ~login time), and once that is older than
    ``SESSION_ABSOLUTE_MAX_AGE`` the session is force-logged-out regardless of
    activity, bounding the replay window of a stolen cookie. Disabled when the setting
    is falsy.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from django.conf import settings
        from django.contrib.auth import logout

        max_age = getattr(settings, "SESSION_ABSOLUTE_MAX_AGE", 0)
        user = getattr(request, "user", None)
        if max_age and user is not None and user.is_authenticated:
            started = request.session.get("_auth_started_at")
            now = time.time()
            if started is None:
                request.session["_auth_started_at"] = now
            elif now - started > max_age:
                logout(request)
                return redirect(settings.LOGIN_URL)
        return self.get_response(request)


def _image_csp_source() -> str:
    """The extra ``img-src`` origin to allow, derived from EVE_IMAGE_BASE_URL.

    When images are served same-origin (prod's ``/eveimg`` proxy-cache) the base is
    a relative path → no extra source needed ('self' covers it). When pointed at an
    absolute host (dev/test default of CCP's server) that host is allowlisted. This
    keeps the policy automatically consistent with wherever imagery is actually
    fetched from.
    """
    from urllib.parse import urlparse

    from django.conf import settings

    base = getattr(settings, "EVE_IMAGE_BASE_URL", "")
    parsed = urlparse(base)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def _sso_csp_source() -> str:
    """The EVE SSO login origin to allow in ``form-action``, from EVE_SSO_AUTHORIZE_URL.

    The recruitment "Authorise with EVE" button is a POST form (it mints PKCE server-side)
    that then 302-redirects to CCP's authorize endpoint at ``login.eveonline.com``.
    Chrome/Safari enforce ``form-action`` against the *redirect target* of a form
    submission, so with ``form-action 'self'`` alone the browser silently blocks the
    navigation to EVE and the button appears to do nothing. (Member login is a plain <a>
    GET link, which ``form-action`` does not govern — which is why only the recruitment
    flow was affected.) Derived from settings so it tracks the configured SSO host.
    """
    from urllib.parse import urlparse

    from django.conf import settings

    parsed = urlparse(getattr(settings, "EVE_SSO_AUTHORIZE_URL", "") or "")
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return "https://login.eveonline.com"


def _build_csp(nonce: str) -> str:
    # All SCRIPTS are first-party: Tailwind is compiled to a static stylesheet and
    # Alpine/htmx/chart.js/svg-pan-zoom are vendored under /static, so no script CDN
    # origin remains. Inline <script> blocks that embed server data carry the
    # per-request `nonce` below, which lets us drop 'unsafe-inline' for scripts.
    # Google Fonts is the one third-party origin left (style-src + font-src). Removing
    # it means self-hosting the three OFL typefaces; see
    # handbooks/third-party-services.md#google-fonts-web-fonts.
    img_extra = _image_csp_source()
    img_src = "img-src 'self'" + (f" {img_extra}" if img_extra else "") + " data:"
    return (
        "default-src 'self'; "
        # 'unsafe-eval' remains ONLY because Alpine.js evaluates its x-* directives
        # (x-data/@click/x-show …) via the Function constructor; the standard build
        # cannot run without it. This is the documented residual (R-1 in
        # handbooks/contributor-handbook/security-guidelines.md) and is narrow: no user-controlled text is
        # ever placed inside an Alpine directive (all directives are static template
        # authored). Removing it requires migrating to Alpine's CSP build.
        f"script-src 'self' 'nonce-{nonce}' 'unsafe-eval'; "
        # style-src keeps 'unsafe-inline' (lower risk — styles can't execute): the
        # compiled stylesheet is first-party, but template `style=""` attributes and
        # Google Fonts' stylesheet still need it.
        "style-src 'self' https://fonts.googleapis.com 'unsafe-inline'; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        f"{img_src}; "
        "connect-src 'self'; "
        "base-uri 'self'; "
        # No plugins and no embedded frames — closes <object>/<embed> and clickjacking
        # vectors cheaply.
        "object-src 'none'; "
        "frame-src 'none'; "
        # 'self' plus the EVE SSO login origin: the recruitment consent POST-form redirects
        # to CCP's authorize endpoint, and browsers apply form-action to that redirect.
        f"form-action 'self' {_sso_csp_source()}; "
        "frame-ancestors 'none'"
    )


class SecurityHeadersMiddleware:
    """Apply baseline security headers on every response (defense in depth).

    The headers are set regardless of DEBUG so a reachable dev/staging instance is
    never header-less. The CSP is already nonce-based with no 'unsafe-inline' for
    scripts and no CDN origins (Tailwind is compiled, Alpine/htmx/chart.js are
    vendored); the one residual is 'unsafe-eval', which Alpine's standard build
    requires. See ``_build_csp`` above and
    handbooks/contributor-handbook/security-guidelines.md.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Generate the nonce BEFORE the view/template render so inline <script>
        # tags can stamp it (via the core.context.csp_nonce processor); the same
        # value then goes into the CSP header below.
        nonce = secrets.token_urlsafe(16)
        request.csp_nonce = nonce
        response = self.get_response(request)
        response.setdefault("Content-Security-Policy", _build_csp(nonce))
        response.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.setdefault("X-Content-Type-Options", "nosniff")
        response.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        # Authenticated pages render corp-private data (assets, finance, Director
        # intelligence, SRP, roster). Mark them uncacheable so a shared/kiosk browser's
        # Back button / bfcache / disk cache — or a mis-scoped intermediary — can't
        # replay the previous user's corp-secret pages. Anonymous public pages (landing,
        # SDE search, static) stay cacheable so the edge/CDN still works. `setdefault`
        # lets a specific view opt out with its own Cache-Control if ever needed.
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            response.setdefault("Cache-Control", "private, no-store")
        return response
