"""Combat Signatures — background-library infrastructure (WS-2).

The reusable, testable seam shared by the generator command, the seed migration, the
``sync_signature_backgrounds`` command and the tests. It deliberately owns everything *except*
the procedural art itself (that lives in the ``generate_signature_backgrounds`` command):

* the canonical size presets and safe-area geometry every design must honour,
* the app-token palette the backgrounds are anchored on,
* ``text_zone_ok`` — the contrast gate that proves a design keeps its text areas legible —
  and ``apply_safe_scrim`` — the darkening the generator bakes in to satisfy it,
* manifest location/loading helpers robust to the working directory (they resolve against
  ``settings.BASE_DIR``, never ``os.getcwd()``), and
* ``sync_from_manifest`` — the upsert used identically by the data migration and the sync
  command (never deletes a row; a key dropped from the manifest is retired, not removed).

Pillow is imported lazily inside the drawing/checking helpers so importing this module from a
migration stays cheap and dependency-free.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from django.conf import settings

log = logging.getLogger("forca.killboard")

# --------------------------------------------------------------------------- #
#  Size presets (plan A14) — fixed enum, no arbitrary dimensions.
# --------------------------------------------------------------------------- #
PRESETS: dict[str, tuple[int, int]] = {
    "compact": (468, 120),
    "standard": (600, 150),
    "wide": (728, 120),
    "card": (600, 200),
}
# The picker thumbnail is the standard preset downscaled to a fixed strip.
THUMB_KEY = "thumb"
THUMB_SIZE: tuple[int, int] = (200, 50)

# Every committed file for a design, in manifest order.
FILE_KEYS: tuple[str, ...] = (*PRESETS.keys(), THUMB_KEY)


def preset_size(name: str) -> tuple[int, int]:
    """The pixel dimensions of a preset (or the thumbnail)."""
    if name == THUMB_KEY:
        return THUMB_SIZE
    return PRESETS[name]


# --------------------------------------------------------------------------- #
#  Safe-area geometry — the two zones a design must keep dark/low-detail so the
#  renderer (WS-3) can lay text over them at any preset without a fight.
# --------------------------------------------------------------------------- #
# Portrait/name column: the left fraction of the width, full height.
SAFE_LEFT_FRAC = 0.30
# Central text band: the vertical middle where the primary stat line sits.
SAFE_BAND_TOP = 0.34
SAFE_BAND_BOTTOM = 0.66

# text_zone_ok thresholds (0..255 luminance). A design whose safe zones exceed either is
# rejected by the generator and by the test suite.
LUMA_MAX = 66.0
CONTRAST_MAX = 48.0

# Scrim strengths (0..255 alpha toward the space colour) apply_safe_scrim bakes in. Kept only as
# strong as the contrast gate needs (measured safe-zone luma lands ~3x under LUMA_MAX), so the band
# reads as an atmospheric darkening rather than a hard bar and art still breathes through it.
_LEFT_ALPHA = 236
_BAND_ALPHA = 200
_FLOOR_ALPHA = 30
# Feather (fraction of height) easing the band in/out of the flat dark core over the check zone.
_BAND_FEATHER = 0.11

# --------------------------------------------------------------------------- #
#  Palette — mirrors the canonical Tailwind design tokens
#  (frontend/tailwind.config.js:26-34) so a banner sits under the app's UI.
# --------------------------------------------------------------------------- #
SPACE = (10, 14, 22)      # #0a0e16 page background — also the scrim colour
PANEL = (16, 21, 31)      # #10151f
PANEL2 = (22, 29, 41)     # #161d29
LINE = (34, 45, 62)       # #222d3e
GOLD = (244, 165, 43)     # #f4a52b
GOLDB = (255, 200, 97)    # #ffc861
CYAN = (70, 207, 224)     # #46cfe0
KILL = (63, 185, 80)      # #3fb950
LOSS = (240, 83, 63)      # #f0533f
WIN = (52, 211, 153)      # #34d399
INK = (232, 238, 246)     # #e8eef6
MUTED = (138, 152, 171)   # #8a98ab
FAINT = (90, 102, 120)    # #5a6678

# Atmospheric accent hues (not UI tokens) used only to tint nebulae, glows and warp trails.
# Kept muted and anchored near the token family so a banner still reads as part of the app.
EMBER = (196, 72, 40)
ROSE = (198, 60, 96)
VIOLET = (128, 84, 208)
INDIGO = (52, 74, 150)
TEAL = (36, 150, 168)
AZURE = (60, 120, 210)
GOLDDUST = (200, 150, 74)
SMOKE = (120, 120, 132)

PALETTE = {
    "space": SPACE, "panel": PANEL, "panel2": PANEL2, "line": LINE,
    "gold": GOLD, "goldb": GOLDB, "cyan": CYAN, "kill": KILL, "loss": LOSS,
    "win": WIN, "ink": INK, "muted": MUTED, "faint": FAINT,
}


# --------------------------------------------------------------------------- #
#  Contrast gate
# --------------------------------------------------------------------------- #
def _interp(frac: float, stops: list[tuple[float, float]]) -> float:
    """Piecewise-linear interpolation of ``frac`` (0..1) across sorted ``(pos, value)`` stops."""
    if frac <= stops[0][0]:
        return stops[0][1]
    if frac >= stops[-1][0]:
        return stops[-1][1]
    for (p0, v0), (p1, v1) in zip(stops, stops[1:], strict=False):
        if p0 <= frac <= p1:
            span = p1 - p0
            if span <= 0:
                return v1
            return v0 + (v1 - v0) * (frac - p0) / span
    return stops[-1][1]


def _ramp_mask(size: tuple[int, int], axis: str, stops: list[tuple[float, float]]):
    """A one-dimensional alpha ramp stretched over ``size`` — cheap (one C resize, no pixel loop)."""
    from PIL import Image

    w, h = size
    n = w if axis == "x" else h
    denom = max(1, n - 1)
    profile = [int(round(_interp(i / denom, stops))) for i in range(n)]
    if axis == "x":
        line = Image.new("L", (n, 1))
        line.putdata(profile)
        return line.resize((w, h))
    line = Image.new("L", (1, n))
    line.putdata(profile)
    return line.resize((w, h))


def apply_safe_scrim(art):
    """Return ``art`` with the two safe zones darkened toward the space colour.

    Every shipped design ends with this call so ``text_zone_ok`` holds at every preset: a strong
    left-column ramp (portrait zone), a strong central-band ramp (text zone) and a gentle global
    floor, combined by taking the darkest contribution per pixel, then composited over the art.
    """
    from PIL import Image, ImageChops

    rgb = art.convert("RGB")
    size = rgb.size
    left = _ramp_mask(size, "x", [(0.0, _LEFT_ALPHA), (SAFE_LEFT_FRAC, _LEFT_ALPHA), (0.53, 0.0)])
    band = _ramp_mask(size, "y", [
        (0.0, 0.0), (SAFE_BAND_TOP - _BAND_FEATHER, 0.0), (SAFE_BAND_TOP, _BAND_ALPHA),
        (SAFE_BAND_BOTTOM, _BAND_ALPHA), (SAFE_BAND_BOTTOM + _BAND_FEATHER, 0.0), (1.0, 0.0),
    ])
    floor = Image.new("L", size, _FLOOR_ALPHA)
    mask = ImageChops.lighter(ImageChops.lighter(left, band), floor)
    space_img = Image.new("RGB", size, SPACE)
    return Image.composite(space_img, rgb, mask)


def _zone_stats(zone) -> tuple[float, float]:
    """The mean luminance and worst local (tiled) contrast of a grayscale zone."""
    from PIL import ImageStat

    mean = ImageStat.Stat(zone).mean[0]
    w, h = zone.size
    cols, rows = 6, 3
    worst = 0.0
    for cx in range(cols):
        for cy in range(rows):
            x0, x1 = w * cx // cols, w * (cx + 1) // cols
            y0, y1 = h * cy // rows, h * (cy + 1) // rows
            if x1 <= x0 or y1 <= y0:
                continue
            worst = max(worst, ImageStat.Stat(zone.crop((x0, y0, x1, y1))).stddev[0])
    return mean, worst


def text_zone_ok(img, *, luma_max: float = LUMA_MAX, contrast_max: float = CONTRAST_MAX) -> bool:
    """True when both safe zones are dark and low-contrast enough to carry overlaid text.

    Checks the portrait column (left ``SAFE_LEFT_FRAC``) and the central text band
    (``SAFE_BAND_TOP``..``SAFE_BAND_BOTTOM``): each must have mean luminance below ``luma_max``
    and a bounded worst-tile contrast below ``contrast_max``. Used both to self-verify the
    generator and to gate every committed design in the test suite.
    """
    gray = img.convert("L")
    w, h = gray.size
    zones = [
        gray.crop((0, 0, max(1, round(w * SAFE_LEFT_FRAC)), h)),
        gray.crop((0, round(h * SAFE_BAND_TOP), w, round(h * SAFE_BAND_BOTTOM))),
    ]
    for zone in zones:
        mean, contrast = _zone_stats(zone)
        if mean > luma_max or contrast > contrast_max:
            return False
    return True


# --------------------------------------------------------------------------- #
#  Manifest location & loading (cwd-independent)
# --------------------------------------------------------------------------- #
# The canonical committed location, repo-root-relative — recorded in the manifest regardless of
# where a --check/test run happens to write, so the manifest always points at the shipped files.
SIGBG_REL = "static/killboard/sigbg"


def sigbg_dir() -> Path:
    """The committed background tree ``static/killboard/sigbg`` under the repo root."""
    return Path(settings.BASE_DIR) / "static" / "killboard" / "sigbg"


def manifest_path() -> Path:
    return sigbg_dir() / "manifest.json"


def canonical_rel(key: str, name: str) -> str:
    """The repo-root-relative POSIX path a design's file has once committed (stable, cwd-free)."""
    return f"{SIGBG_REL}/{key}/{name}.png"


