"""The selector's ``set_language`` endpoint (our safe wrapper).

Validates the chosen code against the enabled allow-list, persists the account
preference for authenticated operators (impersonation-aware — writes the
director's own preference, never the impersonated pilot's), sets the language
cookie, and redirects only to a same-origin ``next`` (open-redirect guard, D18).
"""
from __future__ import annotations

from django.conf import settings
from django.http import HttpResponseRedirect
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .config import enabled_locales


@require_POST
def set_language(request):
    lang = (request.POST.get("language") or "").strip()
    valid = lang in set(enabled_locales())

    nxt = request.POST.get("next") or request.META.get("HTTP_REFERER") or "/"
    if not url_has_allowed_host_and_scheme(
        url=nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        nxt = "/"

    response = HttpResponseRedirect(nxt)
    if valid:
        operator = getattr(request, "real_user", None) or getattr(request, "user", None)
        if getattr(operator, "is_authenticated", False) and getattr(operator, "language", None) != lang:
            operator.language = lang
            operator.save(update_fields=["language"])
        response.set_cookie(
            settings.LANGUAGE_COOKIE_NAME,
            lang,
            max_age=settings.LANGUAGE_COOKIE_AGE,
            path=settings.LANGUAGE_COOKIE_PATH,
            domain=settings.LANGUAGE_COOKIE_DOMAIN,
            secure=settings.LANGUAGE_COOKIE_SECURE,
            httponly=settings.LANGUAGE_COOKIE_HTTPONLY,
            samesite=settings.LANGUAGE_COOKIE_SAMESITE,
        )
    return response
