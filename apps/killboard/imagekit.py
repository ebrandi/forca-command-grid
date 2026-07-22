"""Shared Pillow kernel for the killboard's server-rendered images.

The small, dependency-light primitives the kill-card / CV-card renderer (``killcard.py``) and
the Combat Signatures renderer (``signature_render.py``) both need, plus the single compact-ISK
formatter the ``eve.isk`` template filter delegates to — one implementation, no drift.

Two things live here that ``killcard.py`` predates and cannot express:

* **A CJK-capable font chain.** DejaVu Sans covers Latin/Greek/Cyrillic but has no CJK glyphs,
  and Pillow does no automatic font fallback. ``split_runs`` slices a string into runs by script
  (a cheap, cached per-codepoint decision) so each run is drawn with the face that covers it —
  DejaVu for Latin/Cyrillic, Noto Sans CJK for Chinese/Japanese/Korean. When the Noto package is
  absent (a stale self-hosted image) the chain degrades to DejaVu: it logs once and renders (CJK
  turns to tofu boxes rather than crashing).
* **CJK-aware text measurement / drawing / truncation** built on those runs, so a mixed-script
  pilot name measures and truncates correctly instead of being clipped mid-glyph.

Pillow is imported lazily inside the drawing helpers so ``compact_isk`` (pure Python) can be
imported by a template-tag module without pulling Pillow at app-load time.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("forca.killboard")


# --------------------------------------------------------------------------- #
#  Compact ISK — the single formatter (eve.isk filter + card renderers).
# --------------------------------------------------------------------------- #
def compact_isk(value) -> str:
    """Compact ISK formatting: ``1.2B``, ``340M``, ``12.5k``, ``540``.

    Byte-for-byte identical to the historical ``eve.isk`` template filter and ``killcard._isk``
    (both delegate here now). NOT locale-aware — the period decimal separator is intentional and
    consistent everywhere ISK is shown, in the UI and burned into an image alike.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "0"
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(v) >= div:
            return f"{v / div:.2f}{unit}"
    return f"{v:.0f}"


# --------------------------------------------------------------------------- #
#  Fonts — DejaVu primary, Noto Sans CJK fallback.
# --------------------------------------------------------------------------- #
_DEJAVU = {
    False: (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ),
    True: (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    ),
}
# fonts-noto-cjk ships OpenType Collections (.ttc); Pillow opens face 0. The bold collection
# carries the bold weight. Kept as a candidate list so a differently-packaged image still resolves.
_NOTO_CJK = {
    False: (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    ),
    True: (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    ),
}

# Cache font objects by (size, bold) so a render that draws dozens of strings loads each face once.
_font_cache: dict[tuple, object] = {}
_missing_cjk_warned = False


def _first_existing(paths) -> str | None:
    for path in paths:
        if os.path.exists(path):
            return path
    return None


def load_font(size: int, *, bold: bool = False):
    """The primary DejaVu face at ``size`` (bold optional), or Pillow's scalable default.

    Never raises on a missing package — a bare environment renders plainer text rather than
    failing. Cached per (size, bold).
    """
    from PIL import ImageFont

    key = ("dejavu", size, bold)
    hit = _font_cache.get(key)
    if hit is not None:
        return hit
    path = _first_existing(_DEJAVU[bold])
    font = None
    if path:
        try:
            font = ImageFont.truetype(path, size)
        except OSError:
            font = None
    if font is None:
        try:
            font = ImageFont.load_default(size)
        except TypeError:  # very old Pillow without a sized default
            font = ImageFont.load_default()
    _font_cache[key] = font
    return font


def load_cjk_font(size: int, *, bold: bool = False):
    """The Noto Sans CJK face at ``size``, or ``None`` when the package is not installed.

    ``None`` (not an exception) is the signal the caller degrades on: CJK runs then fall back to
    the primary face (tofu boxes), and the missing package is logged exactly once per process.
    """
    global _missing_cjk_warned
    from PIL import ImageFont

    key = ("cjk", size, bold)
    if key in _font_cache:
        return _font_cache[key]
    path = _first_existing(_NOTO_CJK[bold]) or _first_existing(_NOTO_CJK[False])
    font = None
    if path:
        try:
            font = ImageFont.truetype(path, size)
        except OSError:
            font = None
    if font is None and not _missing_cjk_warned:
        _missing_cjk_warned = True
        log.warning(
            "Noto Sans CJK font not found; CJK text in signature images will not render. "
            "Rebuild the image with fonts-noto-cjk installed."
        )
    _font_cache[key] = font
    return font


