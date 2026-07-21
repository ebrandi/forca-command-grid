"""Bearer-token authentication for the killboard REST API (KB-28).

A member mints a personal token at ``/killboard/api-tokens/`` and presents it as
``Authorization: Bearer <token>``. Only the SHA-256 hash is stored, so authentication is a
single indexed hash lookup — the plaintext never touches the DB. The token authenticates
*as its owning user*, so every downstream RBAC check (``core.rbac``) sees exactly the
account's standing: a token is a credential for a member, never a way to exceed one.

Session authentication (the browser member) is layered alongside this in the views, so the
same endpoints serve both the logged-in UI and off-site integrations.
"""
from __future__ import annotations

from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework import authentication, exceptions

from apps.killboard.models import KillboardApiToken

_KEYWORD = "bearer"


class KillboardTokenAuthentication(authentication.BaseAuthentication):
    """Authenticate ``Authorization: Bearer <token>`` against ``KillboardApiToken``."""

    keyword = "Bearer"

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).split()
        if not header or header[0].lower() != _KEYWORD.encode():
            return None  # no bearer credential — let another authenticator (session) try
        if len(header) == 1:
            raise exceptions.AuthenticationFailed(_("Invalid token header. No credentials provided."))
        if len(header) > 2:
            raise exceptions.AuthenticationFailed(_("Invalid token header. Token string should not contain spaces."))
        try:
            raw = header[1].decode()
        except UnicodeError:
            raise exceptions.AuthenticationFailed(_("Invalid token header. Token string contains invalid characters."))
        return self._authenticate_credentials(raw)

    def _authenticate_credentials(self, raw: str):
        token = (
            KillboardApiToken.objects.select_related("user")
            .filter(key_hash=KillboardApiToken.hash_key(raw), revoked_at__isnull=True)
            .first()
        )
        if token is None:
            raise exceptions.AuthenticationFailed(_("Invalid or revoked API token."))
        user = token.user
        if not user or not user.is_active:
            raise exceptions.AuthenticationFailed(_("User inactive or deleted."))
        # Stamp last-used without a full save (and without touching the hot auth object's
        # other columns) — a lightweight update so a leaked/idle token is visible in the UI.
        KillboardApiToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())
        return (user, token)

    def authenticate_header(self, request):
        # Drives the 401 WWW-Authenticate response so a client learns the scheme.
        return self.keyword


class KillboardTokenScheme(OpenApiAuthenticationExtension):
    """Document the bearer token in the OpenAPI schema (drives Swagger's Authorize button)."""

    target_class = "apps.killboard.api.auth.KillboardTokenAuthentication"
    name = "KillboardApiToken"

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "description": "A personal token from /killboard/api-tokens/, sent as `Authorization: Bearer <token>`.",
        }
