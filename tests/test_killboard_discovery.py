"""Periodic home-corp killmail discovery (zKill intraday + ESI corp authoritative).

Regression guard for the bug where neither path was scheduled, so the killboard
silently stopped importing recent corp kills.
"""
from __future__ import annotations

import pytest
from django.test import override_settings

from apps.killboard import tasks

HOME = 98028546


def test_both_periodic_imports_are_scheduled():
    """The two corp-feed tasks must be in the beat schedule (the original bug was
    that they existed as tasks but were never scheduled)."""
    from config.celery import app

    scheduled = {entry["task"] for entry in app.conf.beat_schedule.values()}
    assert "killboard.import_home_corp_from_zkill" in scheduled
    assert "killboard.discover_home_corp_killmails" in scheduled


@override_settings(FORCA_HOME_CORP_ID=HOME)
def test_zkill_import_targets_home_corp(monkeypatch):
    calls = []
    monkeypatch.setattr(tasks, "import_from_zkill", lambda et, eid: calls.append((et, eid)) or 3)
    assert tasks.import_home_corp_from_zkill() == 3
    assert calls == [("corporation", HOME)]


@override_settings(FORCA_HOME_CORP_ID=HOME)
def test_zkill_import_swallows_errors(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("zkill 503")

    monkeypatch.setattr(tasks, "import_from_zkill", boom)
    # A zKill outage must not raise out of the beat cycle.
    assert tasks.import_home_corp_from_zkill() == 0


@override_settings(FORCA_HOME_CORP_ID=0)
def test_zkill_import_noop_without_home_corp(monkeypatch):
    called = []
    monkeypatch.setattr(tasks, "import_from_zkill", lambda *a: called.append(1))
    assert tasks.import_home_corp_from_zkill() == 0
    assert called == []


@override_settings(FORCA_HOME_CORP_ID=HOME)
def test_esi_corp_discovery_runs_when_director_present(monkeypatch):
    class FakeDirector:
        character_id = 4242

    monkeypatch.setattr(tasks, "_find_corp_killmail_director", lambda corp: FakeDirector())
    calls = []
    monkeypatch.setattr(
        tasks, "discover_corporation_killmails",
        lambda corp, director_id: calls.append((corp, director_id)) or 7,
    )
    assert tasks.discover_home_corp_killmails() == 7
    assert calls == [(HOME, 4242)]


@override_settings(FORCA_HOME_CORP_ID=HOME)
def test_esi_corp_discovery_noop_without_director(monkeypatch):
    monkeypatch.setattr(tasks, "_find_corp_killmail_director", lambda corp: None)
    called = []
    monkeypatch.setattr(tasks, "discover_corporation_killmails",
                        lambda corp, did: called.append(1))
    assert tasks.discover_home_corp_killmails() == 0
    assert called == []


@pytest.mark.django_db
@override_settings(FORCA_HOME_CORP_ID=HOME)
def test_director_finder_skips_chars_without_the_scope(monkeypatch):
    from apps.corporation.models import EveCorporation
    from apps.sso.models import AuthToken, EveCharacter
    from apps.sso.token_service import NoValidToken

    EveCorporation.objects.create(corporation_id=HOME, name="Home")
    # One member has a token but not the corp-killmails scope, one has it.
    EveCharacter.objects.create(character_id=1, name="NoScope",
                                corporation_id=HOME, is_corp_member=True)
    has_scope = EveCharacter.objects.create(character_id=2, name="Director",
                                            corporation_id=HOME, is_corp_member=True)
    AuthToken.objects.create(character_id=1)
    AuthToken.objects.create(character_id=2)

    def fake_token(character, scopes):
        if character.character_id == 2:
            return "access"
        raise NoValidToken("missing scope")

    monkeypatch.setattr(tasks, "get_valid_access_token", fake_token)
    # The scoped member is also a Director, so it is selected.
    monkeypatch.setattr(tasks, "character_is_corp_director", lambda c, *a, **k: True)
    found = tasks._find_corp_killmail_director(HOME)
    assert found is not None and found.character_id == has_scope.character_id


@pytest.mark.django_db
@override_settings(FORCA_HOME_CORP_ID=HOME)
def test_director_finder_requires_director_role(monkeypatch):
    """Regression: ``esi-killmails.read_corporation_killmails.v1`` is a DEFAULT scope
    every member grants at login, so the selector must verify the in-game Director
    ROLE — not just the scope — or it picks the first (non-Director) member and CCP
    answers 403 'does not have required role(s)'. The Director must win even when a
    scope-holding non-Director is iterated first.
    """
    from apps.corporation.models import EveCorporation
    from apps.sso.models import AuthToken, EveCharacter

    EveCorporation.objects.create(corporation_id=HOME, name="Home")
    # Iterated first: holds the (universal) scope but is NOT a Director — must be skipped.
    EveCharacter.objects.create(character_id=1, name="ScopedMember",
                                corporation_id=HOME, is_corp_member=True)
    director = EveCharacter.objects.create(character_id=2, name="RealDirector",
                                           corporation_id=HOME, is_corp_member=True)
    AuthToken.objects.create(character_id=1)
    AuthToken.objects.create(character_id=2)

    # Both carry the default killmails scope (as in production).
    monkeypatch.setattr(tasks, "get_valid_access_token", lambda c, scopes: "access")
    # Only character 2 actually holds the Director role.
    monkeypatch.setattr(tasks, "character_is_corp_director",
                        lambda c, *a, **k: c.character_id == 2)

    found = tasks._find_corp_killmail_director(HOME)
    assert found is not None and found.character_id == director.character_id


def test_auto_cluster_beat_is_scheduled():
    """KB-12: the battle auto-clustering task must be registered in the schedule
    (the task-exists-but-unscheduled bug guard applies here too)."""
    from config.celery import app

    scheduled = {entry["task"] for entry in app.conf.beat_schedule.values()}
    assert "killboard.auto_cluster_battles" in scheduled


@pytest.mark.django_db
@override_settings(FORCA_HOME_CORP_ID=HOME)
def test_auto_cluster_battles_creates_and_dedups(sde):
    """KB-12: a system with a recent home-corp cluster gets a battle report, and
    re-running is idempotent (no duplicate report for the same system+window)."""
    from datetime import timedelta

    from django.utils import timezone

    from apps.killboard.ingest import ingest_killmail
    from apps.killboard.models import BattleReport

    recent = (timezone.now() - timedelta(hours=1)).isoformat()
    for i in range(5):
        ingest_killmail(700000 + i, f"h{i}", body={
            "killmail_id": 700000 + i, "killmail_time": recent, "solar_system_id": 30002053,
            "victim": {"corporation_id": HOME, "ship_type_id": 587, "items": []},
            "attackers": [{"character_id": 1, "corporation_id": 99}],
        })

    assert tasks.auto_cluster_battles() == 1
    assert BattleReport.objects.get().system_ids == [30002053]
    # A system already covered in this window is skipped on re-run.
    assert tasks.auto_cluster_battles() == 0
    assert BattleReport.objects.count() == 1
