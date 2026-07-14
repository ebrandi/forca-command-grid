"""EVE SSO views: login, callback, logout, scope management."""
from __future__ import annotations

import logging
import secrets

import requests
from django.conf import settings
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.html import escape
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from core.audit import audit_log, client_ip
from core.esi import oauth

from . import services
from .models import AuthToken
from .services import CharacterAlreadyLinked, CharacterOwnershipChanged

log = logging.getLogger("forca.sso")

_SESS_STATE = "eve_sso_state"
_SESS_VERIFIER = "eve_sso_verifier"
_SESS_SCOPES = "eve_sso_scopes"
# Where to land after a successful login. Held in the SESSION, never round-tripped through
# EVE's OAuth params: a `next` the client could tamper with on the way back would be an open
# redirect. It is validated on the way in as well (see _safe_next).
_SESS_NEXT = "eve_sso_next"


def _safe_next(request: HttpRequest, url: str | None) -> str | None:
    """``url`` if it is a same-origin path we may bounce the pilot to, else ``None``."""
    if not url:
        return None
    if not url_has_allowed_host_and_scheme(
        url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return None
    return url


def login_view(request: HttpRequest) -> HttpResponse:
    """Begin the OAuth2 authorization-code + PKCE flow.

    An optional ``feature`` query param adds allowlisted feature scopes (e.g.
    corp-asset reading) on top of the defaults — used by the "grant access"
    buttons. Only server-defined scopes are ever requested; user input cannot
    inject an arbitrary scope.
    """
    # Defence in depth: an SSO login must never run under a director's "view-as" — the
    # callback would link the director's own character + tokens to the impersonated pilot's
    # account. The impersonation middleware already blocks this route (belt to that braces).
    if getattr(request, "is_impersonating", False):
        return HttpResponseBadRequest(_("Not available while viewing as another pilot."))
    state = oauth.generate_state()
    verifier, challenge = oauth.generate_pkce()
    scopes = list(settings.EVE_SSO_DEFAULT_SCOPES)
    feature = request.GET.get("feature")
    extra = settings.EVE_SSO_FEATURE_SCOPES.get(feature, [])
    for scope in extra:
        if scope not in scopes:
            scopes.append(scope)
    request.session[_SESS_STATE] = state
    request.session[_SESS_VERIFIER] = verifier
    request.session[_SESS_SCOPES] = scopes
    # Remember where the pilot was headed when the login gate stopped them, so the callback
    # can finish the journey. Overwrite unconditionally: a stale target from an abandoned
    # login must not hijack the next one.
    next_url = _safe_next(request, request.GET.get("next"))
    if next_url:
        request.session[_SESS_NEXT] = next_url
    else:
        request.session.pop(_SESS_NEXT, None)
    return redirect(oauth.build_authorize_url(state, challenge, scopes))


def callback_view(request: HttpRequest) -> HttpResponse:
    """Handle the SSO redirect: verify state, exchange code, validate JWT, link."""
    # Defence in depth (see login_view): never complete an SSO login while impersonating —
    # resolve_login_account would resolve the swapped pilot as the login account and link the
    # director's character/tokens (and possibly a Director grant) to the pilot's account.
    if getattr(request, "is_impersonating", False):
        return HttpResponseBadRequest(_("Not available while viewing as another pilot."))
    error = request.GET.get("error")
    if error:
        # Escape: this value is reflected into an HTML response and is fully
        # attacker-controlled (the callback is reachable before state validation).
        return HttpResponseBadRequest(_("SSO error: %(error)s") % {"error": escape(error)})

    state = request.GET.get("state")
    expected_state = request.session.pop(_SESS_STATE, None)
    verifier = request.session.pop(_SESS_VERIFIER, None)
    scopes = request.session.pop(_SESS_SCOPES, settings.EVE_SSO_DEFAULT_SCOPES)
    code = request.GET.get("code")

    if not state or not expected_state or not secrets.compare_digest(state, expected_state):
        return HttpResponseBadRequest(_("Invalid OAuth state."))
    if not code or not verifier:
        return HttpResponseBadRequest(_("Missing authorization code."))

    try:
        token = oauth.exchange_code(code, verifier)
        claims = oauth.validate_access_token(token.access_token)
        character_id = oauth.character_id_from_claims(claims)
    except (oauth.JWTValidationError, requests.RequestException, ValueError) as exc:
        log.warning("SSO callback failed: %s", exc)
        audit_log(
            request.user if request.user.is_authenticated else None,
            "sso.login.failed",
            metadata={"reason": str(exc)[:200]},
            ip=client_ip(request),
        )
        return HttpResponseBadRequest(_("Authentication failed."))

    # Rotate the session key to defeat session fixation on login.
    request.session.cycle_key()

    # Decide which platform account this character belongs to (idempotent on
    # re-login — see services.resolve_login_account).
    account = services.resolve_login_account(
        request.user, character_id, claims.get("name", "")
    )

    try:
        services.complete_login(account, claims, token)
    except CharacterAlreadyLinked:
        return HttpResponseBadRequest(
            _("This EVE character is already linked to another account.")
        )
    except CharacterOwnershipChanged:
        audit_log(
            request.user if request.user.is_authenticated else None,
            "sso.login.owner_changed",
            target_type="character",
            target_id=str(character_id),
            ip=client_ip(request),
        )
        return HttpResponseBadRequest(
            _(
                "This EVE character's ownership has changed since it was linked. "
                "For security it cannot sign in until an officer detaches it."
            )
        )

    if not request.user.is_authenticated:
        login(request, account)

    audit_log(
        account,
        "sso.login",
        target_type="character",
        target_id=str(character_id),
        metadata={"scopes": scopes},
        ip=client_ip(request),
    )
    # Land them where they were going, not on a generic dashboard. Re-validated even though
    # it came from our own session: the check is free and the failure mode is an open redirect.
    destination = _safe_next(request, request.session.pop(_SESS_NEXT, None))
    return redirect(destination or settings.LOGIN_REDIRECT_URL)


@require_POST
def logout_view(request: HttpRequest) -> HttpResponse:
    logout(request)
    return redirect(settings.LOGOUT_REDIRECT_URL)


@login_required
def scopes_view(request: HttpRequest) -> HttpResponse:
    """ESI scope-management page: per character, show every grantable feature with
    its current grant state plus a button to grant the ones still missing.

    The feature catalog (apps.sso.scopes) is the single place that decides what can
    be granted, so adding a feature scope automatically surfaces it here — closing
    the gap where most features had no grant prompt at all.
    """
    from .scopes import DIRECTOR, PILOT, feature_states

    characters = request.user.characters.prefetch_related("scope_grants", "tokens").all()
    cards = []
    for character in characters:
        granted = {g.scope for g in character.scope_grants.all() if g.active}
        # Most-recent CCP verification across the character's live tokens (4.7).
        verified_at = max(
            (t.scopes_verified_at for t in character.tokens.all()
             if t.revoked_at is None and t.scopes_verified_at is not None),
            default=None,
        )
        cards.append({
            "character": character,
            "granted_scopes": sorted(granted),
            "pilot_features": feature_states(granted, PILOT),
            "director_features": feature_states(granted, DIRECTOR),
            "verified_at": verified_at,
        })
    return render(request, "sso/scopes.html", {"cards": cards})


@login_required
@require_POST
def reconcile_view(request: HttpRequest) -> HttpResponse:
    """Self-service: re-verify this pilot's own scope grants against CCP (4.7).

    Least-privilege by construction — a pilot only ever reconciles their own linked
    characters. Fixes the honesty gap where a grant revoked at CCP still shows as
    active here.

    The instant part — a **passive** recompute (no ESI cost) — runs in-request so the
    page reflects the truth immediately for any scope whose token is already dead. The
    **active** CCP verification (which can block on a slow CCP token endpoint for up to
    20s per token) is offloaded to Celery and cooldown-gated, so a pilot can never tie
    up (or spam) a gunicorn worker with synchronous CCP calls — the same rule
    disconnect_view follows for its CCP revoke."""
    from django.contrib import messages
    from django.core.cache import cache

    from .reconcile import reconcile_user_scopes

    try:
        res = reconcile_user_scopes(request.user, verify=False)  # instant, no ESI
    except Exception:  # noqa: BLE001 - reconciliation is best-effort, never a 500
        log.exception("self-service scope reconcile failed for %s", request.user.pk)
        messages.error(request, _("Could not re-check your access just now — try again shortly."))
        return redirect("sso:scopes")

    # Offload the CCP-hitting deep verify; one run per user per 5 min.
    queued = False
    if cache.add(f"sso:reconcile:{request.user.pk}", "1", timeout=300):
        try:
            from .tasks import reconcile_user_scopes_active

            reconcile_user_scopes_active.delay(request.user.pk)
            queued = True
        except Exception:  # noqa: BLE001 - the passive recompute already applied; queue is a bonus
            log.warning("could not enqueue reconcile_user_scopes_active for %s", request.user.pk)

    audit_log(
        request.user, "sso.scopes.reconciled",
        metadata={"activated": res["activated"], "deactivated": res["deactivated"], "deep": queued},
        ip=client_ip(request),
    )
    deep = " " + _("A full CCP re-check is running in the background.") if queued else ""
    if res["deactivated"]:
        messages.warning(
            request,
            _("%(n)d scope(s) are no longer granted and have been marked inactive.%(deep)s")
            % {"n": len(res["deactivated"]), "deep": deep},
        )
    elif res["activated"]:
        messages.success(
            request,
            _("%(n)d scope(s) reconciled.%(deep)s")
            % {"n": len(res["activated"]), "deep": deep},
        )
    else:
        messages.success(request, _("Your access is in sync.%(deep)s") % {"deep": deep})
    return redirect("sso:scopes")


@login_required
@require_POST
def disconnect_view(request: HttpRequest, character_id: int) -> HttpResponse:
    """Disconnect a character: revoke its tokens (audit-logged)."""
    character = request.user.characters.filter(character_id=character_id).first()
    if not character:
        return HttpResponseBadRequest(_("Not your character."))
    # Local erasure is the hard guarantee, done synchronously. Capture the refresh-token
    # plaintexts FIRST (the ciphertext is about to be wiped), then hand the best-effort
    # CCP revocation to a Celery task so a CCP /revoke slowdown can't tie up this gunicorn
    # thread for up to N×20s.
    active = list(AuthToken.objects.filter(character=character, revoked_at__isnull=True))
    refresh_tokens = [rt for tok in active if (rt := tok.refresh_token)]
    AuthToken.objects.filter(character=character, revoked_at__isnull=True).update(
        revoked_at=timezone.now()
    )
    # Erase ciphertext on EVERY row for this character, including already-revoked ones —
    # a prior login-prune / daily-sweep row could still hold a decryptable refresh token,
    # which would otherwise survive an explicit disconnect and defeat the severance.
    AuthToken.objects.filter(character=character).update(_refresh_token="", _access_token="")
    if refresh_tokens:
        from .tasks import revoke_tokens_at_ccp

        revoke_tokens_at_ccp.delay(refresh_tokens)
    character.scope_grants.update(active=False)
    # Re-evaluate auto-managed roles: revoking the proving token must be able to
    # withdraw a Director grant that can no longer be substantiated.
    services.sync_roles_for_user(request.user)
    audit_log(
        request.user,
        "sso.disconnect",
        target_type="character",
        target_id=str(character_id),
        ip=client_ip(request),
    )
    return redirect("sso:scopes")
