"""Django-admin registrations for Command Intelligence (read-mostly).

Reports and snapshots are immutable institutional records; the admin is for
inspection, not authoring (authoring is the officer/Director UI + console pages).
"""
from __future__ import annotations

from django.contrib import admin

from .models import (
    ActionOutcome,
    BattleAnalysis,
    Campaign,
    CampaignMilestone,
    ConversationTurn,
    CourseOfAction,
    IntelligenceReport,
    IntelligenceSnapshot,
    OperationalConstraint,
    PilotDirective,
)


@admin.register(IntelligenceSnapshot)
class IntelligenceSnapshotAdmin(admin.ModelAdmin):
    list_display = ("id", "created_at", "trigger", "config_version", "build_ms")
    list_filter = ("trigger",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(OperationalConstraint)
class OperationalConstraintAdmin(admin.ModelAdmin):
    list_display = ("key", "category", "binding_metric", "unit", "severity", "status", "snapshot")
    list_filter = ("category", "severity", "status")
    search_fields = ("key", "label")


@admin.register(IntelligenceReport)
class IntelligenceReportAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "status", "classification", "model_name", "created_at")
    list_filter = ("status", "classification", "template_key")
    readonly_fields = ("created_at", "updated_at", "generated_at")


@admin.register(CourseOfAction)
class CourseOfActionAdmin(admin.ModelAdmin):
    list_display = ("slug", "objective", "state", "priority", "confidence_label", "responsible_user")
    list_filter = ("state", "effort", "confidence_label")
    search_fields = ("slug", "objective")


@admin.register(ActionOutcome)
class ActionOutcomeAdmin(admin.ModelAdmin):
    list_display = ("coa", "metric_key", "predicted_delta", "measured_delta", "error", "measured_at")


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ("id", "objective", "target_metric", "status", "success_probability", "owner")
    list_filter = ("status", "target_metric")


@admin.register(CampaignMilestone)
class CampaignMilestoneAdmin(admin.ModelAdmin):
    list_display = ("campaign", "order", "title", "status", "coa", "responsible_user")
    list_filter = ("status",)


@admin.register(PilotDirective)
class PilotDirectiveAdmin(admin.ModelAdmin):
    list_display = ("user", "title", "category", "state", "leverage", "constraint_key", "updated_at")
    list_filter = ("state", "category")
    search_fields = ("title", "slug", "constraint_key")


@admin.register(ConversationTurn)
class ConversationTurnAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "question", "status", "grounded", "clearance", "created_at")
    list_filter = ("status", "grounded", "clearance")
    search_fields = ("question", "answer")
    readonly_fields = ("created_at", "updated_at", "answered_at")


@admin.register(BattleAnalysis)
class BattleAnalysisAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "battle_report_id", "status", "classification", "model_name", "created_at")
    list_filter = ("status", "classification")
    search_fields = ("title",)
    readonly_fields = ("created_at", "updated_at", "generated_at")
