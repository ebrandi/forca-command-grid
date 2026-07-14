"""Campaign Command service-layer tests.

Covers the pieces the design docs pin as normative: the T1–T11 lifecycle (every legal and
illegal edge, per role), the visibility chokepoint (tiers + restricted + draft rules), progress
math (gte/lte/degenerates/weights), the health rules one-by-one plus worst-wins, dependency cycle
rejection, the issue→blocked→resolve round trip, verification separation of duties, and the
completion guard with its director override. House style: ``pytest.mark.django_db``, ``_user``
role helpers via ``apps.sso.services.ensure_role`` + ``core.rbac`` constants, no factory-boy.
"""
from __future__ import annotations

import contextlib
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone, translation

from apps.campaigns import progress as progress_mod
from apps.campaigns import services
from apps.campaigns.models import (
    Campaign,
    CampaignActivity,
    DependencyKind,
    Issue,
    Milestone,
    Objective,
    ObjectiveSample,
    Risk,
)
from apps.identity.models import RoleAssignment
from apps.sso.services import ensure_role
from apps.tasks.models import Task
from core import rbac

pytestmark = pytest.mark.django_db

GTE = Objective.Direction.GTE
LTE = Objective.Direction.LTE
OS = Objective.ObjectiveStatus
CS = Campaign.Status


# --------------------------------------------------------------------------- #
#  helpers
# --------------------------------------------------------------------------- #
def _user(django_user_model, username, *role_keys):
    u = django_user_model.objects.create(username=username)
    for key in role_keys:
        RoleAssignment.objects.create(user=u, role=ensure_role(key))
    return u


def _campaign(**kwargs) -> Campaign:
    return Campaign.objects.create(name=kwargs.pop("name", "Deployment"), **kwargs)


def _objective(campaign, **kwargs) -> Objective:
    return Objective.objects.create(campaign=campaign, title=kwargs.pop("title", "Objective"), **kwargs)


def _ready_draft(django_user_model, commander=None) -> Campaign:
    """A draft that satisfies the T1 propose gate (outcome + one objective)."""
    c = _campaign(desired_outcome="Be ready", commander=commander)
    _objective(c, status=OS.ACTIVE)
    return c


# ===========================================================================
#  Lifecycle — legal & illegal transitions (doc 04 §1, T1–T11)
# ===========================================================================
def test_t1_propose_requires_outcome_and_objective(django_user_model):
    officer = _user(django_user_model, "eve:o1", rbac.ROLE_OFFICER)
    bare = _campaign()  # no outcome, no objective
    with pytest.raises(ValidationError):
        services.set_status(bare, CS.PROPOSED, officer)

    no_obj = _campaign(desired_outcome="Ready")
    with pytest.raises(ValidationError):
        services.set_status(no_obj, CS.PROPOSED, officer)

    ok = _ready_draft(django_user_model)
    assert services.set_status(ok, CS.PROPOSED, officer) is True
    ok.refresh_from_db()
    assert ok.status == CS.PROPOSED
    assert ok.activity.filter(verb="status.changed").exists()


def test_t2_rework_requires_reason(django_user_model):
    officer = _user(django_user_model, "eve:o2", rbac.ROLE_OFFICER)
    c = _ready_draft(django_user_model)
    services.set_status(c, CS.PROPOSED, officer)
    with pytest.raises(ValidationError):
        services.set_status(c, CS.DRAFT, officer)  # no reason
    assert services.set_status(c, CS.DRAFT, officer, reason="needs scope") is True
    c.refresh_from_db()
    assert c.status == CS.DRAFT


def test_t3_approval_is_director_only(django_user_model):
    officer = _user(django_user_model, "eve:o3", rbac.ROLE_OFFICER)
    lead = _user(django_user_model, "eve:cl3", rbac.ROLE_MEMBER, rbac.ROLE_CAMPAIGN_LEAD)
    director = _user(django_user_model, "eve:d3", rbac.ROLE_DIRECTOR)
    c = _ready_draft(django_user_model)
    services.set_status(c, CS.PROPOSED, officer)

    for actor in (officer, lead):
        with pytest.raises(ValidationError):
            services.set_status(c, CS.APPROVED, actor)
    assert services.set_status(c, CS.APPROVED, director) is True
    c.refresh_from_db()
    assert c.status == CS.APPROVED


def test_t4_start_sets_start_at_and_rejects_past_target(django_user_model):
    officer = _user(django_user_model, "eve:o4", rbac.ROLE_OFFICER)
    director = _user(django_user_model, "eve:d4", rbac.ROLE_DIRECTOR)
    c = _ready_draft(django_user_model)
    services.set_status(c, CS.PROPOSED, officer)
    services.set_status(c, CS.APPROVED, director)
    c.target_end_at = timezone.now() - timezone.timedelta(days=1)
    c.save(update_fields=["target_end_at"])
    with pytest.raises(ValidationError):
        services.set_status(c, CS.ACTIVE, officer)

    c.target_end_at = timezone.now() + timezone.timedelta(days=30)
    c.save(update_fields=["target_end_at"])
    assert services.set_status(c, CS.ACTIVE, officer) is True
    c.refresh_from_db()
    assert c.status == CS.ACTIVE
    assert c.start_at is not None


def test_t5_t6_pause_resume(django_user_model):
    officer = _user(django_user_model, "eve:o5", rbac.ROLE_OFFICER)
    director = _user(django_user_model, "eve:d5", rbac.ROLE_DIRECTOR)
    c = _ready_draft(django_user_model)
    services.set_status(c, CS.PROPOSED, officer)
    services.set_status(c, CS.APPROVED, director)
    services.set_status(c, CS.ACTIVE, officer)
    with pytest.raises(ValidationError):
        services.set_status(c, CS.PAUSED, officer)  # reason required
    assert services.set_status(c, CS.PAUSED, officer, reason="stand down") is True
    assert services.set_status(c, CS.ACTIVE, officer) is True
    c.refresh_from_db()
    assert c.status == CS.ACTIVE


def test_t8_t9_terminal_transitions_need_reason_and_close_out(django_user_model):
    officer = _user(django_user_model, "eve:o8", rbac.ROLE_OFFICER)
    director = _user(django_user_model, "eve:d8", rbac.ROLE_DIRECTOR)
    c = _active_campaign(django_user_model, officer, director)
    with pytest.raises(ValidationError):
        services.set_status(c, CS.CANCELLED, officer)  # reason required
    assert services.set_status(c, CS.CANCELLED, officer, reason="scrubbed") is True
    c.refresh_from_db()
    assert c.status == CS.CANCELLED
    assert c.actual_end_at is not None
    assert c.closed_by_id == officer.pk


