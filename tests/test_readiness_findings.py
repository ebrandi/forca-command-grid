"""Phase 2 — ReadinessFinding upsert + finding→task workflow + officer queue."""
from __future__ import annotations

import pytest

from apps.characters.models import CharacterSkillSnapshot
from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit, SkillRequirement
from apps.identity.models import RoleAssignment
from apps.readiness import config
from apps.readiness.models import ReadinessFinding
from apps.readiness.services import compute_readiness
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from apps.tasks.models import Task
from core import rbac

GUNNERY = 3300
RIFTER = 587


def _doctrine(name="Core", priority=100, req_level=3):
    cat, _ = DoctrineCategory.objects.get_or_create(key="dps", label="DPS")
    d = Doctrine.objects.create(name=name, category=cat, priority=priority)
    fit = DoctrineFit.objects.create(doctrine=d, name=name, ship_type_id=RIFTER)
    SkillRequirement.objects.create(fit=fit, skill_type_id=GUNNERY, min_level=req_level, optimal_level=req_level)
    return d


def _char(django_user_model, cid, gunnery_level):
    user = django_user_model.objects.create(username=f"eve:{cid}")
    ch = EveCharacter.objects.create(character_id=cid, user=user, name=f"P{cid}", is_corp_member=True)
    CharacterSkillSnapshot.objects.create(
        character=ch, is_latest=True, skills={str(GUNNERY): {"trained_level": gunnery_level, "sp": 0}}
    )
    return ch


def _undercovered_doctrine(django_user_model, name="Core"):
    """A doctrine 1-of-2 pilots can fly → emits one doctrine gap finding."""
    d = _doctrine(name)
    _char(django_user_model, 9001, 5)  # can fly
    _char(django_user_model, 9002, 1)  # cannot
    return d


def _officer(django_user_model, username="off"):
    user = django_user_model.objects.create(username=username)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_OFFICER))
    return user


# --- finding upsert & lifecycle ----------------------------------------------
@pytest.mark.django_db
def test_findings_upsert_with_age_and_owner(django_user_model, sde):
    d = _undercovered_doctrine(django_user_model)
    compute_readiness(persist=True, use_cache=False)

    finding = ReadinessFinding.objects.get(dimension_key="doctrine", ref_id=str(d.id))
    assert finding.status == ReadinessFinding.Status.OPEN
    assert finding.weight == 50.0  # round(100 · (1 - 1/2))
    # Owner resolved from the default dimension_owner map (doctrine → training_officer).
    assert finding.owner_tag == "training_officer"
    first_seen = finding.first_seen

    # Re-run: same gap → same row updated in place (no duplicate), age preserved.
    compute_readiness(persist=True, use_cache=False)
    assert ReadinessFinding.objects.filter(dimension_key="doctrine", ref_id=str(d.id)).count() == 1
    finding.refresh_from_db()
    assert finding.first_seen == first_seen
    assert finding.last_seen >= first_seen


@pytest.mark.django_db
def test_finding_resolves_when_gap_clears_then_reopens(django_user_model, sde):
    d = _undercovered_doctrine(django_user_model)
    compute_readiness(persist=True, use_cache=False)
    finding = ReadinessFinding.objects.get(dimension_key="doctrine", ref_id=str(d.id))
    assert finding.status == ReadinessFinding.Status.OPEN

    # Train the lagging pilot so the gap clears → finding resolves (not deleted).
    snap = CharacterSkillSnapshot.objects.get(character_id=9002)
    snap.skills = {str(GUNNERY): {"trained_level": 5, "sp": 0}}
    snap.save()
    compute_readiness(persist=True, use_cache=False)
    finding.refresh_from_db()
    assert finding.status == ReadinessFinding.Status.RESOLVED

    # Gap returns (pilot un-trains) → the SAME row flips back to open.
    snap.skills = {str(GUNNERY): {"trained_level": 1, "sp": 0}}
    snap.save()
    compute_readiness(persist=True, use_cache=False)
    finding.refresh_from_db()
    assert finding.status == ReadinessFinding.Status.OPEN
    assert ReadinessFinding.objects.filter(dimension_key="doctrine", ref_id=str(d.id)).count() == 1


# --- manual finding → task ---------------------------------------------------
@pytest.mark.django_db
def test_manual_create_task_from_finding_is_idempotent(client, django_user_model, sde):
    d = _undercovered_doctrine(django_user_model)
    compute_readiness(persist=True, use_cache=False)
    finding = ReadinessFinding.objects.get(dimension_key="doctrine", ref_id=str(d.id))

    client.force_login(_officer(django_user_model))
    client.post("/readiness/gap/task/", {"finding_id": str(finding.id)})
    client.post("/readiness/gap/task/", {"finding_id": str(finding.id)})  # idempotent

    tasks = Task.objects.filter(related_type="readiness", related_id=str(finding.id))
    assert tasks.count() == 1
    task = tasks.first()
    assert task.type == Task.Type.TRAIN  # finding.task_type
    finding.refresh_from_db()
    assert finding.task_id == task.id  # back-linked


