"""Public pilot profiles — the owner's management page and the anonymous public page.

Owner routes are ``@login_required`` and every mutation is ``@require_POST``. No route takes a
profile id: the character id a form posts is resolved *through the caller's own pilots*
(``core.pilots.owned_pilot``), so a character the caller does not hold simply does not resolve —
there is no fetch-then-check path (the IDOR shape), matching apps.identity.pilot_views.

The public route answers ONE constant-shape 404 for every way a token can fail (malformed,
unknown, disabled, expired, unlinked, owner no longer a member). Its responses are never
cacheable, never indexable and never send a Referer, and the password form is throttled per
visitor-and-profile so a leaked link cannot be brute-forced through the second factor.
"""
from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods, require_POST

from core import pilots
from core.audit import client_ip

from . import services
from .models import TOKEN_RE, PublicProfile
from .sections import DEFAULT_SECTIONS, SECTIONS_BY_KEY, available_sections
from .services import ProfileError, PublishInput

# Session key → ``{str(profile_pk): password_fingerprint}`` for the profiles this visitor has
# unlocked. Keyed by pk (not token) so a link rotation does not re-prompt people already let in,
# and by fingerprint so a password change does.
GRANTS_SESSION_KEY = "profile_grants"


def _public_rate() -> int:
    """Anonymous page loads per IP per minute (``<=0`` disables the throttle)."""
    return int(getattr(settings, "PROFILE_PUBLIC_RATE", 120))


def _password_rate() -> int:
    """Password attempts per (IP, profile) per five minutes."""
    return int(getattr(settings, "PROFILE_PASSWORD_RATE", 10))


# --------------------------------------------------------------------------- #
#  Owner half.
# --------------------------------------------------------------------------- #
def _absolute_public_url(request: HttpRequest, token: str) -> str:
    path = reverse("profiles:public", args=[token])
    base = (getattr(settings, "FORCA_SITE_URL", "") or "").rstrip("/")
    return f"{base}{path}" if base else request.build_absolute_uri(path)


def _cards(request: HttpRequest) -> list[dict]:
    """One card per linked pilot: the character, its profile (if any) and a display state."""
    existing = services.profiles_for(request.user)
    cards = []
    for character in pilots.linked_pilots(request.user):
        profile = existing.get(character.character_id)
        if profile is None:
            state = "none"
        elif not profile.enabled:
            state = "disabled"
        elif profile.is_expired:
            state = "expired"
        else:
            state = "live"
        cards.append({
            "character": character,
            "profile": profile,
            "state": state,
            "url": _absolute_public_url(request, profile.token) if profile else "",
            "section_labels": [
                SECTIONS_BY_KEY[k].label for k in (profile.sections if profile else [])
                if k in SECTIONS_BY_KEY
            ],
            "selected": set(profile.sections) if profile else set(DEFAULT_SECTIONS),
            # The form re-opens on the saved state: a dated profile shows its date under "on a
            # date I pick", an undated one shows "never"; a new profile gets the default preset.
            "expiry_choice": (
                services.DEFAULT_EXPIRY if profile is None
                else ("custom" if profile.expires_at else "never")
            ),
        })
    return cards


@login_required
def manage(request: HttpRequest) -> HttpResponse:
    """The management page: every linked pilot, its profile state and the publish form."""
    focus = request.GET.get("c", "")
    return render(
        request,
        "profiles/manage.html",
        {
            "cards": _cards(request),
            "available_sections": available_sections(),
            "expiry_presets": [k for k, _days in services.EXPIRY_PRESETS],
            "default_expiry": services.DEFAULT_EXPIRY,
            "focus_character_id": int(focus) if focus.isdigit() else None,
            "password_min": services.PASSWORD_MIN_LENGTH,
        },
    )


def _owned_or_bounce(request: HttpRequest):
    """The posted character, resolved through the caller's own pilots — or ``None`` (with the
    flash already queued)."""
    character = pilots.owned_pilot(request.user, request.POST.get("character_id"))
    if character is None:
        messages.error(request, _("That pilot is not linked to your account."))
    return character


def _back(character=None):
    url = reverse("profiles:manage")
    return redirect(f"{url}?c={character.character_id}" if character is not None else url)


@login_required
@require_POST
def save(request: HttpRequest) -> HttpResponse:
    """Publish a pilot's profile, or update an existing one in place."""
    character = _owned_or_bounce(request)
    if character is None:
        return _back()
    post = request.POST
    try:
        sections = services.validate_sections(post.getlist("sections"))
        expires_at = services.expiry_from_form(post.get("expiry", ""), post.get("expires_on", ""))
        password: str | None = None
        if post.get("clear_password"):
            password = ""
        elif post.get("password", ""):
            password = services.validate_password(post.get("password", ""))
    except ProfileError as exc:
        messages.error(request, str(exc))
        return _back(character)

    data = PublishInput(
        sections=sections,
        expires_at=expires_at,
        note=post.get("note", ""),
        password=password,
    )
    _profile, created = services.publish(request.user, character, data, ip=client_ip(request))
    messages.success(
        request,
        _("Public profile for %(name)s is live.") % {"name": character.name}
        if created else
        _("Public profile for %(name)s updated.") % {"name": character.name},
    )
    return _back(character)


