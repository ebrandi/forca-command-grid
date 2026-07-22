"""Combat Signatures — the pure Pillow renderer (plan A2/A15/A16).

``render_signature_png(signature, payload)`` composites a banner from a validated config and the
localised :func:`signature_stats.build_signature_payload` dict; ``render_placeholder_png`` draws a
data-free "pending" card. Both are **pure**: no DB query, no network, no clock or randomness in a
draw path (the only time value is ``payload['generated_at']``, passed in). Given the same inputs
and the same background/font files on disk, the bytes are deterministic.

Design:

* **Supersampling.** Everything is drawn at 2× the preset size in logical coordinates (a small
  ``_Painter`` scales logical→device) then LANCZOS-downscaled — so text and drawn glyphs stay crisp
  at banner sizes.
* **Layouts × presets.** Three curated layouts (identity / tactical / minimal) each place the
  selected components into per-preset slots. :func:`plan_layout` — payload-independent, reusable by
  the WS-6 builder to warn — decides which components fit and which overflow (dropped in declared
  order); the renderer draws only the placed ones.
* **CJK & truncation.** All text goes through the :mod:`imagekit` font chain (DejaVu + Noto CJK
  per-glyph fallback) and is truncated to its slot width, so a long or mixed-script name is clipped
  with an ellipsis, never overflowed.
* **Emblems are drawn, not raster art.** Rank emblem (chevrons), trophy medals (tier discs) and the
  rank progress bar are Pillow primitives — no icon assets exist to embed (scout-ranks-medals).
"""
from __future__ import annotations

import io
import os

from .imagekit import (
    draw_text,
    load_cjk_font,
    load_font,
    text_width,
    truncate,
)
from .signature_assets import (
    CYAN,
    FAINT,
    GOLD,
    INK,
    KILL,
    LINE,
    LOSS,
    MUTED,
    PANEL,
    PANEL2,
    PRESETS,
    SPACE,
    sigbg_dir,
)

_SUPERSAMPLE = 2

# theme (config) → accent RGB. Distinct from the model's ``background`` / ``layout`` fields.
_THEME_ACCENT = {"gold": GOLD, "cyan": CYAN, "kill": KILL}

# Stat components that occupy a tile in the stat grid (value + label). trophy_count reads as a stat.
_STAT_COMPONENTS = (
    "kills", "losses", "solo_kills", "final_blows",
    "isk_destroyed", "isk_lost", "isk_efficiency", "kd_ratio",
    "trophy_count", "best_kill", "last_kill", "favourite_ship", "top_ship_class",
)
_STAT_SET = frozenset(_STAT_COMPONENTS)

# Single-slot components and the layout region they occupy.
_GROUP = {
    "portrait": "header", "pilot_name": "header", "corp": "header", "alliance": "header",
    "rank_title": "rank", "rank_progress": "rank",
    "activity_period_label": "meta", "stats_timestamp": "meta",
}

# Which single-slot components each layout supports (an unsupported selection is dropped).
_SUPPORTS = {
    "identity": {"portrait", "pilot_name", "corp", "alliance", "rank_title", "rank_progress",
                 "activity_period_label", "stats_timestamp"},
    "tactical": {"portrait", "pilot_name", "corp", "rank_title", "rank_progress",
                 "activity_period_label", "stats_timestamp"},
    "minimal": {"pilot_name", "corp", "activity_period_label", "stats_timestamp"},
}

# Stat-tile and trophy-medal capacity per (layout, preset) — sized so nothing overflows the box.
_CAP = {
    ("identity", "compact"): {"stats": 3, "trophies": 2},
    ("identity", "standard"): {"stats": 4, "trophies": 3},
    ("identity", "wide"): {"stats": 5, "trophies": 4},
    ("identity", "card"): {"stats": 6, "trophies": 4},
    ("tactical", "compact"): {"stats": 2, "trophies": 2},
    ("tactical", "standard"): {"stats": 3, "trophies": 3},
    ("tactical", "wide"): {"stats": 4, "trophies": 3},
    ("tactical", "card"): {"stats": 4, "trophies": 4},
    ("minimal", "compact"): {"stats": 3, "trophies": 0},
    ("minimal", "standard"): {"stats": 4, "trophies": 0},
    ("minimal", "wide"): {"stats": 5, "trophies": 0},
    ("minimal", "card"): {"stats": 4, "trophies": 0},
}

