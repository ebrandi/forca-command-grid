"""Recruitment desk: candidates, evidence brief, notes, consent request.

All access is officer-gated and audit-logged (recruiting reads data about
people who are not yet members).
"""
from __future__ import annotations

import logging

import requests
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import formats, timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from core import rbac
from core.audit import audit_log, client_ip
from core.esi.client import ESIError
from core.rbac import perm_required

from . import oauth, services
from .models import Candidate, CandidateConsent

log = logging.getLogger("forca.recruitment")

# The read-only scopes a candidate consent requests (skills + their corp roles).
CONSENT_SCOPES = settings.RECRUITMENT_SSO_SCOPES

# Session key holding the per-consent PKCE verifier between begin and callback.
_SESS_VERIFIER = "recruit_pkce"  # actual key is f"{_SESS_VERIFIER}:{state}"


@login_required
@perm_required(rbac.PERM_RECRUITMENT_MANAGE)
def candidate_list(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "recruitment/list.html",
        {"candidates": Candidate.objects.all()},
    )


@login_required
@perm_required(rbac.PERM_RECRUITMENT_MANAGE)
def candidate_detail(request: HttpRequest, pk: int) -> HttpResponse:
    candidate = get_object_or_404(Candidate, pk=pk)
    audit_log(
        request.user, "recruitment.viewed",
        target_type="candidate", target_id=str(candidate.pk), ip=client_ip(request),
    )
    by_theme: dict[str, list] = {}
    for ev in candidate.evidence.all():
        by_theme.setdefault(ev.get_theme_display(), []).append(ev)
    return render(
        request,
        "recruitment/detail.html",
        {
            "candidate": candidate,
            "themes": sorted(by_theme.items()),
            "flags": candidate.evidence.filter(is_flag=True),
            "killboard": services.home_killboard_evidence(candidate.character_id),  # REC-KB-2 (3.8)
            "consents": candidate.consents.all(),
            "statuses": Candidate.Status.choices,
            "consent_scopes": CONSENT_SCOPES,
            "recruitment_enabled": settings.RECRUITMENT_SSO_ENABLED,
        },
    )


@login_required
@perm_required(rbac.PERM_RECRUITMENT_MANAGE)
@require_POST
def candidate_add(request: HttpRequest) -> HttpResponse:
    """Add a candidate by EVE pilot name (resolved to a character id via ESI).

    A bare numeric value is still accepted as a character id for power users.
    """
    import requests

    from core.esi.names import resolve_character_id

    raw = (request.POST.get("name") or "").strip()
    if not raw:
        messages.error(request, _("Enter a pilot name."))
        return redirect("recruitment:list")

    if raw.isdigit():
        character_id, resolved_name = int(raw), ""
    else:
        try:
            match = resolve_character_id(raw)
        except requests.RequestException:
            messages.error(request, _("Couldn't reach EVE to look up that name — try again."))
            return redirect("recruitment:list")
        if not match:
            messages.error(
                request,
                _("No EVE pilot found named “%(name)s”. Check the spelling.") % {"name": raw},
            )
            return redirect("recruitment:list")
        character_id, resolved_name = match

    candidate, created = Candidate.objects.get_or_create(
        character_id=character_id,
        defaults={"name": resolved_name or f"Character {character_id}", "added_by": request.user},
    )
    # Backfill the real name if the candidate was previously added by id.
    if not created and resolved_name and candidate.name != resolved_name:
        candidate.name = resolved_name
        candidate.save(update_fields=["name"])

    if created:
        from .tasks import refresh_candidate_evidence

        refresh_candidate_evidence.delay(candidate.pk)
        messages.success(
            request,
            _("Added %(name)s; gathering public evidence.") % {"name": candidate.name},
        )
    else:
        messages.info(request, _("%(name)s is already on the desk.") % {"name": candidate.name})
    return redirect("recruitment:detail", pk=candidate.pk)


@login_required
@perm_required(rbac.PERM_RECRUITMENT_MANAGE)
@require_POST
def candidate_refresh(request: HttpRequest, pk: int) -> HttpResponse:
    candidate = get_object_or_404(Candidate, pk=pk)
    from .tasks import refresh_candidate_evidence

    refresh_candidate_evidence.delay(candidate.pk)
    messages.success(request, _("Refreshing public evidence."))
    return redirect("recruitment:detail", pk=candidate.pk)


