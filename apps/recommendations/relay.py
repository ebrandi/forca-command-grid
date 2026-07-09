"""REC-1 (roadmap 2.10) — designated corp notification/mail relay character.

Both the notification and mail relays previously grabbed the *first* corp member
holding the scope — non-deterministic, ignored the corp, and could silently relay a
non-director's personal feed. Leadership now designates **one** relay character;
both relays use it (falling back to the first valid token only when none is
designated, or the designated one has lost/revoked the scope), so the corp's
defensive feed is authoritative and predictable, and least-privilege.

Stored in an ``AppSetting`` (no migration). ``relay_character`` is the shared helper
both relays call; ``eligible_relay_characters`` powers the console picker using a
cheap *local* scope check (no ESI refresh storm).
"""
from __future__ import annotations

from apps.admin_audit.models import AppSetting

_SETTING_KEY = "recommendations:relay_character_id"

NOTIF_SCOPE = "esi-characters.read_notifications.v1"
MAIL_SCOPE = "esi-mail.read_mail.v1"
RELAY_SCOPES = (NOTIF_SCOPE, MAIL_SCOPE)


def designated_relay_character_id() -> int | None:
    row = AppSetting.objects.filter(key=_SETTING_KEY).first()
    val = (row.value or {}).get("character_id") if row else None
    return int(val) if val else None


def set_designated_relay_character(character_id: int | None) -> None:
    if character_id:
        AppSetting.objects.update_or_create(
            key=_SETTING_KEY, defaults={"value": {"character_id": int(character_id)}}
        )
    else:
        AppSetting.objects.filter(key=_SETTING_KEY).delete()


def relay_character(scope: str):
    """The character whose token relays ``scope``: the designated one when it still holds a
    valid token for it, else the first corp member with a valid token (deterministic, by
    character_id) as a fallback. Returns ``None`` when no one holds the scope."""
    from apps.sso.models import EveCharacter
    from apps.sso.token_service import NoValidToken, get_valid_access_token

    designated_id = designated_relay_character_id()
    if designated_id:
        ch = EveCharacter.objects.filter(
            character_id=designated_id, is_corp_member=True
        ).first()
        if ch:
            try:
                if get_valid_access_token(ch, [scope]):
                    return ch
            except NoValidToken:
                pass  # designated character lost the scope → fall back below

    for ch in EveCharacter.objects.filter(is_corp_member=True).order_by("character_id"):
        try:
            if get_valid_access_token(ch, [scope]):
                return ch
        except NoValidToken:
            continue
    return None


def eligible_relay_characters(scopes=RELAY_SCOPES) -> list[dict]:
    """Corp members holding a *valid* token for at least one relay scope, for the console
    picker — each with the relay scopes it holds. One bucketed query, no ESI refresh: it
    honours ``AuthToken.is_valid`` (non-revoked + a real refresh-token ciphertext) so a
    ciphertext-erased token is never offered as designatable."""
    from apps.sso.models import AuthToken

    scopeset = set(scopes)
    held: dict[int, dict] = {}
    tokens = AuthToken.objects.filter(
        character__is_corp_member=True, revoked_at__isnull=True, refresh_fail_count__lt=3
    ).select_related("character")
    for t in tokens:
        if not t.is_valid:  # empty refresh ciphertext etc. — would fail at relay time
            continue
        matched = scopeset & set(t.scopes or [])
        if not matched:
            continue
        ch = t.character
        row = held.setdefault(ch.character_id, {
            "character_id": ch.character_id,
            "name": ch.name or str(ch.character_id),
            "has_notifications": False,
            "has_mail": False,
        })
        if NOTIF_SCOPE in matched:
            row["has_notifications"] = True
        if MAIL_SCOPE in matched:
            row["has_mail"] = True
    return sorted(held.values(), key=lambda r: (r["name"], r["character_id"]))
