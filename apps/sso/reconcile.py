"""CCP-authoritative scope reconciliation (roadmap 4.7).

Our ``EveScopeGrant`` rows record what a character granted *at the time it linked*.
They drift from reality: a pilot who revokes FORCA at CCP's third-party-app page
kills the refresh token, but nothing flips the grant rows — so the ESI Scopes page
keeps showing features as "granted" when they are dead, and the corp's scope-coverage
picture is a fiction. Least-privilege honesty means the UI must reflect what CCP
*actually* still honours, not what we once recorded.

This module reconciles grants against the CCP-authoritative source of truth — the
``scp`` claim inside a CCP-minted access-token JWT:

* **Passive recompute (always, no ESI cost):** the honoured scope set is the union of
  scopes across the character's live (non-revoked) tokens. A whole-app revoke at CCP
  eventually revokes every token (the 3-strikes refresh guard), so its scopes leave the
  union and the grant rows deactivate. This alone fixes the most common honesty gap.

* **Active verification (bounded, opt-in per run):** for live tokens, re-read the CCP
  ``scp`` claim by validating a fresh access token and correct our stored ``scopes``
  copy if CCP reduced them on refresh. This is the genuine "verify against CCP" step;
  it is staleness-filtered and per-run capped so it never threatens the ESI budget.

Grants are only ever flipped ``active`` (never deleted) — mirroring ``disconnect_view``
— so ``granted_at`` history is preserved.
"""
from __future__ import annotations

import logging

from django.utils import timezone

from core.esi import oauth

from .models import AuthToken, EveCharacter, EveScopeGrant
from .token_service import NoValidToken, access_token_for

log = logging.getLogger("forca.sso")


def _verify_token_scopes(token: AuthToken) -> set[str] | None:
    """Read the CCP-authoritative ``scp`` for one token, correcting our stored copy.

    Returns the CCP scope set, or None if the token could not be verified (dead /
    refresh failed / JWT invalid) — in which case the caller falls back to the
    scopes we already have recorded for it.
    """
    try:
        access = access_token_for(token)
    except NoValidToken:
        return None
    try:
        claims = oauth.validate_access_token(access)
    except oauth.JWTValidationError as exc:
        log.warning("scope verify: JWT invalid for token %s: %s", token.pk, exc)
        return None
    scp = set(oauth.scopes_from_claims(claims))
    now = timezone.now()
    stored = set(token.scopes or [])
    fields = {"scopes_verified_at": now}
    if scp and scp != stored:
        # CCP reduced (or otherwise changed) the granted scopes since we recorded them.
        fields["scopes"] = sorted(scp)
        log.info("scope drift on token %s: %s -> %s", token.pk, sorted(stored), sorted(scp))
    # Update WITHOUT touching the ciphertext columns; access_token_for already
    # persisted any refresh.
    AuthToken.objects.filter(pk=token.pk).update(**fields)
    return scp


def reconcile_character_scopes(
    character: EveCharacter, *, verify: bool = True
) -> dict:
    """Reconcile one character's ``EveScopeGrant`` rows against CCP.

    ``verify=True`` re-reads each live token's CCP ``scp`` claim (bounded ESI cost);
    ``verify=False`` recomputes purely from the scopes already recorded on live
    tokens (no ESI cost). Returns a diff summary.
    """
    live = list(character.tokens.filter(revoked_at__isnull=True))
    honored: set[str] = set()
    verified = 0
    for token in live:
        scp = None
        if verify:
            scp = _verify_token_scopes(token)
            if scp is not None:
                verified += 1
        # Fall back to the recorded copy when not verifying / verification failed:
        # those scopes were themselves CCP-authoritative at mint.
        honored |= scp if scp is not None else set(token.scopes or [])

    activated: list[str] = []
    deactivated: list[str] = []
    seen: set[str] = set()
    for grant in character.scope_grants.all():
        seen.add(grant.scope)
        should = grant.scope in honored
        if grant.active != should:
            grant.active = should
            grant.save(update_fields=["active"])
            (activated if should else deactivated).append(grant.scope)
    # A live token can carry a scope we never wrote a grant row for (e.g. a legacy
    # link); surface it so the honoured set and the grant rows agree.
    for scope in honored - seen:
        EveScopeGrant.objects.update_or_create(
            character=character, scope=scope, defaults={"active": True}
        )
        activated.append(scope)

    return {
        "character_id": character.character_id,
        "tokens": len(live),
        "verified": verified,
        "activated": sorted(activated),
        "deactivated": sorted(deactivated),
    }


def reconcile_user_scopes(user, *, verify: bool = True) -> dict:
    """Reconcile every character linked to ``user``. Aggregates the per-character diff."""
    activated: list[str] = []
    deactivated: list[str] = []
    verified = 0
    chars = 0
    for character in user.characters.all():
        try:
            res = reconcile_character_scopes(character, verify=verify)
        except Exception:  # noqa: BLE001 - one character must not abort the rest (a partial
            # apply with a misleading "couldn't reach CCP" is worse than skipping the bad one)
            log.exception("scope reconcile failed for %s", character.character_id)
            continue
        activated += res["activated"]
        deactivated += res["deactivated"]
        verified += res["verified"]
        chars += 1
    return {
        "characters": chars,
        "verified": verified,
        "activated": sorted(set(activated)),
        "deactivated": sorted(set(deactivated)),
    }


def reconcile_scopes_batch(
    *, limit: int = 100, staleness_hours: int = 24, verify: bool = True
) -> dict:
    """Corp-wide sweep: verify the tokens most overdue for a CCP re-check.

    Staleness-filtered + per-run capped to protect the shared ESI error budget at
    alt scale (the audit's single-point-of-failure): only characters whose live
    tokens were last verified more than ``staleness_hours`` ago (or never) are
    picked, oldest-first, up to ``limit`` characters per run. Everything else is a
    cheap passive recompute path via the self-service button / the natural token
    lifecycle — never a fleet-wide forced refresh.
    """
    from django.db.models import F, Min, Q

    cutoff = timezone.now() - timezone.timedelta(hours=staleness_hours)
    # Characters with at least one live token that is stale (or never verified),
    # ordered by the oldest verification timestamp (NULL = never = most overdue first).
    due = (
        EveCharacter.objects.filter(tokens__revoked_at__isnull=True)
        .annotate(oldest=Min("tokens__scopes_verified_at"))
        .filter(Q(oldest__isnull=True) | Q(oldest__lt=cutoff))
        .order_by(F("oldest").asc(nulls_first=True))
        .values_list("character_id", flat=True)
        .distinct()[:limit]
    )
    char_ids = list(due)
    activated = deactivated = verified = 0
    for character in EveCharacter.objects.filter(character_id__in=char_ids):
        try:
            res = reconcile_character_scopes(character, verify=verify)
        except Exception:  # noqa: BLE001 - one bad character must not abort the sweep
            log.exception("scope reconcile failed for %s", character.character_id)
            continue
        activated += len(res["activated"])
        deactivated += len(res["deactivated"])
        verified += res["verified"]
    return {
        "checked": len(char_ids),
        "verified": verified,
        "activated": activated,
        "deactivated": deactivated,
    }
