"""Celery tasks for SSO/affiliation maintenance."""
from __future__ import annotations

import logging

from celery import shared_task

from . import services
from .models import EveCharacter

log = logging.getLogger("forca.sso")


@shared_task(name="sso.warm_pilot_after_login")
def warm_pilot_after_login(character_id: int) -> None:
    """Off-login-critical-path work fired by complete_login.

    Two jobs, each isolated so one failure never affects the other (or, in EAGER
    test mode, the login itself): (1) the in-game Director-role ESI check that was
    deferred from the login path, and (2) pre-warming the pilot's Command Center
    caches (readiness + closest-doctrines) so their first dashboard visit after
    login isn't a cold multi-second recompute.
    """
    character = (
        EveCharacter.objects.filter(character_id=character_id).select_related("user").first()
    )
    if character is None:
        return
    user = character.user

    if user is not None:
        try:
            services.sync_roles_for_user(user)  # full check, incl. the Director ESI lookup
        except Exception:  # noqa: BLE001 - a role-sync hiccup must not affect the warm below
            log.exception("warm_pilot_after_login: role sync failed for %s", character_id)

    try:
        main = character if character.is_main else (
            user.characters.filter(is_main=True).first() if user else None
        )
        if main is not None:
            from apps.readiness.pilot import compute_pilot
            from apps.skills.services import closest_doctrines

            compute_pilot(main, persist=True)   # warms readiness:pilot:*
            closest_doctrines(main)             # warms skills:closest:*
    except Exception:  # noqa: BLE001 - warming is best-effort; never fail the caller
        log.exception("warm_pilot_after_login: cache warm failed for %s", character_id)


@shared_task(name="sso.refresh_affiliations")
def refresh_affiliations() -> dict:
    """Batched, staleness-filtered affiliation sweep (4.8).

    Replaces the per-character GET + per-character role-sync loop with one
    ``POST /characters/affiliation/`` per 1000 stale characters and a once-per-user
    DB-only member reconcile — protecting the shared ESI error budget at alt scale.
    Leadership-tunable via ``AppSetting sso.affiliation_refresh``."""
    from apps.admin_audit.models import AppSetting

    cfg = AppSetting.get("sso.affiliation_refresh", {}) or {}
    # Clamp operator input: a 0/blank limit means "no cap" (None); a negative limit
    # would crash on qs[:negative]; a non-positive staleness would treat all chars stale.
    raw_limit = cfg.get("limit")
    limit = max(1, int(raw_limit)) if raw_limit else None
    return services.refresh_affiliations_batched(
        staleness_hours=max(1, int(cfg.get("staleness_hours", 4))),
        limit=limit,
    )


@shared_task(name="sso.reconcile_director_roles")
def reconcile_director_roles() -> dict:
    """Bounded periodic in-game Director-role reconcile (4.8).

    Decoupled from the affiliation sweep so the Director ESI check is staleness-filtered
    + per-run capped. Leadership-tunable via ``AppSetting sso.director_reconcile``."""
    from apps.admin_audit.models import AppSetting

    cfg = AppSetting.get("sso.director_reconcile", {}) or {}
    return services.reconcile_director_roles(
        staleness_hours=max(1, int(cfg.get("staleness_hours", 5))),
        limit=max(1, int(cfg.get("limit", 200))),
    )


@shared_task(name="sso.prune_superseded_tokens")
def prune_superseded_tokens() -> int:
    """Revoke tokens made redundant by another token with equal-or-wider scopes.

    Every SSO round-trip mints a new AuthToken and nothing removed the old
    ones, so active pilots accumulated dozens of identical rows (and health-
    page lines). store_token now prunes on login; this daily sweep clears the
    historical backlog and anything that slips through. A token is revoked
    when ANOTHER live token for the same character covers every scope it has
    — widest coverage wins, identical scope sets keep the newest. Tokens
    carrying a scope nothing else covers (e.g. the director grant) survive.
    """
    from django.db.models import Count
    from django.utils import timezone

    from .models import AuthToken

    now = timezone.now()
    revoked = 0
    dupe_chars = (
        AuthToken.objects.filter(revoked_at__isnull=True)
        .values("character_id")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
        .values_list("character_id", flat=True)
    )
    for character_id in dupe_chars:
        tokens = list(
            AuthToken.objects.filter(
                character_id=character_id, revoked_at__isnull=True
            ).order_by("-created_at")
        )
        # Widest coverage first; the stable sort keeps newest-first within a
        # size, so identical scope sets resolve to the newest token. A subset
        # can never precede its superset (it has fewer scopes), so one pass
        # leaves only pairwise-incomparable scope sets alive.
        tokens.sort(key=lambda t: len(t.scopes or []), reverse=True)
        kept_scope_sets: list[set] = []
        for token in tokens:
            scope_set = set(token.scopes or [])
            if any(scope_set <= kept for kept in kept_scope_sets):
                token.revoked_at = now
                token._refresh_token = ""  # erase ciphertext alongside revocation
                token._access_token = ""
                token.save(update_fields=["revoked_at", "_refresh_token", "_access_token"])
                revoked += 1
            else:
                kept_scope_sets.append(scope_set)
    return revoked


@shared_task(name="sso.revoke_tokens_at_ccp")
def revoke_tokens_at_ccp(refresh_tokens: list[str]) -> int:
    """Best-effort CCP revocation of refresh tokens, off the request path.

    Used by ``disconnect_view``: the local ciphertext erase + ``revoked_at`` stamp are
    done synchronously (the hard severance guarantee); this drops the grant at CCP too,
    without tying up a gunicorn thread for up to N×20s during a CCP slowdown.
    ``oauth.revoke_token`` never raises.
    """
    from core.esi import oauth

    n = 0
    for rt in refresh_tokens or []:
        oauth.revoke_token(rt)
        n += 1
    return n


@shared_task(name="sso.scan_ingestion_tokens")
def scan_ingestion_tokens() -> dict:
    """SSO-2 (2.1): nudge the owning director when a corp-ingestion token dies; one
    alert per death, re-armed on recovery, no-op when disabled."""
    from .token_alerts import scan_ingestion_tokens as _scan

    return _scan()


@shared_task(name="sso.reconcile_user_scopes_active")
def reconcile_user_scopes_active(user_id: int) -> dict:
    """4.7: the CCP-hitting active verification for one user, off the request thread.

    Fired by the self-service "Re-check with CCP" button so a slow CCP token endpoint
    can never tie up a gunicorn worker (mirrors disconnect_view's revoke offload)."""
    from django.contrib.auth import get_user_model

    from .reconcile import reconcile_user_scopes

    user = get_user_model().objects.filter(pk=user_id).first()
    if user is None:
        return {"status": "no_user"}
    return reconcile_user_scopes(user, verify=True)


@shared_task(name="sso.reconcile_scopes")
def reconcile_scopes() -> dict:
    """4.7: reconcile recorded scope grants against CCP-authoritative token claims.

    Staleness-filtered + per-run capped (leadership-tunable via AppSetting) so the
    verification never threatens the shared ESI error budget at alt scale."""
    from apps.admin_audit.models import AppSetting

    from .reconcile import reconcile_scopes_batch

    cfg = AppSetting.get("sso.scope_reconcile", {}) or {}
    return reconcile_scopes_batch(
        limit=int(cfg.get("limit", 100)),
        staleness_hours=int(cfg.get("staleness_hours", 24)),
        verify=bool(cfg.get("verify", True)),
    )
