"""Capsuleer Path URL map (doc 10 §3).

The whole namespace is feature-gated through ``_NAMESPACE_FEATURE["capsuleer"]`` (Stage 1) and
membership-gated (``/capsuleer/`` is absent from ``_RECRUIT_ALLOWED_PREFIXES``); object routes 404
on a visibility miss via the ``services.can_view_goal`` chokepoint. All mutations are POST.
"""
from __future__ import annotations

from django.urls import path

from . import views

app_name = "capsuleer"

urlpatterns = [
    path("", views.home, name="home"),
    path("start/", views.start_wizard, name="start"),
    path("paths/", views.paths_browse, name="paths"),
    path("paths/compare/", views.paths_compare, name="compare"),
    path("paths/<slug:key>/", views.path_detail, name="path_detail"),
    path("paths/<slug:key>/start/", views.path_start, name="path_start"),
    path("goals/new/", views.goal_new, name="goal_new"),
    path("goals/<int:pk>/", views.goal_detail, name="goal_detail"),
    path("goals/<int:pk>/edit/", views.goal_edit, name="goal_edit"),
    path("goals/<int:pk>/status/", views.goal_status, name="goal_status"),
    path("goals/<int:pk>/build-plan/", views.goal_build_plan, name="goal_build_plan"),
    path("goals/<int:pk>/share/", views.goal_share, name="goal_share"),
    path("goals/<int:pk>/review/", views.goal_review, name="goal_review"),
    path("goals/<int:pk>/note/", views.goal_note, name="goal_note"),
    path("goals/<int:pk>/endorse/", views.goal_endorse, name="goal_endorse"),
    path("goals/<int:pk>/milestones/add/", views.milestone_add, name="milestone_add"),
    path("milestones/<int:pk>/update/", views.milestone_update, name="milestone_update"),
    path("milestones/<int:pk>/status/", views.milestone_status, name="milestone_status"),
    path("goals/<int:pk>/steps/add/", views.step_add, name="step_add"),
    path("steps/<int:pk>/status/", views.step_status, name="step_status"),
    path("steps/<int:pk>/task/", views.step_task, name="step_task"),
    path("profile/", views.profile, name="profile"),
    path("suggestions/<int:pk>/act/", views.suggestion_act, name="suggestion_act"),
    path("quests/<str:ref>/act/", views.quest_action, name="quest_action"),
    path("leadership/", views.leadership, name="leadership"),
    path("types/", views.types_json, name="types"),
    path("ships/", views.ships_json, name="ships"),
    path("doctrines/", views.doctrines_json, name="doctrines"),
]
