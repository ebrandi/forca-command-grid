"""KB-39 (WS-D6) — server-rendered shareable card images (Pillow).

Two artifacts share this module:

* the **kill-card** (``/killboard/<id>/card.png``) — the 1200x630 image an unfurled killmail
  link shows in Discord/X. Public, same access gate as the killmail detail page.
* the **CV card** (``/killboard/pilot/<id>/cv/card.png``) — a pilot's combat-identity card,
  member-gated (the CV page is member-gated; the card is for members to save deliberately).

Self-host constraints (memory: no external CDN fetch at render):

* The victim **ship render** is read straight off the local ``EVE_IMAGE_MIRROR_DIR`` disk mirror
  (``types/<id>/render-<size>.{png,jpg}`` — the exact layout ``mirror_type_images`` writes). No
  network call is ever made. When the render isn't mirrored (a fresh type, or any environment
  without the mirror, including tests) the card falls back to a drawn placeholder, so the PNG is
  always produced and always the same size.
* Portraits and corp/alliance logos are NOT part of the disk mirror (nginx proxies them per
  request), so a card never embeds them: the CV card draws a monogram avatar, and the kill-card
  shows the corp *name*. A branding logo is embedded only when it is a bundled ``/static/`` asset
  readable from disk — never an ``https://`` URL fetched at render time.

Caching: the rendered PNG is stored in the Django cache (Redis in prod) keyed by id + a version
token that folds in the branding fingerprint, so a branding change transparently invalidates
every card without an explicit purge. A per-IP fixed-window throttle bounds the public endpoint.
"""
from __future__ import annotations

import hashlib
import io
import logging
import os

from django.conf import settings
from django.core.cache import cache

log = logging.getLogger("forca.killboard")

CARD_W, CARD_H = 1200, 630
# Bump when the drawn layout changes so stale cached PNGs are abandoned (folded into the
# cache-key version alongside the live branding fingerprint).
_LAYOUT_VERSION = 1
_CACHE_TTL = 7 * 24 * 3600  # a card is cheap to regenerate; a week bounds the keyspace

# --- palette (matches the app's space/gold theme) --------------------------------------------
_BG = (11, 14, 20)
_PANEL = (20, 25, 34)
_INK = (232, 236, 242)
_MUTED = (150, 160, 175)
_FAINT = (108, 118, 133)
_GOLD = (227, 179, 65)
_KILL = (61, 220, 132)
_LOSS = (240, 96, 96)
_LINE = (44, 52, 66)

_FONT_CANDIDATES = {
    False: (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ),
    True: (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    ),
}


# --------------------------------------------------------------------------------------------- #
#  Fonts
# --------------------------------------------------------------------------------------------- #
def _font(size: int, *, bold: bool = False):
    """A TrueType DejaVu face at ``size``, or Pillow's scalable default when the font package
    isn't installed (keeps rendering working in a bare environment; the card just looks plainer)."""
    from PIL import ImageFont

    for path in _FONT_CANDIDATES[bold]:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size)
    except TypeError:  # very old Pillow without a sized default
        return ImageFont.load_default()


# --------------------------------------------------------------------------------------------- #
#  Local-mirror image loading (never a network fetch)
# --------------------------------------------------------------------------------------------- #
def _mirror_dir() -> str:
    return getattr(settings, "EVE_IMAGE_MIRROR_DIR", "") or ""


def _load_ship_render(type_id: int | None):
    """The victim ship render from the local disk mirror, or ``None`` when not mirrored.

    Reads ``<mirror>/types/<id>/render-<size>.{png,jpg}`` — the exact path
    ``mirror_type_images`` writes (it mirrors render sizes 64 and 512 by default). Prefers 512.
    """
    if not type_id:
        return None
    root = _mirror_dir()
    if not root:
        return None
    from PIL import Image

    for size in (512, 256, 128, 64):
        for ext in (".png", ".jpg"):
            path = os.path.join(root, "types", str(type_id), f"render-{size}{ext}")
            if os.path.exists(path):
                try:
                    return Image.open(path).convert("RGBA")
                except (OSError, ValueError):
                    continue
    return None


def _load_static_logo(logo_url: str):
    """A branding logo image ONLY when it is a bundled ``/static/`` asset readable from disk.

    An ``https://`` logo is intentionally not fetched at render time (self-host: no external
    fetch), so those installs get the text wordmark instead of an embedded logo.
    """
    if not logo_url or not logo_url.startswith("/static/"):
        return None
    from django.contrib.staticfiles import finders
    from PIL import Image

    rel = logo_url[len("/static/"):]
    path = finders.find(rel)
    if not path or not os.path.exists(path):
        return None
    try:
        return Image.open(path).convert("RGBA")
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------------------------- #
#  Small formatting / name helpers (direct, cache-free — a card render is rare)
# --------------------------------------------------------------------------------------------- #
def _isk(value) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "0"
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(v) >= div:
            return f"{v / div:.2f}{unit}"
    return f"{v:.0f}"


