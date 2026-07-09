from __future__ import annotations

from django.urls import path

from . import views

app_name = "market"

urlpatterns = [
    path("", views.market_dashboard, name="dashboard"),
    path("watch/", views.toggle_watch, name="toggle_watch"),
    path("locations/create/", views.create_location, name="create_location"),
    path("locations/<int:pk>/edit/", views.edit_location, name="edit_location"),
    path("locations/<int:pk>/toggle/", views.toggle_location, name="toggle_location"),
    path("refresh/", views.refresh_market, name="refresh"),
]
