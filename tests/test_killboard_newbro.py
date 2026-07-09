"""KB-4 — newbro combat milestones + softened 'Snuggly' danger label."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from apps.identity.models import RoleAssignment
from apps.killboard.leaderboards import danger_rating
from apps.killboard.milestones import milestones_for, scan_milestones
from apps.killboard.models import NewbroConfig, PilotMilestone
from apps.pingboard import config
from apps.pingboard.models import Alert
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac
from tests._raffle_utils import HOME_CORP, enrol_pilot, home_kill

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _reset_config():
    config.reset("notifications")
    cache.delete("killboard:newbro_soften")
    yield
    config.reset("notifications")
    cache.delete("killboard:newbro_soften")


# --- danger-label softening --------------------------------------------------
def test_snuggly_softened_to_learning_for_new_pilots():
    NewbroConfig.objects.create(soften_danger_label=True, soften_below_events=20)
    cache.delete("killboard:newbro_soften")
    # 1 kill, 3 losses → ratio 0.25 (Snuggly territory), total 4 < 20 → softened
    assert danger_rating(1, 3)["label"] == "Learning"


def test_snuggly_kept_when_softening_disabled():
    NewbroConfig.objects.create(soften_danger_label=False, soften_below_events=20)
    cache.delete("killboard:newbro_soften")
    assert danger_rating(1, 3)["label"] == "Snuggly"


def test_snuggly_kept_above_activity_floor():
    NewbroConfig.objects.create(soften_danger_label=True, soften_below_events=5)
    cache.delete("killboard:newbro_soften")
    assert danger_rating(2, 8)["label"] == "Snuggly"  # total 10 >= 5 floor


def test_danger_labels_unaffected_by_softening():
    NewbroConfig.objects.create(soften_danger_label=True, soften_below_events=100)
    cache.delete("killboard:newbro_soften")
    assert danger_rating(9, 1)["label"] == "Dangerous"
    assert danger_rating(6, 4)["label"] == "Risky"
    assert danger_rating(0, 0)["label"] == "Untested"


# --- milestone scan ----------------------------------------------------------
def _recent_alerts(user):
    return Alert.objects.filter(source_service="killboard",
                                audience={"kind": "user", "id": user.id})


def test_recent_first_kill_recorded_and_celebrated(django_user_model):
    user, _ = enrol_pilot(django_user_model, 6001)
    home_kill(1, attackers=[(6001, HOME_CORP, True)], when=timezone.now() - timedelta(hours=2))
    sent = scan_milestones()
    kinds = set(PilotMilestone.objects.filter(character_id=6001).values_list("kind", flat=True))
    assert "first_kill" in kinds and "first_final_blow" in kinds
    assert sent >= 1
    assert _recent_alerts(user).exists()


def test_old_first_kill_recorded_but_not_celebrated(django_user_model):
    user, _ = enrol_pilot(django_user_model, 6002)
    home_kill(2, attackers=[(6002, HOME_CORP, False)], when=timezone.now() - timedelta(days=90))
    sent = scan_milestones()
    assert PilotMilestone.objects.filter(character_id=6002, kind="first_kill").exists()
    assert sent == 0  # too old to celebrate
    assert not _recent_alerts(user).exists()


def test_milestone_scan_is_idempotent(django_user_model):
    enrol_pilot(django_user_model, 6003)
    home_kill(3, attackers=[(6003, HOME_CORP, True)], when=timezone.now() - timedelta(hours=1))
    scan_milestones()
    n = PilotMilestone.objects.filter(character_id=6003).count()
    scan_milestones()
    assert PilotMilestone.objects.filter(character_id=6003).count() == n  # no duplicates


def test_solo_milestone(django_user_model):
    enrol_pilot(django_user_model, 6004)
    home_kill(4, attackers=[(6004, HOME_CORP, True)], is_solo=True,
              when=timezone.now() - timedelta(hours=1))
    scan_milestones()
    kinds = set(PilotMilestone.objects.filter(character_id=6004).values_list("kind", flat=True))
    assert {"first_kill", "first_solo", "first_final_blow"} <= kinds


def test_disabled_event_records_nothing(django_user_model):
    enrol_pilot(django_user_model, 6005)
    home_kill(5, attackers=[(6005, HOME_CORP, True)], when=timezone.now() - timedelta(hours=1))
    config.set("notifications", {"events": {"killboard.newbro_milestone": {"enabled": False}}})
    assert scan_milestones() == 0
    assert not PilotMilestone.objects.filter(character_id=6005).exists()


def test_unlinked_character_skipped(django_user_model):
    # a home-corp character with no linked user is not scanned
    EveCharacter.objects.create(character_id=7777, name="Detached", is_corp_member=True)
    home_kill(6, attackers=[(7777, HOME_CORP, True)], when=timezone.now() - timedelta(hours=1))
    scan_milestones()
    assert not PilotMilestone.objects.filter(character_id=7777).exists()


def test_milestones_for_returns_pilot_records(django_user_model):
    enrol_pilot(django_user_model, 6006)
    home_kill(7, attackers=[(6006, HOME_CORP, True)], when=timezone.now() - timedelta(hours=1))
    scan_milestones()
    assert len(milestones_for([6006])) >= 1
    assert milestones_for([]) == []


# --- console toggle ----------------------------------------------------------
def test_newbro_settings_saves_and_is_director_only(client, django_user_model):
    member = django_user_model.objects.create(username="u-member")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(member)
    assert client.post(reverse("admin_audit:newbro_settings"),
                       {"soften_below_events": 30}).status_code in (403, 302)

    director = django_user_model.objects.create(username="u-director")
    RoleAssignment.objects.create(user=director, role=ensure_role(rbac.ROLE_DIRECTOR))
    client.force_login(director)
    resp = client.post(reverse("admin_audit:newbro_settings"),
                       {"soften_danger_label": "on", "soften_below_events": 30})
    assert resp.status_code == 302
    assert NewbroConfig.load().soften_below_events == 30
