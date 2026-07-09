from django.contrib import admin

from .models import KbPage, KbRevision


@admin.register(KbPage)
class KbPageAdmin(admin.ModelAdmin):
    list_display = ("title", "category", "visibility", "updated_at")
    list_filter = ("visibility", "category")
    prepopulated_fields = {"slug": ("title",)}


@admin.register(KbRevision)
class KbRevisionAdmin(admin.ModelAdmin):
    list_display = ("page", "edited_by", "edited_at")
