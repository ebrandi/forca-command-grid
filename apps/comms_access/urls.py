from __future__ import annotations

from django.urls import path

from . import views

app_name = "comms_access"

urlpatterns = [
    path("", views.connect, name="connect"),
    path("discord/begin/", views.discord_begin, name="discord_begin"),
    path("discord/callback/", views.discord_callback, name="discord_callback"),
    path("discord/unlink/", views.discord_unlink, name="discord_unlink"),
]
