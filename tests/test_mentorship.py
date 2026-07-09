"""Mentorship Program: business rules & security boundaries.

Covers eligibility, registration/approval, matching, the pairing lifecycle, every
task validation method, the reward workflow (dedupe, caps, cooldown, SoD, points
crediting), anomaly detection, and the RBAC/IDOR view guards. The create-only seed
migration runs in the test DB, so the 12 tracks / 51 exercises / reward rules are
present.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.utils import timezone

from apps.identity.models import RoleAssignment
from apps.mentorship import matching, rewards, services, trust, workflow
from apps.mentorship.models import (
    MenteeProfile,
    MentorProfile,
    MentorshipPairing,
    MentorshipRewardLedger,
    MentorshipTask,
    MentorshipTaskAssignment,
    MentorshipTrack,
)
from apps.pilots.models import ContributionEvent
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac

_A = MentorshipTaskAssignment.Status
_V = MentorshipTask.Validation


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _no_esi(monkeypatch):
    """Never touch the network for eligibility facts in tests."""
    monkeypatch.setattr(
        "apps.mentorship.eligibility._fetch_facts",
        lambda ch: {"age_days": 400, "tenure_days": 200, "confidence": "high", "source": "esi"},
    )


def _member(dum, suffix, cid=None):
    user, _ = dum.objects.get_or_create(username=f"m-{suffix}")
    RoleAssignment.objects.get_or_create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.get_or_create(
        character_id=cid or (700000 + abs(hash(suffix)) % 100000),
        defaults={"user": user, "name": suffix, "is_main": True, "is_corp_member": True},
    )
    return user


def _officer(dum, suffix="off"):
    user, _ = dum.objects.get_or_create(username=f"o-{suffix}")
    RoleAssignment.objects.get_or_create(user=user, role=ensure_role(rbac.ROLE_OFFICER))
    return user


def _mentor(user, **kw):
    p, _ = MentorProfile.objects.get_or_create(
        user=user, defaults={"status": MentorProfile.Status.ACTIVE, **kw})
    if p.status != MentorProfile.Status.ACTIVE:
        p.status = MentorProfile.Status.ACTIVE
        p.save()
    for k, v in kw.items():
        setattr(p, k, v)
    p.save()
    return p


def _mentee(user, **kw):
    p, _ = MenteeProfile.objects.get_or_create(
        user=user, defaults={"status": MenteeProfile.Status.ACTIVE, **kw})
    p.status = MenteeProfile.Status.ACTIVE
    for k, v in kw.items():
        setattr(p, k, v)
    p.save()
    return p


def _active_pairing(dum, mentor_suffix="mtr", mentee_suffix="cdt"):
    mentor = _mentor(_member(dum, mentor_suffix, cid=800001))
    mentee = _mentee(_member(dum, mentee_suffix, cid=800002))
    pairing = services.propose_pairing(
        mentor, mentee, initiated_by=MentorshipPairing.InitiatedBy.LEADER,
        status=MentorshipPairing.Status.PENDING_APPROVAL)
    services.approve_pairing(pairing, _officer(dum))
    pairing.refresh_from_db()
    return pairing


def _assignment(pairing, task_key):
    return pairing.assignments.get(task__key=task_key)


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_mentor_eligibility_either_vs_both(django_user_model, monkeypatch):
    from apps.mentorship import eligibility

    program = services.active_program()
    user = _member(django_user_model, "elig")
    # Old character (age ok) but short tenure.
    monkeypatch.setattr(eligibility, "_fetch_facts",
                        lambda ch: {"age_days": 400, "tenure_days": 30,
                                    "confidence": "high", "source": "esi"})
    program.mentor_eligibility_logic = program.EligibilityLogic.EITHER
    program.save()
    assert eligibility.evaluate(user, program, "mentor")["eligible"] is True
    program.mentor_eligibility_logic = program.EligibilityLogic.BOTH
    program.save()
    assert eligibility.evaluate(user, program, "mentor")["eligible"] is False


@pytest.mark.django_db
def test_mentee_eligibility_tenure_cap(django_user_model, monkeypatch):
    from apps.mentorship import eligibility

    program = services.active_program()
    user = _member(django_user_model, "cadet")
    monkeypatch.setattr(eligibility, "_fetch_facts",
                        lambda ch: {"age_days": 100, "tenure_days": 30,
                                    "confidence": "high", "source": "esi"})
    assert eligibility.evaluate(user, program, "mentee")["eligible"] is True
    monkeypatch.setattr(eligibility, "_fetch_facts",
                        lambda ch: {"age_days": 100, "tenure_days": 200,
                                    "confidence": "high", "source": "esi"})
    assert eligibility.evaluate(user, program, "mentee")["eligible"] is False


# ---------------------------------------------------------------------------
# Registration & approval
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_mentor_registration_respects_approval_flag(django_user_model):
    program = services.active_program()
    program.mentor_requires_approval = True
    program.save()
    user = _member(django_user_model, "reg1")
    profile = services.register_mentor(user, {"areas": ["pvp"], "timezone": "EU"})
    assert profile.status == MentorProfile.Status.PENDING
    assert services.approve_mentor(profile, _officer(django_user_model))
    profile.refresh_from_db()
    assert profile.status == MentorProfile.Status.ACTIVE


@pytest.mark.django_db
def test_mentee_auto_active_when_no_approval(django_user_model):
    program = services.active_program()
    program.mentee_requires_approval = False
    program.save()
    profile = services.register_mentee(_member(django_user_model, "reg2"), {"goals": ["mining"]})
    assert profile.status == MenteeProfile.Status.ACTIVE


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_matching_scores_overlap_and_excludes_full_mentors(django_user_model):
    mentor = _mentor(_member(django_user_model, "mx"), areas=["pvp", "fleet"],
                     timezone="EU", languages=["English"])
    mentee = _mentee(_member(django_user_model, "cx"), goals=["pvp"], timezone="EU",
                     languages=["English"])
    s, reasons = matching.score(mentor, mentee)
    assert s > 50 and any("focus" in r.lower() for r in reasons)
    # A full mentor is excluded from suggestions.
    program = services.active_program()
    program.max_active_mentees_per_mentor = 0
    program.save()
    assert matching.suggest_mentors_for(mentee) == []


# ---------------------------------------------------------------------------
# Pairing lifecycle
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_propose_is_idempotent(django_user_model):
    mentor = _mentor(_member(django_user_model, "p1m"))
    mentee = _mentee(_member(django_user_model, "p1c"))
    first = services.propose_pairing(mentor, mentee,
                                     initiated_by=MentorshipPairing.InitiatedBy.LEADER)
    dup = services.propose_pairing(mentor, mentee,
                                   initiated_by=MentorshipPairing.InitiatedBy.LEADER)
    assert first is not None and dup is None


@pytest.mark.django_db
def test_activation_enrols_core_tracks_and_materialises_tasks(django_user_model):
    pairing = _active_pairing(django_user_model)
    assert pairing.status == MentorshipPairing.Status.ACTIVE
    core = MentorshipTrack.objects.filter(active=True, is_core=True).count()
    assert pairing.enrollments.count() == core
    # Every active task in a core track has an assignment.
    assert pairing.assignments.count() > 0
    assert pairing.events.filter(to_status="active").exists()


@pytest.mark.django_db
def test_capacity_guard_blocks_activation(django_user_model):
    program = services.active_program()
    program.max_active_mentees_per_mentor = 1
    program.save()
    mentor = _mentor(_member(django_user_model, "capm"))
    m1 = _mentee(_member(django_user_model, "cap1"))
    m2 = _mentee(_member(django_user_model, "cap2"))
    p1 = services.propose_pairing(mentor, m1, initiated_by=MentorshipPairing.InitiatedBy.LEADER,
                                  status=MentorshipPairing.Status.PENDING_APPROVAL)
    assert services.approve_pairing(p1, _officer(django_user_model)) is True
    p2 = services.propose_pairing(mentor, m2, initiated_by=MentorshipPairing.InitiatedBy.LEADER,
                                  status=MentorshipPairing.Status.PENDING_APPROVAL)
    # Mentor is now full → activation blocked.
    assert services.approve_pairing(p2, _officer(django_user_model)) is False


# ---------------------------------------------------------------------------
# Task validation methods
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_mentee_confirm_completes_immediately(django_user_model):
    pairing = _active_pairing(django_user_model)
    a = _assignment(pairing, "welcome-rules")  # mentee_confirm
    workflow.mentee_submit(a, pairing.mentee.user)
    a.refresh_from_db()
    assert a.status == _A.COMPLETED


@pytest.mark.django_db
def test_manual_mentor_needs_mentor_signoff(django_user_model):
    pairing = _active_pairing(django_user_model)
    a = _assignment(pairing, "client-review")  # manual_mentor, reward_eligible
    workflow.mentee_submit(a, pairing.mentee.user)
    a.refresh_from_db()
    assert a.status == _A.PENDING_MENTOR
    # Mentor confirms → completed.
    workflow.mentor_decide(a, pairing.mentor.user, approve=True)
    a.refresh_from_db()
    assert a.status == _A.COMPLETED


@pytest.mark.django_db
def test_dual_confirm_requires_both(django_user_model):
    pairing = _active_pairing(django_user_model)
    a = _assignment(pairing, "welcome-comms")  # dual_confirm
    workflow.mentee_submit(a, pairing.mentee.user)
    a.refresh_from_db()
    assert a.status == _A.PENDING_MENTOR
    workflow.mentor_decide(a, pairing.mentor.user, approve=True)
    a.refresh_from_db()
    assert a.status == _A.COMPLETED


@pytest.mark.django_db
def test_auto_internal_passes_with_activity(django_user_model):
    pairing = _active_pairing(django_user_model)
    a = _assignment(pairing, "fleet-join")  # auto_internal fleet_attended
    # No fleet event yet → stays pending API.
    workflow.mentee_submit(a, pairing.mentee.user)
    a.refresh_from_db()
    assert a.status == _A.PENDING_API
    # Record a fleet contribution during the pairing window, then sweep.
    ContributionEvent.objects.create(
        user=pairing.mentee.user, kind=ContributionEvent.Kind.FLEET, magnitude=1,
        unit="fleets", ref_type="operation", ref_id="1", occurred_at=timezone.now())
    assert workflow.sweep_pending_api(a) is True
    a.refresh_from_db()
    assert a.status == _A.COMPLETED and a.rewardable is True


@pytest.mark.django_db
def test_prerequisites_block_submission(django_user_model):
    pairing = _active_pairing(django_user_model)
    from apps.mentorship.models import MentorshipTaskPrerequisite
    a = _assignment(pairing, "welcome-services")
    pre = _assignment(pairing, "welcome-rules")
    MentorshipTaskPrerequisite.objects.create(task=a.task, requires=pre.task)
    assert workflow.mentee_submit(a, pairing.mentee.user) == "blocked"
    # Complete the prerequisite, then it unblocks.
    workflow.mentee_submit(pre, pairing.mentee.user)
    assert workflow.mentee_submit(a, pairing.mentee.user) != "blocked"


@pytest.mark.django_db
def test_unverified_reward_gated_by_policy(django_user_model):
    program = services.active_program()
    program.esi_validation_required = True
    program.save()
    pairing = _active_pairing(django_user_model)
    a = _assignment(pairing, "client-review")  # manual_mentor, reward_eligible, no auto-check
    workflow.mentee_submit(a, pairing.mentee.user)
    workflow.mentor_decide(a, pairing.mentor.user, approve=True)
    a.refresh_from_db()
    # Reward-eligible but unverifiable under esi_validation_required → not rewardable.
    assert a.status == _A.COMPLETED_UNREWARDABLE and a.rewardable is False


# ---------------------------------------------------------------------------
# Rewards
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_task_reward_grants_points_and_dedupes(django_user_model):
    pairing = _active_pairing(django_user_model)
    a = _assignment(pairing, "fleet-join")
    ContributionEvent.objects.create(
        user=pairing.mentee.user, kind=ContributionEvent.Kind.FLEET, magnitude=1,
        unit="fleets", ref_type="operation", ref_id="9", occurred_at=timezone.now())
    workflow.mentee_submit(a, pairing.mentee.user)
    workflow.sweep_pending_api(a)
    a.refresh_from_db()
    # 'first-fleet' rule (points) should have granted to the mentee.
    ledger = MentorshipRewardLedger.objects.filter(
        recipient=pairing.mentee.user, rule_key="first-fleet")
    assert ledger.count() == 1
    entry = ledger.first()
    assert entry.status == MentorshipRewardLedger.Status.APPROVED and entry.points > 0
    # Points credited to the corp contribution ledger (idempotent).
    assert ContributionEvent.objects.filter(
        user=pairing.mentee.user, ref_type="mentorship_reward").exists()
    # Re-granting the same rule is a no-op (dedupe).
    rewards.on_task_completed(a)
    assert ledger.count() == 1


@pytest.mark.django_db
def test_isk_reward_separation_of_duties(django_user_model):
    from django.core.exceptions import PermissionDenied

    program = services.active_program()
    program.reward_mode = program.RewardMode.QUEUED
    program.save()
    pairing = _active_pairing(django_user_model)
    # graduate-isk fires on program completion (ISK, requires approval).
    granted = rewards.on_program_completed(pairing)
    isk = [e for e in granted if e.reward_type == "isk"]
    assert isk, "expected an ISK graduation reward"
    entry = isk[0]
    assert entry.status == MentorshipRewardLedger.Status.PENDING_APPROVAL
    # The recipient cannot approve their own reward.
    with pytest.raises(PermissionDenied):
        rewards.approve_reward(entry, entry.recipient)
    # An officer can.
    assert rewards.approve_reward(entry, _officer(django_user_model)) is True


@pytest.mark.django_db
def test_reward_cap_trims_isk(django_user_model):
    program = services.active_program()
    program.mentee_reward_cap_isk = Decimal("10000000")  # 10M cap
    program.save()
    pairing = _active_pairing(django_user_model)
    granted = rewards.on_program_completed(pairing)  # graduate-isk = 50M
    isk = [e for e in granted if e.reward_type == "isk"]
    assert isk and isk[0].amount == Decimal("10000000")  # trimmed to the cap


# ---------------------------------------------------------------------------
# Anomaly detection
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_self_confirm_streak_flag(django_user_model):
    pairing = _active_pairing(django_user_model)
    now = timezone.now()
    # Fabricate a streak of mentee-only completions.
    for a in pairing.assignments.all()[:6]:
        a.status = _A.COMPLETED
        a.completed_at = now
        a.save()
        a.validations.create(source="mentee", result="pass", confidence=30)
    trust.scan_pairing(pairing)
    assert pairing.flags.filter(kind="self_confirm_streak", resolved=False).exists()


# ---------------------------------------------------------------------------
# RBAC / IDOR view guards
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_non_participant_cannot_view_pairing(client, django_user_model):
    pairing = _active_pairing(django_user_model)
    intruder = _member(django_user_model, "intruder", cid=800099)
    client.force_login(intruder)
    resp = client.get(f"/mentorship/pair/{pairing.pk}/")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_mentee_cannot_sign_off_own_task(client, django_user_model):
    pairing = _active_pairing(django_user_model)
    a = _assignment(pairing, "client-review")
    workflow.mentee_submit(a, pairing.mentee.user)
    client.force_login(pairing.mentee.user)
    client.post(f"/mentorship/task/{a.pk}/action/", {"action": "confirm"})
    a.refresh_from_db()
    # Mentee's confirm attempt is rejected; the task stays pending the mentor.
    assert a.status == _A.PENDING_MENTOR


@pytest.mark.django_db
def test_feature_disabled_hides_landing(client, django_user_model):
    from core.features import set_disabled
    set_disabled(["mentorship"])
    user = _member(django_user_model, "ftr")
    client.force_login(user)
    assert client.get("/mentorship/").status_code == 404
    set_disabled([])  # re-enable for other tests


@pytest.mark.django_db
def test_landing_and_dashboard_render(client, django_user_model):
    user = _member(django_user_model, "render")
    client.force_login(user)
    assert client.get("/mentorship/").status_code == 200
    assert client.get("/mentorship/me/").status_code == 200
    assert client.get("/mentorship/tracks/").status_code == 200


@pytest.mark.django_db
def test_officer_only_reward_queue(client, django_user_model):
    member = _member(django_user_model, "plainmember", cid=800077)
    client.force_login(member)
    assert client.get("/ops/admin/mentorship/rewards/").status_code == 403
    client.force_login(_officer(django_user_model))
    assert client.get("/ops/admin/mentorship/rewards/").status_code == 200


def _director(dum):
    user, _ = dum.objects.get_or_create(username="dir-1")
    RoleAssignment.objects.get_or_create(user=user, role=ensure_role(rbac.ROLE_DIRECTOR))
    return user


@pytest.mark.django_db
def test_every_pilot_page_renders(client, django_user_model):
    """A rich scenario: render every pilot template without a 500."""
    pairing = _active_pairing(django_user_model)
    # Progress a couple of exercises and schedule a session.
    a = _assignment(pairing, "client-review")
    workflow.mentee_submit(a, pairing.mentee.user)
    services.schedule_session(pairing, actor=pairing.mentor.user, topic="First chat")
    # A pending reward + an anomaly flag for the officer view.
    rewards.on_program_completed(pairing)
    trust.scan_pairing(pairing)
    track = MentorshipTrack.objects.filter(active=True).first()

    client.force_login(pairing.mentee.user)
    for url in ["/mentorship/", "/mentorship/me/", "/mentorship/tracks/",
                f"/mentorship/tracks/{track.key}/", "/mentorship/mentors/",
                f"/mentorship/pair/{pairing.pk}/"]:
        assert client.get(url).status_code == 200, url


@pytest.mark.django_db
def test_every_admin_page_renders(client, django_user_model):
    pairing = _active_pairing(django_user_model)
    # A pending mentor + mentee application and a pending pairing for the queues.
    services.register_mentor(_member(django_user_model, "pendmtr", cid=810001),
                             {"areas": ["pvp"]})
    services.active_program().__class__.objects.update(mentor_requires_approval=True,
                                                       mentee_requires_approval=True)
    services.register_mentee(_member(django_user_model, "pendcdt", cid=810002), {"goals": ["mining"]})
    rewards.on_program_completed(pairing)
    trust.scan_pairing(pairing)
    track = MentorshipTrack.objects.filter(active=True).first()

    client.force_login(_director(django_user_model))
    for url in ["/ops/admin/mentorship/", "/ops/admin/mentorship/config/",
                "/ops/admin/mentorship/cohorts/", "/ops/admin/mentorship/tracks/",
                f"/ops/admin/mentorship/tracks/{track.pk}/build/",
                "/ops/admin/mentorship/rewards-config/", "/ops/admin/mentorship/approvals/",
                "/ops/admin/mentorship/matching/", "/ops/admin/mentorship/rewards/",
                "/ops/admin/mentorship/report/"]:
        assert client.get(url).status_code == 200, url


@pytest.mark.django_db
def test_propose_pairing_dms_the_counterparty(django_user_model, monkeypatch):
    """0.6/MEN-1: a proposed pairing notifies the counterparty per-pilot — a mentee's
    request DMs the mentor, a mentor's invite DMs the mentee, and an auto-suggested
    (SYSTEM) pairing DMs both, since neither pilot initiated it."""
    from apps.pingboard import services as pingboard

    audiences: list = []
    monkeypatch.setattr(pingboard, "emit_broadcast",
                        lambda **kw: audiences.append(kw.get("audience")) or None)

    def _pair(i):
        mtr = _mentor(_member(django_user_model, f"mtr{i}", cid=810000 + i * 2))
        cdt = _mentee(_member(django_user_model, f"cdt{i}", cid=810001 + i * 2))
        return mtr, cdt

    # Mentee requests a mentor → only the mentor is DMed.
    mtr, cdt = _pair(1)
    audiences.clear()
    services.propose_pairing(mtr, cdt, initiated_by=MentorshipPairing.InitiatedBy.MENTEE)
    assert audiences == [{"kind": "user", "id": mtr.user_id}]

    # Mentor invites a cadet → only the mentee is DMed.
    mtr, cdt = _pair(2)
    audiences.clear()
    services.propose_pairing(mtr, cdt, initiated_by=MentorshipPairing.InitiatedBy.MENTOR)
    assert audiences == [{"kind": "user", "id": cdt.user_id}]

    # Auto-suggested (SYSTEM) → both pilots are DMed.
    mtr, cdt = _pair(3)
    audiences.clear()
    services.propose_pairing(mtr, cdt, initiated_by=MentorshipPairing.InitiatedBy.SYSTEM,
                             status=MentorshipPairing.Status.SUGGESTED)
    assert {a["id"] for a in audiences} == {mtr.user_id, cdt.user_id}