@pytest.mark.django_db
def test_acknowledged_finding_reopens_when_gap_persists(django_user_model, sde):
    """A 'done' task acknowledges its finding, but if the next compute still measures
    the gap (the work didn't actually fix it) the finding returns to OPEN — eligible
    for a fresh task."""
    from apps.readiness.tasks_bridge import task_for_finding

    d = _undercovered_doctrine(django_user_model)
    compute_readiness(persist=True, use_cache=False)
    finding = ReadinessFinding.objects.get(dimension_key="doctrine", ref_id=str(d.id))
    task = task_for_finding(finding, user=None)
    task.status = Task.Status.DONE
    task.save()
    finding.refresh_from_db()
    assert finding.status == ReadinessFinding.Status.ACKNOWLEDGED

    # The gap is still present (pilot 9002 still can't fly) → reopen.
    compute_readiness(persist=True, use_cache=False)
    finding.refresh_from_db()
    assert finding.status == ReadinessFinding.Status.OPEN


@pytest.mark.django_db
def test_provider_failure_does_not_resolve_its_findings(django_user_model, sde, monkeypatch):
    """Provider isolation: a dimension whose provider RAISES this run emits nothing,
    but its existing findings must NOT be auto-resolved (a transient failure can't
    wipe the risk register)."""
    d = _undercovered_doctrine(django_user_model)
    compute_readiness(persist=True, use_cache=False)
    finding = ReadinessFinding.objects.get(dimension_key="doctrine", ref_id=str(d.id))
    assert finding.status == ReadinessFinding.Status.OPEN

    def boom(ctx):
        raise RuntimeError("doctrine provider down")

    monkeypatch.setattr("apps.readiness.dimensions.doctrine.get_doctrine_skill", boom)
    compute_readiness(persist=True, use_cache=False)  # doctrine/skill providers isolated
    finding.refresh_from_db()
    assert finding.status == ReadinessFinding.Status.OPEN  # untouched, not resolved


@pytest.mark.django_db
def test_task_done_acknowledges_finding(django_user_model, sde):
    from apps.readiness.tasks_bridge import task_for_finding

    d = _undercovered_doctrine(django_user_model)
    compute_readiness(persist=True, use_cache=False)
    finding = ReadinessFinding.objects.get(dimension_key="doctrine", ref_id=str(d.id))
    task = task_for_finding(finding, user=None)

    task.status = Task.Status.DONE
    task.save()  # post_save signal → finding acknowledged

    finding.refresh_from_db()
    assert finding.status == ReadinessFinding.Status.ACKNOWLEDGED


# --- generate_tasks beat -----------------------------------------------------
@pytest.mark.django_db
def test_generate_tasks_is_inert_without_rules(django_user_model, sde):
    from apps.readiness.tasks import generate_tasks

    _undercovered_doctrine(django_user_model)
    compute_readiness(persist=True, use_cache=False)
    assert generate_tasks() == 0  # no alert rules configured → nothing auto-tasked
    assert not Task.objects.filter(related_type="readiness").exists()


@pytest.mark.django_db
def test_generate_tasks_creates_when_rule_opts_in(django_user_model, sde):
    from apps.readiness.tasks import generate_tasks

    d = _undercovered_doctrine(django_user_model)
    compute_readiness(persist=True, use_cache=False)
    config.set("alerts", {"rules": [
        {"key": "doctrine_gap", "match": {"dimension": "doctrine"}, "generate_task": True}
    ]}, user=None)

    assert generate_tasks() == 1
    finding = ReadinessFinding.objects.get(dimension_key="doctrine", ref_id=str(d.id))
    assert finding.task_id is not None
    # Idempotent: re-running doesn't double-create (the task FK / active guard).
    assert generate_tasks() == 0
    assert Task.objects.filter(related_type="readiness").count() == 1


@pytest.mark.django_db
def test_generate_tasks_cancels_resolved_untouched_task(django_user_model, sde):
    from apps.readiness.tasks import generate_tasks

    d = _undercovered_doctrine(django_user_model)
    compute_readiness(persist=True, use_cache=False)
    config.set("alerts", {"rules": [
        {"key": "doctrine_gap", "match": {"dimension": "doctrine"}, "generate_task": True}
    ]}, user=None)
    generate_tasks()
    finding = ReadinessFinding.objects.get(dimension_key="doctrine", ref_id=str(d.id))
    task = finding.task
    assert task.status == Task.Status.OPEN

    # Clear the gap → finding resolves → generate_tasks cancels the untouched task.
    snap = CharacterSkillSnapshot.objects.get(character_id=9002)
    snap.skills = {str(GUNNERY): {"trained_level": 5, "sp": 0}}
    snap.save()
    compute_readiness(persist=True, use_cache=False)
    generate_tasks()
    task.refresh_from_db()
    assert task.status == Task.Status.CANCELLED


# --- officer surfaces --------------------------------------------------------
@pytest.mark.django_db
def test_findings_register_is_officer_only(client, django_user_model, sde):
    member = django_user_model.objects.create(username="m")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(member)
    assert client.get("/readiness/findings/").status_code == 403


@pytest.mark.django_db
def test_findings_register_and_queue_render(client, django_user_model, sde):
    d = _undercovered_doctrine(django_user_model)
    compute_readiness(persist=True, use_cache=False)
    finding = ReadinessFinding.objects.get(dimension_key="doctrine", ref_id=str(d.id))

    client.force_login(_officer(django_user_model, "off2"))
    reg = client.get("/readiness/findings/").content.decode()
    assert finding.title in reg and "Create task" in reg

    # Make a task so the queue groups it under the owner desk.
    client.post("/readiness/gap/task/", {"finding_id": str(finding.id)})
    queue = client.get("/readiness/tasks/").content.decode()
    assert "Training Officer" in queue  # default owner label for the doctrine dimension
