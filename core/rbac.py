"""Role-based access control: tiers, permission keys, helpers, DRF classes."""
from __future__ import annotations

from functools import wraps

from django.core.exceptions import PermissionDenied
from django.utils.translation import gettext as _
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


def authority_ceiling(user) -> int:
    """The most authority the user's ACTIVE pilot can substantiate (LP-4).

    Role grants say what the *human* is trusted with. This says what the pilot they are
    currently flying may *exercise* — the sudo model: holding a right is not the same as
    wielding it from any seat. A user's authority is the lesser of the two, so linking a
    Director alt can never hand Director powers to their other pilots, and switching to a
    pilot in another corporation drops corp standing entirely.

    Outside a request (a Celery worker, a management command) no pilot has been resolved and
    there is no ceiling: a nightly comms reconcile is asking what *the human* is entitled to,
    which is a different and legitimate question. ``has_resolved_pilot`` distinguishes that
    from "this account has no pilots", which fails closed.
    """
    from core import pilots

    if not pilots.has_resolved_pilot(user):
        return ROLE_RANK[ROLE_ADMIN]  # no request resolved a pilot → account-wide authority

    pilot = pilots.active_pilot(user)
    if pilot is None:
        # The account holds NO pilots at all — an operator account created by hand, or one whose
        # pilots an officer detached. A ceiling exists to stop authority leaking from one pilot
        # to another; with no pilots there is nothing to leak from and nothing to leak to, so
        # the account's own grants stand exactly as they did before this feature. (This is not
        # a way in: a pilot-less account cannot reach any pilot-specific surface, because those
        # all resolve a character first, and an account WITH pilots can never land here — the
        # middleware always resolves one of them.)
        return ROLE_RANK[ROLE_ADMIN]
    if not pilot.is_corp_member:
        # A pilot outside the home corporation carries no corp standing whatsoever — not
        # member, not officer, not director — regardless of what other pilots the human owns.
        return ROLE_RANK[ROLE_PUBLIC]
    if pilot.is_corp_director:
        return ROLE_RANK[ROLE_ADMIN]  # in-game Director: the account's grant decides
    # In the corp but not an in-game Director: corp standing, but Director authority is out of
    # reach. Officer stays available because it is a trust grant to the *person* and no
    # per-character evidence exists that could narrow it further (see LP-4).
    return ROLE_RANK[ROLE_OFFICER]


def effective_rank(user) -> int:
    """Highest role rank a user may exercise right now (0 for anonymous/public).

    The lesser of what the account was granted and what the active pilot can substantiate.
    """
    if not getattr(user, "is_authenticated", False):
        return ROLE_RANK[ROLE_PUBLIC]
    if getattr(user, "is_superuser", False):
        # The platform break-glass (the operator), not a corp role. Deliberately NOT ceilinged:
        # an admin must not be able to lock themselves out by switching to an alt.
        return ROLE_RANK[ROLE_ADMIN]
    account = getattr(user, "max_role_rank", lambda: ROLE_RANK[ROLE_PUBLIC])()
    if account >= ROLE_RANK[ROLE_ADMIN]:
        # ROLE_ADMIN is the platform operator, held by a human, evidenced by nothing in-game —
        # the same kind of authority as is_superuser above, and exempt for the same reason. It
        # also cannot be ceilinged coherently: the ranks are a ladder, so any cap low enough to
        # withdraw Director (30) would also withdraw Admin (40).
        return account
    return min(account, authority_ceiling(user))


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
ROLE_CAMPAIGN_LEAD = "campaign_lead"

PERM_RECRUITMENT_MANAGE = "recruitment.manage"
PERM_FLEET_MANAGE = "fleet.manage"
PERM_CAMPAIGN_MANAGE = "campaign.manage"

# Each capability is ALSO implied by a rank baseline, so every surface that was rank-gated
# before 4.16 keeps working for officers/directors; a lateral role just extends that one
# capability down to a member who holds it.
_PERM_RANK_BASELINE = {
    PERM_RECRUITMENT_MANAGE: ROLE_RANK[ROLE_OFFICER],
    PERM_FLEET_MANAGE: ROLE_RANK[ROLE_OFFICER],
    PERM_CAMPAIGN_MANAGE: ROLE_RANK[ROLE_OFFICER],
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
    # A lateral grant (recruiter / FC / campaign lead) is subject to the same active-pilot
    # ceiling as a rank (LP-4): a recruiter flying an alt in another corporation is not
    # recruiting for us from that seat.
    if authority_ceiling(user) < ROLE_RANK[ROLE_MEMBER]:
        return False
    getter = getattr(user, "active_permission_keys", None)
    return callable(getter) and perm_key in getter()


def perm_required(perm_key: str):
    """View decorator enforcing a capability (rank baseline OR explicit grant)."""

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if not has_perm(request.user, perm_key):
                raise PermissionDenied(_("Insufficient permission."))
            return view_func(request, *args, **kwargs)

        # Make the guard introspectable, so callers can ask "would this view admit that user?"
        # WITHOUT calling it. Pilot switching needs exactly that: it must decide whether the
        # page you are on is still yours under the pilot you just switched to, and the honest
        # answer is the one this decorator would give — not a hand-maintained second list of
        # which URLs are officer-only, which would drift the day someone adds a view.
        _wrapped.required_perm = perm_key
        return _wrapped

    return decorator


def role_required(role: str):
    """View decorator enforcing a minimum role tier."""

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if not has_role(request.user, role):
                raise PermissionDenied(_("Insufficient role."))
            return view_func(request, *args, **kwargs)

        _wrapped.required_role = role  # see perm_required
        return _wrapped

    return decorator


def view_admits(view_func, user) -> bool:
    """Would ``view_func``'s own rbac guards let ``user`` through right now?

    Answers only for the guards this module installs (``role_required`` / ``perm_required``).
    A view with neither is treated as admitting — it may still have gates of its own, and the
    caller is expected to check the middleware gates (membership, feature audience) separately.
    """
    role = getattr(view_func, "required_role", None)
    if role is not None and not has_role(user, role):
        return False
    perm = getattr(view_func, "required_perm", None)
    return not (perm is not None and not has_perm(user, perm))


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
