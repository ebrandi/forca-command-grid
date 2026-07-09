from __future__ import annotations

from django.urls import path

from . import views

app_name = "onboarding"

urlpatterns = [
    path("", views.onboarding_dashboard, name="dashboard"),
    path("milestone/<int:pk>/", views.milestone_action, name="milestone_action"),
]