@login_required
@perm_required(rbac.PERM_RECRUITMENT_MANAGE)
@require_POST
def candidate_update(request: HttpRequest, pk: int) -> HttpResponse:
    candidate = get_object_or_404(Candidate, pk=pk)
    was_joined = candidate.status == Candidate.Status.JOINED
    status = request.POST.get("status")
    if status in Candidate.Status.values:
        candidate.status = status
    candidate.notes = request.POST.get("notes", candidate.notes)
    candidate.save(update_fields=["status", "notes", "updated_at"])
    # Purge ESI-derived data when a candidate is rejected (data minimisation).
    if candidate.status == Candidate.Status.REJECTED:
        candidate.evidence.filter(source="esi").delete()
        candidate.consents.filter(revoked_at__isnull=True).update(revoked_at=timezone.now())
    # REC-KB-3 (3.16): a fresh join routes into onboarding + mentorship, not a dead end.
    elif candidate.status == Candidate.Status.JOINED and not was_joined:
        result = services.handoff_joined_candidate(candidate)
        if result["handed_off"]:
            routed = [w for w, on in ((_("onboarding"), result["onboarding_started"]),
                                      (_("mentorship matching"), result["mentee_created"])) if on]
            if routed:
                messages.success(
                    request,
                    _("Joined — routed into %(targets)s.")
                    % {"targets": " and ".join(str(w) for w in routed)},
                )
            else:
                messages.info(request, _("Joined. (Onboarding and mentorship were already set "
                                         "up or are paused.)"))
        else:
            messages.info(
                request, _("Marked joined. Ask them to sign in with EVE SSO so we can start "
                           "their onboarding and suggest a mentor."))
    messages.success(request, _("Candidate updated."))
    return redirect("recruitment:detail", pk=candidate.pk)


@login_required
@perm_required(rbac.PERM_RECRUITMENT_MANAGE)
@require_POST
def request_consent(request: HttpRequest, pk: int) -> HttpResponse:
    candidate = get_object_or_404(Candidate, pk=pk)
    consent = services.request_consent(candidate, request.user, CONSENT_SCOPES)
    audit_log(
        request.user, "recruitment.consent_requested",
        target_type="candidate", target_id=str(candidate.pk),
        metadata={"scopes": CONSENT_SCOPES, "expires_at": consent.expires_at.isoformat()},
        ip=client_ip(request),
    )
    begin_url = request.build_absolute_uri(
        reverse("recruitment:oauth_begin", args=[consent.state])
    )
    if settings.RECRUITMENT_SSO_ENABLED:
        # strftime's %b is C-locale (LC_TIME) and never follows the active language;
        # date_format does ("juil." fr, "7月" ja). The rest of the stamp is numeric.
        expires_at = consent.expires_at
        expires = (
            f"{expires_at:%j} {formats.date_format(expires_at, 'M')} {expires_at:%H:%M}"
        )
        messages.success(
            request,
            _("Consent link ready — send it to %(name)s to authorise (expires "
              "%(expires)s UTC): %(url)s")
            % {
                "name": candidate.name,
                "expires": expires,
                "url": begin_url,
            },
        )
    else:
        messages.warning(
            request,
            _("Consent recorded, but the live ESI link is not configured on this "
              "deployment — recruitment stays public-evidence-only."),
        )
    return redirect("recruitment:detail", pk=candidate.pk)


# --- Candidate-facing live ESI link (the second EVE application) ------------
# These two views are PUBLIC: the candidate is a prospect, not a member. They are
# protected by (a) a one-time, time-boxed, unguessable consent state created by an
# officer, (b) a PKCE verifier bound to the candidate's own browser session, and
# (c) a hard check that the authorised character matches the candidate. The token
# is read once and never stored.


def _active_consent(state: str) -> CandidateConsent | None:
    consent = CandidateConsent.objects.filter(state=state).select_related("candidate").first()
    if consent and consent.is_active and consent.granted_at is None:
        return consent
    return None


