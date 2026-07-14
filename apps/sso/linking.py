"""Linked pilots: authorising, releasing and describing the health of a pilot link (LP-5/LP-7).

A "link" is the statement that one authenticated human is authorised to act as one EVE pilot.
It is established only by that pilot completing a full EVE SSO authorisation — never by typing a
name or a character id — and it is severed by destroying the pilot's tokens.

CCP exposes no way to discover which characters share an EVE account, and this module does not
pretend otherwise: every pilot is authorised individually, and nothing here infers a
relationship between two pilots beyond "the same human proved control of both".
"""
from __future__ import annotations

from django.utils.translation import gettext_lazy as _

from .models import AuthToken

# The pilot is linked but cannot act: no live token at all (revoked at CCP, or refresh has
# failed past the revoke threshold). Re-authorisation is the only fix.
STATUS_HEALTHY = "healthy"
STATUS_REAUTH_REQUIRED = "reauth_required"
STATUS_SCOPES_MISSING = "scopes_missing"

# Codes are language-neutral and stable; the LABEL is chosen at render time. Storing or
# comparing a *translated* status would freeze whichever locale happened to write it, and any
# `{% if status == "Healthy" %}` would silently stop matching the moment the page was viewed in
# another language — the failure mode that killed the PI "Beginner recommended" badge.
STATUS_LABELS = {
    STATUS_HEALTHY: _("ESI authorisation healthy"),
    STATUS_REAUTH_REQUIRED: _("ESI authorisation required"),
    STATUS_SCOPES_MISSING: _("Missing ESI permissions"),
}

STATUS_HELP = {
    STATUS_HEALTHY: _("This pilot's ESI authorisation is current. Nothing to do."),
    STATUS_REAUTH_REQUIRED: _(
        "This pilot's EVE authorisation has expired or been revoked. You can still switch to "
        "them, but their ESI data cannot refresh until you reauthorise."
    ),
    STATUS_SCOPES_MISSING: _(
        "This pilot has not granted every permission FORCA Command Grid asks for. Some pages "
        "will be incomplete until you reauthorise."
    ),
}

# A refresh_fail_count at or above this is a permanent death (mirrors token_service's revoke
# policy and token_alerts._REVOKE_THRESHOLD — one definition of "dead", three readers).
_REVOKE_THRESHOLD = 3


def _live_tokens(character) -> list[AuthToken]:
    return [
        t
        for t in character.tokens.all()
        if t.revoked_at is None and t.refresh_fail_count < _REVOKE_THRESHOLD and t._refresh_token
    ]


def link_health(character) -> dict:
    """Describe one pilot's authorisation, as machine codes plus the data to render it.

    A broken token for one pilot must never block switching to another (the brief is explicit),
    so this *describes* a problem; it never gates. The selector shows a warning chip and the
    management page offers the reauthorise button — both remain fully usable.
    """
    from django.conf import settings

    required = set(getattr(settings, "EVE_SSO_DEFAULT_SCOPES", ()) or ())
    tokens = _live_tokens(character)
    granted: set[str] = set()
    for token in tokens:
        granted |= set(token.scopes or [])

    if not tokens:
        status = STATUS_REAUTH_REQUIRED
    elif required - granted:
        status = STATUS_SCOPES_MISSING
    else:
        status = STATUS_HEALTHY

    last_refresh = max(
        (t.last_refresh_ok_at for t in tokens if t.last_refresh_ok_at is not None),
        default=None,
    )
    return {
        "status": status,
        "label": STATUS_LABELS[status],
        "help": STATUS_HELP[status],
        "healthy": status == STATUS_HEALTHY,
        "missing_scopes": sorted(required - granted),
        "last_refresh_ok_at": last_refresh,
        # The last time we re-read this pilot's corp/alliance from ESI — the honest answer to
        # "last successful synchronisation" for the pilot's own record.
        "last_synced_at": character.affiliation_updated_at,
    }


def healthy_ids(characters) -> set[int]:
    """Which of these pilots hold a live token covering the required scopes — in ONE query.

    The selector renders in the sidebar of every authenticated page, so the "needs
    reauthorisation" warning dot cannot cost a query per pilot. :func:`link_health` is the
    detailed answer for the management page; this is the cheap yes/no for the chrome.
    """
    from django.conf import settings

    required = set(getattr(settings, "EVE_SSO_DEFAULT_SCOPES", ()) or ())
    ids = [c.character_id for c in characters]
    if not ids:
        return set()

    granted: dict[int, set[str]] = {cid: set() for cid in ids}
    rows = (
        AuthToken.objects.filter(
            character_id__in=ids,
            revoked_at__isnull=True,
            refresh_fail_count__lt=_REVOKE_THRESHOLD,
        )
        .exclude(_refresh_token="")
        .values_list("character_id", "scopes")
    )
    for character_id, scopes in rows:
        granted[character_id] |= set(scopes or [])
    return {cid for cid, have in granted.items() if required <= have}