def load_manifest(path: Path | None = None) -> dict:
    with open(path or manifest_path(), encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------- #
#  DB sync — shared by the data migration and the sync command.
# --------------------------------------------------------------------------- #
_SYNC_FIELDS = ("name", "category", "display_order", "version", "checksum")


def sync_from_manifest(manifest: dict, model) -> tuple[int, int, int]:
    """Upsert ``SignatureBackground`` rows from ``manifest``; never delete a row.

    For each manifest entry: create it (enabled) if new, else update only the synced metadata
    fields — an admin's ``enabled`` choice is preserved. Any existing key *absent* from the
    manifest is retired (``enabled=False``) rather than deleted, so a removed design stops being
    offered without breaking signatures that still reference it (the FK is ``PROTECT``).

    ``model`` is the concrete or historical ``SignatureBackground`` model, so the migration can
    pass ``apps.get_model(...)``. Returns ``(created, updated, retired)`` counts.
    """
    keys: set[str] = set()
    created = updated = 0
    for bg in manifest.get("backgrounds", []):
        key = bg["key"]
        keys.add(key)
        values = {
            "name": bg["name"],
            "category": bg.get("category", ""),
            "display_order": bg.get("display_order", 0),
            "version": bg.get("version", 1),
            "checksum": bg.get("checksum", ""),
        }
        obj, was_created = model.objects.get_or_create(key=key, defaults={**values, "enabled": True})
        if was_created:
            created += 1
            continue
        dirty = [f for f in _SYNC_FIELDS if getattr(obj, f) != values[f]]
        if dirty:
            for field in dirty:
                setattr(obj, field, values[field])
            obj.save(update_fields=[*dirty, "updated_at"])
            updated += 1
    retired = model.objects.exclude(key__in=keys).filter(enabled=True).update(enabled=False)
    return created, updated, retired


# --------------------------------------------------------------------------- #
#  Portrait / logo mirror (plan A7) — worker-side fetch, never at render time.
# --------------------------------------------------------------------------- #
# Portraits and corp/alliance logos are mutable (unlike the immutable type-image mirror), so they
# are fetched and cached under ``EVE_IMAGE_MIRROR_DIR`` on a 7-day refresh cadence by the render
# pre-step (Celery-side in WS-4). The renderer itself only ever reads a local path — no network.
#
# The guarded-fetch shape is copied from ``mirror_type_images`` (fixed host from
# ``EVE_IMAGE_SOURCE_URL``, strictly numeric ids, content-type→extension whitelist, atomic
# tmp+replace write) and HARDENED here with a streamed size cap the one-shot command does not need
# (a busy render worker must never buffer an unbounded upstream response). The existing command is
# left untouched.
_MAX_IMAGE_BYTES = 5 * 1024 * 1024          # abort a stream past 5 MB — a portrait is ~10-50 KB
_IMAGE_REFETCH_AGE = 7 * 24 * 3600           # refetch only once the cached copy is a week old
_IMAGE_TIMEOUT = 15                          # seconds; a render must not hang on a slow CDN
_CONTENT_EXT = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png"}


def _atomic_write(path: str, data: bytes) -> None:
    """Write ``data`` to ``path`` via a tmp file + ``os.replace`` so a reader never sees a
    half-written image (identical to the type-image mirror's ``_write``)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def _existing_asset(base: str) -> str | None:
    """The mirrored file for ``base`` (``…/portrait-256``) at either allowed extension, or None."""
    for ext in ("jpg", "png"):
        candidate = f"{base}.{ext}"
        if os.path.exists(candidate):
            return candidate
    return None


def _is_fresh(path: str) -> bool:
    try:
        return (time.time() - os.path.getmtime(path)) < _IMAGE_REFETCH_AGE
    except OSError:
        return False


def _fetch_image(category: str, entity_id: int, kind: str, size: int, base: str,
                 timeout: float = _IMAGE_TIMEOUT) -> str | None:
    """Stream one image from the fixed EVE image server into the mirror; return its path or None.

    The URL is assembled ONLY from ``EVE_IMAGE_SOURCE_URL`` + the numeric id (no user input, no
    redirects followed to a caller-chosen host — SSRF surface is a fixed template). The response is
    streamed with a hard 5 MB ceiling, its content-type must be jpeg/png, and it is written
    atomically. Any failure returns None so the caller can fall back to a stale copy or a monogram.
    ``timeout`` is short on the interactive preview path so a cold fetch can't hold a web worker.
    """
    import requests

    base_url = getattr(settings, "EVE_IMAGE_SOURCE_URL", "https://images.evetech.net").rstrip("/")
    url = f"{base_url}/{category}/{entity_id}/{kind}?size={size}"
    headers = {"User-Agent": getattr(settings, "ESI_USER_AGENT", "forca-command-grid")}
    try:
        # allow_redirects=False makes the docstring's "no redirects to a caller-chosen host" true in
        # code: a 30x from the image host becomes a non-200 (→ None) instead of being transparently
        # followed to wherever Location points — closing an SSRF-via-redirect vector if the fixed
        # upstream is ever compromised/MITM'd. The real image server answers valid ids with a 200.
        resp = requests.get(url, headers=headers, timeout=timeout, stream=True,
                            allow_redirects=False)
    except requests.RequestException:
        return None
    try:
        if resp.status_code != 200:
            return None
        ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
        ext = _CONTENT_EXT.get(ctype)
        if not ext:
            return None
        buf = bytearray()
        for chunk in resp.iter_content(8192):
            if not chunk:
                continue
            buf.extend(chunk)
            if len(buf) > _MAX_IMAGE_BYTES:
                return None  # oversized upstream — abort BEFORE any file is written
    except requests.RequestException:
        return None
    finally:
        resp.close()
    if not buf:
        return None
    path = f"{base}.{ext}"
    _atomic_write(path, bytes(buf))
    # If the content-type flipped since a prior fetch, drop the stale sibling so lookups are exact.
    sibling = f"{base}.{'png' if ext == 'jpg' else 'jpg'}"
    if os.path.exists(sibling):
        try:
            os.remove(sibling)
        except OSError:
            pass
    return path


def _ensure_image(category: str, entity_id, kind: str, size: int,
                  timeout: float = _IMAGE_TIMEOUT) -> str | None:
    """Return a local mirror path for one entity image, fetching/refreshing when stale.

    Returns the freshest available local path, or None when the id is unusable, the mirror dir is
    unset, or the fetch failed with no cached copy to fall back to. ``timeout`` bounds the network
    fetch: the Celery render uses the full default, the interactive preview passes a short value.
    """
    try:
        eid = int(entity_id)
    except (TypeError, ValueError):
        return None
    if eid <= 0:
        return None
    size = int(size)
    root = getattr(settings, "EVE_IMAGE_MIRROR_DIR", "") or ""
    if not root:
        return None
    base = os.path.join(root, category, str(eid), f"{kind}-{size}")
    existing = _existing_asset(base)
    if existing and _is_fresh(existing):
        return existing
    fetched = _fetch_image(category, eid, kind, size, base, timeout=timeout)
    if fetched:
        return fetched
    return existing  # a stale copy is still better than a monogram; None if there is none


def ensure_portrait(character_id, size: int = 256, timeout: float = _IMAGE_TIMEOUT) -> str | None:
    """Local path to a pilot's portrait (``characters/<id>/portrait-<size>.<ext>``), or None."""
    return _ensure_image("characters", character_id, "portrait", size, timeout=timeout)


def ensure_corp_logo(corp_id, size: int = 128, timeout: float = _IMAGE_TIMEOUT) -> str | None:
    """Local path to a corporation logo (``corporations/<id>/logo-<size>.<ext>``), or None."""
    return _ensure_image("corporations", corp_id, "logo", size, timeout=timeout)


def ensure_alliance_logo(alliance_id, size: int = 128, timeout: float = _IMAGE_TIMEOUT) -> str | None:
    """Local path to an alliance logo (``alliances/<id>/logo-<size>.<ext>``), or None."""
    return _ensure_image("alliances", alliance_id, "logo", size, timeout=timeout)