def test_t11_archive_is_terminal(django_user_model):
    officer = _user(django_user_model, "eve:o11", rbac.ROLE_OFFICER)
    director = _user(django_user_model, "eve:d11", rbac.ROLE_DIRECTOR)
    c = _active_campaign(django_user_model, officer, director)
    services.set_status(c, CS.CANCELLED, officer, reason="x")
    assert services.set_status(c, CS.ARCHIVED, officer) is True
    c.refresh_from_db()
    assert c.status == CS.ARCHIVED
    # Archived is terminal: nothing leaves it.
    for target in (CS.ACTIVE, CS.DRAFT, CS.COMPLETED, CS.PROPOSED):
        with pytest.raises(ValidationError):
            services.set_status(c, target, director)


def test_illegal_transitions_rejected(django_user_model):
    officer = _user(django_user_model, "eve:oil", rbac.ROLE_OFFICER)
    director = _user(django_user_model, "eve:dil", rbac.ROLE_DIRECTOR)
    # draft → active (skipping approval)
    c = _ready_draft(django_user_model)
    with pytest.raises(ValidationError):
        services.set_status(c, CS.ACTIVE, officer)
    # paused → completed (must resume first)
    active = _active_campaign(django_user_model, officer, director)
    services.set_status(active, CS.PAUSED, officer, reason="hold")
    with pytest.raises(ValidationError):
        services.set_status(active, CS.COMPLETED, officer)
    # active → approved (backwards)
    active2 = _active_campaign(django_user_model, officer, director)
    with pytest.raises(ValidationError):
        services.set_status(active2, CS.APPROVED, director)


def test_transition_is_idempotent_on_same_status(django_user_model):
    officer = _user(django_user_model, "eve:oid", rbac.ROLE_OFFICER)
    c = _ready_draft(django_user_model)
    services.set_status(c, CS.PROPOSED, officer)
    before = CampaignActivity.objects.filter(campaign=c).count()
    assert services.set_status(c, CS.PROPOSED, officer) is True
    assert CampaignActivity.objects.filter(campaign=c).count() == before


def test_campaign_lead_seed_grants_manage_capability(django_user_model):
    # Proves the 0002 seed migration wired the lateral role → permission M2M: a member holding
    # campaign_lead gains campaign.manage without officer rank (doc 07 §1.1).
    lead = _user(django_user_model, "eve:seed", rbac.ROLE_MEMBER, rbac.ROLE_CAMPAIGN_LEAD)
    assert rbac.has_perm(lead, rbac.PERM_CAMPAIGN_MANAGE) is True
    assert services.can_manage(lead, _campaign()) is True
    plain = _user(django_user_model, "eve:plain", rbac.ROLE_MEMBER)
    assert rbac.has_perm(plain, rbac.PERM_CAMPAIGN_MANAGE) is False


def test_commander_can_manage_without_capability(django_user_model):
    member_commander = _user(django_user_model, "eve:cmd", rbac.ROLE_MEMBER)
    c = _ready_draft(django_user_model, commander=member_commander)
    # A plain member who is the commander manages their own campaign (doc 07 §1.1).
    assert services.can_manage(member_commander, c) is True
    assert services.set_status(c, CS.PROPOSED, member_commander) is True


# ===========================================================================
#  Completion guard + director override (doc 04 T7)
# ===========================================================================
def _active_campaign(django_user_model, officer, director, **kwargs) -> Campaign:
    c = _campaign(desired_outcome="Ready", **kwargs)
    _objective(c, status=OS.ACTIVE)
    services.set_status(c, CS.PROPOSED, officer)
    services.set_status(c, CS.APPROVED, director)
    services.set_status(c, CS.ACTIVE, officer)
    return c


def test_completion_blocked_until_mandatory_objectives_resolved(django_user_model):
    officer = _user(django_user_model, "eve:oc", rbac.ROLE_OFFICER)
    director = _user(django_user_model, "eve:dc", rbac.ROLE_DIRECTOR)
    c = _campaign(desired_outcome="Ready")
    mand = _objective(c, status=OS.ACTIVE, is_mandatory=True)
    services.set_status(c, CS.PROPOSED, officer)
    services.set_status(c, CS.APPROVED, director)
    services.set_status(c, CS.ACTIVE, officer)

    with pytest.raises(ValidationError):
        services.set_status(c, CS.COMPLETED, officer, via_closeout=True)

    services.set_objective_status(mand, officer, OS.MET)
    assert services.set_status(c, CS.COMPLETED, officer, via_closeout=True) is True
    c.refresh_from_db()
    assert c.status == CS.COMPLETED


def test_completion_director_override_with_reason(django_user_model):
    officer = _user(django_user_model, "eve:oc2", rbac.ROLE_OFFICER)
    director = _user(django_user_model, "eve:dc2", rbac.ROLE_DIRECTOR)
    c = _campaign(desired_outcome="Ready")
    _objective(c, status=OS.ACTIVE, is_mandatory=True)  # left unresolved
    services.set_status(c, CS.PROPOSED, officer)
    services.set_status(c, CS.APPROVED, director)
    services.set_status(c, CS.ACTIVE, officer)

    # An officer cannot override; a director with a reason can.
    with pytest.raises(ValidationError):
        services.set_status(c, CS.COMPLETED, officer, reason="ship it", via_closeout=True)
    assert services.set_status(c, CS.COMPLETED, director, reason="strategic call",
                               via_closeout=True) is True
    c.refresh_from_db()
    assert c.status == CS.COMPLETED


def test_unverified_met_claim_blocks_completion(django_user_model):
    officer = _user(django_user_model, "eve:ov", rbac.ROLE_OFFICER)
    director = _user(django_user_model, "eve:dv", rbac.ROLE_DIRECTOR)
    member = _user(django_user_model, "eve:mv", rbac.ROLE_MEMBER)
    c = _campaign(desired_outcome="Ready")
    obj = _objective(c, status=OS.ACTIVE, is_mandatory=True, requires_verification=True, owner=member)
    services.set_status(c, CS.PROPOSED, officer)
    services.set_status(c, CS.APPROVED, director)
    services.set_status(c, CS.ACTIVE, officer)

    services.set_objective_status(obj, member, OS.MET)  # met but unverified
    with pytest.raises(ValidationError):
        services.set_status(c, CS.COMPLETED, officer, via_closeout=True)
    # Once an officer (≠ claimant) verifies, completion is allowed.
    services.verify_objective(obj, officer)
    assert services.set_status(c, CS.COMPLETED, officer, via_closeout=True) is True


