"""Linked Pilots: the management page and the switch / main / unlink controls (LP-6, LP-7).

Every route here is ``@login_required`` and every mutation is ``@require_POST``. A switch that
answered GET would be triggerable from an ``<img src>`` on any page on the internet — silently
changing which pilot the victim is acting as, which is a CSRF with real operational teeth in a
game where the wrong seat means the wrong corporation's assets.

No route ever takes a pilot id as a lookup key. Ids arrive from the client, so they are only
ever used to *filter the caller's own pilots* (``core.pilots.owned_pilot``): a pilot the caller
does not hold simply does not resolve, and there is no code path where a row is fetched first
and checked afterwards — the shape IDOR bugs take.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import Resolver404, resolve
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.sso import linking
from core import pilots, rbac
from core.audit import audit_log, client_ip


@login_required
def linked_pilots(request: HttpRequest) -> HttpResponse:
    """The Linked Pilots page: every pilot on this account, its health, and what you can do."""
    cards = linking.pilot_cards(request.user)
    return render(
        request,
        "identity/linked_pilots.html",
        {
            "cards": cards,
            "pilot_count": len(cards),
            # The last remaining pilot is the account's only credential, so the unlink control
            # is disabled rather than merely failing on submit (the server refuses it too —
            # see linking.unlink — this is only so the UI does not offer a dead end).
            "can_unlink": len(cards) > 1,
        },
    )


@login_required
@require_POST
def switch_pilot(request: HttpRequest) -> HttpResponse:
    """Change the active pilot.

    Server-side ownership is the whole game here. The target is resolved *through the signed-in
    user's own pilots*, so a forged character_id resolves to nothing and is refused — and the
    refusal is audited, because a switch attempt at a pilot you do not hold is not a typo.
    """
    target = pilots.owned_pilot(request.user, request.POST.get("character_id"))
    if target is None:
        audit_log(
            request.user,
            "pilot.switch_denied",
            target_type="character",
            target_id=str(request.POST.get("character_id", ""))[:32],
            ip=client_ip(request),
        )
        messages.error(request, _("That pilot is not linked to your account."))
        return redirect("identity:linked_pilots")

    previous = pilots.active_pilot(request.user)
    if previous is not None and previous.character_id == target.character_id:
        # Already flying them. Not an error, and not worth an audit row — just go back.
        return redirect(_destination(request) or "identity:dashboard")

    pilots.select(request, target)
    audit_log(
        request.user,
        "pilot.switched",
        target_type="character",
        target_id=str(target.character_id),
        metadata={"from": previous.character_id if previous else None, "to": target.character_id},
        ip=client_ip(request),
    )
    messages.success(
        request,
        # The active pilot IS the identity the user is now acting under, so say it plainly —
        # a silent switch is how someone ends up moving the wrong corporation's assets.
        _("You are now flying as %(pilot)s.") % {"pilot": target.name},
    )

    # Warm the pilot we are about to render. Every cache warmer in the app filters is_main, so
    # an alt's readiness and closest-doctrine caches are cold on arrival and compute_pilot is a
    # multi-second recompute — the page after a switch is precisely where that stall lands.
    # Best-effort: a broker hiccup must never fail a switch.
    try:
        from apps.sso.tasks import warm_pilot_caches

        warm_pilot_caches.delay(target.character_id)
    except Exception:  # noqa: BLE001 - the switch already succeeded; warming is a bonus
        logging.getLogger("forca.pilots").warning(
            "could not enqueue warm_pilot_caches for %s", target.character_id
        )

    destination = _destination(request)
    if destination is None:
        return redirect("identity:dashboard")
    return redirect(destination)


@login_required
@require_POST
def set_main(request: HttpRequest) -> HttpResponse:
    """Set the account's main pilot (the one a fresh session starts on).

    "Main", not "primary": ``primary`` is a PROTECTED EVE term (the FC's called kill target)
    that core/i18n/data/protected-terms.yml requires every locale to keep in English, so a
    "Primary Pilot" label would be forced to render as the English word "Primary" in all eight
    non-English locales. "Main" is what EVE players actually say — main and alt — and it is
    already the model's own word (``EveCharacter.is_main``). See LP-12.
    """
    target = pilots.owned_pilot(request.user, request.POST.get("character_id"))
    if target is None:
        messages.error(request, _("That pilot is not linked to your account."))
        return redirect("identity:linked_pilots")

    linking.promote_main(request.user, target)
    audit_log(
        request.user,
        "pilot.main_changed",
        target_type="character",
        target_id=str(target.character_id),
        ip=client_ip(request),
    )
    messages.success(
        request, _("%(pilot)s is now your main pilot.") % {"pilot": target.name}
    )
    return redirect("identity:linked_pilots")


@login_required
@require_POST
def unlink_pilot(request: HttpRequest) -> HttpResponse:
    """Release a pilot: destroy its ESI authorisation and detach it from this account."""
    target = pilots.owned_pilot(request.user, request.POST.get("character_id"))
    if target is None:
        messages.error(request, _("That pilot is not linked to your account."))
        return redirect("identity:linked_pilots")

    name = target.name
    was_active = (
        (active := pilots.active_pilot(request.user)) is not None
        and active.character_id == target.character_id
    )

    try:
        linking.unlink(request.user, target)
    except linking.LastPilotError:
        messages.error(
            request,
            _(
                "%(pilot)s is the only pilot linked to your account, and an EVE pilot is how "
                "you sign in. Link another pilot first, or delete your account from the "
                "privacy page."
            )
            % {"pilot": name},
        )
        return redirect("identity:linked_pilots")

    audit_log(
        request.user,
        "pilot.unlinked",
        target_type="character",
        target_id=str(target.character_id),
        ip=client_ip(request),
    )

    if was_active:
        # Do not leave the session pointing at a pilot the user no longer holds: resolve a new
        # one NOW rather than let the next request fall back, so the page they land on is
        # already rendered as the pilot they are actually flying.
        replacement = pilots.linked_pilots(request.user)[0]
        pilots.select(request, replacement)
        messages.success(
            request,
            _("%(pilot)s has been unlinked. You are now flying as %(replacement)s.")
            % {"pilot": name, "replacement": replacement.name},
        )
    else:
        messages.success(
            request, _("%(pilot)s has been unlinked from your account.") % {"pilot": name}
        )
    return redirect("identity:linked_pilots")


def _destination(request: HttpRequest) -> str | None:
    """Where to land after a switch: the page they were on, if it is still theirs to see.

    "Still theirs" is answered by asking the app's own gates, not by a hand-kept list of which
    URLs are officer-only — such a list drifts the first time someone adds a view. We re-run,
    against the *new* pilot's authority, exactly the three checks that would have run on a real
    request to that URL: the membership gate, the feature/audience gate, and the view's own rbac
    decorator. If any refuses, the pilot's dashboard is the honest place to land, and we say why.
    """
    from core.features import (
        AUDIENCE_FEATURES,
        feature_enabled,
        feature_for_view,
        feature_visible_to,
    )
    from core.middleware import _path_allowed

    raw = request.POST.get("next")
    if not raw or not url_has_allowed_host_and_scheme(
        raw, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return None

    path = urlparse(raw).path
    try:
        match = resolve(path)
    except Resolver404:
        return None

    user = request.user
    current = pilots.active_pilot(user)
    if current is None:
        return None
    unavailable = _(
        "That page is not available to %(pilot)s, so you are on their dashboard instead."
    ) % {"pilot": current.name}

    # 1. The membership gate: a pilot outside the corporation is confined to the recruitment
    #    surface, and the authority ceiling (LP-4) means switching to an out-of-corp alt is
    #    exactly how a member becomes a non-member.
    if (
        not user.is_superuser
        and not rbac.has_role(user, rbac.ROLE_MEMBER)
        and not _path_allowed(path)
    ):
        messages.info(request, unavailable)
        return None

    # 2. The feature gate, including per-user audience features.
    feature = feature_for_view(match.namespace, match.url_name)
    if feature is not None:
        visible = (
            feature_visible_to(feature, user)
            if feature in AUDIENCE_FEATURES
            else feature_enabled(feature)
        )
        if not visible:
            messages.info(request, unavailable)
            return None

    # 3. The view's own role/permission guard (core.rbac.view_admits).
    if not rbac.view_admits(match.func, user):
        messages.info(request, unavailable)
        return None

    return raw
