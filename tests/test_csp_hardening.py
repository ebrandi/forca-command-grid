"""Tests for the CSP hardening (R-1): self-hosted scripts + per-request nonce.

Covers the middleware/header behaviour and a set of repo-level regression guards
that fail if anyone reintroduces a remote script CDN, an inline on* handler, or an
un-nonced inline <script> (any of which would silently weaken the policy or break
under it).
"""
from __future__ import annotations

import re

from django.conf import settings
from django.http import HttpResponse
from django.test import RequestFactory

from core import context as core_context
from core.middleware import SecurityHeadersMiddleware

CSP = "Content-Security-Policy"


def _csp_for(path: str = "/") -> str:
    """Run a request through the real middleware and return its CSP header.

    The header is applied to every response (incl. redirects), so this is robust
    regardless of whether the path needs auth."""
    mw = SecurityHeadersMiddleware(lambda request: HttpResponse("ok"))
    return mw(RequestFactory().get(path))[CSP]


def _script_src(csp: str) -> str:
    for directive in csp.split(";"):
        directive = directive.strip()
        if directive.startswith("script-src"):
            return directive
    raise AssertionError(f"no script-src in CSP: {csp!r}")


# --- script-src is locked down ----------------------------------------------
def test_script_src_is_self_nonce_and_eval_only():
    src = _script_src(_csp_for())
    assert "'self'" in src
    assert re.search(r"'nonce-[A-Za-z0-9_-]{16,}'", src), src
    # unsafe-eval is the accepted residual (Alpine); unsafe-inline must be gone.
    assert "'unsafe-eval'" in src
    assert "'unsafe-inline'" not in src


def test_no_remote_script_origins_in_csp():
    src = _script_src(_csp_for())
    for host in ("cdn.tailwindcss.com", "unpkg.com", "jsdelivr", "cdnjs", "http://", "https://"):
        assert host not in src, f"{host} leaked into script-src: {src}"


def test_other_hardening_directives_preserved():
    csp = _csp_for()
    for directive in (
        "default-src 'self'",
        "object-src 'none'",
        "frame-src 'none'",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'",
    ):
        assert directive in csp, f"missing {directive!r} in {csp!r}"


def test_form_action_allows_eve_sso_for_the_recruitment_redirect():
    # Regression: the recruitment "Authorise with EVE" POST-form 302s to CCP's authorize
    # endpoint, and browsers enforce form-action against that redirect target. With
    # form-action 'self' ALONE the browser silently blocks the redirect and the button does
    # nothing, so the EVE SSO login origin must be allowed here (member login is a GET <a>,
    # which form-action does not govern — only this POST-form flow was affected).
    csp = _csp_for()
    assert "form-action 'self' https://login.eveonline.com" in csp, csp


# --- nonce wiring ------------------------------------------------------------
def test_nonce_is_set_on_request_before_view_and_matches_header():
    seen = {}

    def get_response(request):
        seen["nonce"] = request.csp_nonce  # available during template render
        return HttpResponse("ok")

    resp = SecurityHeadersMiddleware(get_response)(RequestFactory().get("/"))
    assert seen["nonce"]
    assert f"'nonce-{seen['nonce']}'" in resp[CSP]


def test_nonce_is_unique_per_request():
    n1 = re.search(r"'nonce-([^']+)'", _csp_for()).group(1)
    n2 = re.search(r"'nonce-([^']+)'", _csp_for()).group(1)
    assert n1 != n2


def test_context_processor_exposes_request_nonce():
    req = RequestFactory().get("/")
    req.csp_nonce = "deadbeef"
    assert core_context.csp_nonce(req) == {"csp_nonce": "deadbeef"}


def test_context_processor_fails_safe_without_nonce():
    # If the middleware never ran, an inline script gets an empty nonce → blocked,
    # which is the safe outcome (no accidental allow).
    assert core_context.csp_nonce(RequestFactory().get("/")) == {"csp_nonce": ""}


# --- repo-level regression guards -------------------------------------------
def _template_files() -> list:
    roots = [settings.BASE_DIR / "templates"]
    roots += sorted((settings.BASE_DIR / "apps").glob("*/templates"))
    files: list = []
    for root in roots:
        if root.exists():
            files += root.rglob("*.html")
    return files


def test_no_remote_script_cdns_in_templates():
    offenders = []
    for f in _template_files():
        text = f.read_text(encoding="utf-8")
        for needle in ("cdn.tailwindcss.com", "unpkg.com", "jsdelivr.net", "cdnjs.cloudflare.com"):
            if needle in text:
                offenders.append(f"{f}: {needle}")
    assert not offenders, "remote script CDN(s) in templates:\n" + "\n".join(offenders)


def test_no_inline_event_handlers_in_templates():
    # Inline on*="" handlers are blocked once 'unsafe-inline' is dropped; use
    # data-autosubmit / data-confirm (wired in static/js/app.js) instead.
    pat = re.compile(r"\son[a-z]+=\"")
    offenders = [str(f) for f in _template_files() if pat.search(f.read_text(encoding="utf-8"))]
    assert not offenders, "inline event handlers found in:\n" + "\n".join(offenders)


def test_every_inline_script_carries_a_nonce():
    # Any <script> that is not a src= include must declare nonce="{{ csp_nonce }}".
    open_tag = re.compile(r"<script\b[^>]*>", re.IGNORECASE)
    offenders = []
    for f in _template_files():
        for tag in open_tag.findall(f.read_text(encoding="utf-8")):
            if "src=" in tag:
                continue
            if "nonce=" not in tag:
                offenders.append(f"{f}: {tag}")
    assert not offenders, "un-nonced inline <script> tag(s):\n" + "\n".join(offenders)


def test_vendored_js_and_compiled_css_exist():
    static = settings.BASE_DIR / "static"
    expected = [
        static / "css" / "app.css",
        static / "js" / "app.js",
        static / "js" / "vendor" / "alpine.min.js",
        static / "js" / "vendor" / "htmx.min.js",
        static / "js" / "vendor" / "chart.umd.js",
        static / "js" / "vendor" / "svg-pan-zoom.min.js",
    ]
    missing = [str(p) for p in expected if not p.exists()]
    assert not missing, "missing self-hosted asset(s):\n" + "\n".join(missing)
