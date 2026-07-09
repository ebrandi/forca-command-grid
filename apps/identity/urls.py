from __future__ import annotations

from django.urls import path

from . import views

app_name = "identity"

urlpatterns = [
    path("dashboard/", views.dashboard, name="dashboard"),
    path("dashboard/layout/", views.save_dashboard_layout, name="save_dashboard_layout"),
    path("characters/<int:character_id>/", views.character_dashboard, name="character"),
    path("privacy/", views.privacy, name="privacy"),
    path("privacy/delete/", views.delete_my_data, name="delete_data"),
]
