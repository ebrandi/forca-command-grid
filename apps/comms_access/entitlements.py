"""Desired-state resolver — the platform-neutral entitlement vocabulary.

``entitlements(user)`` returns the set of abstract entitlement keys a pilot *should* hold,
derived purely from FORCA's authoritative state. It deliberately mirrors
``core.features.feature_visible_to`` so a pilot's comms access can never drift from their
in-app access: same membership authority (``is_service_alliance_pilot``), same RBAC
(``core.rbac``), same live-not-memoised revocation posture.

Pure function, no external calls — the reconcile engine maps these keys to platform roles
via :class:`apps.comms_access.models.EntitlementMapping`.
"""
from __future__ import annotations

from apps.corporation.access import is_service_alliance_pilot
from core import rbac

# The vocabulary the admin UI offers when building a mapping. Extensible: a future
# ``doctrine:<slug>`` or ``srp:approver`` key is one branch below + one row here.
ENTITLEMENTS: dict[str, str] = {
    "member": "Home-corp member",
    "officer": "Officer (or higher)",
    "director": "Director (or higher)",
    "recruiter": "Recruiter (recruitment.manage)",
    "fc": "Fleet Commander (fleet.manage)",
    "alliance": "Alliance / friendly-corp pilot (not a home member)",
}


def entitlements(user) -> set[str]:
    """Abstract entitlement keys a pilot should currently hold (empty if anonymous)."""
    if not getattr(user, "is_authenticated", False):
        return set()

    ents: set[str] = set()
    is_member = rbac.has_role(user, rbac.ROLE_MEMBER)
    if is_member:
        ents.add("member")
    if rbac.has_role(user, rbac.ROLE_OFFICER):
        ents.add("officer")
    if rbac.has_role(user, rbac.ROLE_DIRECTOR):
        ents.add("director")
    if rbac.has_perm(user, rbac.PERM_RECRUITMENT_MANAGE):
        ents.add("recruiter")
    if rbac.has_perm(user, rbac.PERM_FLEET_MANAGE):
        ents.add("fc")
    # "alliance" is the friendly-but-not-home bucket (partner alliance / friendly corp),
    # kept disjoint from "member" so a mapping can target blues without touching members.
    if not is_member and is_service_alliance_pilot(user):
        ents.add("alliance")
    return ents
