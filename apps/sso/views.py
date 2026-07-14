"""EVE SSO views: login, callback, logout, scope management."""
from __future__ import annotations

import logging
import secrets

import requests
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.html import escape
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from core import pilots
from core.audit import audit_log, client_ip
from core.esi import oauth

from . import services
from .models import AuthToken, EveCharacter
from .services import CharacterAlreadyLinked, CharacterOwnershipChanged

log = logging.getLogger("forca.sso")

_SESS_STATE = "eve_sso_state"
_SESS_VERIFIER = "eve_sso_verifier"
_SESS_SCOPES = "eve_sso_scopes"
# Where to land after a successful login. Held in the SESSION, never round-tripped through
# EVE's OAuth params: a `next` the client could tamper with on the way back would be an open
# redirect. It is validated on the way in as well (see _safe_next).
_SESS_NEXT = "eve_sso_next"

# --- Linked Pilots (LP-5) ---------------------------------------------------------------
# EVE validates `redirect_uri` against the URL registered in the CCP developer application, so
# a second callback route would force every operator to reconfigure their CCP app on deploy.
# One callback therefore serves both intents, and the intent is bound to the OAuth state
# SERVER-SIDE: it is written here, next to the state we generated, and read back in the
# callback. The client never sees it and cannot flip it. Without this the callback has no way
# to tell "I am signing in" from "I am adding a pilot" — which is the shape of a callback-
# confusion bug.
_SESS_FLOW = "eve_sso_flow"
FLOW_LOGIN = "login"
FLOW_LINK = "link"
# The account that STARTED a link flow. Re-checked in the callback: without it, a link begun by
# one user and completed after the session became another user would attach the freshly
# authorised pilot to the wrong account.
_SESS_LINK_USER = "eve_sso_link_user"
# Which pilot the user said they were reauthorising, so we can tell them plainly when EVE hands
# back a different one (they picked the wrong character on CCP's screen — an easy mistake, and
# a confusing one if the app silently links a pilot they did not mean to add).
_SESS_LINK_EXPECT = "eve_sso_link_expect"