def oauth_begin(request: HttpRequest, state: str) -> HttpResponse:
    """Candidate consent interstitial. GET shows what is being shared; POST starts
    the EVE authorize flow (generates PKCE, stashes the verifier in the session)."""
    consent = _active_consent(state)
    ctx = {"consent": consent, "enabled": settings.RECRUITMENT_SSO_ENABLED}
    if request.method == "POST":
        if not (consent and settings.RECRUITMENT_SSO_ENABLED):
            return render(request, "recruitment/oauth_begin.html", ctx, status=400)
        verifier, challenge = oauth.generate_pkce()
        request.session[f"{_SESS_VERIFIER}:{state}"] = verifier
        return redirect(
            oauth.build_authorize_url(state, challenge, list(consent.scopes or CONSENT_SCOPES))
        )
    return render(request, "recruitment/oauth_begin.html", ctx)


def oauth_callback(request: HttpRequest) -> HttpResponse:
    """The EVE redirect target. Validates state + PKCE, exchanges the code once,
    confirms the character matches the candidate, derives evidence, then discards
    the token. Never stores access/refresh tokens."""
    err = request.GET.get("error")
    if err:
        return render(
            request, "recruitment/oauth_done.html", {"ok": False, "reason": _("cancelled")}
        )

    state = request.GET.get("state") or ""
    code = request.GET.get("code")
    consent = _active_consent(state)
    verifier = request.session.pop(f"{_SESS_VERIFIER}:{state}", None)

    if not consent or not code or not verifier:
        return render(
            request, "recruitment/oauth_done.html",
            {"ok": False, "reason": _("This link is invalid, already used, or expired.")},
            status=400,
        )

    candidate = consent.candidate
    try:
        token = oauth.exchange_code(code, verifier)
        claims = oauth.validate_access_token(token.access_token)
        character_id = oauth.character_id_from_claims(claims)
    except (oauth.JWTValidationError, requests.RequestException, ValueError) as exc:
        log.warning("recruitment OAuth callback failed: %s", exc)
        audit_log(
            None, "recruitment.consent.failed",
            target_type="candidate", target_id=str(candidate.pk),
            metadata={"reason": str(exc)[:200]}, ip=client_ip(request),
        )
        return render(
            request, "recruitment/oauth_done.html",
            {"ok": False, "reason": _("We could not verify that authorization. Please try again.")},
            status=400,
        )

    # The authorised character MUST be the candidate we are vetting; otherwise a
    # different pilot's data would be attached to this candidate. Reject, store
    # nothing, and discard the token.
    if character_id != candidate.character_id:
        audit_log(
            None, "recruitment.consent.character_mismatch",
            target_type="candidate", target_id=str(candidate.pk),
            metadata={"expected": candidate.character_id, "got": character_id},
            ip=client_ip(request),
        )
        return render(
            request, "recruitment/oauth_done.html",
            {"ok": False, "candidate": candidate,
             "reason": _("You signed in as a different character. Please authorise as %(name)s.")
             % {"name": candidate.name}},
            status=400,
        )

    granted = oauth.scopes_from_claims(claims)
    try:
        skills, roles = services.read_candidate_esi(character_id, token.access_token, granted)
    except (requests.RequestException, ESIError) as exc:
        log.warning("recruitment ESI read failed: %s", exc)
        return render(
            request, "recruitment/oauth_done.html",
            {"ok": False, "candidate": candidate,
             "reason": _("We reached EVE but could not read your data — please try again shortly.")},
            status=502,
        )
    finally:
        # Belt-and-braces: the token leaves scope here regardless. Never persisted.
        token = None  # noqa: F841

    rows = services.build_esi_evidence(skills, roles)
    services.store_esi_evidence(candidate, rows)
    consent.granted_at = timezone.now()
    consent.save(update_fields=["granted_at"])
    if candidate.status == Candidate.Status.PROSPECT:
        candidate.status = Candidate.Status.LINKED
        candidate.save(update_fields=["status", "updated_at"])
    audit_log(
        None, "recruitment.consent.granted",
        target_type="candidate", target_id=str(candidate.pk),
        metadata={"scopes": granted, "evidence_rows": len(rows)}, ip=client_ip(request),
    )
    return render(request, "recruitment/oauth_done.html", {"ok": True, "candidate": candidate})
