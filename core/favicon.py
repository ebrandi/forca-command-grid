"""Home-corporation favicon — the corp's in-game logo as the app's browser icon.

The home corporation's logo is already the app mark everywhere in the chrome (the
sidebar and mobile header render it via ``home_corp_id`` — see ``core.context.roles``).
This module extends that identity to the browser itself: ``favicon.ico``, the PNG
icon sizes and the ``apple-touch-icon`` are all derived from the same logo, so the
tab, the bookmark and the home-screen shortcut carry the corp's mark without any
operator-uploaded asset.

The logo bytes come from the hardened corp-logo mirror
(``apps.killboard.signature_assets.ensure_corp_logo``): fixed upstream host, streamed
size cap, atomic writes, 7-day refresh with stale fallback. Derived files live under
``EVE_IMAGE_MIRROR_DIR/branding/<corp_id>/`` — keyed by corp id so changing
``FORCA_HOME_CORP_ID`` can never serve the previous corp's icons — and regenerate
exactly when the mirrored logo file changes (mtime), so an in-game logo update
propagates within the mirror's refresh cadence.

These are anonymous root-level endpoints hit by every browser, so the request path
is engineered to almost never do I/O beyond two stats and a read (adversarial
review 2026-08-01):

* **Hot path** — logo mirrored and fresh, derived file up to date → serve. No
  cache round-trips, no network, no locks.
* **Refresh** (logo missing or older than the mirror's 7-day window) — the
  outbound fetch is single-flighted through ``cache.add`` with a cooldown, so an
  image-server outage costs ONE held thread per cooldown window, not one per
  request (the mirror's stale fallback returns a non-None path on failure, so a
  result-based negative cache alone would never engage).
* **Failure** (fetch failed with nothing mirrored, disk unwritable, corrupt or
  bomb-sized logo) — any exception is contained here, the previously derived
  icons keep serving if they exist, and otherwise the miss is negative-cached so
  the failure is re-probed at most every ``_MISS_CACHE_TTL``, never per request.
"""
from __future__ import annotations

import io
import logging
import os

from django.conf import settings
from django.core.cache import cache

log = logging.getLogger("forca.core")

# The mirrored logo size the icons are cut from. 256 keeps every derived size a
# downscale (never an upscale), including the 180px apple-touch icon.
_SOURCE_SIZE = 256
# Classic multi-resolution .ico — the sizes Windows/browsers actually pick from.
_ICO_SIZES = [(16, 16), (32, 32), (48, 48)]
_APPLE_SIZE = 180
# iOS composites transparent touch icons onto black; flatten onto the app's own page
# background instead so the home-screen tile matches the chrome (signature palette).
_APPLE_BG = (10, 14, 22)  # SPACE in apps.killboard.signature_assets

# A favicon request is the least important request a browser makes: a cold-cache
# fetch of the logo must not hold a gthread for the mirror's default 15 s.
_FETCH_TIMEOUT = 5.0
# One outbound refresh attempt per window, deployment-wide (cache.add single-flight).
# Within the window every request serves what is on disk. A healthy refresh takes
# one request ~200 ms once a week; an upstream outage costs one 5 s hold per hour.
_REFRESH_COOLDOWN = 3600
# After a hard failure with nothing servable, don't re-probe on every request.
_MISS_CACHE_TTL = 300
_MISS_CACHE_KEY = "branding:favicon:miss:{corp_id}"
_REFRESH_CACHE_KEY = "branding:favicon:refresh:{corp_id}"

# A corp logo is at most 1024x1024; anything bigger in the mirror is hostile or
# corrupt. Checked from the PNG/JPEG header BEFORE decoding, so a decompression
# bomb is rejected without allocating (Pillow's own bomb error is not an OSError,
# and its warning threshold sits far above any sane logo anyway).
_MAX_SOURCE_PIXELS = 4096 * 4096

VARIANT_FILES = {
    "ico": "favicon.ico",
    "png32": "favicon-32.png",
    "png16": "favicon-16.png",
    "apple": "apple-touch-icon.png",
}

# The five URLs config.views.favicon serves. core.i18n.LocaleMiddleware skips its
# Vary patch for these (binary, language-independent — Vary: Cookie would make
# every session rotation refetch them), and core.middleware's membership-gate
# allowlist carries the same five literals.
ICON_PATHS = frozenset({
    "/favicon.ico",
    "/favicon-32.png",
    "/favicon-16.png",
    "/apple-touch-icon.png",
    "/apple-touch-icon-precomposed.png",
})