# Tier → medal / emblem colours (bronze / silver / gold).
_TIER_COLOR = {
    "bronze": (205, 127, 50),
    "silver": (196, 201, 214),
    "gold": (227, 179, 65),
}
_ACCENT_BY_STAT = {
    "kills": KILL, "isk_destroyed": KILL, "solo_kills": KILL,
    "losses": LOSS, "isk_lost": LOSS,
}


# --------------------------------------------------------------------------- #
#  Layout planning (payload-independent — reusable by the WS-6 builder UI).
# --------------------------------------------------------------------------- #
def plan_layout(layout: str, size_preset: str, components) -> dict:
    """Assign ``components`` to this layout+preset's slots; report what fits and what overflows.

    Walks the components in their declared order, filling each region up to its capacity; anything
    a region can't hold (or a layout doesn't support) is dropped. Pure and payload-free so the
    builder can preview "these 2 components won't fit" before a render exists.

    Returns ``{"placed", "dropped", "groups": {header, rank, stats, trophies, meta}, ...}``.
    """
    cap = _CAP.get((layout, size_preset), {"stats": 4, "trophies": 3})
    supports = _SUPPORTS.get(layout, set())
    groups: dict[str, list[str]] = {"header": [], "rank": [], "stats": [], "trophies": [],
                                    "meta": []}
    placed: list[str] = []
    dropped: list[str] = []
    stat_used = 0
    for comp in components:
        if comp in _STAT_SET:
            if stat_used < cap["stats"]:
                stat_used += 1
                groups["stats"].append(comp)
                placed.append(comp)
            else:
                dropped.append(comp)
        elif comp == "trophies_featured":
            if cap["trophies"] > 0:
                groups["trophies"].append(comp)
                placed.append(comp)
            else:
                dropped.append(comp)
        elif comp in supports:
            groups[_GROUP[comp]].append(comp)
            placed.append(comp)
        else:
            dropped.append(comp)
    return {
        "placed": placed,
        "dropped": dropped,
        "groups": groups,
        "stat_capacity": cap["stats"],
        "trophy_capacity": cap["trophies"],
    }


