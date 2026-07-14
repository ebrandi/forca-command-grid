"""Command-center dashboard: aggregates combat, actions, and operations."""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.utils import timezone

from apps.identity.models import RoleAssignment
from apps.industry.models import IndustryProject
from apps.killboard.models import Killmail
from apps.recommendations.models import Recommendation
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from apps.stockpile.models import HaulingTask
from core import rbac


def _member(django_user_model, role=rbac.ROLE_MEMBER):
    user = django_user_model.objects.create(username="cmdr", first_name="Cmdr")
    RoleAssignment.objects.create(user=user, role=ensure_role(role))
    EveCharacter.objects.create(character_id=777, user=user, name="Cmdr", is_main=True, is_corp_member=True)
    return user


@pytest.mark.django_db
def test_dashboard_aggregates_command_center(client, django_user_model, sde):
    user = _member(django_user_model)
    now = timezone.now()
    Killmail.objects.create(
        killmail_id=1, killmail_hash="a", killmail_time=now, solar_system_id=30000142,
        victim_ship_type_id=587, involves_home_corp=True,
        home_corp_role=Killmail.HomeRole.ATTACKER, total_value=Decimal("100000000"),
    )
    Killmail.objects.create(
        killmail_id=2, killmail_hash="b", killmail_time=now, solar_system_id=30000142,
        victim_ship_type_id=587, involves_home_corp=True,
        home_corp_role=Killmail.HomeRole.VICTIM, total_value=Decimal("50000000"),
    )
    IndustryProject.objects.create(name="Unclaimed build", status=IndustryProject.Status.ACTIVE)
    HaulingTask.objects.create(type_id=34, status=HaulingTask.Status.OPEN, volume_m3=1000)
    # The personal queue is the unified quest log (PilotDirective +
    # PilotRecommendation) — prime the CI cache so the view renders the
    # persisted directive without recomputing (which would prune it as stale).
    from django.core.cache import cache

    from apps.command_intel import pilot as ci_pilot
    from apps.command_intel.models import PilotDirective

    # The quest log belongs to the PILOT it was computed for (LP-3).
    PilotDirective.objects.create(
        user=user, character=user.characters.get(character_id=777),
        slug="doctrine/1", category="skill", title="Train Gunnery V", points=10,
    )
    cache.set(ci_pilot.cache_key(777), {"directives": []})

    client.force_login(user)
    resp = client.get("/dashboard/")
    assert resp.status_code == 200
    ctx = resp.context
    assert ctx["combat7"]["kills"] == 1
    assert ctx["combat7"]["losses"] == 1
    assert ctx["combat7"]["isk_destroyed"] == Decimal("100000000")
    assert ctx["industry"]["active"] == 1 and ctx["industry"]["unclaimed"] == 1
    assert ctx["logistics"]["open"] == 1
    assert any(q["title"] == "Train Gunnery V" for q in ctx["quests"])
    body = resp.content.decode()
    assert "Command Center" in body
    assert "Train Gunnery V" in body  # quest-log order surfaced
    assert "officer" not in ctx  # members don't get the command panel


@pytest.mark.django_db
def test_dashboard_officer_panel_present_for_officer(client, django_user_model, sde):
    user = _member(django_user_model, role=rbac.ROLE_OFFICER)
    Recommendation.objects.create(
        type=Recommendation.Type.STOCK_SHORTAGE, subject_type="type", subject_id="34",
        message="Build or buy 100", required_permission="officer",
    )
    client.force_login(user)
    resp = client.get("/dashboard/")
    assert resp.status_code == 200
    assert resp.context["officer"]["open_recs"] == 1
    assert "Command" in resp.content.decode()


@pytest.mark.django_db
def test_dashboard_without_characters_prompts_link(client, django_user_model):
    # A member (e.g. via manual role) who hasn't linked a character yet still
    # reaches the dashboard and is prompted to link one. (Non-members are gated
    # to recruitment before the dashboard — see test_membership_gate.)
    user = django_user_model.objects.create(username="nochar")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(user)
    resp = client.get("/dashboard/")
    assert resp.status_code == 200
    assert "Link a character" in resp.content.decode()
