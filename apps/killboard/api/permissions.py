"""Permission classes for the killboard REST API (KB-28).

Two gates, layered on the existing RBAC (``core.rbac``):

* :class:`IsMemberOrPublicRead` — the public-board-equivalent endpoints (killmail
  list/detail, fitting/eft/esi, history, schema/docs). Members (session or token) always
  pass; anonymous callers pass ONLY when ``settings.KILLBOARD_API_PUBLIC_READ`` is on. Every
  endpoint is read-only, so this never widens write access.
* ``core.rbac.IsMember`` (re-exported) — the members-only endpoints (stats, leaderboards),
  which stay gated regardless of the public-read flag, mirroring the website (the stats
  dashboard and pilot analytics are member-gated there too).

Field-level tiering (own-loss SRP/deviation for members, any-loss for officers) is done in
the serializers from request context, not here — a permission decides *access to the
endpoint*, the serializer decides *which fields* within it.
"""
from __future__ import annotations

from django.conf import settings
from rest_framework.permissions import BasePermission

from core import rbac
from core.rbac import IsMember  # noqa: F401  (re-exported for the members-only views)


class IsMemberOrPublicRead(BasePermission):
    """Allow corp members always; allow anonymous only when public-read is enabled."""

    message = "Authentication is required for the killboard API."

    def has_permission(self, request, view) -> bool:
        if rbac.has_role(request.user, rbac.ROLE_MEMBER):
            return True
        return bool(getattr(settings, "KILLBOARD_API_PUBLIC_READ", False))
