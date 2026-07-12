"""Profile-/impersonation-aware locale activation middleware.

Placed immediately AFTER ``apps.impersonation.middleware.ImpersonationMiddleware``
(so ``request.user`` and ``request.real_user`` are populated) and before the
membership/feature gates — see docs/i18n/adr/ADR-0003. Django's stock
``LocaleMiddleware`` runs before ``request.user`` exists and therefore cannot
honour the account preference or the impersonation swap; we also deliberately do
NOT use ``i18n_patterns`` (no URL-prefixing), so a late placement is correct.

Responsibilities: resolve → ``activate`` → stamp ``request.LANGUAGE_CODE`` →
render → ``deactivate`` (in ``finally``) → set ``Content-Language`` + patch
``Vary`` so shared caches never serve the wrong language on public pages.
"""
from __future__ import annotations

from django.utils import translation
from django.utils.cache import patch_vary_headers

from .resolver import resolve_language


class LocaleMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        language = resolve_language(request)
        translation.activate(language)
        request.LANGUAGE_CODE = language
        try:
            response = self.get_response(request)
        finally:
            translation.deactivate()
        response.setdefault("Content-Language", language)
        patch_vary_headers(response, ("Accept-Language", "Cookie"))
        return response
