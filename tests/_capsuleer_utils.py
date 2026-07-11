"""Shared helpers for the Capsuleer Path test suite (doc 14 §2).

Plain functions in the flat house style of ``tests/_campaign_utils.py`` (no factory-boy). Role
helpers build real ``RoleAssignment`` rows via ``ensure_role`` + ``core.rbac`` constants; the goal
and milestone builders create rows directly for fixture control (the *services* are exercised by
``test_capsuleer_services.py`` through their public functions, not here). ``_pair`` wires a real
``mentorship`` mentor/mentee/pairing so the visibility chokepoint's active-pairing check runs
against genuine rows.
"""
from __future__ import annotations

from django.utils import timezone

from apps.capsuleer.models import (
    CareerGoal,
    CareerMilestone,
    CareerProfile,
    GoalType,
    MilestoneKind,
    Verification,
    Visibility,
)
from apps.characters.models import CharacterSkillSnapshot
from apps.identity.models import RoleAssignment
from apps.mentorship.models import MenteeProfile, MentorProfile, MentorshipPairing
from apps.pilots.models import ContributionEvent
from apps.sde.models import SdeCategory, SdeGroup, SdeType, SdeTypeSkill
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac


def _grant(user, *role_keys):
    for key in role_keys:
        RoleAssignment.objects.create(user=user, role=ensure_role(key))
    return user


def _member(django_user_model, suffix="m"):
    u = django_user_model.objects.create(username=f"eve:{suffix}")
    return _grant(u, rbac.ROLE_MEMBER)


def _officer(django_user_model, suffix="o"):
    u = django_user_model.objects.create(username=f"eve:{suffix}")
    return _grant(u, rbac.ROLE_MEMBER, rbac.ROLE_OFFICER)


def _director(django_user_model, suffix="d"):
    u = django_user_model.objects.create(username=f"eve:{suffix}")
    return _grant(u, rbac.ROLE_MEMBER, rbac.ROLE_DIRECTOR)


def _character(user, cid, name="Pilot"):
    return EveCharacter.objects.create(
        character_id=cid, user=user, name=name, is_main=True, is_corp_member=True
    )


def _profile(user, **overrides):
    return CareerProfile.objects.create(user=user, **overrides)


def _goal(user, **overrides):
    """A minimal goal for fixtures (created directly; not through the service)."""
    fields = {
        "title": "Fly logistics",
        "goal_type": GoalType.CUSTOM,
        "visibility": Visibility.PRIVATE,
    }
    fields.update(overrides)
    return CareerGoal.objects.create(user=user, **fields)


def _milestone(goal, **overrides):
    """A milestone attached to ``goal`` (order auto-assigned after the current max)."""
    next_order = (
        max((m.order for m in goal.milestones.all()), default=0) + 1
    )
    fields = {
        "order": next_order,
        "title": "Do the thing",
        "kind": MilestoneKind.MANUAL,
        "verification": Verification.SELF,
        "required": True,
        "params": {},
    }
    fields.update(overrides)
    return CareerMilestone.objects.create(goal=goal, **fields)


# --- SDE / snapshot / contribution fixtures (Stage 2) -----------------------
def _sde_groups():
    skill_cat, _ = SdeCategory.objects.get_or_create(category_id=16, defaults={"name": "Skill"})
    ship_cat, _ = SdeCategory.objects.get_or_create(category_id=6, defaults={"name": "Ship"})
    skill_grp, _ = SdeGroup.objects.get_or_create(
        group_id=9001, defaults={"category": skill_cat, "name": "TestSkills"}
    )
    ship_grp, _ = SdeGroup.objects.get_or_create(
        group_id=9002, defaults={"category": ship_cat, "name": "TestShips"}
    )
    return skill_grp, ship_grp


def _skill_type(type_id, name, *, rank=1, primary=None, secondary=None, prereqs=None):
    """A skill ``SdeType`` (category 16) with optional prerequisites ``[(skill_type_id, level)]``."""
    skill_grp, _ = _sde_groups()
    t, _ = SdeType.objects.get_or_create(
        type_id=type_id,
        defaults={"name": name, "group": skill_grp, "published": True, "rank": rank,
                  "primary_attribute_id": primary, "secondary_attribute_id": secondary},
    )
    for psid, plvl in (prereqs or []):
        SdeTypeSkill.objects.get_or_create(type_id=type_id, skill_type_id=psid,
                                           defaults={"level": plvl})
    return t


def _ship_type(type_id, name, *, base_price=None, required_skills=None):
    """A ship ``SdeType`` (category 6) with ``SdeTypeSkill`` requirements ``[(skill_id, level)]``."""
    _, ship_grp = _sde_groups()
    t, _ = SdeType.objects.get_or_create(
        type_id=type_id,
        defaults={"name": name, "group": ship_grp, "published": True, "base_price": base_price},
    )
    for sid, lvl in (required_skills or []):
        SdeTypeSkill.objects.get_or_create(type_id=type_id, skill_type_id=sid, defaults={"level": lvl})
    return t


def _snapshot(character, trained, *, as_of=None):
    """A latest ``CharacterSkillSnapshot`` for ``character`` from ``{skill_type_id: level}``."""
    character.skill_snapshots.filter(is_latest=True).update(is_latest=False)
    snap = CharacterSkillSnapshot.objects.create(
        character=character, is_latest=True,
        skills={str(k): {"trained_level": v, "sp": 0} for k, v in trained.items()},
    )
    if as_of is not None:
        CharacterSkillSnapshot.objects.filter(pk=snap.pk).update(as_of=as_of)
        snap.refresh_from_db()
    return snap


def _contribution(user, kind, n=1, *, when=None):
    """Create ``n`` ledger events of ``kind`` for ``user``."""
    when = when or timezone.now()
    for i in range(n):
        ContributionEvent.objects.create(
            user=user, kind=kind, magnitude=1, unit="count", occurred_at=when,
            ref_type="test", ref_id=f"{kind}:{i}:{when.timestamp()}",
        )


def _pair(mentor_user, mentee_user, status=MentorshipPairing.Status.ACTIVE):
    """Wire a real mentor→mentee pairing so ``services._active_mentee_user_ids`` sees it.

    The profiles are OneToOne on user, so they are reused when a pilot appears in more than one
    pairing (e.g. a mentee paired to both an active and an ended mentor).
    """
    mentor, _ = MentorProfile.objects.get_or_create(user=mentor_user)
    mentee, _ = MenteeProfile.objects.get_or_create(user=mentee_user)
    return MentorshipPairing.objects.create(mentor=mentor, mentee=mentee, status=status)
