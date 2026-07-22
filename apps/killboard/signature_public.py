"""Combat Signatures — the public, anonymous banner delivery view (WS-5, plan A3/A4).

One route lives here: ``GET/HEAD /s/<token>.png`` (wired at the URL root, outside the killboard
namespace). It is the login-free, CSRF-free, cookie-free surface a pilot embeds in a forum
signature or Discord. In production nginx serves the rendered file straight off the shared media
volume (``location ~ ^/s/…`` in deploy/nginx/forca.prod.conf); this Django view is the ONLY path
in dev/tests and the prod fallback whenever the file is absent — a render is still pending, or the
token was disabled / rotated away / never existed.

Three response tiers, all image/png, all with an explicit ``Cache-Control`` (so the global
authenticated ``no-store`` never masks a public banner) plus ``nosniff`` + ``X-Robots-Tag``:

* **served** — the row is ACTIVE/FROZEN and its artifact exists → the file streams with a strong
  ``ETag`` (mtime+size), ``Last-Modified``, conditional 304s, and ``max-age=300``.
* **pending** — the row is ACTIVE/FROZEN but has no artifact yet → a neutral placeholder at 200
  with ``max-age=60`` (200-not-404 so a forum never caches a broken image while the first render
  is queued).
* **unavailable** — the token maps to a DISABLED row OR to no row at all → a **constant-shape**
  404 carrying an identical neutral placeholder (fixed preset) and identical headers. Disabled
  and nonexistent are byte-for-byte indistinguishable (anti-enumeration, threat model).

Token format is validated FIRST, so a traversal / malformed token 404s before any filesystem or
DB access. A per-IP fixed-window throttle (``SIGNATURE_PUBLIC_RATE``) guards this fallback only —
nginx-served hits bypass Django entirely — and is checked after the format gate, before the DB.
"""
from __future__ import annotations

import os
from functools import lru_cache

from django.conf import settings
from django.core.cache import cache
from django.http import (
    FileResponse,
    HttpRequest,
    HttpResponse,
    HttpResponseNotModified,
)
from django.utils.http import http_date, parse_http_date_safe
from django.views.decorators.http import require_safe

from core.audit import client_ip

from . import signatures
from .models import CombatSignature
from .signature_render import render_placeholder_png

# The "unavailable" (disabled / unknown) placeholder is always the same preset so a disabled
# signature is byte-identical to a never-existed token — its real preset must NOT leak here.
_UNAVAILABLE_PRESET = "standard"

_SERVED_MAX_AGE = 300   # a rendered banner: long-ish public cache; a refresh lands within minutes.
_PENDING_MAX_AGE = 60   # pending / unavailable: short, so a placeholder is replaced promptly.


# --------------------------------------------------------------------------- #
#  Throttle — a per-IP fixed window, mirroring killcard.throttle_ok exactly.
# --------------------------------------------------------------------------- #
def _public_rate() -> int:
    return int(getattr(settings, "SIGNATURE_PUBLIC_RATE", 120))


def _throttle_ok(ip: str) -> bool:
    """True while this IP is under the per-minute fallback budget. The TTL is set once by ``add``
    and not refreshed by ``incr`` so the window genuinely closes; ``<=0`` disables the throttle."""
    limit = _public_rate()
    if limit <= 0:
        return True
    key = f"kb:sig:pub:throttle:{ip or 'anon'}"
    cache.add(key, 0, 60)
    try:
        count = cache.incr(key)
    except ValueError:
        # Key expired between add and incr (window rolled over) — treat as the first of a new one.
        cache.add(key, 1, 60)
        count = 1
    return count <= limit


# --------------------------------------------------------------------------- #
#  Placeholder bytes (memoised per preset — pure + deterministic).
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=8)
def _placeholder_png(size_preset: str) -> bytes:
    """The data-free placeholder PNG for ``size_preset``, computed once per preset per process.

    Memoising bounds the CPU an enumeration attack on the fallback can spend and guarantees the
    disabled and unknown-token responses return the exact same bytes."""
    return render_placeholder_png(size_preset)


# --------------------------------------------------------------------------- #
#  Header helpers.
# --------------------------------------------------------------------------- #
def _apply_image_headers(resp: HttpResponse, *, max_age: int) -> None:
    """Set the headers every tier shares. ``Cache-Control`` is set EXPLICITLY (not setdefault) so
    the global authenticated ``private, no-store`` never overrides a public banner for a logged-in
    viewer, and ``X-Robots-Tag`` keeps the raw image out of search indexes."""
    resp["Cache-Control"] = f"public, max-age={max_age}"
    resp["X-Content-Type-Options"] = "nosniff"
    resp["X-Robots-Tag"] = "noindex, nofollow"