def test_direct_complete_or_fail_requires_closeout(django_user_model):
    # ACTIVE → completed/failed is reachable only through the guided close-out; a direct
    # set_status is refused so the mandatory permanent record can never be bypassed (doc 04 T7/T8).
    officer = _user(django_user_model, "eve:oclos", rbac.ROLE_OFFICER)
    director = _user(django_user_model, "eve:dclos", rbac.ROLE_DIRECTOR)
    c = _active_campaign(django_user_model, officer, director)
    for target in (CS.COMPLETED, CS.FAILED):
        with pytest.raises(ValidationError):
            services.set_status(c, target, officer, reason="short circuit")
    c.refresh_from_db()
    assert c.status == CS.ACTIVE
    assert services.set_status(c, CS.COMPLETED, officer, via_closeout=True) is True


# ===========================================================================
#  Verification separation of duties (doc 07 §1.1)
# ===========================================================================
def test_verifier_cannot_verify_own_claim(django_user_model):
    officer_a = _user(django_user_model, "eve:va", rbac.ROLE_OFFICER)
    officer_b = _user(django_user_model, "eve:vb", rbac.ROLE_OFFICER)
    member = _user(django_user_model, "eve:vm", rbac.ROLE_MEMBER)
    c = _campaign(desired_outcome="Ready")
    obj = _objective(c, status=OS.ACTIVE, requires_verification=True)
    services.set_status(c, CS.PROPOSED, officer_a)

    services.set_objective_status(obj, officer_a, OS.MET)  # officer_a is the claimant
    assert services.can_verify(officer_a, obj) is False
    assert services.can_verify(officer_b, obj) is True
    assert services.can_verify(member, obj) is False  # not officer rank
    with pytest.raises(ValidationError):
        services.verify_objective(obj, officer_a)
    services.verify_objective(obj, officer_b)
    obj.refresh_from_db()
    assert obj.verified_by_id == officer_b.pk


# ===========================================================================
#  Visibility matrix (doc 07 §1.3–1.4)
# ===========================================================================
def test_visibility_tiers(django_user_model):
    member = _user(django_user_model, "eve:vis_m", rbac.ROLE_MEMBER)
    officer = _user(django_user_model, "eve:vis_o", rbac.ROLE_OFFICER)
    director = _user(django_user_model, "eve:vis_d", rbac.ROLE_DIRECTOR)

    members_c = _campaign(name="M", status=CS.ACTIVE, visibility=Campaign.Visibility.MEMBERS)
    officers_c = _campaign(name="O", status=CS.ACTIVE, visibility=Campaign.Visibility.OFFICERS)
    directors_c = _campaign(name="D", status=CS.ACTIVE, visibility=Campaign.Visibility.DIRECTORS)

    def ids(user):
        return set(services.visible_campaigns(user).values_list("pk", flat=True))

    assert ids(member) == {members_c.pk}
    assert ids(officer) == {members_c.pk, officers_c.pk}
    assert ids(director) == {members_c.pk, officers_c.pk, directors_c.pk}
    # Object re-check agrees with the chokepoint.
    assert services.can_view(member, officers_c) is False
    assert services.can_view(officer, officers_c) is True


def test_restricted_visibility_requires_membership(django_user_model):
    member = _user(django_user_model, "eve:rm", rbac.ROLE_MEMBER)
    insider = _user(django_user_model, "eve:ri", rbac.ROLE_MEMBER)
    director = _user(django_user_model, "eve:rd", rbac.ROLE_DIRECTOR)
    c = _campaign(name="R", status=CS.ACTIVE, visibility=Campaign.Visibility.RESTRICTED)
    c.restricted_users.add(insider)

    assert services.can_view(member, c) is False
    assert services.can_view(insider, c) is True
    assert services.can_view(director, c) is True


def test_draft_visible_only_to_its_runners(django_user_model):
    member = _user(django_user_model, "eve:dm", rbac.ROLE_MEMBER)
    officer = _user(django_user_model, "eve:do", rbac.ROLE_OFFICER)
    creator = _user(django_user_model, "eve:dcr", rbac.ROLE_MEMBER)
    director = _user(django_user_model, "eve:dd", rbac.ROLE_DIRECTOR)
    # A members-tier DRAFT is still hidden from the general membership/officers (doc 04 §1).
    c = _campaign(name="Secret plan", status=CS.DRAFT,
                  visibility=Campaign.Visibility.MEMBERS, created_by=creator)

    assert services.can_view(member, c) is False
    assert services.can_view(officer, c) is False
    assert services.can_view(creator, c) is True   # personal widening
    assert services.can_view(director, c) is True   # directors see all


def test_participant_widening_shows_only_that_campaign(django_user_model):
    owner = _user(django_user_model, "eve:pw", rbac.ROLE_MEMBER)
    officers_c = _campaign(name="O", status=CS.ACTIVE, visibility=Campaign.Visibility.OFFICERS)
    other = _campaign(name="Other", status=CS.ACTIVE, visibility=Campaign.Visibility.OFFICERS)
    _objective(officers_c, owner=owner, status=OS.ACTIVE)
    visible = set(services.visible_campaigns(owner).values_list("pk", flat=True))
    assert visible == {officers_c.pk}
    assert other.pk not in visible


# ===========================================================================
#  Progress math (doc 04 §3.1) — pure function
# ===========================================================================
@pytest.mark.parametrize(
    "baseline,target,current,direction,expected",
    [
        (Decimal(0), Decimal(10), Decimal(5), GTE, 50),
        (Decimal(0), Decimal(10), Decimal(20), GTE, 100),   # clamp high
        (Decimal(0), Decimal(10), Decimal("-5"), GTE, 0),   # clamp low
        (None, Decimal(10), Decimal(5), GTE, 50),           # NULL baseline → 0
        (Decimal(5), Decimal(5), Decimal(5), GTE, 100),     # degenerate t==b, met
        (Decimal(5), Decimal(5), Decimal(4), GTE, 0),       # degenerate t==b, not met
        (None, None, Decimal(5), GTE, 0),                   # no target
        (Decimal(20), Decimal(10), Decimal(8), LTE, 100),   # lte at/below target
        (Decimal(20), Decimal(10), Decimal(15), LTE, 50),   # lte proportional
        (Decimal(20), Decimal(10), Decimal("10.05"), LTE, 99),  # lte cap at 99
        (None, Decimal(10), Decimal(15), LTE, 0),           # lte NULL baseline over target
        (Decimal(5), Decimal(10), Decimal(15), LTE, 0),     # lte baseline <= target, over
        (Decimal(0), Decimal(10), None, GTE, 0),            # never measured
    ],
)
def test_objective_progress_value(baseline, target, current, direction, expected):
    assert progress_mod.objective_progress_value(baseline, target, current, direction) == expected


