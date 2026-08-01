"""Home-corp favicon (core/favicon.py + config.views.favicon).

The app's browser icons are derived from the home corporation's in-game logo via the
hardened corp-logo mirror. These tests mock the image server with ``responses`` (the
pattern from test_signature_render) — no real sockets, ever.
"""
from __future__ import annotations

import io
import os

import pytest
import requests
import responses
from PIL import Image

# The request path runs middleware that reads the DB even for anonymous hits.
pytestmark = pytest.mark.django_db

HOME_CORP = 98000001  # config.settings.test's FORCA_HOME_CORP_ID
_LOGO_URL = f"https://images.evetech.net/corporations/{HOME_CORP}/logo"


def _png(color=(40, 90, 200, 255), size=256) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (size, size), color).save(buf, format="PNG")
    return buf.getvalue()


def _logo_path(root) -> str:
    return os.path.join(str(root), "corporations", str(HOME_CORP), "logo-256.png")


@pytest.fixture
def favicon_env(settings, tmp_path):
    """Isolated mirror dir + a clean cache (the negative-fetch marker is cached)."""
    from django.core.cache import cache

    settings.EVE_IMAGE_MIRROR_DIR = str(tmp_path)
    settings.FORCA_HOME_CORP_ID = HOME_CORP
    cache.clear()
    return tmp_path


# --- serving the derived set -------------------------------------------------
@responses.activate
def test_favicon_ico_is_multires_corp_logo(client, favicon_env):
    responses.add(responses.GET, _LOGO_URL, body=_png(), content_type="image/png", status=200)
    resp = client.get("/favicon.ico")
    assert resp.status_code == 200
    assert resp["Content-Type"] == "image/x-icon"
    assert resp["Cache-Control"] == "public, max-age=86400"
    assert resp.content[:4] == b"\x00\x00\x01\x00"  # ICO magic
    ico = Image.open(io.BytesIO(resp.content))
    assert ico.format == "ICO"
    assert {(16, 16), (32, 32), (48, 48)} <= set(ico.info["sizes"])
    # The mirrored logo is fresh — a second request must not re-fetch upstream.
    assert client.get("/favicon.ico").status_code == 200
    assert len(responses.calls) == 1


@responses.activate
@pytest.mark.parametrize(("url", "px"), [("/favicon-32.png", 32), ("/favicon-16.png", 16)])
def test_favicon_pngs_are_exact_sizes(client, favicon_env, url, px):
    responses.add(responses.GET, _LOGO_URL, body=_png(), content_type="image/png", status=200)
    resp = client.get(url)
    assert resp.status_code == 200
    assert resp["Content-Type"] == "image/png"
    img = Image.open(io.BytesIO(resp.content))
    assert img.format == "PNG" and img.size == (px, px)


@responses.activate
@pytest.mark.parametrize("url", ["/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"])
def test_apple_touch_icon_is_opaque_180(client, favicon_env, url):
    # A transparent touch icon gets composited onto black by iOS — ours must be
    # flattened onto the app background (opaque RGB), at Apple's 180x180.
    responses.add(responses.GET, _LOGO_URL, body=_png((200, 40, 40, 128)),
                  content_type="image/png", status=200)
    resp = client.get(url)
    assert resp.status_code == 200
    img = Image.open(io.BytesIO(resp.content))
    assert img.size == (180, 180) and img.mode == "RGB"


# --- unconfigured / failure paths -------------------------------------------
@responses.activate
def test_unconfigured_corp_404s_without_network(client, favicon_env, settings):
    settings.FORCA_HOME_CORP_ID = 0
    resp = client.get("/favicon.ico")
    assert resp.status_code == 404
    assert resp["Cache-Control"] == "public, max-age=300"
    assert len(responses.calls) == 0


@responses.activate
def test_fetch_failure_404s_and_is_negative_cached(client, favicon_env):
    responses.add(responses.GET, _LOGO_URL, body=requests.exceptions.ConnectTimeout())
    assert client.get("/favicon.ico").status_code == 404
    # The miss is cached: the immediate retry must not hit upstream again.
    assert client.get("/favicon-32.png").status_code == 404
    assert len(responses.calls) == 1


@responses.activate
def test_stale_logo_with_dead_upstream_serves_without_per_request_fetches(client, favicon_env):
    from django.core.cache import cache

    from core.favicon import _REFRESH_CACHE_KEY

    responses.add(responses.GET, _LOGO_URL, body=_png(), content_type="image/png", status=200)
    assert client.get("/favicon.ico").status_code == 200
    # Age the mirrored logo past the 7-day refresh window, then kill the upstream:
    # the stale copy (and the already-derived icons) must keep serving, and the
    # outbound refetch must be single-flighted (one attempt per cooldown window,
    # NEVER one per request — that starved the gunicorn pool in review).
    logo = _logo_path(favicon_env)
    old = os.path.getmtime(logo) - 8 * 24 * 3600
    os.utime(logo, (old, old))
    responses.reset()
    responses.add(responses.GET, _LOGO_URL, body=requests.exceptions.ConnectTimeout())
    for _ in range(3):  # cooldown from the first fetch still held: zero attempts
        assert client.get("/favicon.ico").status_code == 200
    assert len(responses.calls) == 0
    # Cooldown expiry (simulated): exactly one failed attempt, still serving stale.
    cache.delete(_REFRESH_CACHE_KEY.format(corp_id=HOME_CORP))
    assert client.get("/favicon.ico").status_code == 200
    assert client.get("/favicon-32.png").status_code == 200
    assert len(responses.calls) == 1


