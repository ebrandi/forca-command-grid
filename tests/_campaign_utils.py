"""Shared helpers for the Campaign Command test suite (doc 12 §2).

Plain functions in the style of ``tests/test_command_center.py`` (no factory-boy, matching the
codebase decision). Role helpers build real ``RoleAssignment`` rows via ``ensure_role`` +
``core.rbac`` constants; ``_advance`` walks the legal lifecycle chain through ``services.set_status``
so tests never poke ``status`` directly; ``_reference_campaign`` instantiates the seeded builtin
"Establish Armour Battleship Deployment Readiness" template.
"""
from __future__ import annotations

from django.utils import timezone

from apps.campaigns import services
from apps.campaigns.models import Campaign, CampaignTemplate, Objective
from apps.identity.models import RoleAssignment
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac

CS = Campaign.Status
OS = Objective.ObjectiveStatus


def _grant(user, *role_keys):
    for key in role_keys:
        RoleAssignment.objects.create(user=user, role=ensure_role(key))
    return user


def _member(django_user_model, suffix="m", cid=None):
    u = django_user_model.objects.create(username=f"eve:{suffix}")
    _grant(u, rbac.ROLE_MEMBER)
    if cid:
        EveCharacter.objects.create(
            character_id=cid, user=u, name=str(suffix), is_main=True, is_corp_member=True
        )
    return u


def _officer(django_user_model, suffix="o", cid=None):
    u = django_user_model.objects.create(username=f"eve:{suffix}")
    _grant(u, rbac.ROLE_OFFICER)
    if cid:
        EveCharacter.objects.create(
            character_id=cid, user=u, name=str(suffix), is_main=True, is_corp_member=True
        )
    return u


def _director(django_user_model, suffix="d", cid=None):
    u = django_user_model.objects.create(username=f"eve:{suffix}")
    _grant(u, rbac.ROLE_DIRECTOR)
    if cid:
        EveCharacter.objects.create(
            character_id=cid, user=u, name=str(suffix), is_main=True, is_corp_member=True
        )
    return u


def _campaign_lead(django_user_model, suffix="cl"):
    u = django_user_model.objects.create(username=f"eve:{suffix}")
    _grant(u, rbac.ROLE_MEMBER, rbac.ROLE_CAMPAIGN_LEAD)
    return u


def _campaign(**overrides) -> Campaign:
    """A minimal, proposable campaign (outcome + a future target end); status defaults to draft."""
    fields = {
        "name": "Deployment",
        "desired_outcome": "Be ready",
        "category": Campaign.Category.DEPLOYMENT,
        "visibility": Campaign.Visibility.MEMBERS,
        "start_at": timezone.now(),
        "target_end_at": timezone.now() + timezone.timedelta(days=30),
    }
    fields.update(overrides)
    return Campaign.objects.create(**fields)


def _objective(campaign, **overrides) -> Objective:
    fields = {
        "title": "Objective", "weight": 1, "direction": Objective.Direction.GTE,
        "baseline_value": 0, "target_value": 100,
    }
    fields.update(overrides)
    return Objective.objects.create(campaign=campaign, **fields)


def _auto_objective(campaign, source_key, params=None, **overrides) -> Objective:
    return _objective(campaign, metric_source=source_key, metric_params=params or {}, **overrides)


_CHAIN = {
    CS.PROPOSED: [CS.PROPOSED],
    CS.APPROVED: [CS.PROPOSED, CS.APPROVED],
    CS.ACTIVE: [CS.PROPOSED, CS.APPROVED, CS.ACTIVE],
}


def _advance(campaign, to_status, actor, reason="ok"):
    """Walk the legal transition chain to ``to_status`` (draft→…) via the guarded service.

    ``actor`` must be a director (director can propose, approve and start), so a single actor
    drives the whole chain without smuggling illegal edges past the state machine."""
    for step in _CHAIN[to_status]:
        services.set_status(campaign, step, actor, reason=reason)
    campaign.refresh_from_db()
    return campaign


def _reference_campaign(user, *, name=None, start_at=None, target_end_at=None) -> Campaign:
    """Instantiate the seeded builtin reference campaign (12 objectives, 9 workstreams)."""
    template = CampaignTemplate.objects.get(key="armour_bs_deployment")
    if start_at is None:
        start_at = timezone.now()
    if target_end_at is None:
        target_end_at = start_at + timezone.timedelta(days=30)
    return services.instantiate_template(
        template, user, name=name, start_at=start_at, target_end_at=target_end_at
    )