def _type_name(type_id: int | None) -> str:
    if not type_id:
        return ""
    from apps.sde.models import SdeType

    return (
        SdeType.objects.filter(type_id=type_id).values_list("name", flat=True).first()
        or f"Type {type_id}"
    )


def _system(system_id: int | None) -> tuple[str, float | None]:
    if not system_id:
        return "", None
    from apps.sde.models import SdeSolarSystem

    row = (
        SdeSolarSystem.objects.filter(system_id=system_id)
        .values_list("name", "security").first()
    )
    if not row:
        return f"System {system_id}", None
    return row[0], (round(row[1], 1) if row[1] is not None else None)


def _entity_name(entity_id: int | None) -> str:
    if not entity_id:
        return ""
    from apps.corporation.models import EveName

    return (
        EveName.objects.filter(entity_id=entity_id).values_list("name", flat=True).first() or ""
    )


def _sec_color(sec: float | None) -> tuple[int, int, int]:
    if sec is None:
        return _FAINT
    if sec >= 0.75:
        return (86, 197, 224)
    if sec >= 0.45:
        return (222, 199, 74)
    if sec >= 0.05:
        return (224, 148, 74)
    return (224, 86, 86)


def _monogram(name: str) -> str:
    parts = [p for p in (name or "").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _hex_to_rgb(value: str, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    value = (value or "").strip().lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    if len(value) != 6:
        return fallback
    try:
        return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))
    except ValueError:
        return fallback


# --------------------------------------------------------------------------------------------- #
#  Drawing primitives
# --------------------------------------------------------------------------------------------- #
def _truncate(draw, text: str, font, max_width: int) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    ell = "…"
    while text and draw.textlength(text + ell, font=font) > max_width:
        text = text[:-1]
    return (text + ell) if text else ell


def _dark_scrim(size: tuple[int, int], fade_from: int, fade_to: int):
    """A left-anchored horizontal scrim: opaque ``_BG`` on the left, transparent past
    ``fade_to`` — so text over the ship render stays legible."""
    from PIL import Image

    w, h = size
    scrim = Image.new("RGBA", size, (0, 0, 0, 0))
    px = scrim.load()
    for x in range(w):
        if x <= fade_from:
            a = 255
        elif x >= fade_to:
            a = 0
        else:
            a = int(255 * (fade_to - x) / (fade_to - fade_from))
        col = (_BG[0], _BG[1], _BG[2], a)
        for y in range(h):
            px[x, y] = col
    return scrim


