from __future__ import annotations

from django.contrib import admin

from .models import (
    Doctrine,
    DoctrineCategory,
    DoctrineFit,
    DoctrineRequirement,
    SkillRequirement,
)


class SkillRequirementInline(admin.TabularInline):
    model = SkillRequirement
    extra = 0


class DoctrineRequirementInline(admin.TabularInline):
    model = DoctrineRequirement
    extra = 0


@admin.register(DoctrineCategory)
class DoctrineCategoryAdmin(admin.ModelAdmin):
    list_display = ("key", "label", "sort_order")


class DoctrineFitInline(admin.StackedInline):
    model = DoctrineFit
    extra = 0
    fields = ("name", "ship_type_id", "role", "is_cheap_alt", "eft_text")


@admin.register(Doctrine)
class DoctrineAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "status", "priority")
    list_filter = ("status", "category")
    search_fields = ("name",)
    inlines = [DoctrineFitInline]


@admin.register(DoctrineFit)
class DoctrineFitAdmin(admin.ModelAdmin):
    list_display = ("name", "doctrine", "ship_type_id", "role")
    inlines = [SkillRequirementInline, DoctrineRequirementInline]