# --------------------------------------------------------------------------- #
#  Painter — logical (preset) coordinates scaled to the 2× device canvas.
# --------------------------------------------------------------------------- #
class _Painter:
    """Draws in logical preset coordinates; every geometry is multiplied by the supersample factor
    so the caller writes clean 1× numbers and gets crisp 2× output that downscales sharply."""

    def __init__(self, img, draw, scale: int, w: int, h: int, accent, payload: dict, trace=None):
        self.img = img
        self.draw = draw
        self.s = scale
        self.w = w            # logical width
        self.h = h            # logical height
        self.accent = accent
        self.payload = payload
        # Optional list the painter appends each labelled text decision to (requested vs drawn
        # string, chosen size). The WS-6 builder and the regression tests read it to know exactly
        # what a slot rendered without pixel-inspecting the image.
        self.trace = trace
        self._fonts: dict = {}

    # -- fonts (logical size → device face) --
    def font(self, size: int, *, bold: bool = False):
        key = (size, bold)
        hit = self._fonts.get(key)
        if hit is None:
            hit = load_font(int(size * self.s), bold=bold)
            self._fonts[key] = hit
        return hit

    def _cjk(self, size: int, *, bold: bool = False):
        return load_cjk_font(int(size * self.s), bold=bold)

    # -- text --
    def width(self, text: str, size: int, *, bold: bool = False) -> float:
        """Logical advance width of ``text`` at ``size``."""
        return text_width(self.draw, text, self.font(size, bold=bold),
                          self._cjk(size, bold=bold)) / self.s

    def _fit_size(self, text: str, size: int, min_size: int, bold: bool, max_width: float) -> int:
        """The largest size in ``[min_size, size]`` at which ``text`` fits ``max_width`` (device px),
        else ``min_size`` (the caller then ellipsizes). Shrink-before-truncate keeps a slightly
        long value — a hull name like ``Jackdaw`` — whole rather than clipping it."""
        for candidate in range(int(size), int(min_size) - 1, -1):
            if text_width(self.draw, text, self.font(candidate, bold=bold),
                          self._cjk(candidate, bold=bold)) <= max_width:
                return candidate
        return int(min_size)

    def text(self, x: float, y: float, text: str, *, size: int, bold: bool = False,
             fill, max_width: float | None = None, min_size: int | None = None,
             role: str | None = None) -> None:
        if max_width is not None and min_size is not None and min_size < size:
            size = self._fit_size(text, size, min_size, bold, max_width * self.s)
        prim = self.font(size, bold=bold)
        cjk = self._cjk(size, bold=bold)
        drawn = text
        if max_width is not None:
            drawn = truncate(self.draw, text, prim, cjk, max_width * self.s)
        draw_text(self.draw, (x * self.s, y * self.s), drawn, primary=prim, cjk=cjk, fill=fill)
        if self.trace is not None and role:
            self.trace.append({"role": role, "requested": text, "drawn": drawn, "size": size,
                               "fill": tuple(fill) if isinstance(fill, tuple | list) else fill,
                               "x": x, "max_width": max_width})

    def text_centered(self, cx: float, y: float, text: str, *, size: int, bold: bool = False,
                      fill, max_width: float | None = None) -> None:
        prim = self.font(size, bold=bold)
        cjk = self._cjk(size, bold=bold)
        if max_width is not None:
            text = truncate(self.draw, text, prim, cjk, max_width * self.s)
        tw = text_width(self.draw, text, prim, cjk) / self.s
        draw_text(self.draw, ((cx - tw / 2) * self.s, y * self.s), text,
                 primary=prim, cjk=cjk, fill=fill)

    # -- shapes (logical coords) --
    def rect(self, box, *, fill=None, outline=None, width: int = 1) -> None:
        self.draw.rectangle([v * self.s for v in box], fill=fill, outline=outline,
                            width=max(1, int(width * self.s)))

    def rrect(self, box, radius: float, *, fill=None, outline=None, width: int = 1) -> None:
        self.draw.rounded_rectangle(
            [v * self.s for v in box], radius=radius * self.s,
            fill=fill, outline=outline, width=max(1, int(width * self.s)),
        )

    def line(self, points, *, fill, width: int = 1) -> None:
        self.draw.line([(x * self.s, y * self.s) for x, y in points], fill=fill,
                      width=max(1, int(width * self.s)))

    def ellipse(self, box, *, fill=None, outline=None, width: int = 1) -> None:
        self.draw.ellipse([v * self.s for v in box], fill=fill, outline=outline,
                         width=max(1, int(width * self.s)))

    def polygon(self, pts, *, fill=None, outline=None, width: int = 1) -> None:
        self.draw.polygon([(x * self.s, y * self.s) for x, y in pts], fill=fill, outline=outline,
                        width=max(1, int(width * self.s)))


# --------------------------------------------------------------------------- #
#  Asset / background loading (local files only — no network).
# --------------------------------------------------------------------------- #
def _load_background(key: str, size_preset: str, device_size):
    """The committed background PNG for ``key``+``preset`` scaled to the device canvas, or None."""
    if not key:
        return None
    from PIL import Image

    path = sigbg_dir() / key / f"{size_preset}.png"
    if not path.exists():
        return None
    try:
        bg = Image.open(path).convert("RGB")
    except (OSError, ValueError):
        return None
    if bg.size != device_size:
        bg = bg.resize(device_size, Image.LANCZOS)
    return bg