def test_campaign_weighted_progress_excludes_dropped(django_user_model):
    c = _campaign(progress_mode=Campaign.ProgressMode.WEIGHTED)
    _objective(c, weight=1, baseline_value=Decimal(0), target_value=Decimal(10),
               current_value=Decimal(10), status=OS.ACTIVE)   # 100%
    _objective(c, weight=3, baseline_value=Decimal(0), target_value=Decimal(10),
               current_value=Decimal(0), status=OS.ACTIVE)    # 0%
    _objective(c, weight=5, current_value=Decimal(0), status=OS.DROPPED)  # excluded
    # (100*1 + 0*3) / (1+3) = 25
    assert services.campaign_progress(c) == 25


def test_campaign_milestone_progress(django_user_model):
    c = _campaign(progress_mode=Campaign.ProgressMode.MILESTONES)
    for i in range(4):
        Milestone.objects.create(
            campaign=c, title=f"m{i}",
            status=Milestone.MilestoneStatus.DONE if i < 2 else Milestone.MilestoneStatus.PENDING,
        )
    assert services.campaign_progress(c) == 50


def test_manual_progress_is_left_untouched(django_user_model):
    c = _campaign(progress_mode=Campaign.ProgressMode.MANUAL, progress_pct=73)
    assert services.campaign_progress(c) == 73


def test_update_manual_value_records_sample_and_progress(django_user_model):
    officer = _user(django_user_model, "eve:mv1", rbac.ROLE_OFFICER)
    c = _campaign(status=CS.ACTIVE)
    obj = _objective(c, baseline_value=Decimal(0), target_value=Decimal(10), status=OS.ACTIVE)
    with pytest.raises(ValidationError):
        services.update_manual_value(obj, officer, Decimal(5), note="")  # note mandatory
    services.update_manual_value(obj, officer, Decimal(5), note="counted heads")
    obj.refresh_from_db()
    assert obj.current_value == Decimal(5)
    assert obj.progress_pct == 50
    assert ObjectiveSample.objects.filter(objective=obj).count() == 1
    # A same-value, same-minute replay writes no second sample (doc 07 T14).
    services.update_manual_value(obj, officer, Decimal(5), note="again")
    assert ObjectiveSample.objects.filter(objective=obj).count() == 1


# ===========================================================================
#  Health rules one-by-one + worst-wins (doc 00 §4)
# ===========================================================================
def _active_measurable(django_user_model, **campaign_kwargs) -> tuple[Campaign, object]:
    """An active campaign with one owned, measured objective (baseline healthy)."""
    owner = _user(django_user_model, f"eve:h{Campaign.objects.count()}", rbac.ROLE_MEMBER)
    c = _campaign(status=CS.ACTIVE, **campaign_kwargs)
    _objective(c, owner=owner, current_value=Decimal(5), target_value=Decimal(10), status=OS.ACTIVE)
    return c, owner


def _codes(campaign):
    _state, reasons = services.campaign_health(campaign)
    return {r["code"] for r in reasons}


def test_health_unknown_when_not_active():
    c = _campaign(status=CS.DRAFT)
    state, reasons = services.campaign_health(c)
    assert state == "unknown"
    assert reasons[0]["code"] == "not_active"


def test_health_unknown_when_nothing_measurable():
    c = _campaign(status=CS.ACTIVE)
    _objective(c, current_value=None, status=OS.ACTIVE)
    state, _ = services.campaign_health(c)
    assert state == "unknown"


def test_health_healthy_baseline(django_user_model):
    c, _ = _active_measurable(django_user_model)
    state, reasons = services.campaign_health(c)
    assert state == "healthy"
    assert reasons == []


def test_health_blocked_on_mandatory_blocked_objective(django_user_model):
    c, _ = _active_measurable(django_user_model)
    _objective(c, is_mandatory=True, status=OS.BLOCKED, current_value=Decimal(1),
               target_value=Decimal(10))
    state, reasons = services.campaign_health(c)
    assert state == "blocked"
    assert "mandatory_blocked" in {r["code"] for r in reasons}


def test_health_critical_on_budget_overrun(django_user_model):
    c, _ = _active_measurable(django_user_model, budget_isk=Decimal(100), spent_isk=Decimal(120))
    state = services.campaign_health(c)[0]
    assert state == "critical"
    assert "budget_overrun" in _codes(c)


def test_health_critical_past_deadline(django_user_model):
    c, _ = _active_measurable(django_user_model)
    c.target_end_at = timezone.now() - timezone.timedelta(days=1)
    c.progress_pct = 40
    assert services.campaign_health(c)[0] == "critical"
    assert "past_deadline" in _codes(c)


def test_health_critical_overdue_severity9_risk(django_user_model):
    c, _ = _active_measurable(django_user_model)
    Risk.objects.create(
        campaign=c, description="cyno down", probability=Risk.RiskLevel.HIGH,
        impact=Risk.RiskLevel.HIGH, severity=9, status=Risk.RiskStatus.OPEN,
        due_at=timezone.now() - timezone.timedelta(days=1),
    )
    assert services.campaign_health(c)[0] == "critical"
    assert "risk_critical" in _codes(c)


def test_health_at_risk_budget_warning(django_user_model):
    c, _ = _active_measurable(django_user_model, budget_isk=Decimal(100), spent_isk=Decimal(96))
    assert services.campaign_health(c)[0] == "at_risk"
    assert "budget_warning" in _codes(c)


def test_health_at_risk_overdue_objective(django_user_model):
    c, _ = _active_measurable(django_user_model)
    _objective(c, status=OS.ACTIVE, current_value=Decimal(1), target_value=Decimal(10),
               due_at=timezone.now() - timezone.timedelta(days=2))
    assert services.campaign_health(c)[0] == "at_risk"
    assert "overdue_objectives" in _codes(c)


def test_health_at_risk_escalated_issue(django_user_model):
    c, _ = _active_measurable(django_user_model)
    Issue.objects.create(campaign=c, description="x", status=Issue.IssueStatus.ESCALATED)
    assert services.campaign_health(c)[0] == "at_risk"
    assert "escalated_issues" in _codes(c)


def test_health_at_risk_deadline_shortfall(django_user_model):
    c, _ = _active_measurable(django_user_model)
    c.start_at = timezone.now() - timezone.timedelta(days=10)
    c.target_end_at = timezone.now() + timezone.timedelta(days=10)  # halfway → expect ~50%
    c.progress_pct = 0
    assert services.campaign_health(c)[0] == "at_risk"
    assert "deadline_shortfall" in _codes(c)


def test_health_watch_unowned_objective(django_user_model):
    c, _ = _active_measurable(django_user_model)
    _objective(c, owner=None, status=OS.ACTIVE, current_value=Decimal(1), target_value=Decimal(10))
    assert services.campaign_health(c)[0] == "watch"
    assert "unowned_objectives" in _codes(c)


