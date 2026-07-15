"""Pilot-facing comms-account linking (5.1).

A pilot opts in to link their Discord account via OAuth2 (consent-first, the
``/auth/eve/scopes/`` posture). We keep only the external user id + a display handle — the
short-lived OAuth token is discarded after the identity lookup (the bot, not the pilot's
token, manages roles). Linking enqueues a reconcile so their roles apply immediately.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.translation import gettext
from django.views.decorators.http import require_POST

from core.audit import audit_log, client_ip

from . import oauth
from .hooks import enqueue_user_reconcile
from .models import CommsAccount, Platform

_SESSION_PREFIX = "comms_discord_pkce:"


@login_required
def connect(request):
    """The pilot's comms-account hub: what's linked, connect/unlink."""
    accounts = {a.platform: a for a in CommsAccount.objects.filter(user=request.user)}
    ctx = {
        "discord": accounts.get(Platform.DISCORD),
        "discord_enabled": oauth.enabled(),
    }
    return render(request, "comms_access/connect.html", ctx)


@login_required
@require_POST
def discord_begin(request):
    if not oauth.enabled():
        messages.error(request, gettext("Discord linking is not configured."))
        return redirect("comms_access:connect")
    state = oauth.generate_state()
    verifier, challenge = oauth.generate_pkce()
    request.session[f"{_SESSION_PREFIX}{state}"] = verifier
    return redirect(oauth.build_authorize_url(state, challenge))


@login_required
def discord_callback(request):
    # Defence in depth: identity-linking must never run under a director's "view-as" — it
    # would rebind the director's Discord to the impersonated pilot (and hand it the pilot's
    # comms roles). The impersonation middleware already blocks this route.
    if getattr(request, "is_impersonating", False):
        messages.error(
            request, gettext("Account linking isn't available while viewing as another pilot.")
        )
        return redirect("comms_access:connect")
    state = request.GET.get("state", "")
    code = request.GET.get("code", "")
    session_key = f"{_SESSION_PREFIX}{state}"
    verifier = request.session.pop(session_key, None) if state else None

    if request.GET.get("error"):
        messages.error(request, gettext("Discord linking was cancelled."))
        return redirect("comms_access:connect")
    if not code or not verifier:
        # Missing/expired state ⇒ reject (CSRF protection for the OAuth redirect).
        messages.error(request, gettext("Discord linking failed — please try again."))
        return redirect("comms_access:connect")

    try:
        tokens = oauth.exchange_code(code, verifier)
        identity = oauth.fetch_identity(tokens["access_token"])
    except oauth.OAuthError as exc:
        messages.error(request, gettext("Discord linking failed: %(error)s") % {"error": exc})
        return redirect("comms_access:connect")

    external_id = str(identity["id"])
    # One Discord account may not be claimed by two different pilots.
    clash = (
        CommsAccount.objects.filter(platform=Platform.DISCORD, external_id=external_id)
        .exclude(user=request.user)
        .exists()
    )
    if clash:
        messages.error(request, gettext("That Discord account is already linked to another pilot."))
        return redirect("comms_access:connect")

    account, _ = CommsAccount.objects.get_or_create(user=request.user, platform=Platform.DISCORD)
    account.external_id = external_id
    account.external_handle = oauth.display_handle(identity)
    account.verified = True
    account.linked_at = timezone.now()
    account.last_error = ""
    # We do not retain the pilot's OAuth token — the bot manages roles, not this token.
    account.save()

    audit_log(request.user, "comms_access.link.discord", target_type="comms_account",
              target_id=account.pk, metadata={"handle": account.external_handle},
              ip=client_ip(request))
    enqueue_user_reconcile(request.user, source_ref="link")
    messages.success(
        request, gettext("Linked Discord account %(handle)s.") % {"handle": account.external_handle}
    )
    return redirect("comms_access:connect")


@login_required
@require_POST
def discord_unlink(request):
    account = CommsAccount.objects.filter(user=request.user, platform=Platform.DISCORD).first()
    if account:
        aid = account.pk
        # Unlinking stops management; we deliberately do NOT strip existing Discord roles
        # here (additive default, and once the link is gone we can no longer act on the
        # account). Leadership-driven revoke-on-departure runs while the link still exists,
        # via the reconcile hooks on corp-leave / token-disconnect.
        account.delete()
        audit_log(request.user, "comms_access.unlink.discord", target_type="comms_account",
                  target_id=aid, ip=client_ip(request))
        messages.success(request, gettext("Unlinked your Discord account."))
    return redirect("comms_access:connect")
