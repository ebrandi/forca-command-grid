from __future__ import annotations

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import Permission, Role, RoleAssignment, User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = ("username", "first_name", "main_character_id", "is_staff", "is_superuser")
    fieldsets = DjangoUserAdmin.fieldsets + (("EVE", {"fields": ("main_character_id",)}),)


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ("key", "label", "rank")
    filter_horizontal = ("permissions",)


@admin.register(Permission)
class PermissionAdmin(admin.ModelAdmin):
    list_display = ("key", "label")


@admin.register(RoleAssignment)
class RoleAssignmentAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "granted_by", "expires_at")
    list_select_related = ("user", "role")