@responses.activate
def test_unwritable_mirror_404s_never_500s(client, favicon_env):
    # Fetch succeeds but the eveimg volume is unwritable (ENOSPC / ro-mount):
    # must degrade to the negative-cached 404, not an uncached 500 loop.
    responses.add(responses.GET, _LOGO_URL, body=_png(), content_type="image/png", status=200)
    os.chmod(favicon_env, 0o500)
    try:
        assert client.get("/favicon.ico").status_code == 404
        assert client.get("/apple-touch-icon.png").status_code == 404
        assert len(responses.calls) == 1  # miss is cached — no per-request refetch
    finally:
        os.chmod(favicon_env, 0o755)  # noqa: S103 — restore the tmp dir so pytest can clean it up


@responses.activate
def test_derivation_failure_stale_serves_existing_icons(client, favicon_env, monkeypatch):
    # A regen failure (disk full mid-derive) must keep serving yesterday's icons,
    # not black-hole five routes behind a browser-cached 404.
    responses.add(responses.GET, _LOGO_URL, body=_png(), content_type="image/png", status=200)
    first = client.get("/favicon-32.png")
    assert first.status_code == 200
    logo = _logo_path(favicon_env)
    bump = os.path.getmtime(logo) + 5
    os.utime(logo, (bump, bump))  # derived set is now stale → regen wanted

    import core.favicon as favicon_mod

    def _boom(logo_path, out_dir):
        raise OSError("disk full")

    monkeypatch.setattr(favicon_mod, "_derive_all", _boom)
    resp = client.get("/favicon-32.png")
    assert resp.status_code == 200
    assert resp.content == first.content  # the stale-but-valid derivation


@responses.activate
def test_hostile_logo_dimensions_404_not_500(client, favicon_env, monkeypatch):
    # A decompression-bomb-shaped mirror file must be rejected from the header and
    # degrade to the negative-cached 404 (Pillow's bomb error is NOT an OSError).
    import core.favicon as favicon_mod

    monkeypatch.setattr(favicon_mod, "_MAX_SOURCE_PIXELS", 100)
    responses.add(responses.GET, _LOGO_URL, body=_png(), content_type="image/png", status=200)
    assert client.get("/favicon.ico").status_code == 404
    assert client.get("/favicon-16.png").status_code == 404
    assert len(responses.calls) == 1


# --- refresh seam ------------------------------------------------------------
@responses.activate
def test_derived_icons_refresh_when_mirrored_logo_changes(client, favicon_env):
    responses.add(responses.GET, _LOGO_URL, body=_png((0, 0, 255, 255)),
                  content_type="image/png", status=200)
    assert client.get("/favicon-32.png").status_code == 200
    # Simulate the mirror's 7-day refresh landing a NEW logo: newer file, new pixels.
    logo = _logo_path(favicon_env)
    with open(logo, "wb") as fh:
        fh.write(_png((255, 0, 0, 255)))
    bump = os.path.getmtime(logo) + 5
    os.utime(logo, (bump, bump))
    resp = client.get("/favicon-32.png")
    img = Image.open(io.BytesIO(resp.content)).convert("RGB")
    assert img.getpixel((16, 16)) == (255, 0, 0)


# --- integration seams -------------------------------------------------------
@responses.activate
def test_icons_carry_no_vary_cookie(client, favicon_env):
    # The locale middleware exempts icon paths from Vary: Accept-Language, Cookie —
    # otherwise every session rotation would invalidate the day-long browser cache.
    responses.add(responses.GET, _LOGO_URL, body=_png(), content_type="image/png", status=200)
    resp = client.get("/favicon.ico")
    assert resp.status_code == 200
    assert "Cookie" not in resp.get("Vary", "")
    assert "Cookie" not in client.get("/apple-touch-icon.png").get("Vary", "")


def test_atomic_write_publishes_world_readable(tmp_path):
    # mkstemp creates 0600 tmp files; the published asset must come out 0644 or
    # nginx (a different user) stops being able to serve signature banners.
    from apps.killboard.signature_assets import _atomic_write

    target = os.path.join(str(tmp_path), "nested", "icon.png")
    _atomic_write(target, b"png-bytes")
    assert os.stat(target).st_mode & 0o777 == 0o644
    assert open(target, "rb").read() == b"png-bytes"


def test_membership_gate_allows_icon_probes():
    from core.middleware import _path_allowed

    for path in ("/favicon.ico", "/favicon-32.png", "/favicon-16.png",
                 "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"):
        assert _path_allowed(path) is True


@pytest.mark.django_db
def test_base_head_emits_icon_links_when_configured(client, settings):
    settings.FORCA_HOME_CORP_ID = HOME_CORP
    html = client.get("/").content.decode()
    assert 'rel="apple-touch-icon"' in html
    assert "/favicon-32.png" in html and "/favicon.ico" in html


@pytest.mark.django_db
def test_base_head_omits_icon_links_when_unconfigured(client, settings):
    settings.FORCA_HOME_CORP_ID = 0
    html = client.get("/").content.decode()
    assert "apple-touch-icon" not in html