def _load_portrait(path, device_px: int):
    """A portrait/logo image from a LOCAL path, resized square to ``device_px``, or None.

    Never opens a URL — the payload carries a mirror file path (or None) resolved off-request.
    """
    if not path or not os.path.exists(path):
        return None
    from PIL import Image

    try:
        im = Image.open(path).convert("RGBA")
    except (OSError, ValueError):
        return None
    return im.resize((device_px, device_px), Image.LANCZOS)


# --------------------------------------------------------------------------- #
#  Stat-cell value extraction (reads the payload; never a DB/network touch).
# --------------------------------------------------------------------------- #
def _stat_cell(payload: dict, comp: str) -> tuple[str, str, tuple]:
    """``(label, value_text, colour)`` for a stat component, ``"—"`` when its data is absent."""
    labels = payload.get("labels", {})
    label = labels.get(comp, comp)
    color = _ACCENT_BY_STAT.get(comp, INK)
    dash = "—"
    if comp in ("kills", "losses", "solo_kills", "final_blows"):
        v = payload.get(comp)
        return label, (f"{v:,}" if v is not None else dash), color
    if comp in ("isk_destroyed", "isk_lost", "isk_efficiency", "kd_ratio"):
        d = payload.get(comp)
        return label, (d["text"] if d else dash), color
    if comp == "trophy_count":
        v = payload.get("trophy_count")
        return label, (f"{v:,}" if v is not None else dash), GOLD
    if comp == "best_kill":
        d = payload.get("best_kill")
        return label, (d["text"] if d else dash), GOLD
    if comp == "last_kill":
        d = payload.get("last_kill")
        return label, (d["ship_name"] if d else dash), INK
    if comp == "favourite_ship":
        d = payload.get("favourite_ship")
        return label, (d["ship_name"] if d else dash), INK
    if comp == "top_ship_class":
        v = payload.get("top_ship_class")
        return label, (v or dash), INK
    return label, dash, INK


# --------------------------------------------------------------------------- #
#  Drawn emblems.
# --------------------------------------------------------------------------- #
def _draw_avatar(P: _Painter, x: float, y: float, size: float) -> None:
    """A portrait (from the mirror path) or a monogram fallback in a rounded panel."""
    portrait = P.payload.get("portrait") or {}
    device = int(size * P.s)
    im = _load_portrait(portrait.get("path"), device)
    P.rrect((x, y, x + size, y + size), radius=size * 0.18, fill=PANEL,
            outline=P.accent + (150,) if len(P.accent) == 3 else P.accent, width=1)
    if im is not None:
        from PIL import Image, ImageDraw

        mask = Image.new("L", (device, device), 0)
        md = ImageDraw.Draw(mask)
        md.rounded_rectangle((0, 0, device - 1, device - 1), radius=int(size * 0.18 * P.s),
                             fill=255)
        P.img.paste(im.convert("RGB"), (int(x * P.s), int(y * P.s)), mask)
    else:
        mono = portrait.get("monogram") or "?"
        P.text_centered(x + size / 2, y + size * 0.24, mono, size=int(size * 0.44),
                        bold=True, fill=P.accent, max_width=size * 0.9)