def test_health_watch_inactive(django_user_model):
    c, _ = _active_measurable(django_user_model)
    old = timezone.now() - timezone.timedelta(days=30)
    act = services.record_activity(c, None, "objective.progress")
    CampaignActivity.objects.filter(pk=act.pk).update(created_at=old)
    assert services.campaign_health(c)[0] == "watch"
    assert "inactive" in _codes(c)


def test_health_worst_wins(django_user_model):
    # A watch condition (unowned) plus a critical condition (budget) → overall critical, both listed.
    c, _ = _active_measurable(django_user_model, budget_isk=Decimal(100), spent_isk=Decimal(200))
    _objective(c, owner=None, status=OS.ACTIVE, current_value=Decimal(1), target_value=Decimal(10))
    state, reasons = services.campaign_health(c)
    assert state == "critical"
    codes = {r["code"] for r in reasons}
    assert "budget_overrun" in codes
    assert "unowned_objectives" in codes


# ===========================================================================
#  Dependencies (doc 04 §8)
# ===========================================================================
def test_dependency_direct_cycle_rejected(django_user_model):
    officer = _user(django_user_model, "eve:dep1", rbac.ROLE_OFFICER)
    c = _campaign()
    a = _objective(c, title="A")
    b = _objective(c, title="B")
    services.add_dependency(c, DependencyKind.OBJECTIVE, a.pk, DependencyKind.OBJECTIVE, b.pk, user=officer)
    with pytest.raises(ValidationError):
        services.add_dependency(c, DependencyKind.OBJECTIVE, b.pk, DependencyKind.OBJECTIVE, a.pk, user=officer)


def test_dependency_transitive_cycle_rejected(django_user_model):
    c = _campaign()
    a, b, d = (_objective(c, title=n) for n in "ABD")
    services.add_dependency(c, DependencyKind.OBJECTIVE, a.pk, DependencyKind.OBJECTIVE, b.pk)
    services.add_dependency(c, DependencyKind.OBJECTIVE, b.pk, DependencyKind.OBJECTIVE, d.pk)
    with pytest.raises(ValidationError):
        services.add_dependency(c, DependencyKind.OBJECTIVE, d.pk, DependencyKind.OBJECTIVE, a.pk)


def test_dependency_self_edge_and_external_rules(django_user_model):
    c = _campaign()
    a = _objective(c, title="A")
    with pytest.raises(ValidationError):
        services.add_dependency(c, DependencyKind.OBJECTIVE, a.pk, DependencyKind.OBJECTIVE, a.pk)
    with pytest.raises(ValidationError):  # external cannot be a source
        services.add_dependency(c, DependencyKind.EXTERNAL, 0, DependencyKind.OBJECTIVE, a.pk)
    with pytest.raises(ValidationError):  # external target needs a note
        services.add_dependency(c, DependencyKind.OBJECTIVE, a.pk, DependencyKind.EXTERNAL, 0)
    dep = services.add_dependency(c, DependencyKind.OBJECTIVE, a.pk, DependencyKind.EXTERNAL, 0,
                                  note="waiting on market delivery")
    assert dep.to_id == 0


def test_dependency_duplicate_is_idempotent(django_user_model):
    c = _campaign()
    a, b = _objective(c, title="A"), _objective(c, title="B")
    first = services.add_dependency(c, DependencyKind.OBJECTIVE, a.pk, DependencyKind.OBJECTIVE, b.pk)
    again = services.add_dependency(c, DependencyKind.OBJECTIVE, a.pk, DependencyKind.OBJECTIVE, b.pk)
    assert first.pk == again.pk
    assert c.dependencies.count() == 1


def test_dependency_depth_cap(django_user_model):
    c = _campaign()
    # A chain of real in-campaign objectives longer than the cap; an edge into its head then walks
    # the chain past the depth guard (endpoints must be genuine campaign entities now, #4).
    objs = [_objective(c, title=f"O{i}") for i in range(60)]
    for i in range(59):
        services.add_dependency(c, DependencyKind.OBJECTIVE, objs[i].pk,
                                DependencyKind.OBJECTIVE, objs[i + 1].pk)
    head = _objective(c, title="head")
    with pytest.raises(ValidationError):
        services.add_dependency(c, DependencyKind.OBJECTIVE, head.pk,
                                DependencyKind.OBJECTIVE, objs[0].pk)


def test_dependency_auto_resolves_when_target_met(django_user_model):
    officer = _user(django_user_model, "eve:depr", rbac.ROLE_OFFICER)
    c = _campaign()
    a = _objective(c, title="A", status=OS.ACTIVE)
    b = _objective(c, title="B", status=OS.ACTIVE)
    dep = services.add_dependency(c, DependencyKind.OBJECTIVE, a.pk, DependencyKind.OBJECTIVE, b.pk)
    services.set_objective_status(b, officer, OS.MET)
    dep.refresh_from_db()
    assert dep.is_resolved is True


# ===========================================================================
#  Issue → blocked round trip (doc 04 §7)
# ===========================================================================
def test_issue_blocks_and_resolution_restores(django_user_model):
    officer = _user(django_user_model, "eve:iss", rbac.ROLE_OFFICER)
    c = _campaign(status=CS.ACTIVE)
    obj = _objective(c, status=OS.ACTIVE)
    issue = services.raise_issue(c, officer, "cannot proceed", objective=obj)
    obj.refresh_from_db()
    assert obj.status == OS.BLOCKED
    assert obj.block_reason

    services.resolve_issue(issue, officer, resolution_notes="fixed")
    obj.refresh_from_db()
    assert obj.status == OS.ACTIVE  # restored to pre-block status


def test_last_open_issue_rule(django_user_model):
    officer = _user(django_user_model, "eve:iss2", rbac.ROLE_OFFICER)
    c = _campaign(status=CS.ACTIVE)
    obj = _objective(c, status=OS.ACTIVE)
    i1 = services.raise_issue(c, officer, "problem one", objective=obj)
    i2 = services.raise_issue(c, officer, "problem two", objective=obj)
    obj.refresh_from_db()
    assert obj.status == OS.BLOCKED

    services.resolve_issue(i1, officer, resolution_notes="one done")
    obj.refresh_from_db()
    assert obj.status == OS.BLOCKED  # i2 still open

    services.resolve_issue(i2, officer, resolution_notes="two done")
    obj.refresh_from_db()
    assert obj.status == OS.ACTIVE


