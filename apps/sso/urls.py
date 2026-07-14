from __future__ import annotations

from django.urls import path

from . import views

app_name = "sso"

urlpatterns = [
    path("login/", views.login_view, name="login"),
    # Adding a pilot to the signed-in account. An SSO authorisation route, so it lives beside
    # login/callback rather than under /pilot/ with the management page — same OAuth state,
    # same PKCE, same callback. POST-only (see link_view: a GET here would be login-CSRF).
    path("link/", views.link_view, name="link"),
    path("callback/", views.callback_view, name="callback"),
    path("logout/", views.logout_view, name="logout"),
    path("scopes/", views.scopes_view, name="scopes"),
    path("scopes/reconcile/", views.reconcile_view, name="reconcile"),
    path("disconnect/<int:character_id>/", views.disconnect_view, name="disconnect"),
]