# --------------------------------------------------------------------------------------------- #
#  Kill-card
# --------------------------------------------------------------------------------------------- #
def render_kill_card(killmail) -> bytes:
    """Render the kill-card PNG bytes for ``killmail`` (deterministic 1200x630)."""
    from PIL import Image, ImageDraw

    from . import branding as branding_mod

    brand = branding_mod.get_branding()
    accent = _hex_to_rgb(brand.get("accent_color", ""), _GOLD)
    is_loss = getattr(killmail, "home_corp_role", "") == "victim"
    role_color = _LOSS if is_loss else _KILL
    role_label = "LOSS" if is_loss else "KILL"

    img = Image.new("RGBA", (CARD_W, CARD_H), _BG + (255,))

    # Ship render bled off the right edge, then a scrim for text contrast on the left.
    render = _load_ship_render(getattr(killmail, "victim_ship_type_id", None))
    if render is not None:
        target_h = 600
        ratio = target_h / render.height
        render = render.resize((int(render.width * ratio), target_h))
        img.alpha_composite(render, (CARD_W - render.width + 40, (CARD_H - render.height) // 2))
        img.alpha_composite(_dark_scrim((CARD_W, CARD_H), 560, 1080))
    else:
        _draw_ship_placeholder(img, accent)

    draw = ImageDraw.Draw(img)
    # Accent rail.
    draw.rectangle((0, 0, 12, CARD_H), fill=accent)

    pad = 64
    right = 760  # text stays clear of the ship render
    # Role eyebrow.
    draw.text((pad, 56), role_label, font=_font(26, bold=True), fill=role_color)
    draw.text((pad + 90, 60), "· " + _isk_points(killmail), font=_font(20), fill=_FAINT)

    # Ship name.
    ship_name = _type_name(getattr(killmail, "victim_ship_type_id", None)) or "Unknown ship"
    draw.text((pad, 96), _truncate(draw, ship_name, _font(58, bold=True), right - pad),
              font=_font(58, bold=True), fill=_INK)

    # Pilot / corp.
    pilot = _entity_name(getattr(killmail, "victim_character_id", None))
    corp = _entity_name(getattr(killmail, "victim_corporation_id", None))
    who = " · ".join(p for p in (pilot, corp) if p) or "Unknown pilot"
    draw.text((pad, 178), _truncate(draw, who, _font(30), right - pad), font=_font(30),
              fill=_MUTED)

    draw.line((pad, 236, right, 236), fill=_LINE, width=2)

    # System + security band.
    sys_name, sec = _system(getattr(killmail, "solar_system_id", None))
    if sec is not None:
        draw.text((pad, 262), f"{sec:.1f}", font=_font(26, bold=True), fill=_sec_color(sec))
        draw.text((pad + 60, 264), _truncate(draw, sys_name, _font(26), right - pad - 60),
                  font=_font(26), fill=_MUTED)
    else:
        draw.text((pad, 264), _truncate(draw, sys_name, _font(26), right - pad), font=_font(26),
                  fill=_MUTED)

    # Headline ISK value (at-kill).
    value = killmail.value_at_kill if getattr(killmail, "value_at_kill", None) is not None \
        else getattr(killmail, "total_value", 0)
    draw.text((pad, 330), _isk(value), font=_font(96, bold=True), fill=accent)
    draw.text((pad + 6, 452), "ISK destroyed" if not is_loss else "ISK lost",
              font=_font(24), fill=_FAINT)

    # Participant count.
    n = _attacker_count(killmail)
    draw.text((pad, 500), f"{n} pilot{'s' if n != 1 else ''} on the kill", font=_font(26),
              fill=_MUTED)

    _draw_footer(img, draw, brand, accent)
    return _to_png(img)


def _isk_points(killmail) -> str:
    pts = getattr(killmail, "points", None)
    return f"{pts} pts" if pts else "kill"


def _attacker_count(killmail) -> int:
    try:
        return killmail.participants.filter(role="attacker").count()
    except Exception:  # noqa: BLE001 — a detached/partial object must not break the card
        return 0


def _draw_ship_placeholder(img, accent) -> None:
    """A subtle geometric glyph where a mirrored ship render would sit (mirror miss / tests)."""
    from PIL import ImageDraw

    draw = ImageDraw.Draw(img)
    cx, cy, r = 940, CARD_H // 2, 150
    draw.polygon([(cx, cy - r), (cx + r, cy + r), (cx, cy + r * 0.55), (cx - r, cy + r)],
                 outline=accent + (70,), width=3)


def _draw_footer(img, draw, brand: dict, accent) -> None:
    y = CARD_H - 58
    x = 64
    logo = _load_static_logo(brand.get("logo_url", ""))
    if logo is not None:
        logo = logo.resize((36, 36))
        img.alpha_composite(logo, (x, y - 4))
        x += 48
    name = brand.get("display_name") or "[FORCA] Command Grid"
    draw.text((x, y), name, font=_font(24, bold=True), fill=_MUTED)
    tw = draw.textlength(name, font=_font(24, bold=True))
    if brand.get("display_name"):
        draw.text((x + tw + 14, y + 3), "· [FORCA] Command Grid", font=_font(20), fill=_FAINT)


# --------------------------------------------------------------------------------------------- #
#  CV card
# --------------------------------------------------------------------------------------------- #
def render_cv_card(character_id: int, ctx: dict) -> bytes:
    """Render the pilot CV card PNG from a ``cv.pilot_cv`` payload (+ resolved ``pilot_name``)."""
    from PIL import Image, ImageDraw

    from . import branding as branding_mod

    brand = branding_mod.get_branding()
    accent = _hex_to_rgb(brand.get("accent_color", ""), _GOLD)
    card = ctx.get("card") or {}
    name = ctx.get("pilot_name") or _entity_name(character_id) or f"Pilot {character_id}"

    img = Image.new("RGBA", (CARD_W, CARD_H), _BG + (255,))
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, 12, CARD_H), fill=accent)

    pad = 64
    # Monogram avatar (portraits aren't part of the disk mirror — see module docstring).
    av = 150
    ax, ay = pad, 64
    draw.rounded_rectangle((ax, ay, ax + av, ay + av), radius=24, fill=_PANEL,
                           outline=accent + (120,), width=3)
    mono = _monogram(name)
    mf = _font(64, bold=True)
    mw = draw.textlength(mono, font=mf)
    draw.text((ax + (av - mw) / 2, ay + 34), mono, font=mf, fill=accent)

    tx = ax + av + 36
    draw.text((tx, 74), "PVP CV", font=_font(24, bold=True), fill=_FAINT)
    draw.text((tx, 108), _truncate(draw, name, _font(56, bold=True), CARD_W - tx - pad),
              font=_font(56, bold=True), fill=_INK)
    rank = (ctx.get("rank_progress") or {}).get("current") or {}
    rank_title = rank.get("title") or ""
    if rank_title:
        draw.text((tx, 180), _truncate(draw, rank_title, _font(30), CARD_W - tx - pad),
                  font=_font(30), fill=accent)

    # Headline stat tiles.
    stats = [
        ("KILLS", str(card.get("kills", 0)), _KILL),
        ("LOSSES", str(card.get("losses", 0)), _LOSS),
        ("EFFICIENCY", f"{_pct(card.get('efficiency'))}%", _INK),
        ("SOLO", f"{_pct(card.get('solo_pct'))}%", _INK),
    ]
    tile_w, tile_h, gap = 258, 128, 20
    ty = 268
    for i, (label, value, color) in enumerate(stats):
        x0 = pad + i * (tile_w + gap)
        draw.rounded_rectangle((x0, ty, x0 + tile_w, ty + tile_h), radius=16, fill=_PANEL)
        draw.text((x0 + 20, ty + 18), label, font=_font(20, bold=True), fill=_FAINT)
        draw.text((x0 + 20, ty + 52), value, font=_font(48, bold=True), fill=color)

    # Trophies + best kill line.
    by = 440
    trophies = ctx.get("trophies") or []
    kotw = ctx.get("kotw") or []
    draw.text((pad, by), f"{len(trophies)} trophies", font=_font(28, bold=True), fill=accent)
    if kotw:
        draw.text((pad + 240, by + 4), f"· {len(kotw)}× Kill of the Week", font=_font(24),
                  fill=_MUTED)
    best = ctx.get("best_kill") or {}
    if best.get("value"):
        draw.text((pad, by + 46), f"Best kill: {_isk(best['value'])} ISK", font=_font(26),
                  fill=_MUTED)

    _draw_footer(img, draw, brand, accent)
    return _to_png(img)


