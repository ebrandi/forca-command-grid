"""Public pilot profiles — two surfaces, one namespace (``profiles``).

* ``/public-profile/`` — the owner's management page and its POST controls. Members only:
  the prefix is NOT on the membership gate's allowlist, so a logged-in non-member is bounced to
  onboarding before any view runs, and every view is ``@login_required`` on top.
* ``/p/<token>/`` — the unlisted public page. Anonymous by design; ``/p`` IS allowlisted so a
  logged-in ex-member can still open a link they were sent.

Both are feature-gated together through ``core.features._NAMESPACE_FEATURE["profiles"]``:
when leadership turns public profiles off, every published link 404s along with the page that
manages them.
"""
from __future__ import annotations

from django.urls import path

from . import views

app_name = "profiles"

urlpatterns = [
    path("public-profile/", views.manage, name="manage"),
    path("public-profile/save/", views.save, name="save"),
    path("public-profile/rotate/", views.rotate_link, name="rotate"),
    path("public-profile/toggle/", views.toggle, name="toggle"),
    path("public-profile/delete/", views.delete, name="delete"),
    path("p/<str:token>/", views.public, name="public"),
]
