"""The selector's ``set_language`` endpoint (our safe wrapper).

Resolves the chosen code through the enabled allow-list, persists the account
preference for authenticated operators (impersonation-aware — writes the
director's own preference, never the impersonated pilot's), sets the language
cookie, and redirects only to a same-origin ``next`` via the shared
``core.redirects.safe_next`` guard (D18).

The cookie is written from the allow-list's own code, not from the posted string,
so nothing that arrives on the wire is ever echoed into a Set-Cookie header.
"""
from __future__ import annotations

from django.conf import settings
from django.http import HttpResponseRedirect
from django.views.decorators.http import require_POST

from core.redirects import safe_next

from .config import enabled_locales


@require_POST
def set_language(request):
    posted = (request.POST.get("language") or "").strip()
    # Re-derive the code from the allow-list rather than echoing the posted string
    # back: everything below writes the settings.LANGUAGES constant, never bytes
    # that came off the wire. None when the code is unknown or not enabled.
    lang = next((code for code in enabled_locales() if code == posted), None)

    candidate = request.POST.get("next") or request.META.get("HTTP_REFERER")
    response = HttpResponseRedirect(safe_next(request, candidate, "/"))

    if lang:
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