def has_cjk() -> bool:
    """Whether a CJK-capable face is available (drives the CJK-specific test assertions)."""
    return load_cjk_font(16) is not None


# --------------------------------------------------------------------------- #
#  Script classification (cheap, cached per codepoint).
# --------------------------------------------------------------------------- #
# Codepoint ranges DejaVu does NOT cover but Noto Sans CJK does. A per-codepoint decision cached
# in ``_cjk_cache`` keeps the hot loop a dict lookup after the first sighting of each character.
_CJK_RANGES = (
    (0x1100, 0x11FF),   # Hangul Jamo
    (0x2E80, 0x2EFF),   # CJK Radicals Supplement
    (0x3000, 0x303F),   # CJK Symbols and Punctuation
    (0x3040, 0x309F),   # Hiragana
    (0x30A0, 0x30FF),   # Katakana
    (0x3130, 0x318F),   # Hangul Compatibility Jamo
    (0x31F0, 0x31FF),   # Katakana Phonetic Extensions
    (0x3400, 0x4DBF),   # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),   # CJK Unified Ideographs
    (0xA960, 0xA97F),   # Hangul Jamo Extended-A
    (0xAC00, 0xD7AF),   # Hangul Syllables (+ Jamo Extended-B)
    (0xF900, 0xFAFF),   # CJK Compatibility Ideographs
    (0xFF00, 0xFFEF),   # Halfwidth and Fullwidth Forms
)
_cjk_cache: dict[str, bool] = {}


def _is_cjk(ch: str) -> bool:
    hit = _cjk_cache.get(ch)
    if hit is None:
        cp = ord(ch)
        hit = any(lo <= cp <= hi for lo, hi in _CJK_RANGES)
        _cjk_cache[ch] = hit
    return hit


def split_runs(text: str, primary, cjk):
    """Slice ``text`` into ``(segment, font)`` runs so each is drawn with a covering face.

    ``primary`` is the DejaVu face, ``cjk`` the Noto face (or ``None``). Consecutive characters of
    the same script coalesce into one run. When ``cjk`` is ``None`` every run uses ``primary`` —
    the graceful-degradation path (CJK renders as tofu but never crashes).
    """
    if not text:
        return []
    runs: list[tuple[str, object]] = []
    use_cjk = cjk is not None
    cur_chars: list[str] = []
    cur_is_cjk = False
    for ch in text:
        ch_cjk = use_cjk and _is_cjk(ch)
        if cur_chars and ch_cjk != cur_is_cjk:
            runs.append(("".join(cur_chars), cjk if cur_is_cjk else primary))
            cur_chars = []
        cur_chars.append(ch)
        cur_is_cjk = ch_cjk
    if cur_chars:
        runs.append(("".join(cur_chars), cjk if cur_is_cjk else primary))
    return runs


# --------------------------------------------------------------------------- #
#  CJK-aware text measurement / drawing / truncation.
# --------------------------------------------------------------------------- #
def text_width(draw, text: str, primary, cjk) -> float:
    """Total advance width of ``text`` across its per-script runs."""
    return sum(draw.textlength(seg, font=font) for seg, font in split_runs(text, primary, cjk))


def draw_text(draw, xy, text: str, *, primary, cjk, fill) -> float:
    """Draw ``text`` left-to-right at ``xy`` (top-left), each run in its covering face.

    Returns the x advance so callers can chain. Segments are top-aligned; a minor cross-script
    baseline difference is acceptable and never overflows a slot (callers pre-truncate to width).
    """
    x, y = xy
    for seg, font in split_runs(text, primary, cjk):
        draw.text((x, y), seg, font=font, fill=fill)
        x += draw.textlength(seg, font=font)
    return x - xy[0]


def truncate(draw, text: str, primary, cjk, max_width: float) -> str:
    """``text`` shortened with an ellipsis until it fits ``max_width`` (CJK-aware measurement)."""
    if text_width(draw, text, primary, cjk) <= max_width:
        return text
    ell = "…"
    while text and text_width(draw, text + ell, primary, cjk) > max_width:
        text = text[:-1]
    return (text + ell) if text else ell


# --------------------------------------------------------------------------- #
#  Small drawing helpers.
# --------------------------------------------------------------------------- #
def monogram(name: str) -> str:
    """Up to two initials for a portrait/avatar fallback (``"?"`` for an empty name)."""
    parts = [p for p in (name or "").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def hex_to_rgb(value: str, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    """Parse ``#rgb`` / ``#rrggbb`` to an RGB tuple, or ``fallback`` on anything malformed."""
    value = (value or "").strip().lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    if len(value) != 6:
        return fallback
    try:
        return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))
    except ValueError:
        return fallback
