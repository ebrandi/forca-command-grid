"""Campaign Command URL conf (mounted at /campaigns/, namespace ``campaigns``).

The Phase 1 member/officer surface (design doc 10 §5): the portfolio, campaign definition
create/edit, the campaign detail page and its lifecycle POST, the progress-explanation
transparency page, and the objective/milestone/workstream/risk/issue/dependency CRUD +
status forms. Every view is ``@login_required`` and gated by the ``campaigns`` feature via
``core.features._NAMESPACE_FEATURE``; object routes re-check ``services.can_view``.
"""
from __future__ import annotations

from django.urls import path

from . import views

app_name = "campaigns"

urlpatterns = [
    # Portfolio + campaign definition.
    path("", views.portfolio, name="index"),
    path("new/", views.campaign_create, name="new"),
    path("new/template/", views.template_picker, name="template_picker"),
    path("lessons/", views.lessons_library, name="lessons"),
    path("workspace/", views.officer_workspace, name="workspace"),
    path("<int:pk>/", views.campaign_detail, name="detail"),
    path("<int:pk>/edit/", views.campaign_edit, name="edit"),
    path("<int:pk>/status/", views.campaign_set_status, name="set_status"),
    path("<int:pk>/progress/", views.campaign_set_progress, name="set_progress"),
    path("<int:pk>/explain/", views.progress_explanation, name="explain"),
    path("<int:pk>/timeline/", views.campaign_timeline, name="timeline"),
    path("<int:pk>/activity/", views.campaign_activity, name="activity"),
    path("<int:pk>/close/", views.campaign_close, name="close"),
    path("<int:pk>/report/", views.campaign_report, name="report"),
    path("<int:pk>/recognition/", views.recognition_manage, name="recognition"),
    path("<int:pk>/save-template/", views.campaign_save_template, name="save_template"),
    # Objectives.
    path("<int:pk>/objectives/new/", views.objective_create, name="objective_create"),
    path("objectives/<int:pk>/", views.objective_detail, name="objective_detail"),
    path("objectives/<int:pk>/edit/", views.objective_edit, name="objective_edit"),
    path("objectives/<int:pk>/update-value/", views.objective_update_value, name="objective_update_value"),
    path("objectives/<int:pk>/verify/", views.objective_verify, name="objective_verify"),
    path("objectives/<int:pk>/status/", views.objective_set_status, name="objective_set_status"),
    path("objectives/<int:pk>/task/", views.objective_create_task, name="objective_task"),
    path("objectives/<int:pk>/volunteer/", views.objective_volunteer, name="volunteer"),
    # Milestones.
    path("<int:pk>/milestones/new/", views.milestone_create, name="milestone_create"),
    path("milestones/<int:pk>/edit/", views.milestone_edit, name="milestone_edit"),
    path("milestones/<int:pk>/status/", views.milestone_set_status, name="milestone_set_status"),
    # Workstreams.
    path("<int:pk>/workstreams/new/", views.workstream_create, name="workstream_create"),
    path("workstreams/<int:pk>/edit/", views.workstream_edit, name="workstream_edit"),
    # Risks.
    path("<int:pk>/risks/new/", views.risk_create, name="risk_create"),
    path("risks/<int:pk>/edit/", views.risk_edit, name="risk_edit"),
    # Issues.
    path("<int:pk>/issues/new/", views.issue_create, name="issue_create"),
    path("issues/<int:pk>/resolve/", views.issue_resolve, name="issue_resolve"),
    path("issues/<int:pk>/escalate/", views.issue_escalate, name="issue_escalate"),
    # Dependencies.
    path("<int:pk>/dependencies/new/", views.dependency_create, name="dependency_create"),
    path("dependencies/<int:pk>/resolve/", views.dependency_resolve, name="dependency_resolve"),
    # Linked operations.
    path("<int:pk>/operations/link/", views.operation_link, name="operation_link"),
    path("<int:pk>/operations/unlink/", views.operation_unlink, name="operation_unlink"),
    # Evidence.
    path("<int:pk>/evidence/new/", views.evidence_create, name="evidence_create"),
    path("evidence/<int:pk>/delete/", views.evidence_delete, name="evidence_delete"),
]