def test_resolve_issue_requires_notes(django_user_model):
    officer = _user(django_user_model, "eve:iss3", rbac.ROLE_OFFICER)
    c = _campaign(status=CS.ACTIVE)
    obj = _objective(c, status=OS.ACTIVE)
    issue = services.raise_issue(c, officer, "blocked", objective=obj)
    with pytest.raises(ValidationError):
        services.resolve_issue(issue, officer, resolution_notes="")


def test_blocked_objective_cannot_be_set_directly(django_user_model):
    officer = _user(django_user_model, "eve:iss4", rbac.ROLE_OFFICER)
    c = _campaign(status=CS.ACTIVE)
    obj = _objective(c, status=OS.ACTIVE)
    with pytest.raises(ValidationError):
        services.set_objective_status(obj, officer, OS.BLOCKED)


# ===========================================================================
#  Risk severity (doc 06 §4.13, §7)
# ===========================================================================
@pytest.mark.parametrize(
    "prob,impact,expected",
    [
        (Risk.RiskLevel.LOW, Risk.RiskLevel.LOW, 1),
        (Risk.RiskLevel.MEDIUM, Risk.RiskLevel.MEDIUM, 4),
        (Risk.RiskLevel.HIGH, Risk.RiskLevel.HIGH, 9),
        (Risk.RiskLevel.LOW, Risk.RiskLevel.HIGH, 3),
        (Risk.RiskLevel.HIGH, Risk.RiskLevel.MEDIUM, 6),
    ],
)
def test_risk_severity_is_product(django_user_model, prob, impact, expected):
    c = _campaign()
    r = Risk(campaign=c, description="x", probability=prob, impact=impact, severity=1)
    services.save_risk(r)
    r.refresh_from_db()
    assert r.severity == expected


# ===========================================================================
#  Adversarial review fixes (findings #1, #4, #6, #7, #8, #10, #13)
# ===========================================================================
def test_restricted_campaign_task_is_neutral_and_assigned(django_user_model):
    # A non-members campaign never puts a leaky title on the corp-wide task board, and its linked
    # task is assigned (never left claimable in the open pool) (#1).
    from apps.tasks.models import Task

    officer = _user(django_user_model, "eve:leak", rbac.ROLE_OFFICER)
    c = _campaign(desired_outcome="x", visibility=Campaign.Visibility.RESTRICTED)
    obj = _objective(c, title="Cyno network coordinates", status=OS.ACTIVE)
    task = services.create_objective_task(obj, officer)
    assert task.title == "Campaign follow-up task"
    assert "Cyno" not in task.title and "Cyno" not in task.description
    assert task.is_open is False and task.assignee_id == officer.pk
    assert task.status != Task.Status.OPEN


def test_members_campaign_task_keeps_objective_title(django_user_model):
    # Members-tier campaigns keep the descriptive title/description (unchanged behaviour, #1).
    officer = _user(django_user_model, "eve:mem1", rbac.ROLE_OFFICER)
    c = _campaign(visibility=Campaign.Visibility.MEMBERS)
    obj = _objective(c, title="Stage 200 hulls")
    task = services.create_objective_task(obj, officer, title="Follow-up: Stage 200 hulls")
    assert task.title == "Follow-up: Stage 200 hulls"
    assert "Stage 200 hulls" in task.description


def test_dependency_endpoints_must_belong_to_campaign(django_user_model):
    # Free-typed ids into another campaign's entities are rejected — no bare-PK oracle (#4).
    c1, c2 = _campaign(name="One"), _campaign(name="Two")
    a = _objective(c1, title="A")
    foreign = _objective(c2, title="Foreign")
    with pytest.raises(ValidationError):  # `from` not in campaign
        services.add_dependency(c1, DependencyKind.OBJECTIVE, foreign.pk,
                                DependencyKind.OBJECTIVE, a.pk)
    with pytest.raises(ValidationError):  # non-external `to` not in campaign
        services.add_dependency(c1, DependencyKind.OBJECTIVE, a.pk,
                                DependencyKind.OBJECTIVE, foreign.pk)
    with pytest.raises(ValidationError):  # non-existent bare id
        services.add_dependency(c1, DependencyKind.OBJECTIVE, a.pk,
                                DependencyKind.OBJECTIVE, 999999)


def test_sensitive_manual_note_omitted_from_activity_reason(django_user_model):
    # A sensitive objective's provenance note must not land in the activity feed (rendered to every
    # viewer), while a non-sensitive objective still records it (#6).
    officer = _user(django_user_model, "eve:sens", rbac.ROLE_OFFICER)
    c = _campaign(status=CS.ACTIVE)
    obj = _objective(c, status=OS.ACTIVE, is_sensitive=True, target_value=Decimal(10),
                     baseline_value=Decimal(0))
    services.update_manual_value(obj, officer, Decimal(4), "confirmed 4.2B in division 3")
    row = CampaignActivity.objects.filter(verb="objective.progress", target_id=obj.pk).latest("id")
    assert row.reason == ""

    plain = _objective(c, status=OS.ACTIVE, target_value=Decimal(10), baseline_value=Decimal(0))
    services.update_manual_value(plain, officer, Decimal(4), "counted on grid")
    prow = CampaignActivity.objects.filter(verb="objective.progress", target_id=plain.pk).latest("id")
    assert "counted on grid" in prow.reason


def test_budget_ratio_absent_from_health_detail(django_user_model):
    # The exact spend ratio is director/commander-only; a health reason renders on plain can_view
    # surfaces, so no percentage may sit in its detail (#7).
    c = _campaign(status=CS.ACTIVE, budget_isk=Decimal(100), spent_isk=Decimal(200),
                  start_at=timezone.now() - timezone.timedelta(days=1),
                  target_end_at=timezone.now() + timezone.timedelta(days=5))
    _objective(c, status=OS.ACTIVE, current_value=Decimal(5), target_value=Decimal(10),
               baseline_value=Decimal(0))
    _state, reasons = services.campaign_health(c)
    budget = [r for r in reasons if r["code"].startswith("budget_")]
    assert budget
    assert all("%" not in (r.get("detail") or "") for r in budget)


def test_closeout_requires_all_objectives_terminal(django_user_model):
    # Close-out rejects a non-terminal objective unless a director overrides (doc 04 §11 step 2, #8),
    # then auto-archives once everything resolves (#9).
    officer = _user(django_user_model, "eve:c8", rbac.ROLE_OFFICER)
    director = _user(django_user_model, "eve:d8", rbac.ROLE_DIRECTOR)
    c = _active_campaign(django_user_model, officer, director)
    with pytest.raises(ValidationError):
        services.close_campaign(c, officer, final_status=CS.COMPLETED,
                                outcome_summary="done", lessons_learned="learned")
    c.refresh_from_db()
    assert c.status == CS.ACTIVE

    obj = c.objectives.first()
    services.close_campaign(c, officer, final_status=CS.COMPLETED,
                            resolutions={obj.pk: {"status": OS.MET}},
                            outcome_summary="done", lessons_learned="learned")
    c.refresh_from_db()
    assert c.status == CS.ARCHIVED