def favicon_path(variant: str) -> str | None:
    """Absolute path of one derived favicon file, refreshing the set when the
    mirrored logo changed. None when the home corp is unconfigured or nothing is
    servable (the caller 404s, exactly the pre-feature behaviour)."""
    filename = VARIANT_FILES.get(variant)
    corp_id = int(getattr(settings, "FORCA_HOME_CORP_ID", 0) or 0)
    if filename is None or corp_id <= 0:
        return None
    from apps.killboard.signature_assets import _existing_asset, _is_fresh

    root = getattr(settings, "EVE_IMAGE_MIRROR_DIR", "") or ""
    if not root:
        return None
    path = os.path.join(root, "branding", str(corp_id), filename)
    logo = _existing_asset(
        os.path.join(root, "corporations", str(corp_id), f"logo-{_SOURCE_SIZE}")
    )
    # Hot path: fresh logo, up-to-date derivation — nothing to do.
    if logo and _is_fresh(logo) and not _needs_regen(path, logo):
        return path
    miss_key = _MISS_CACHE_KEY.format(corp_id=corp_id)
    if cache.get(miss_key):
        return path if os.path.exists(path) else None
    try:
        logo = _refresh_logo(corp_id, logo)
        if logo and _needs_regen(path, logo):
            _derive_all(logo, os.path.dirname(path))
    except Exception:
        # ENOSPC / read-only volume / corrupt or bomb-sized mirror file / tmp
        # races — none of them may 500 an icon probe or take down the good copy.
        log.warning("favicon derivation failed for corp %s", corp_id, exc_info=True)
    if os.path.exists(path):
        return path
    cache.set(miss_key, True, _MISS_CACHE_TTL)
    return None


def _refresh_logo(corp_id: int, existing: str | None) -> str | None:
    """The freshest locally-available logo, fetching at most once per cooldown.

    ``cache.add`` is atomic, so of all requests that find the logo missing or
    stale, exactly ONE per window performs the outbound fetch; the rest serve the
    existing copy (or miss fast). The mirror itself no-ops when the logo is fresh,
    so a held cooldown never delays anything a request could actually use.
    """
    from apps.killboard.signature_assets import ensure_corp_logo

    if not cache.add(_REFRESH_CACHE_KEY.format(corp_id=corp_id), True, _REFRESH_COOLDOWN):
        return existing
    return ensure_corp_logo(corp_id, size=_SOURCE_SIZE, timeout=_FETCH_TIMEOUT) or existing


def _needs_regen(derived: str, logo: str) -> bool:
    """A derived file is refreshed when missing or older than the mirrored logo.
    Comparing mtimes (not just existence) is what makes an in-game logo change
    actually reach the browser once the mirror refresh pulls it."""
    try:
        return os.path.getmtime(derived) < os.path.getmtime(logo)
    except OSError:
        return True


def _derive_all(logo_path: str, out_dir: str) -> None:
    """Regenerate every variant together from the mirrored logo.

    All writes are atomic (unique tmp + ``os.replace``), so two gunicorn threads
    racing a regeneration are harmless — the last writer wins and a reader never
    sees a half-written icon. Failures propagate; the caller stale-serves.
    """
    from PIL import Image

    from apps.killboard.signature_assets import _atomic_write

    with Image.open(logo_path) as src:
        w, h = src.size  # header-only; nothing is decoded yet
        if w * h > _MAX_SOURCE_PIXELS:
            raise ValueError(f"mirrored logo too large to be a corp logo: {w}x{h}")
        logo = src.convert("RGBA")

    def _save(img, filename: str, fmt: str, **kwargs) -> None:
        buf = io.BytesIO()
        img.save(buf, format=fmt, **kwargs)
        _atomic_write(os.path.join(out_dir, filename), buf.getvalue())

    # Multi-resolution .ico: Pillow downscales the source once per listed size.
    _save(logo, VARIANT_FILES["ico"], "ICO", sizes=_ICO_SIZES)
    _save(logo.resize((32, 32), Image.LANCZOS), VARIANT_FILES["png32"], "PNG")
    _save(logo.resize((16, 16), Image.LANCZOS), VARIANT_FILES["png16"], "PNG")

    apple = Image.new("RGB", (_APPLE_SIZE, _APPLE_SIZE), _APPLE_BG)
    scaled = logo.resize((_APPLE_SIZE, _APPLE_SIZE), Image.LANCZOS)
    apple.paste(scaled, (0, 0), scaled)
    _save(apple, VARIANT_FILES["apple"], "PNG")
