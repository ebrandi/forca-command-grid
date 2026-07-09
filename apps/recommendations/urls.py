from __future__ import annotations

from django.urls import path

from . import views

app_name = "recommendations"

urlpatterns = [
    path("officer/", views.officer_dashboard, name="officer"),
    path("notifications/", views.notifications, name="notifications"),
    path("notifications/sync/", views.notifications_sync, name="notifications_sync"),
    path("mine/", views.personal_recs, name="personal"),
    path("<int:pk>/act/", views.act, name="act"),
    path("<int:pk>/link/", views.link_action_item, name="link"),
]
