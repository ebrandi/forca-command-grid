from __future__ import annotations

from django.urls import path

from . import pilot_views, views

app_name = "identity"

urlpatterns = [
    path("dashboard/", views.dashboard, name="dashboard"),
    path("dashboard/layout/", views.save_dashboard_layout, name="save_dashboard_layout"),
    path("characters/<int:character_id>/", views.character_dashboard, name="character"),
    path("privacy/", views.privacy, name="privacy"),
    path("privacy/delete/", views.delete_my_data, name="delete_data"),
    # Linked Pilots. Mounted under /pilot/ (this app is included at the root) so the surface
    # reads as the pilot's own: /pilot/linked-pilots/. The prefix is allowlisted past
    # MembershipGateMiddleware — a user whose active pilot sits outside the corporation is
    # correctly no longer a member, and must still be able to reach this page and switch back.
    # The SSO half of linking (the OAuth begin) is sso:link, beside login and callback.
    path("pilot/linked-pilots/", pilot_views.linked_pilots, name="linked_pilots"),
    path("pilot/switch/", pilot_views.switch_pilot, name="pilot_switch"),
    path("pilot/main/", pilot_views.set_main, name="pilot_main"),
    path("pilot/unlink/", pilot_views.unlink_pilot, name="pilot_unlink"),
]
