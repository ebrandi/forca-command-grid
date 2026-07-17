from __future__ import annotations

from django.urls import path

from . import views

app_name = "supplyboard"

urlpatterns = [
    path("", views.board, name="board"),
    path("refresh/", views.refresh, name="refresh"),
]
