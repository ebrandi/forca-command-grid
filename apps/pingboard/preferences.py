"""Per-pilot notification preferences: which categories reach which DM channels.

A pilot who links a DM channel receives every category on it by default. This module
lets them mute individual categories per channel (e.g. keep mining/industry in-app
only) without ever touching the in-app / EVE-mail legs or the EMERGENCY safety floor.
The mute list is stored sparsely — only suppressed ``(kind, category)`` pairs get a
row — so the common "receive everything" pilot costs nothing.
"""
from __future__ import annotations

from .models import MUTABLE_ALERT_CATEGORIES, AlertCategory, PilotChannelPreference

_MUTABLE_VALUES = {c.value for c in MUTABLE_ALERT_CATEGORIES}


def muted_pairs(user) -> set[tuple[str, str]]:
    """The set of ``(kind, category)`` this pilot has muted."""
    return set(
        PilotChannelPreference.objects.filter(user=user, muted=True).values_list(
            "kind", "category"
        )
    )


def set_preferences(user, kind: str, muted_categories) -> None:
    """Replace this pilot's mute list for one DM ``kind``.

    ``muted_categories`` is the categories to suppress on ``kind``; everything else
    on that kind is (re)enabled by deleting any stale mute rows. EMERGENCY/SYSTEM are
    never mutable, so they are silently dropped from the request.
    """
    valid_kinds = {k for k, _ in PilotChannelPreference._meta.get_field("kind").choices}
    if kind not in valid_kinds:
        return
    wanted = {c for c in muted_categories if c in _MUTABLE_VALUES}
    existing = dict(
        PilotChannelPreference.objects.filter(user=user, kind=kind).values_list(
            "category", "id"
        )
    )
    # Add newly-muted categories.
    to_add = [
        PilotChannelPreference(user=user, kind=kind, category=c, muted=True)
        for c in wanted
        if c not in existing
    ]
    if to_add:
        PilotChannelPreference.objects.bulk_create(to_add, ignore_conflicts=True)
    # Remove mutes the pilot cleared.
    stale_ids = [pk for cat, pk in existing.items() if cat not in wanted]
    if stale_ids:
        PilotChannelPreference.objects.filter(id__in=stale_ids).delete()


def preference_matrix(user, channels) -> list[dict]:
    """A render-ready matrix: one row per verified DM channel, each with a per-category
    ``muted`` flag, for the *My channels* preferences form.
    """
    from .dispatch import DM_HANDLE_KINDS

    muted = muted_pairs(user)
    # Only kinds the dispatcher actually delivers as per-pilot DMs (slack/telegram/
    # whatsapp). ``discord`` is linkable but broadcast-only, so a per-category discord
    # mute would be inert — don't offer it.
    verified_dm = [c for c in channels if c.verified and c.kind in DM_HANDLE_KINDS]
    cats = [
        {"value": c.value, "label": c.label}
        for c in MUTABLE_ALERT_CATEGORIES
        if c != AlertCategory.SYSTEM
    ]
    return [
        {
            "kind": ch.kind,
            "label": ch.get_kind_display(),
            "categories": [
                {**cat, "muted": (ch.kind, cat["value"]) in muted} for cat in cats
            ],
        }
        for ch in verified_dm
    ]
