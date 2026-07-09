from __future__ import annotations

from django.urls import path

from . import views

app_name = "sso"

urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("callback/", views.callback_view, name="callback"),
    path("logout/", views.logout_view, name="logout"),
    path("scopes/", views.scopes_view, name="scopes"),
    path("scopes/reconcile/", views.reconcile_view, name="reconcile"),
    path("disconnect/<int:character_id>/", views.disconnect_view, name="disconnect"),
]