def _placeholder_response(request: HttpRequest, size_preset: str, *, status: int,
                          max_age: int) -> HttpResponse:
    png = _placeholder_png(size_preset)
    if request.method == "HEAD":
        resp = HttpResponse(status=status, content_type="image/png")
        resp["Content-Length"] = str(len(png))
    else:
        resp = HttpResponse(png, status=status, content_type="image/png")
    _apply_image_headers(resp, max_age=max_age)
    return resp


def _unavailable(request: HttpRequest) -> HttpResponse:
    """The constant-shape 404 for a disabled or unknown token (indistinguishable by design)."""
    return _placeholder_response(request, _UNAVAILABLE_PRESET, status=404,
                                 max_age=_PENDING_MAX_AGE)


def _pending(request: HttpRequest, size_preset: str) -> HttpResponse:
    """The 200 placeholder for a live signature whose artifact has not been rendered yet."""
    return _placeholder_response(request, size_preset, status=200, max_age=_PENDING_MAX_AGE)


def _not_modified(etag: str) -> HttpResponseNotModified:
    resp = HttpResponseNotModified()
    resp["ETag"] = etag
    _apply_image_headers(resp, max_age=_SERVED_MAX_AGE)
    return resp


def _etag_matches(header: str, etag: str) -> bool:
    """RFC 7232 If-None-Match: ``*`` matches anything; otherwise a comma list of (possibly weak)
    entity tags, compared after stripping a ``W/`` prefix (we only ever emit strong tags)."""
    header = header.strip()
    if header == "*":
        return True
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate == etag:
            return True
    return False


def _serve_artifact(request: HttpRequest, path: str, st: os.stat_result) -> HttpResponse:
    """Serve an existing artifact with a strong (mtime,size) ETag + Last-Modified and 304 support.

    Per RFC 7232 a present ``If-None-Match`` takes precedence and makes us ignore
    ``If-Modified-Since``; a 304 carries the same ETag + Cache-Control and an empty body.
    """
    etag = f'"{int(st.st_mtime):x}-{st.st_size:x}"'
    if_none_match = request.headers.get("If-None-Match")
    if if_none_match is not None:
        if _etag_matches(if_none_match, etag):
            return _not_modified(etag)
    else:
        if_modified_since = request.headers.get("If-Modified-Since")
        if if_modified_since:
            since = parse_http_date_safe(if_modified_since)
            if since is not None and int(st.st_mtime) <= since:
                return _not_modified(etag)

    if request.method == "HEAD":
        resp: HttpResponse = HttpResponse(content_type="image/png")
        resp["Content-Length"] = str(st.st_size)
    else:
        resp = FileResponse(open(path, "rb"), content_type="image/png")
    resp["ETag"] = etag
    resp["Last-Modified"] = http_date(st.st_mtime)
    _apply_image_headers(resp, max_age=_SERVED_MAX_AGE)
    return resp


# --------------------------------------------------------------------------- #
#  The view.
# --------------------------------------------------------------------------- #
@require_safe
def signature_png(request: HttpRequest, token: str) -> HttpResponse:
    """PUBLIC anonymous banner PNG at ``/s/<token>.png`` (WS-5). No auth, no CSRF, no session.

    Order is security-critical: validate the token FORMAT first (a malformed / traversal token
    404s before any filesystem or DB access), then throttle the fallback, then read the row and
    pick a tier. The response never touches ``request.session`` so no cookie is ever set.
    """
    # 1) Strict token format — malformed never reaches the filesystem or the database.
    if not signatures.TOKEN_RE.match(token or ""):
        return HttpResponse(status=404)

    # 2) Throttle the Django fallback only (nginx-served hits never get here). After the format
    #    gate, before the DB — a flood of junk tokens is already rejected for free above.
    if not _throttle_ok(client_ip(request)):
        resp = HttpResponse(status=429)
        resp["Retry-After"] = "60"
        return resp

    # 3) Resolve the row. A missing row and a DISABLED row are the SAME constant-shape 404.
    sig = (
        CombatSignature.objects
        .filter(public_token=token)
        .only("public_token", "status", "size_preset")
        .first()
    )
    if sig is None or sig.status not in (
        CombatSignature.Status.ACTIVE, CombatSignature.Status.FROZEN
    ):
        return _unavailable(request)

    # 4) ACTIVE / FROZEN: serve the artifact if it exists, else a pending placeholder (200).
    try:
        path = signatures.artifact_path(sig.public_token)
    except ValueError:
        # A real row can never carry a bad token, but never resolve a path from a bad one anyway.
        return _unavailable(request)
    try:
        st = os.stat(path)
    except OSError:
        return _pending(request, sig.size_preset)
    return _serve_artifact(request, path, st)