def test_set_manual_progress(django_user_model):
    # Manual mode is the one mode a human sets by hand — mandatory note, clamped, survives recompute,
    # rejected on any other mode (#10).
    officer = _user(django_user_model, "eve:mp", rbac.ROLE_OFFICER)
    c = _campaign(status=CS.ACTIVE, progress_mode=Campaign.ProgressMode.MANUAL)
    with pytest.raises(ValidationError):
        services.set_manual_progress(c, officer, 60, "")
    services.set_manual_progress(c, officer, 150, "over-shot input clamps")
    c.refresh_from_db()
    assert c.progress_pct == 100 and c.progress_note == "over-shot input clamps"
    services.recompute(c)
    c.refresh_from_db()
    assert c.progress_pct == 100  # manual value survives a recompute

    weighted = _campaign(status=CS.ACTIVE, progress_mode=Campaign.ProgressMode.WEIGHTED)
    with pytest.raises(ValidationError):
        services.set_manual_progress(weighted, officer, 50, "n/a")


def test_issue_cannot_block_terminal_objective(django_user_model):
    # A met/missed/dropped objective can't be blocked — that would resurrect a reason-audited
    # resolution when the issue later clears (#13).
    officer = _user(django_user_model, "eve:i13", rbac.ROLE_OFFICER)
    c = _campaign(status=CS.ACTIVE)
    for status in (OS.DROPPED, OS.MET, OS.MISSED):
        obj = _objective(c, status=status)
        with pytest.raises(ValidationError):
            services.raise_issue(c, officer, "late blocker", objective=obj)


def test_unblock_restores_dropped_status(django_user_model):
    # Belt-and-braces: unblocking an objective that was blocked while dropped restores DROPPED,
    # never resurrects it to ACTIVE (#13 restore whitelist).
    officer = _user(django_user_model, "eve:u13", rbac.ROLE_OFFICER)
    c = _campaign(status=CS.ACTIVE)
    obj = _objective(c, status=OS.DROPPED)
    services._block_objective(obj, officer, reason="forced")
    obj.refresh_from_db()
    assert obj.status == OS.BLOCKED
    services._unblock_objective(obj, officer)
    obj.refresh_from_db()
    assert obj.status == OS.DROPPED


def test_config_kill_switches_exist_and_honoured(django_user_model):
    # The documented refresh/reminder kill-switches exist with safe defaults, and disabling refresh
    # makes the metric beat a no-op (#19).
    from apps.campaigns import config

    assert config.get("refresh")["enabled"] is True
    assert config.get("notifications")["deadline_reminders"] is True
    config.set("refresh", {"enabled": False})
    assert services.run_metric_refresh().get("status") == "disabled"


# ===========================================================================
#  Low-severity review fixes (#24, #26, #27, #28, #30, #36)
# ===========================================================================
def test_manage_met_claim_self_verifies_and_counts_for_completion(django_user_model):
    # A manage-capable claim on a requires_verification mandatory objective self-verifies and counts
    # toward the completion gate at once — no second officer needed (doc 04 §2, #28).
    officer = _user(django_user_model, "eve:sv_o", rbac.ROLE_OFFICER)
    director = _user(django_user_model, "eve:sv_d", rbac.ROLE_DIRECTOR)
    c = _campaign(desired_outcome="Ready")
    obj = _objective(c, status=OS.ACTIVE, is_mandatory=True, requires_verification=True)
    services.set_status(c, CS.PROPOSED, officer)
    services.set_status(c, CS.APPROVED, director)
    services.set_status(c, CS.ACTIVE, officer)

    services.set_objective_status(obj, officer, OS.MET)
    obj.refresh_from_db()
    assert obj.verified_by_id == officer.pk  # self-verified
    assert services.set_status(c, CS.COMPLETED, officer, via_closeout=True) is True


def test_resolve_dependency_requires_reason(django_user_model):
    # Manual dependency resolution is the audited escape hatch; a reason is mandatory (#26).
    officer = _user(django_user_model, "eve:rd_o", rbac.ROLE_OFFICER)
    c = _campaign(status=CS.ACTIVE)
    a = _objective(c, status=OS.ACTIVE)
    dep = services.add_dependency(c, DependencyKind.OBJECTIVE, a.pk,
                                  DependencyKind.EXTERNAL, note="waiting on CCP", user=officer)
    with pytest.raises(ValidationError):
        services.resolve_dependency(dep, officer)  # no reason
    services.resolve_dependency(dep, officer, reason="CCP shipped it")
    dep.refresh_from_db()
    assert dep.is_resolved is True
    assert c.activity.filter(verb="dependency.resolved", reason="CCP shipped it").exists()


def test_milestone_missed_requires_due_passed(django_user_model):
    # A milestone can only be marked missed once its due date has passed (#27).
    officer = _user(django_user_model, "eve:ms_o", rbac.ROLE_OFFICER)
    c = _campaign(status=CS.ACTIVE)
    ms = Milestone.objects.create(campaign=c, title="Deliver",
                                  due_at=timezone.now() + timezone.timedelta(days=2))
    with pytest.raises(ValidationError):
        services.set_milestone_status(ms, officer, Milestone.MilestoneStatus.MISSED)
    Milestone.objects.filter(pk=ms.pk).update(due_at=timezone.now() - timezone.timedelta(days=1))
    ms.refresh_from_db()
    services.set_milestone_status(ms, officer, Milestone.MilestoneStatus.MISSED)
    ms.refresh_from_db()
    assert ms.status == Milestone.MilestoneStatus.MISSED
    # Late completion path is now legal: missed → ready_for_review (#27).
    services.set_milestone_status(ms, officer, Milestone.MilestoneStatus.READY_FOR_REVIEW)
    ms.refresh_from_db()
    assert ms.status == Milestone.MilestoneStatus.READY_FOR_REVIEW


def test_milestone_leaving_done_clears_stamps(django_user_model):
    # A correction back to review clears completed_at/approved_by so a re-approval re-stamps them
    # honestly (#27).
    officer = _user(django_user_model, "eve:msd_o", rbac.ROLE_OFFICER)
    other = _user(django_user_model, "eve:msd_o2", rbac.ROLE_OFFICER)
    c = _campaign(status=CS.ACTIVE)
    ms = Milestone.objects.create(campaign=c, title="Deliver",
                                  due_at=timezone.now() + timezone.timedelta(days=2))
    services.set_milestone_status(ms, officer, Milestone.MilestoneStatus.READY_FOR_REVIEW)
    services.set_milestone_status(ms, other, Milestone.MilestoneStatus.DONE)  # approver ≠ marker
    ms.refresh_from_db()
    assert ms.completed_at is not None and ms.approved_by_id == other.pk
    services.set_milestone_status(ms, other, Milestone.MilestoneStatus.READY_FOR_REVIEW)
    ms.refresh_from_db()
    assert ms.completed_at is None and ms.approved_by_id is None