def pilot_cards(user) -> list[dict]:
    """Every linked pilot with its health, ready for the management page."""
    from core import pilots

    active = pilots.active_pilot(user)
    active_id = active.character_id if active else None
    cards = []
    for character in pilots.ordered_for_selector(user, with_tokens=True):
        cards.append({
            "character": character,
            "is_active": character.character_id == active_id,
            "health": link_health(character),
        })
    return cards


class LastPilotError(Exception):
    """Refusing to unlink an account's only pilot.

    The account has no password — an EVE pilot IS the credential (``resolve_login_account``
    finds the account by the character that just logged in). Releasing the last one would
    strand the user outside their own account with no way back in, taking their history with
    it. Deleting the account is a separate, deliberate act on the privacy page.
    """


def unlink(user, character) -> None:
    """Sever a pilot link: destroy its tokens locally, then detach it from the account.

    Local erasure is the hard guarantee and is synchronous. The best-effort CCP revocation is
    handed to Celery, exactly as ``disconnect_view`` does, so a slow CCP /revoke cannot tie up
    a gunicorn worker for 20 seconds per token.

    Historical data is RETAINED. Killmails, contributions, readiness history and corp records
    are keyed on the character and are referenced by other pilots' records and by corp-wide
    reporting; deleting them would rewrite other people's history. What is destroyed is the
    *authorisation* — the tokens — and what is severed is the *link*. The pilot's own data is
    reachable again if the same human relinks the pilot (a fresh SSO authorisation), and the
    account-deletion path on the privacy page remains the way to erase it.
    """
    from django.db import transaction
    from django.utils import timezone

    from core import pilots

    from .models import EveCharacter
    from .tasks import revoke_tokens_at_ccp

    # The last-pilot guard has to be atomic, or two unlink requests racing on a two-pilot account
    # can both read "2 remaining", both pass, and both detach — leaving the account with zero
    # credentials and no way to sign in. Lock this account's pilots for the duration, count under
    # the lock, and re-confirm the target is still ours (a concurrent unlink may have taken it).
    with transaction.atomic():
        owned = list(
            EveCharacter.objects.select_for_update()
            .filter(user=user)
            .values_list("character_id", flat=True)
        )
        if character.character_id not in owned:
            return  # already unlinked by a concurrent request — nothing to do
        if len(owned) <= 1:
            raise LastPilotError

    # Capture the refresh-token plaintexts BEFORE wiping the ciphertext.
    active = list(AuthToken.objects.filter(character=character, revoked_at__isnull=True))
    refresh_tokens = [rt for tok in active if (rt := tok.refresh_token)]
    AuthToken.objects.filter(character=character, revoked_at__isnull=True).update(
        revoked_at=timezone.now()
    )
    # Erase the ciphertext on EVERY row for this character, including already-revoked ones: a
    # superseded row can still hold a decryptable refresh token, which would otherwise survive
    # an explicit unlink and defeat the severance.
    AuthToken.objects.filter(character=character).update(_refresh_token="", _access_token="")
    character.scope_grants.update(active=False)

    if refresh_tokens:
        revoke_tokens_at_ccp.delay(refresh_tokens)

    was_main = character.is_main
    character.user = None
    character.is_main = False
    # A detached pilot carries no corp standing into anyone's authority ceiling (LP-4).
    character.is_corp_director = False
    character.save(update_fields=["user", "is_main", "is_corp_director"])
    # The roster memo still holds the pilot we just released.
    pilots.invalidate(user)

    remaining = pilots.linked_pilots(user)
    if was_main and remaining:
        promote_main(user, remaining[0])

    # Re-evaluate the account's auto-managed roles: releasing the pilot that proved a Director
    # grant must be able to withdraw it.
    from . import services

    services.sync_roles_for_user(user)


def promote_main(user, character) -> None:
    """Make ``character`` the account's main pilot (at most one, always)."""
    from core import pilots

    user.characters.filter(is_main=True).exclude(
        character_id=character.character_id
    ).update(is_main=False)
    if not character.is_main:
        character.is_main = True
        character.save(update_fields=["is_main"])
    if user.main_character_id != character.character_id:
        user.main_character_id = character.character_id
        user.save(update_fields=["main_character_id"])
    # is_main is part of the selector's sort key.
    pilots.invalidate(user)