def _draw_rank_emblem(P: _Painter, cx: float, cy: float, r: float, tier: int, max_tier: int) -> None:
    """A circular rank badge with a chevron stack whose height scales with the pilot's tier."""
    P.ellipse((cx - r, cy - r, cx + r, cy + r), fill=PANEL2, outline=P.accent, width=2)
    n = 1 + min(3, max(0, tier * 3 // max(1, max_tier)))   # 1..4 chevrons by relative standing
    step = r * 0.34
    top = cy - (n - 1) * step / 2 - r * 0.18
    half = r * 0.42
    for i in range(n):
        y = top + i * step
        P.line([(cx - half, y + half * 0.7), (cx, y), (cx + half, y + half * 0.7)],
               fill=P.accent, width=2)


def _draw_medal(P: _Painter, cx: float, cy: float, r: float, tier: str) -> None:
    """A drawn tier medal: a coloured disc, a ring and a small centre star point."""
    color = _TIER_COLOR.get(tier, _TIER_COLOR["bronze"])
    P.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color, outline=SPACE, width=1)
    P.ellipse((cx - r * 0.62, cy - r * 0.62, cx + r * 0.62, cy + r * 0.62), outline=SPACE, width=1)
    P.ellipse((cx - r * 0.2, cy - r * 0.2, cx + r * 0.2, cy + r * 0.2), fill=SPACE)


def _draw_progress(P: _Painter, x: float, y: float, w: float, h: float, pct: float) -> None:
    """A rounded progress track with an accent-filled portion (``pct`` 0..100)."""
    pct = max(0.0, min(100.0, float(pct)))
    P.rrect((x, y, x + w, y + h), radius=h / 2, fill=LINE)
    fill_w = max(h, w * pct / 100.0) if pct > 0 else 0
    if fill_w > 0:
        P.rrect((x, y, x + fill_w, y + h), radius=h / 2, fill=P.accent)


# --------------------------------------------------------------------------- #
#  Header / meta / stat-grid drawing (shared by the layouts).
# --------------------------------------------------------------------------- #
def _draw_header(P: _Painter, header: list[str], x: float, y: float, w: float, *,
                 with_portrait: bool, name_size: int) -> float:
    """Draw the identity header (portrait + name + corp/alliance) and return the y below it."""
    payload = P.payload
    cursor_x = x
    # A slimmer portrait fraction than the column's third leaves the name a workable slot; the name
    # then shrinks a step before ellipsizing so a normal 12-char handle renders whole.
    portrait_size = min(P.h - 2 * y, w * 0.30)
    if with_portrait and "portrait" in header:
        _draw_avatar(P, x, y, portrait_size)
        cursor_x = x + portrait_size + 10
    text_w = x + w - cursor_x
    line_y = y
    if "pilot_name" in header and payload.get("pilot_name"):
        P.text(cursor_x, line_y, payload["pilot_name"], size=name_size,
               min_size=max(13, int(name_size * 0.66)), bold=True, fill=INK, max_width=text_w,
               role="name")
        line_y += name_size + 4
    sub_parts = []
    corp = payload.get("corp")
    if "corp" in header and corp:
        sub_parts.append((corp.get("ticker") or corp.get("name") or "").strip())
    alliance = payload.get("alliance")
    if "alliance" in header and alliance:
        sub_parts.append((alliance.get("ticker") or alliance.get("name") or "").strip())
    sub = " · ".join(p for p in sub_parts if p)
    if sub:
        P.text(cursor_x, line_y, sub, size=max(10, name_size - 8), fill=MUTED, max_width=text_w,
               role="corp")
        line_y += name_size - 2
    return max(line_y, y + portrait_size if (with_portrait and "portrait" in header) else line_y)


def _draw_stat_grid(P: _Painter, stats: list[str], x: float, y: float, w: float, h: float,
                    cols: int) -> None:
    """A grid of stat tiles filling ``(x, y, w, h)`` in ``cols`` columns (rows derived)."""
    if not stats:
        return
    cols = max(1, min(cols, len(stats)))
    rows = (len(stats) + cols - 1) // cols
    gap = 6
    tile_w = (w - gap * (cols - 1)) / cols
    tile_h = (h - gap * (rows - 1)) / rows
    for i, comp in enumerate(stats):
        r, c = divmod(i, cols)
        tx = x + c * (tile_w + gap)
        ty = y + r * (tile_h + gap)
        label, value, color = _stat_cell(P.payload, comp)
        P.rrect((tx, ty, tx + tile_w, ty + tile_h), radius=6, fill=PANEL)
        pad = 7
        inner = tile_w - 2 * pad
        vsize = max(13, min(int(tile_h * 0.42), 26))
        lsize = max(8, min(int(tile_h * 0.24), 13))
        # Labels shrink before ellipsizing too — "ISK DESTROYED" must fit an EN standard tile
        # whole; only genuinely long translations may still ellipsize at the floor size.
        P.text(tx + pad, ty + pad - 1, label.upper(), size=lsize, bold=True, fill=FAINT,
               max_width=inner, min_size=max(7, int(lsize * 0.6)), role=f"tile_label:{comp}")
        # Shrink the value a few steps before ellipsizing so a hull name like "Jackdaw" stays whole.
        P.text(tx + pad, ty + tile_h - vsize - pad + 2, value, size=vsize, bold=True, fill=color,
               max_width=inner, min_size=max(11, int(vsize * 0.6)), role=f"tile_value:{comp}")


def _draw_trophy_strip(P: _Painter, x: float, y: float, capacity: int) -> float:
    """Draw up to ``capacity`` featured-trophy medals from left; return the x after the strip."""
    featured = P.payload.get("trophies_featured") or []
    r = 9
    cx = x + r
    for medal in featured[:capacity]:
        _draw_medal(P, cx, y + r, r, medal.get("tier", "bronze"))
        cx += r * 2 + 5
    return cx


def _draw_meta(P: _Painter, meta: list[str], x: float, y: float, w: float) -> None:
    """A muted footer line (period label / stats timestamp) over its own dark scrim.

    The pilot opted the timestamp in, so it must stay legible on any background — a translucent
    space-toned scrim behind the strip guarantees contrast regardless of the design under it, and
    the text uses the muted-ink tone (not the near-invisible faint tone) the app's card footers use.
    """
    parts = []
    if "activity_period_label" in meta and P.payload.get("activity_period_label"):
        parts.append(P.payload["activity_period_label"])
    if "stats_timestamp" in meta and P.payload.get("stats_timestamp"):
        upd = P.payload.get("labels", {}).get("stats_timestamp", "Updated")
        parts.append(f"{upd} {P.payload['stats_timestamp']}")
    if not parts:
        return
    text = " · ".join(parts)
    size = 11
    tw = min(w, P.width(text, size))
    # Scrim: opaque space tone behind exactly the footer band so contrast never depends on the art.
    P.draw.rectangle(
        [(x - 4) * P.s, (y - 3) * P.s, (x + tw + 4) * P.s, (y + size + 3) * P.s],
        fill=SPACE + (224,),
    )
    P.text(x, y, text, size=size, fill=MUTED, max_width=w, role="meta")


# --------------------------------------------------------------------------- #
#  Layouts.
# --------------------------------------------------------------------------- #
def _layout_identity(P: _Painter, plan: dict) -> None:
    pad = 12
    g = plan["groups"]
    left_w = P.w * 0.44
    below = _draw_header(P, g["header"], pad + 4, pad, left_w - pad, with_portrait=True,
                         name_size=max(16, int(P.h * 0.15)))
    # rank line under the header (title + optional progress bar)
    ry = below + 4
    if "rank_title" in g["rank"] and P.payload.get("rank_title"):
        P.text(pad + 4, ry, P.payload["rank_title"], size=13, bold=True, fill=P.accent,
               max_width=left_w - pad)
        ry += 18
    if "rank_progress" in g["rank"] and P.payload.get("rank_progress"):
        _draw_progress(P, pad + 4, ry, left_w - pad - 8, 7,
                       P.payload["rank_progress"].get("pct", 0))
    # stat grid on the right
    grid_x = left_w + 4
    grid_w = P.w - grid_x - pad
    grid_y = pad
    grid_h = P.h - 2 * pad - (16 if g["meta"] else 0) - (18 if g["trophies"] else 0)
    cols = 3 if P.w >= 600 else 2
    _draw_stat_grid(P, g["stats"], grid_x, grid_y, grid_w, grid_h, cols)
    y_after = grid_y + grid_h + 4
    if g["trophies"]:
        _draw_trophy_strip(P, grid_x, y_after, plan["trophy_capacity"])
        y_after += 18
    if g["meta"]:
        _draw_meta(P, g["meta"], grid_x, P.h - pad - 10, grid_w)


def _layout_tactical(P: _Painter, plan: dict) -> None:
    pad = 12
    g = plan["groups"]
    # rank emblem focus on the left
    emblem_r = min(P.h * 0.28, 32)
    ecx, ecy = pad + 4 + emblem_r, pad + emblem_r
    prog = P.payload.get("rank_progress") or {}
    tier = 0
    max_tier = 1
    if prog:
        tier = int(round(prog.get("pct", 0) / 100 * 4))
        max_tier = 4
    if "rank_title" in g["rank"] or "rank_progress" in g["rank"]:
        _draw_rank_emblem(P, ecx, ecy, emblem_r, tier, max_tier)
    text_x = ecx + emblem_r + 10
    text_w = P.w - text_x - pad
    ty = pad
    if "pilot_name" in g["header"] and P.payload.get("pilot_name"):
        nsize = max(15, int(P.h * 0.14))
        P.text(text_x, ty, P.payload["pilot_name"], size=nsize, min_size=max(13, int(nsize * 0.66)),
               bold=True, fill=INK, max_width=text_w, role="name")
        ty += nsize + 4
    if "rank_title" in g["rank"] and P.payload.get("rank_title"):
        P.text(text_x, ty, P.payload["rank_title"], size=13, bold=True, fill=P.accent,
               max_width=text_w)
        ty += 18
    if "rank_progress" in g["rank"] and prog:
        _draw_progress(P, text_x, ty, text_w, 7, prog.get("pct", 0))
        ty += 12
    # a compact stat row spanning the bottom
    strip_h = min(40, P.h * 0.32)
    strip_y = P.h - pad - strip_h - (14 if g["meta"] else 0)
    _draw_stat_grid(P, g["stats"], pad + 4, strip_y, P.w - 2 * pad - 8, strip_h, len(g["stats"]) or 1)
    if g["trophies"]:
        _draw_trophy_strip(P, text_x, ty, plan["trophy_capacity"])
    if g["meta"]:
        _draw_meta(P, g["meta"], pad + 4, P.h - pad - 10, P.w - 2 * pad)


def _layout_minimal(P: _Painter, plan: dict) -> None:
    pad = 12
    g = plan["groups"]
    # name on the left ~two-fifths (wider than a third so a normal handle keeps its size);
    # its width is measured from the text origin to the strip boundary minus a gutter, so the
    # name can never touch the first tile.
    strip_x = P.w * 0.42
    name_x = pad + 4
    name_w = strip_x - name_x - 10
    ny = P.h / 2 - int(P.h * 0.10)
    if "pilot_name" in g["header"] and P.payload.get("pilot_name"):
        nsize = max(15, int(P.h * 0.20))
        P.text(name_x, ny, P.payload["pilot_name"], size=nsize, min_size=max(13, int(nsize * 0.62)),
               bold=True, fill=INK, max_width=name_w, role="name")
    corp = P.payload.get("corp")
    if "corp" in g["header"] and corp:
        P.text(name_x, ny + int(P.h * 0.22), (corp.get("ticker") or corp.get("name") or ""),
               size=11, fill=MUTED, max_width=name_w)
    # a single horizontal stat strip
    strip_w = P.w - strip_x - pad
    strip_h = P.h - 2 * pad - (14 if g["meta"] else 0)
    _draw_stat_grid(P, g["stats"], strip_x, pad, strip_w, strip_h, len(g["stats"]) or 1)
    if g["meta"]:
        _draw_meta(P, g["meta"], strip_x, P.h - pad - 10, strip_w)


_LAYOUTS = {
    "identity": _layout_identity,
    "tactical": _layout_tactical,
    "minimal": _layout_minimal,
}


# --------------------------------------------------------------------------- #
#  Canvas + entry points.
# --------------------------------------------------------------------------- #
def _new_canvas(device_size, background_key: str, size_preset: str, accent):
    from PIL import Image, ImageDraw

    bg = _load_background(background_key, size_preset, device_size)
    if bg is not None:
        img = bg
    else:
        img = Image.new("RGB", device_size, SPACE)
    draw = ImageDraw.Draw(img, "RGBA")
    # left accent rail, consistent with the app's card treatment
    draw.rectangle((0, 0, max(2, int(3 * _SUPERSAMPLE)), device_size[1]), fill=accent)
    return img, draw


def _to_png(img) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_signature_png(signature, payload: dict, *, trace: list | None = None) -> bytes:
    """Render the banner PNG for ``signature`` from its ``payload`` (exact preset dimensions).

    Pure: reads only the config carried on ``payload`` (size_preset / layout / theme / components /
    background_key) plus the pre-computed, pre-localised stat data, and the background/font files on
    disk. Overflowing components are dropped per :func:`plan_layout`.

    Pass ``trace=[]`` to receive, appended into that list, one record per labelled text slot
    (``{"role", "requested", "drawn", "size", "fill"}``) — the builder UI and the tests use it to
    read the truncation decision without inspecting pixels. It does not change the rendered bytes.
    """
    from PIL import Image

    size_preset = payload["size_preset"]
    layout = payload.get("layout") or "identity"
    w, h = PRESETS[size_preset]
    scale = _SUPERSAMPLE
    device = (w * scale, h * scale)
    accent = _THEME_ACCENT.get(payload.get("theme", "gold"), GOLD)

    img, draw = _new_canvas(device, payload.get("background_key", ""), size_preset, accent)
    plan = plan_layout(layout, size_preset, payload.get("components", []))
    P = _Painter(img, draw, scale, w, h, accent, payload, trace=trace)
    _LAYOUTS.get(layout, _layout_identity)(P, plan)

    out = img.resize((w, h), Image.LANCZOS)
    return _to_png(out)


def render_placeholder_png(size_preset: str) -> bytes:
    """A polished, data-free "signature pending" card in the house palette (exact preset dims).

    Served by the public delivery view (WS-5) when a signature exists but its artifact has not been
    rendered yet. Carries NO pilot data — safe to serve to anyone.
    """
    from PIL import Image, ImageDraw

    w, h = PRESETS[size_preset]
    scale = _SUPERSAMPLE
    device = (w * scale, h * scale)
    img = Image.new("RGB", device, SPACE)
    draw = ImageDraw.Draw(img, "RGBA")
    draw.rectangle((0, 0, max(2, int(3 * scale)), device[1]), fill=GOLD)
    P = _Painter(img, draw, scale, w, h, GOLD, {})
    # a faint framing panel + a centred pending label with a small emblem
    P.rrect((10, 10, w - 10, h - 10), radius=10, outline=LINE, width=1)
    cx = w / 2
    P.ellipse((cx - 11, h / 2 - 22, cx + 11, h / 2), outline=GOLD, width=2)
    P.line([(cx, h / 2 - 17), (cx, h / 2 - 11), (cx + 5, h / 2 - 8)], fill=GOLD, width=2)
    P.text_centered(cx, h / 2 + 4, "SIGNATURE PENDING", size=13, bold=True, fill=MUTED,
                    max_width=w - 30)
    P.text_centered(cx, h / 2 + 22, "[FORCA] Command Grid", size=10, fill=FAINT, max_width=w - 30)

    out = img.resize((w, h), Image.LANCZOS)
    return _to_png(out)