def _safe_next(request: HttpRequest, url: str | None) -> str | None:
    """``url`` if it is a same-origin path we may bounce the pilot to, else ``None``."""
    if not url:
        return None
    if not url_has_allowed_host_and_scheme(
        url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return None
    return url


def _begin_authorize(
    request: HttpRequest,
    *,
    flow: str,
    feature: str | None,
    next_url: str | None,
    expect_character_id: int | None = None,
) -> HttpResponse:
    """Mint state + PKCE, record the flow's intent server-side, and bounce to EVE.

    Every value that decides what the callback does — the state, the verifier, the scopes, the
    intent, the account that owns a link flow, where to land — is written to the SESSION here
    and read back there. Nothing rides on the OAuth round trip except EVE's own ``state`` and
    ``code``, because anything that goes through the identity provider comes back under the
    client's control.
    """
    state = oauth.generate_state()
    verifier, challenge = oauth.generate_pkce()
    scopes = list(settings.EVE_SSO_DEFAULT_SCOPES)
    for scope in settings.EVE_SSO_FEATURE_SCOPES.get(feature, []):
        if scope not in scopes:
            scopes.append(scope)

    request.session[_SESS_STATE] = state
    request.session[_SESS_VERIFIER] = verifier
    request.session[_SESS_SCOPES] = scopes
    request.session[_SESS_FLOW] = flow
    # Overwrite unconditionally, both keys: a stale target or a stale link-owner left behind by
    # an abandoned flow must never be picked up by the next one.
    if flow == FLOW_LINK:
        request.session[_SESS_LINK_USER] = request.user.pk
    else:
        request.session.pop(_SESS_LINK_USER, None)
    if expect_character_id:
        request.session[_SESS_LINK_EXPECT] = int(expect_character_id)
    else:
        request.session.pop(_SESS_LINK_EXPECT, None)
    if next_url:
        request.session[_SESS_NEXT] = next_url
    else:
        request.session.pop(_SESS_NEXT, None)

    return redirect(oauth.build_authorize_url(state, challenge, scopes))


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
    return _begin_authorize(
        request,
        flow=FLOW_LOGIN,
        feature=request.GET.get("feature"),
        # Remember where the pilot was headed when the login gate stopped them, so the
        # callback can finish the journey.
        next_url=_safe_next(request, request.GET.get("next")),
    )


@login_required
@require_POST
def link_view(request: HttpRequest) -> HttpResponse:
    """Begin an SSO authorisation that ADDS a pilot to the signed-in account (LP-5).

    POST-only and CSRF-protected. A GET would be triggerable from an ``<img>`` tag on any
    site, which is login-CSRF: an attacker could start a link flow in the victim's session and,
    with a little social engineering, have the victim authorise the ATTACKER's pilot into their
    account — handing the attacker a seat inside it.

    Reauthorising an existing pilot is the same flow (EVE's own screen is where the human picks
    the character; we cannot preselect it), so ``character_id`` is only a statement of intent —
    it lets the callback say "you authorised Ishtar Pilot, not the pilot you meant to fix".
    """
    if getattr(request, "is_impersonating", False):
        return HttpResponseBadRequest(_("Not available while viewing as another pilot."))

    expect = request.POST.get("character_id") or None
    if expect is not None:
        # Only ever a hint, and only ever one of your own pilots — never a lookup key.
        from core import pilots

        owned = pilots.owned_pilot(request.user, expect)
        expect = owned.character_id if owned else None

    return _begin_authorize(
        request,
        flow=FLOW_LINK,
        feature=request.POST.get("feature"),
        next_url=_safe_next(request, request.POST.get("next")),
        expect_character_id=expect,
    )


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
    # Pop the whole flow intent alongside the state, before any branch can run: a flow that
    # fails must leave nothing behind for the next one to pick up, and the state is single-use.
    flow = request.session.pop(_SESS_FLOW, FLOW_LOGIN)
    link_user_id = request.session.pop(_SESS_LINK_USER, None)
    expect_character_id = request.session.pop(_SESS_LINK_EXPECT, None)
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

    if flow == FLOW_LINK:
        return _complete_link(request, claims, token, link_user_id, expect_character_id)

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

    # The pilot you signed in AS becomes the pilot you are flying (LP-13).
    #
    # Choosing a character on CCP's own login screen is an explicit, deliberate statement of
    # which pilot you intend to be for this session — a far stronger signal than whatever an
    # older session happened to leave behind. It also makes the feature's promise literally
    # true in both directions: selecting a pilot behaves as though you had logged in as them,
    # and logging in as a pilot behaves as though you had selected them.
    #
    # This is a considered deviation from "the selected pilot survives logout and re-login": a
    # user who signs in as their main and is silently put in their alt's seat has been given an
    # identity they did not ask for, which in a game where the wrong seat means the wrong
    # corporation's assets is the more dangerous of the two surprises.
    character = account.characters.filter(character_id=character_id).first()
    if character is not None:
        pilots.select(request, character)

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


def _complete_link(
    request: HttpRequest,
    claims: dict,
    token,
    link_user_id,
    expect_character_id,
) -> HttpResponse:
    """Attach a freshly authorised pilot to the account that STARTED this link flow (LP-5).

    The session is not cycled: this is not an authentication event. The user was already signed
    in when they began, and they must still be the *same* signed-in user now — which is what
    ``link_user_id`` proves. Without that check, a link begun by one account and completed after
    the session became another (a logout and re-login mid-flow, or a session swapped underneath
    the user) would bind the newly authorised pilot to the wrong account.
    """
    user = request.user
    if (
        not user.is_authenticated
        or link_user_id is None
        or user.pk != link_user_id
    ):
        audit_log(
            user if user.is_authenticated else None,
            "pilot.link_rejected",
            metadata={"reason": "session_changed"},
            ip=client_ip(request),
        )
        return HttpResponseBadRequest(
            _("This pilot-linking request is no longer valid. Please start again.")
        )

    character_id = oauth.character_id_from_claims(claims)
    existing = (
        EveCharacter.objects.filter(character_id=character_id).select_related("user").first()
    )
    already_mine = bool(existing and existing.user_id == user.pk)

    try:
        # The same routine the login path uses: upsert the character, store the token, refresh
        # affiliation, reconcile roles. Re-running it for a pilot you already hold IS the
        # reauthorisation path — a fresh token replaces the dead one.
        character = services.complete_login(user, claims, token)
    except CharacterAlreadyLinked:
        audit_log(
            user,
            "pilot.link_rejected",
            target_type="character",
            target_id=str(character_id),
            metadata={"reason": "ownership_conflict"},
            ip=client_ip(request),
        )
        # Telling the authoriser that this pilot sits on another FORCA account discloses
        # nothing they could not already establish: they only reached this line by completing
        # a full EVE SSO authorisation for that very pilot, which is proof they control it.
        # What we never do is name the other account, or hint at who holds it.
        messages.error(
            request,
            _(
                "%(pilot)s is already linked to a different FORCA Command Grid account. "
                "An officer must detach it before it can be linked here."
            )
            % {"pilot": escape(claims.get("name", "") or character_id)},
        )
        return redirect("identity:linked_pilots")
    except CharacterOwnershipChanged:
        audit_log(
            user,
            "pilot.link_rejected",
            target_type="character",
            target_id=str(character_id),
            metadata={"reason": "owner_changed"},
            ip=client_ip(request),
        )
        messages.error(
            request,
            _(
                "This pilot's EVE ownership has changed since it was linked. For security an "
                "officer must detach it before it can be used again."
            ),
        )
        return redirect("identity:linked_pilots")

    if already_mine:
        audit_log(
            user, "pilot.reauthorised", target_type="character", target_id=str(character_id),
            metadata={"scopes": oauth.scopes_from_claims(claims)},
            ip=client_ip(request),
        )
        messages.success(
            request,
            _("%(pilot)s has been reauthorised.") % {"pilot": character.name},
        )
    else:
        audit_log(
            user, "pilot.linked", target_type="character", target_id=str(character_id),
            ip=client_ip(request),
        )
        messages.success(
            request,
            _("%(pilot)s is now linked to your account.") % {"pilot": character.name},
        )

    if expect_character_id and int(expect_character_id) != int(character_id):
        # They meant to fix one pilot and authorised another on CCP's login screen. Say so
        # plainly — silently linking a pilot they did not mean to add is exactly the kind of
        # surprise that erodes trust in an identity feature.
        messages.warning(
            request,
            _(
                "You authorised %(pilot)s, which is not the pilot you selected. That pilot is "
                "now linked; the pilot you meant to reauthorise still needs attention."
            )
            % {"pilot": character.name},
        )

    return redirect("identity:linked_pilots")


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
