from __future__ import annotations

from django.contrib import admin

from .models import AuthToken, EveCharacter, EveScopeGrant


@admin.register(EveCharacter)
class EveCharacterAdmin(admin.ModelAdmin):
    list_display = ("character_id", "name", "user", "corporation", "is_corp_member", "is_main")
    list_filter = ("is_corp_member", "is_main")
    search_fields = ("name", "character_id")


@admin.register(AuthToken)
class AuthTokenAdmin(admin.ModelAdmin):
    """Tokens are never displayed or editable in the admin (security)."""

    list_display = ("character", "token_type", "access_expires_at", "revoked_at", "refresh_fail_count")
    readonly_fields = ("character", "scopes", "token_type", "access_expires_at", "revoked_at")
    exclude = ("_refresh_token", "_access_token")

    def has_add_permission(self, request) -> bool:
        return False


@admin.register(EveScopeGrant)
class EveScopeGrantAdmin(admin.ModelAdmin):
    list_display = ("character", "scope", "active", "granted_at")
    list_filter = ("active",)
    search_fields = ("scope",)
