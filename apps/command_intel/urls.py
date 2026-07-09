"""Command Intelligence URL conf (mounted at /command/).

The leadership surface (design doc 16): the Command Overview, the Operational
Constraints board, the classification-filtered report list and the report-as-briefing
detail with its htmx generation poll, plus the Course-of-Action accept/dismiss POSTs.
"""
from __future__ import annotations

from django.urls import path

from . import views

app_name = "command_intel"

urlpatterns = [
    path("", views.overview, name="overview"),
    path("constraints/", views.constraints, name="constraints"),
    path("reports/", views.reports, name="reports"),
    path("reports/generate/", views.generate, name="generate"),
    path("reports/<int:pk>/", views.report_detail, name="report_detail"),
    path("reports/<int:pk>/status/", views.report_status, name="report_status"),
    path("coa/<int:pk>/accept/", views.coa_accept, name="coa_accept"),
    path("coa/<int:pk>/dismiss/", views.coa_dismiss, name="coa_dismiss"),
    path("campaigns/", views.campaigns, name="campaigns"),
    path("campaigns/new/", views.campaign_new, name="campaign_new"),
    path("campaigns/compose/", views.campaign_compose, name="campaign_compose"),
    path("campaigns/<int:pk>/", views.campaign_detail, name="campaign_detail"),
    path("campaigns/<int:pk>/launch/", views.campaign_launch, name="campaign_launch"),
    path("campaigns/<int:pk>/abandon/", views.campaign_abandon, name="campaign_abandon"),
    # Pilot Intelligence (member self-service) — doc 16 §7.
    path("me/", views.me, name="me"),
    path("me/directive/<int:pk>/", views.directive_action, name="directive_action"),
    # Simulation / Readiness Digital Twin (officer what-if) — doc 17 §1.
    path("sim/", views.simulator, name="simulator"),
    path("sim/save/", views.simulator_save, name="simulator_save"),
    path("sim/compare/", views.simulator_compare, name="simulator_compare"),
    path("sim/<int:pk>/delete/", views.simulator_delete, name="simulator_delete"),
    # Conversational intelligence (officer Q&A over the archive) — doc 17 §3.
    path("ask/", views.ask, name="ask"),
    path("ask/submit/", views.ask_submit, name="ask_submit"),
    path("ask/<int:pk>/status/", views.ask_status, name="ask_status"),
    # Combat Intelligence — battle after-action reviews (officer).
    path("battles/", views.battles, name="battles"),
    path("battles/<int:battle_id>/", views.battle_detail, name="battle_detail"),
    path("battles/<int:battle_id>/analyze/", views.battle_generate, name="battle_generate"),
    path("battles/analysis/<int:pk>/status/", views.battle_status, name="battle_status"),
]