def test_save_risk_recomputes_health(django_user_model):
    # A severity-9 overdue risk is a health input; save_risk refreshes health immediately (#36).
    c, _owner = _active_measurable(django_user_model)
    r = Risk(campaign=c, description="reactor breach",
             probability=Risk.RiskLevel.HIGH, impact=Risk.RiskLevel.HIGH,
             status=Risk.RiskStatus.OPEN, due_at=timezone.now() - timezone.timedelta(days=1))
    services.save_risk(r)
    c.refresh_from_db()
    assert any(reason["code"] == "risk_critical" for reason in c.health_reasons)


def test_sensitive_measurement_redacted_in_activity(django_user_model):
    # A sensitive objective's before/after figures are redacted in the pull-based activity feed;
    # the real value lives only in the director-only audit (doc 07 T10, #30).
    officer = _user(django_user_model, "eve:red_o", rbac.ROLE_OFFICER)
    c = _campaign(status=CS.ACTIVE)
    obj = _objective(c, status=OS.ACTIVE, is_sensitive=True,
                     target_value=Decimal(10), baseline_value=Decimal(0))
    services.update_manual_value(obj, officer, Decimal(4), "confidential count")
    row = CampaignActivity.objects.filter(verb="objective.progress", target_id=obj.pk).latest("id")
    assert row.after == {"current_value": "<redacted>"}

    plain = _objective(c, status=OS.ACTIVE, target_value=Decimal(10), baseline_value=Decimal(0))
    services.update_manual_value(plain, officer, Decimal(4), "on grid")
    prow = CampaignActivity.objects.filter(verb="objective.progress", target_id=plain.pk).latest("id")
    assert prow.after == {"current_value": "4"}


# ===========================================================================
#  i18n: translated text must never be used as a lookup key
# ===========================================================================
@contextlib.contextmanager
def _translated_de(**msgstrs):
    """Activate ``de`` with ``msgstrs`` genuinely translated, then restore the catalogue.

    The shipped ``de`` catalogue has no msgstr for the follow-up titles *yet*, so plain
    ``translation.override("de")`` still returns the English msgid and the bug stays invisible.
    Seeding the msgstrs here is what a translator filling in that .po entry does — it makes the
    latent breakage reproducible now, and pins the invariant so it cannot regress later.
    """
    from django.utils.translation import trans_real

    with translation.override("de"):
        # ``_catalog`` is Django's TranslationCatalog (a chain of dicts), so write through
        # __setitem__ — its ``update()`` takes a whole translation object, not a mapping.
        catalog = trans_real.catalog()._catalog
        saved = {k: catalog.get(k, _MISSING) for k in msgstrs}
        for key, value in msgstrs.items():
            catalog[key] = value
        try:
            yield
        finally:
            for key, value in saved.items():
                if value is _MISSING:
                    catalog._catalogs[0].pop(key, None)
                else:
                    catalog[key] = value


_MISSING = object()


def test_close_out_followups_survive_a_non_english_closer(client, django_user_model):
    """A follow-up spawned by a German-locale officer still appears in the close-out report.

    Regression for the close-out report filtering ``Task.title`` with the English literals
    ``"Follow-up: …"`` / ``"Campaign follow-up task"``. Those titles are WRITTEN through gettext,
    so an officer closing the campaign in any non-English locale stores a translated title and the
    filter matches nothing — every one of their follow-ups silently vanishes from the report. The
    report now selects on the structural ``related_id`` marker, which no locale can touch.
    """
    officer = _user(django_user_model, "eve:de_close", rbac.ROLE_OFFICER, rbac.ROLE_DIRECTOR)
    # MEMBERS visibility → the descriptive "Follow-up: …" title (the officers/restricted tiers get
    # the neutral title, which is equally translated and equally unusable as a key).
    c = _campaign(desired_outcome="Ready", status=CS.ACTIVE,
                  visibility=Campaign.Visibility.MEMBERS)
    obj = _objective(c, title="Stage 200 hulls", status=OS.ACTIVE)

    # The whole close-out runs in German — exactly what a non-English officer's request does.
    with _translated_de(**{"Follow-up: %(title)s": "Nachfassen: %(title)s"}):
        services.close_campaign(
            c, officer, final_status=CS.COMPLETED,
            resolutions={obj.pk: {"status": OS.MET, "note": ""}},
            outcome_summary="Fleet staged.", lessons_learned="Start earlier.",
            followup_objective_ids=[obj.pk],
        )

    task = Task.objects.get(related_type=Objective.RELATED_TYPE)
    # The stored title really is German — which is exactly why it can never be the lookup key.
    assert task.title == "Nachfassen: Stage 200 hulls"
    assert task.related_id.startswith(Objective.followup_id_prefix(obj.pk))

    # …and the report still finds it. (Pre-fix, the English title filter matched nothing here and
    # this list came back empty.)
    client.force_login(officer)
    resp = client.get(reverse("campaigns:report", args=[c.pk]))
    assert resp.status_code == 200
    assert [t.pk for t in resp.context["followups"]] == [task.pk]


def test_close_out_report_lists_only_followups_not_every_linked_task(django_user_model):
    """The report shows close-out follow-ups only — not ordinary linked tasks.

    On a restricted campaign every linked task (volunteer, manually added) is given the SAME
    neutral title as a follow-up, so the old title filter over-matched and pulled unrelated tasks
    into the close-out report. The structural marker distinguishes them.
    """
    officer = _user(django_user_model, "eve:only_fu", rbac.ROLE_OFFICER)
    c = _campaign(desired_outcome="x", visibility=Campaign.Visibility.RESTRICTED, status=CS.ACTIVE)
    obj = _objective(c, title="Cyno network", status=OS.ACTIVE)

    ordinary = services.create_objective_task(obj, officer)
    followup = services.create_objective_task(obj, officer, followup=True)
    assert ordinary.title == followup.title == "Campaign follow-up task"  # indistinguishable

    prefix = Objective.followup_id_prefix(obj.pk)
    marked = Task.objects.filter(
        related_type=Objective.RELATED_TYPE, related_id__startswith=prefix
    )
    assert [t.pk for t in marked] == [followup.pk]
    # The signal still maps the marked task back to its objective (the marker is transparent).
    assert followup in obj.linked_tasks()
