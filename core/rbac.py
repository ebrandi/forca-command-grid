"""Role-based access control: tiers, permission keys, helpers, DRF classes."""
from __future__ import annotations

from functools import wraps

from django.core.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission

# Ordered role tiers (higher rank = more authority).
ROLE_PUBLIC = "public"
ROLE_MEMBER = "member"
ROLE_OFFICER = "officer"
ROLE_DIRECTOR = "director"
ROLE_ADMIN = "admin"

ROLE_RANK = {
    ROLE_PUBLIC: 0,
    ROLE_MEMBER: 10,
    ROLE_OFFICER: 20,
    ROLE_DIRECTOR: 30,
    ROLE_ADMIN: 40,
}


def effective_rank(user) -> int:
    """Highest role rank a user holds (0 for anonymous/public)."""
    if not getattr(user, "is_authenticated", False):
        return ROLE_RANK[ROLE_PUBLIC]
    if getattr(user, "is_superuser", False):
        return ROLE_RANK[ROLE_ADMIN]
    return getattr(user, "max_role_rank", lambda: ROLE_RANK[ROLE_PUBLIC])()


def has_role(user, role: str) -> bool:
    """True if the user holds at least the given role tier."""
    return effective_rank(user) >= ROLE_RANK[role]


# --- Capability permissions (least-privilege layer, 4.16) --------------------
# Lateral roles grant a specific capability WITHOUT full officer rank: a recruiter can work
# the recruitment pipeline, an FC can run fleet ops, neither is an officer everywhere else.
# These role keys are deliberately absent from ROLE_RANK, so holding one contributes rank 0
# — the grant adds a capability, never authority elsewhere.
ROLE_RECRUITER = "recruiter"
ROLE_FC = "fc"

PERM_RECRUITMENT_MANAGE = "recruitment.manage"
PERM_FLEET_MANAGE = "fleet.manage"

# Each capability is ALSO implied by a rank baseline, so every surface that was rank-gated
# before 4.16 keeps working for officers/directors; a lateral role just extends that one
# capability down to a member who holds it.
_PERM_RANK_BASELINE = {
    PERM_RECRUITMENT_MANAGE: ROLE_RANK[ROLE_OFFICER],
    PERM_FLEET_MANAGE: ROLE_RANK[ROLE_OFFICER],
}


# Granting one of these roles needs a SECOND director's approval (4.17 dual-control): a
# single (possibly compromised) director can't unilaterally mint another. Revokes still
# apply immediately, guarded elsewhere by the last-director floor.
_DUAL_CONTROL_ROLES = {ROLE_DIRECTOR}


def requires_dual_control(role_key: str) -> bool:
    """True if GRANTING this role requires a second director's approval."""
    return role_key in _DUAL_CONTROL_ROLES


def has_perm(user, perm_key: str) -> bool:
    """True if the user holds a capability — implicitly (rank at/above the capability's
    baseline, so an officer/director keeps it) or by an explicit NON-expired role grant (a
    recruiter/FC who isn't a full officer). Superuser holds everything. An unknown key
    fails closed to a DIRECTOR baseline, so a typo never silently grants a member access."""
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    baseline = _PERM_RANK_BASELINE.get(perm_key, ROLE_RANK[ROLE_DIRECTOR])
    if effective_rank(user) >= baseline:
        return True
    getter = getattr(user, "active_permission_keys", None)
    return callable(getter) and perm_key in getter()


def perm_required(perm_key: str):
    """View decorator enforcing a capability (rank baseline OR explicit grant)."""

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if not has_perm(request.user, perm_key):
                raise PermissionDenied("Insufficient permission.")
            return view_func(request, *args, **kwargs)

        return _wrapped

    return decorator


def role_required(role: str):
    """View decorator enforcing a minimum role tier."""

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if not has_role(request.user, role):
                raise PermissionDenied("Insufficient role.")
            return view_func(request, *args, **kwargs)

        return _wrapped

    return decorator


class _MinRolePermission(BasePermission):
    role = ROLE_MEMBER

    def has_permission(self, request, view) -> bool:
        return has_role(request.user, self.role)


class IsMember(_MinRolePermission):
    role = ROLE_MEMBER


class IsOfficer(_MinRolePermission):
    role = ROLE_OFFICER


class IsDirector(_MinRolePermission):
    role = ROLE_DIRECTOR


class IsAdmin(_MinRolePermission):
    role = ROLE_ADMIN
