"""Language-scoped cache keys.

Any cached payload that embeds *translated prose* must fold the active language
into its key, or a value rendered in one language would be served to a user in
another (docs/i18n/03-decisions.md D17). Language-neutral caches (ids, role
booleans, English-canonical SDE data before the display seam) do NOT use this.
"""
from __future__ import annotations

from django.utils import translation


def i18n_cache_key(base: str) -> str:
    """``"<base>:<active-language>"`` — e.g. ``briefing:42`` → ``briefing:42:de``."""
    return f"{base}:{translation.get_language() or 'en'}"