def _pct(value) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "0"
    if v >= 100:
        return "100"
    if v <= 0:
        return "0"
    return f"{round(v, 1):.1f}"


def _to_png(img) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# --------------------------------------------------------------------------------------------- #
#  Cache + version
# --------------------------------------------------------------------------------------------- #
def card_version() -> str:
    """A version token folding in the layout version and the live branding fingerprint, so a
    branding change (accent/logo/name) transparently invalidates every previously cached card."""
    from . import branding as branding_mod

    brand = branding_mod.get_branding()
    digest = hashlib.sha1(
        repr(sorted(brand.items())).encode("utf-8"), usedforsecurity=False
    ).hexdigest()[:10]
    return f"{_LAYOUT_VERSION}-{digest}"


def kill_card_png(killmail) -> tuple[bytes, bool]:
    """Cached kill-card bytes + a ``cache_hit`` flag (for the ``X-Card-Cache`` header/tests)."""
    key = f"kb:card:kill:{killmail.killmail_id}:{card_version()}"
    cached = cache.get(key)
    if cached is not None:
        return cached, True
    png = render_kill_card(killmail)
    cache.set(key, png, _CACHE_TTL)
    return png, False


def cv_card_png(character_id: int, ctx: dict) -> tuple[bytes, bool]:
    key = f"kb:card:cv:{character_id}:{card_version()}"
    cached = cache.get(key)
    if cached is not None:
        return cached, True
    png = render_cv_card(character_id, ctx)
    cache.set(key, png, _CACHE_TTL)
    return png, False


# --------------------------------------------------------------------------------------------- #
#  Throttle — a per-IP fixed window, mirroring the API's anon rate
# --------------------------------------------------------------------------------------------- #
def _card_rate() -> int:
    return int(getattr(settings, "KILLBOARD_CARD_RATE", 60))


def throttle_ok(ip: str) -> bool:
    """True if this IP is under the per-minute card-render budget. A true fixed window: the TTL
    is set once by ``add`` and not refreshed by ``incr``, so the window actually closes."""
    limit = _card_rate()
    if limit <= 0:
        return True
    key = f"kb:card:throttle:{ip or 'anon'}"
    cache.add(key, 0, 60)
    try:
        count = cache.incr(key)
    except ValueError:
        # Key expired between add and incr (window rolled over) — treat as the first in a new one.
        cache.add(key, 1, 60)
        count = 1
    return count <= limit