def _profile_for_post(request: HttpRequest):
    character = _owned_or_bounce(request)
    if character is None:
        return None, None
    profile = PublicProfile.objects.filter(character=character, owner=request.user).first()
    if profile is None:
        messages.error(request, _("%(name)s has no public profile.") % {"name": character.name})
    return character, profile


@login_required
@require_POST
def rotate_link(request: HttpRequest) -> HttpResponse:
    character, profile = _profile_for_post(request)
    if profile is None:
        return _back(character)
    services.rotate(profile, request.user, ip=client_ip(request))
    messages.success(request, _("New link generated — the old one no longer works."))
    return _back(character)


@login_required
@require_POST
def toggle(request: HttpRequest) -> HttpResponse:
    character, profile = _profile_for_post(request)
    if profile is None:
        return _back(character)
    enable = not profile.enabled
    services.set_enabled(profile, request.user, enable, ip=client_ip(request))
    messages.success(
        request,
        _("Public profile re-enabled.") if enable else _("Public profile disabled — the link now shows nothing."),
    )
    return _back(character)


@login_required
@require_POST
def delete(request: HttpRequest) -> HttpResponse:
    character, profile = _profile_for_post(request)
    if profile is None:
        return _back(character)
    services.retire(profile, request.user, ip=client_ip(request))
    messages.success(request, _("Public profile deleted."))
    return _back(character)


# --------------------------------------------------------------------------- #
#  Visitor half.
# --------------------------------------------------------------------------- #
def _harden(resp: HttpResponse) -> HttpResponse:
    """The headers every public-route response carries. Set explicitly (not setdefault) so the
    global middleware never softens them: the page must not be cached anywhere, must not be
    indexed or archived, and must not hand its token to the next site as a Referer."""
    resp["Cache-Control"] = "private, no-store"
    resp["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    resp["Referrer-Policy"] = "no-referrer"
    return resp


def _not_found(request: HttpRequest) -> HttpResponse:
    """The ONE 404 every failure mode shares — a visitor cannot tell "never existed" from
    "expired", "disabled", "unlinked" or "withdrawn"."""
    return _harden(render(request, "profiles/unavailable.html", {"robots_noindex": True},
                          status=404))


def _grants(request: HttpRequest) -> dict:
    grants = request.session.get(GRANTS_SESSION_KEY)
    return dict(grants) if isinstance(grants, dict) else {}


@require_http_methods(["GET", "HEAD", "POST"])
def public(request: HttpRequest, token: str) -> HttpResponse:
    """The unlisted page at ``/p/<token>/``. Anonymous; no auth, no listing, no indexing.

    Order is security-relevant: token FORMAT first (a malformed token never reaches the
    database), then the per-IP throttle, then the resolver (the only place a row is looked up),
    then the password gate, then the render.
    """
    if not TOKEN_RE.match(token or ""):
        return _not_found(request)
    ip = client_ip(request)
    if not services.throttle_ok("page", ip, limit=_public_rate()):
        resp = HttpResponse(status=429)
        resp["Retry-After"] = "60"
        return _harden(resp)
    profile = services.resolve_public(token)
    if profile is None:
        return _not_found(request)

    user = request.user
    owner_view = bool(getattr(user, "is_authenticated", False)) and user.pk == profile.owner_id

    if profile.has_password and not owner_view:
        if _grants(request).get(str(profile.pk)) != profile.password_fingerprint():
            return _password_gate(request, profile, ip)
    if request.method == "POST":
        # Nothing to post once unlocked; treat it as a reload.
        return redirect(request.path)

    ctx = services.visitor_context(profile)
    ctx["owner_view"] = owner_view
    ctx["robots_noindex"] = True
    if not owner_view and request.method == "GET":
        services.record_view(profile)
    return _harden(render(request, "profiles/public.html", ctx))


def _password_gate(request: HttpRequest, profile: PublicProfile, ip: str) -> HttpResponse:
    """Render the lock screen, or check a submitted password and grant the session."""
    error = ""
    if request.method == "POST":
        # Two fixed windows: per (visitor IP, profile), and — defence in depth against a
        # distributed guess from many addresses — a wider cap per profile regardless of IP.
        per_ip_ok = services.throttle_ok("pw", f"{ip}:{profile.pk}", limit=_password_rate(), window=300)
        per_profile_ok = services.throttle_ok("pwp", str(profile.pk), limit=_password_rate() * 6, window=300)
        if not (per_ip_ok and per_profile_ok):
            error = _("Too many attempts. Wait a few minutes and try again.")
            status = 429
        elif profile.check_password(request.POST.get("password", "")):
            grants = _grants(request)
            grants[str(profile.pk)] = profile.password_fingerprint()
            request.session[GRANTS_SESSION_KEY] = grants
            return redirect(request.path)
        else:
            error = _("That password is not right.")
            status = 200
    else:
        status = 200
    return _harden(render(
        request,
        "profiles/locked.html",
        {"character": profile.character, "error": error, "robots_noindex": True},
        status=status,
    ))
