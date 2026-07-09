"""My Skills & Training page — the in-game skill-queue overview (read-only via ESI).

ESI has no skill-queue *write* endpoint, so this surfaces the imported queue and the
clipboard "copy to in-game planner" path; these tests cover the queue parsing
(currently-training detection + progress, ETA, idle) and the redesigned page render.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.utils import timezone

from apps.characters.models import CharacterSkillSnapshot, SkillQueueSnapshot
from apps.identity.models import RoleAssignment
from apps.sde.models import SdeType
from apps.skills.overview import character_training
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac

GUNNERY, DRONES, MISSILES = 3300, 3436, 3319


def _pilot(django_user_model, cid=7001):
    user = django_user_model.objects.create(username=f"eve:{cid}")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    ch = EveCharacter.objects.create(character_id=cid, name=f"P{cid}", is_main=True,
                                     is_corp_member=True, user=user)
    return user, ch


def _iso(dtobj):
    return dtobj.strftime("%Y-%m-%dT%H:%M:%SZ")


def _names():
    from apps.sde.models import SdeCategory, SdeGroup

    cat, _ = SdeCategory.objects.get_or_create(category_id=16, defaults={"name": "Skill"})
    grp, _ = SdeGroup.objects.get_or_create(group_id=255, defaults={"category": cat, "name": "Gunnery"})
    for tid, name in [(GUNNERY, "Gunnery"), (DRONES, "Drones"), (MISSILES, "Missile Launcher Operation")]:
        SdeType.objects.get_or_create(type_id=tid, defaults={"name": name, "group": grp, "published": True})


@pytest.mark.django_db
def test_queue_parsing_current_progress_and_eta(django_user_model):
    _names()
    _, ch = _pilot(django_user_model)
    now = timezone.now()
    CharacterSkillSnapshot.objects.create(
        character=ch, is_latest=True, total_sp=5_000_000,
        skills={str(GUNNERY): {"trained_level": 5, "sp": 0}, str(DRONES): {"trained_level": 3, "sp": 0}},
    )
    SkillQueueSnapshot.objects.create(character=ch, is_latest=True, entries=[
        # already finished since last sync → excluded
        {"skill_id": GUNNERY, "finished_level": 4, "queue_position": 0,
         "start_date": _iso(now - dt.timedelta(days=3)), "finish_date": _iso(now - dt.timedelta(days=1))},
        # currently training: started 1d ago, finishes in 1d → ~50% progress
        {"skill_id": DRONES, "finished_level": 4, "queue_position": 1,
         "start_date": _iso(now - dt.timedelta(days=1)), "finish_date": _iso(now + dt.timedelta(days=1))},
        # queued next
        {"skill_id": MISSILES, "finished_level": 5, "queue_position": 2,
         "start_date": _iso(now + dt.timedelta(days=1)), "finish_date": _iso(now + dt.timedelta(days=4))},
    ])
    t = character_training(ch, now=now)
    assert t["is_training"] is True and t["is_empty_queue"] is False
    assert t["total_sp"] == 5_000_000 and t["n_skills"] == 2 and t["n_at_v"] == 1
    # the finished entry is dropped; two remain, the current one first
    assert [q["name"] for q in t["queue"]] == ["Drones", "Missile Launcher Operation"]
    assert t["current"]["name"] == "Drones" and t["current"]["level"] == "IV"
    assert 40 <= t["current"]["progress"] <= 60  # ~halfway
    # queue empties at the last entry's finish (~4 days out)
    assert t["queue_remaining_seconds"] > 3 * 86400


@pytest.mark.django_db
def test_empty_queue_is_idle(django_user_model):
    _, ch = _pilot(django_user_model, 7002)
    CharacterSkillSnapshot.objects.create(character=ch, is_latest=True, total_sp=1, skills={})
    SkillQueueSnapshot.objects.create(character=ch, is_latest=True, entries=[])
    t = character_training(ch)
    assert t["is_empty_queue"] is True and t["is_training"] is False and t["current"] is None


@pytest.mark.django_db
def test_no_queue_data(django_user_model):
    _, ch = _pilot(django_user_model, 7003)
    t = character_training(ch)
    assert t["has_queue_data"] is False and t["has_skills"] is False


@pytest.mark.django_db
def test_page_renders_queue_and_copy(client, django_user_model):
    _names()
    user, ch = _pilot(django_user_model, 7004)
    now = timezone.now()
    CharacterSkillSnapshot.objects.create(character=ch, is_latest=True, total_sp=9_000_000, skills={})
    SkillQueueSnapshot.objects.create(character=ch, is_latest=True, entries=[
        {"skill_id": DRONES, "finished_level": 5, "queue_position": 0,
         "start_date": _iso(now - dt.timedelta(hours=1)), "finish_date": _iso(now + dt.timedelta(days=2))},
    ])
    client.force_login(user)
    # Skills & training is consolidated onto the per-character page.
    html = client.get(f"/characters/{ch.character_id}/").content.decode()
    assert "In-game training queue" in html
    assert "Now training" in html
    assert "Drones" in html
    assert "Read-only from ESI" in html
    # the doctrine list framing is gone; points at the Doctrines page instead
    assert "Doctrines" in html


@pytest.mark.django_db
def test_skills_url_redirects_to_character_page(client, django_user_model):
    """Consolidation: /skills/ now sends the member to their main character's page."""
    user, ch = _pilot(django_user_model, 7006)
    client.force_login(user)
    resp = client.get("/skills/")
    assert resp.status_code == 302
    assert resp.headers["Location"] == f"/characters/{ch.character_id}/"


@pytest.mark.django_db
def test_character_page_groups_skills(client, django_user_model):
    _names()
    user, ch = _pilot(django_user_model, 7007)
    CharacterSkillSnapshot.objects.create(
        character=ch, is_latest=True, total_sp=6_000_000,
        skills={str(GUNNERY): {"trained_level": 5, "sp": 0}, str(DRONES): {"trained_level": 3, "sp": 0}},
    )
    client.force_login(user)
    html = client.get(f"/characters/{ch.character_id}/").content.decode()
    assert "Trained skills" in html
    assert "Gunnery" in html and "Drones" in html  # grouped + listed
    # each collapsible group carries the disclosure-chevron affordance, so a group
    # visibly reads as expand/collapsible (guards the bare-summary regression where
    # the flex <summary> suppressed the native marker and nothing signalled a toggle)
    assert "disclosure-chevron" in html


@pytest.mark.django_db
def test_idle_warning_on_page(client, django_user_model):
    user, ch = _pilot(django_user_model, 7005)
    CharacterSkillSnapshot.objects.create(character=ch, is_latest=True, total_sp=1, skills={})
    SkillQueueSnapshot.objects.create(character=ch, is_latest=True, entries=[])
    client.force_login(user)
    html = client.get(f"/characters/{ch.character_id}/").content.decode()
    assert "training queue is empty" in html.lower()
