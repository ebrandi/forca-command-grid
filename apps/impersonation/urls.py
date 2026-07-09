from __future__ import annotations

from django.urls import path

from . import views

app_name = "impersonation"

urlpatterns = [
    path("start/<int:user_id>/", views.start, name="start"),
    path("stop/", views.stop, name="stop"),
    path("log/", views.log, name="log"),
]
