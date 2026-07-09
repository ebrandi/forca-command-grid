from __future__ import annotations

from django.urls import path

from . import views

app_name = "planetary"

urlpatterns = [
    path("", views.landing, name="landing"),
    path("learn/", views.learn, name="learn"),
    path("explore/", views.explore, name="explore"),
    path("recommend/", views.recommend_view, name="recommend"),
    path("colonies/", views.colonies, name="colonies"),
    path("colonies/sync/", views.colonies_sync, name="colonies_sync"),
    path("type-search/", views.type_search, name="type_search"),
    path("plans/new/", views.plan_create, name="create"),
    path("plans/<int:pk>/", views.plan_detail, name="detail"),
    path("plans/<int:pk>/edit/", views.plan_edit, name="edit"),
    path("plans/<int:pk>/recalc/", views.plan_recalc, name="recalc"),
    path("plans/<int:pk>/duplicate/", views.plan_duplicate, name="duplicate"),
    path("plans/<int:pk>/status/", views.plan_status, name="status"),
    path("plans/<int:pk>/delete/", views.plan_delete, name="delete"),
]
