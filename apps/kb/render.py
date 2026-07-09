"""A small, safe markdown renderer with allowlisted live embeds.

Security model: all page text is HTML-escaped *first*, so any raw HTML a user
writes is rendered inert — there is no path from page content to live markup.
Only a fixed set of inline transforms (headings, bold, italic, code, safe
links) and a fixed allowlist of embed tags can emit markup, and embeds are
resolved by trusted server code, not by page content.
"""
from __future__ import annotations

import html
import re

from django.utils.safestring import mark_safe

_EMBED_RE = re.compile(r"\{\{\s*([a-z][a-z_-]*)(?::([^}]*))?\s*\}\}")
# Links only to http(s) or site-internal paths; anything else stays literal text.
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+|/[^\s)]*)\)")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\s][^*]*)\*(?!\*)")
_CODE_RE = re.compile(r"`([^`]+)`")


def _inline(text: str) -> str:
    text = html.escape(text)  # inert first
    # Stash each generated <a> behind a sentinel token before running the other
    # inline transforms, so bold/italic/code never re-scan markup we just built
    # (e.g. "**" inside a URL would otherwise inject literal <strong> into the
    # href value). The token is restored last, keeping the escape-first invariant
    # truly intact rather than relying on the attribute quote happening to survive.
    links: dict[str, str] = {}

    def _stash_link(m: re.Match) -> str:
        token = f"\x00L{len(links)}\x00"
        links[token] = f'<a href="{m.group(2)}" class="text-gold hover:underline">{m.group(1)}</a>'
        return token

    text = _LINK_RE.sub(_stash_link, text)
    text = _BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = _ITALIC_RE.sub(r"<em>\1</em>", text)
    text = _CODE_RE.sub(r'<code class="rounded bg-space px-1">\1</code>', text)
    for token, frag in links.items():
        text = text.replace(token, frag)
    return text


def render_markdown(body: str, resolver=None) -> str:
    """Render a safe markdown subset to HTML, resolving embeds via ``resolver``.

    ``resolver(name, params)`` returns trusted HTML for an embed, or None to
    leave a graceful placeholder.
    """
    embeds: dict[str, str] = {}

    def _stash(match: re.Match) -> str:
        token = f"\x00E{len(embeds)}\x00"
        frag = None
        if resolver is not None:
            frag = resolver(match.group(1), (match.group(2) or "").strip())
        embeds[token] = frag if frag is not None else '<span class="text-faint">[unavailable]</span>'
        return token

    body = _EMBED_RE.sub(_stash, body or "")

    blocks: list[str] = []
    para: list[str] = []
    bullets: list[str] = []

    def flush_para():
        if para:
            blocks.append("<p>" + "<br>".join(_inline(p) for p in para) + "</p>")
            para.clear()

    def flush_ul():
        if bullets:
            items = "".join(f"<li>{_inline(b)}</li>" for b in bullets)
            blocks.append(f"<ul class='ml-5 list-disc space-y-1'>{items}</ul>")
            bullets.clear()

    def flush_blocks():
        flush_para()
        flush_ul()

    headings = {
        "### ": "<h3 class='mt-4 font-display font-semibold text-ink'>{}</h3>",
        "## ": "<h2 class='mt-5 font-display text-lg font-semibold text-ink'>{}</h2>",
        "# ": "<h1 class='mt-5 font-display text-xl font-bold text-ink'>{}</h1>",
    }
    for raw in body.split("\n"):
        line = raw.rstrip()
        heading = next((p for p in headings if line.startswith(p)), None)
        if not line.strip():
            flush_blocks()
        elif heading:
            flush_blocks()
            blocks.append(headings[heading].format(_inline(line[len(heading):])))
        elif line.startswith("- "):
            flush_para()
            bullets.append(line[2:])
        else:
            flush_ul()
            para.append(line)
    flush_blocks()

    out = "\n".join(blocks)
    for token, frag in embeds.items():
        out = out.replace(token, frag)
    # Safe by construction: every dynamic value is html-escaped in _inline before
    # any markup is added, and embed fragments are produced by trusted server code.
    return mark_safe(out)  # noqa: S308
