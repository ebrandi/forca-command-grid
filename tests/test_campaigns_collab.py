"""Campaign Command Phase 2 (collaboration layer) tests.

Covers the pieces the design docs pin as normative for the collaboration layer: linked-task
round-trip + roll-up (doc 04 §10, doc 08 §5), the pingboard notification chokepoint with its
idempotency keys, ``is_enabled`` gating, restricted-payload restraint and health-signature dedup
(doc 09), calendar publish/update/cancel + restricted skip + sweep idempotency (doc 09 §5),
evidence validation + delete permissions (doc 04 §9), the volunteer flow (doc 10 §6.5), officer
workspace gating + contents and the pilot Command-Center panel (doc 10 §6.5, §6.7).

House style: ``pytest.mark.django_db``, ``_user`` role helpers via ``ensure_role`` +
``core.rbac`` constants, in-DB ``Alert`` / ``CalendarEvent`` assertions (as
``tests/test_pingboard_*`` do), ``django_capture_on_commit_callbacks`` where a service schedules
its notification/calendar effect after commit.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.campaigns import calendar as cal
from apps.campaigns import notify, services
from apps.campaigns.models import (
    Campaign,
    CampaignActivity,
    CampaignEvidence,
    Issue,
    Milestone,
    Objective,
)
from apps.identity.models import RoleAssignment
from apps.pingboard import config as pb_config
from apps.pingboard.models import Alert, CalendarEvent, CalendarEventStatus
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from apps.tasks.models import Task
from apps.tasks.services import set_status as task_set_status
from core import rbac

pytestmark = pytest.mark.django_db

CS = Campaign.Status
OS = Objective.ObjectiveStatus
V = Campaign.Visibility


# --------------------------------------------------------------------------- #
#  helpers
# --------------------------------------------------------------------------- #
def _user(django_user_model, username, *role_keys, corp_member=True):
    u = django_user_model.objects.create(username=username)
    for key in role_keys:
        RoleAssignment.objects.create(user=u, role=ensure_role(key))
    if corp_member:
        # A corp-member character so role/corp audiences resolve to real recipients.
        EveCharacter.objects.create(
            character_id=abs(hash(username)) % 2_000_000_000, user=u, name=username,
            is_main=True, is_corp_member=True,
        )
    return u


def _active_campaign(**kwargs):
    now = timezone.now()
    defaults = dict(
        name=kwargs.pop("name", "Deployment"), status=CS.ACTIVE,
        start_at=now - timezone.timedelta(days=2),
        target_end_at=now + timezone.timedelta(days=10),
        visibility=kwargs.pop("visibility", V.MEMBERS),
    )
    defaults.update(kwargs)
    return Campaign.objects.create(**defaults)


def _objective(campaign, **kwargs):
    return Objective.objects.create(
        campaign=campaign, title=kwargs.pop("title", "Objective"), **kwargs
    )


def _campaign_alerts():
    return Alert.objects.filter(source_service="campaigns")


# ===========================================================================
#  Linked tasks: round-trip + roll-up (doc 04 §10, doc 08 §5)
# ===========================================================================
def test_linked_task_done_records_activity_without_status_change(
    django_user_model, django_capture_on_commit_callbacks
):
    owner = _user(django_user_model, "eve:owner", rbac.ROLE_MEMBER)
    c = _active_campaign()
    o = _objective(c, owner=owner, status=OS.ACTIVE)

    task = services.create_objective_task(o, owner, assignee=owner)
    assert task.related_type == Objective.RELATED_TYPE
    assert task.related_id == str(o.pk)
    assert list(o.linked_tasks()) == [task]

    # The "all tasks done" roll-up runs on_commit now (lost-event-race fix, #32).
    with django_capture_on_commit_callbacks(execute=True):
        task_set_status(task, owner, Task.Status.DONE)

    o.refresh_from_db()
    assert o.status == OS.ACTIVE  # human closes the objective — never automation
    verbs = set(
        CampaignActivity.objects.filter(campaign=c, target_id=o.pk).values_list("verb", flat=True)
    )
    assert "objective.task_done" in verbs
    assert "objective.tasks_done" in verbs  # roll-up: all linked tasks done


def test_resaving_done_task_does_not_duplicate_rollup(
    django_user_model, django_capture_on_commit_callbacks
):
    # A re-save of an already-done linked task (e.g. an admin title fix) is not a transition and
    # must not append duplicate objective.task_done / objective.tasks_done rows (#31).
    owner = _user(django_user_model, "eve:resave", rbac.ROLE_MEMBER)
    c = _active_campaign()
    o = _objective(c, owner=owner, status=OS.ACTIVE)
    task = services.create_objective_task(o, owner, assignee=owner)
    with django_capture_on_commit_callbacks(execute=True):
        task_set_status(task, owner, Task.Status.DONE)

    def _count(verb):
        return CampaignActivity.objects.filter(campaign=c, target_id=o.pk, verb=verb).count()

    assert _count("objective.task_done") == 1 and _count("objective.tasks_done") == 1

    task.title = "renamed by admin"
    with django_capture_on_commit_callbacks(execute=True):
        task.save()

    assert _count("objective.task_done") == 1     # no duplicate terminal row
    assert _count("objective.tasks_done") == 1    # no duplicate roll-up


def test_linked_task_cancelled_records_activity_no_rollup(django_user_model):
    owner = _user(django_user_model, "eve:owner2", rbac.ROLE_MEMBER)
    c = _active_campaign()
    o = _objective(c, owner=owner, status=OS.ACTIVE)
    task = services.create_objective_task(o, owner, assignee=owner)

    task_set_status(task, owner, Task.Status.CANCELLED)

    verbs = set(
        CampaignActivity.objects.filter(campaign=c, target_id=o.pk).values_list("verb", flat=True)
    )
    assert "objective.task_cancelled" in verbs
    assert "objective.tasks_done" not in verbs


def test_additional_objective_tasks_get_suffixed_related_ids(django_user_model):
    officer = _user(django_user_model, "eve:o", rbac.ROLE_OFFICER)
    c = _active_campaign()
    o = _objective(c, status=OS.ACTIVE)
    t1 = services.create_objective_task(o, officer)
    t2 = services.create_objective_task(o, officer)
    assert t1.related_id == str(o.pk)
    assert t2.related_id == f"{o.pk}:2"
    assert set(o.linked_tasks().values_list("pk", flat=True)) == {t1.pk, t2.pk}


# ===========================================================================
#  Notification chokepoint (doc 09)
# ===========================================================================
def test_assigned_emits_one_alert_idempotently(django_user_model):
    owner = _user(django_user_model, "eve:assignee", rbac.ROLE_MEMBER)
    c = _active_campaign(name="Doctrine Rollout")
    notify.assigned(c, "objective", 42, owner, what="an objective")
    notify.assigned(c, "objective", 42, owner, what="an objective")  # replay
    alerts = _campaign_alerts()
    assert alerts.count() == 1
    alert = alerts.get()
    assert alert.idempotency_key == f"campaigns:assigned:objective:42:{owner.pk}"
    assert alert.category == "campaign"
    assert alert.audience == {"kind": "user", "id": owner.pk}


def test_is_enabled_gate_suppresses_emission(django_user_model):
    owner = _user(django_user_model, "eve:assignee2", rbac.ROLE_MEMBER)
    c = _active_campaign()
    pb_config.set("notifications", {"events": {"campaigns.assigned": {"enabled": False}}})
    notify.assigned(c, "objective", 7, owner, what="an objective")
    assert _campaign_alerts().count() == 0


def test_restricted_campaign_payload_is_name_only_and_targeted(django_user_model):
    member = _user(django_user_model, "eve:secret", rbac.ROLE_MEMBER)
    c = _active_campaign(name="Nightfall", visibility=V.RESTRICTED,
                         summary="covert K-6 forward staging detail", commander=member)
    c.restricted_users.add(member)

    notify.assigned(c, "objective", 99, member, what="an objective")

    alert = _campaign_alerts().get()
    # Individually targeted — never a broadcast audience (doc 09 §4.1 rule 1).
    assert alert.audience.get("kind") in ("user", "users")
    # Name only, no objective/summary/staging detail (doc 09 §4.1 rule 2).
    assert "Nightfall" in alert.body
    assert "covert" not in alert.body.lower()
    assert "staging" not in alert.body.lower()
    assert "objective" not in alert.body.lower()


def test_restricted_campaign_never_broadcasts_health(django_user_model):
    # health_changed's non-restricted audience is the officer role; a restricted campaign must
    # convert it to individually-targeted users (doc 09 §4.1 rule 1).
    director = _user(django_user_model, "eve:dir", rbac.ROLE_DIRECTOR)
    c = _active_campaign(name="Shadow", visibility=V.RESTRICTED, commander=director)
    _objective(c, status=OS.ACTIVE, due_at=timezone.now() - timezone.timedelta(days=1),
               current_value=Decimal(1), target_value=Decimal(10), baseline_value=Decimal(0))
    services.recompute(c)
    notify.health_changed(c)
    alert = _campaign_alerts().filter(idempotency_key__startswith="campaigns:health:").first()
    assert alert is not None
    assert alert.audience.get("kind") == "users"


def test_health_changed_dedups_on_unchanged_signature(django_user_model):
    c = _active_campaign(name="Ops")
    _objective(c, status=OS.ACTIVE, due_at=timezone.now() - timezone.timedelta(days=1),
               current_value=Decimal(1), target_value=Decimal(10), baseline_value=Decimal(0))
    services.recompute(c)
    c.refresh_from_db()
    assert c.health in {"at_risk", "critical", "blocked", "watch"}

    notify.health_changed(c)
    notify.health_changed(c)  # same signature — must not re-ping
    assert _campaign_alerts().filter(idempotency_key__startswith="campaigns:health:").count() == 1


def test_health_changed_skips_unknown_health(django_user_model):
    c = Campaign.objects.create(name="Draft", status=CS.DRAFT, visibility=V.MEMBERS)
    notify.health_changed(c)  # unknown health is not a signal
    assert _campaign_alerts().count() == 0


def test_set_status_active_emits_started(django_user_model, django_capture_on_commit_callbacks):
    director = _user(django_user_model, "eve:cmdr", rbac.ROLE_DIRECTOR)
    now = timezone.now()
    c = Campaign.objects.create(
        name="Kickoff", status=CS.APPROVED, visibility=V.MEMBERS, commander=director,
        target_end_at=now + timezone.timedelta(days=5),
    )
    _objective(c, status=OS.ACTIVE)
    with django_capture_on_commit_callbacks(execute=True):
        services.set_status(c, CS.ACTIVE, director)
    started = _campaign_alerts().filter(idempotency_key=f"campaigns:status:{c.pk}:active")
    assert started.count() == 1
    assert started.get().audience == {"kind": "corp"}


def test_issue_escalation_emits_and_audits(django_user_model, django_capture_on_commit_callbacks):
    officer = _user(django_user_model, "eve:esc", rbac.ROLE_OFFICER)
    c = _active_campaign()
    issue = services.raise_issue(c, officer, "Freight stuck")
    with django_capture_on_commit_callbacks(execute=True):
        services.escalate_issue(issue, officer, "No route for 48h")
    issue.refresh_from_db()
    assert issue.status == Issue.IssueStatus.ESCALATED
    assert _campaign_alerts().filter(
        idempotency_key=f"campaigns:issue_escalated:{issue.pk}"
    ).count() == 1


def test_objective_blocked_emits_with_signature_key(django_user_model,
                                                    django_capture_on_commit_callbacks):
    owner = _user(django_user_model, "eve:blk", rbac.ROLE_MEMBER)
    c = _active_campaign(commander=owner)
    o = _objective(c, owner=owner, status=OS.ACTIVE)
    with django_capture_on_commit_callbacks(execute=True):
        services.raise_issue(c, owner, "Blocked by war", objective=o)
    o.refresh_from_db()
    assert o.status == OS.BLOCKED
    assert _campaign_alerts().filter(
        idempotency_key__startswith=f"campaigns:blocked:{o.pk}:"
    ).count() == 1


# ===========================================================================
#  Calendar (doc 09 §5)
# ===========================================================================
def _cal_events(campaign):
    return CalendarEvent.objects.filter(source_system="campaigns").exclude(
        status=CalendarEventStatus.CANCELLED
    )


def test_publish_campaign_creates_window_and_milestone_rows(django_user_model):
    c = _active_campaign(name="Window")
    ms = Milestone.objects.create(
        campaign=c, title="Staging move", due_at=timezone.now() + timezone.timedelta(days=3)
    )
    cal.publish_campaign(c)
    keys = set(_cal_events(c).values_list("source_object_id", flat=True))
    assert f"campaign:{c.pk}" in keys
    assert f"milestone:{ms.pk}" in keys


def test_publish_campaign_skips_restricted(django_user_model):
    c = _active_campaign(name="Hidden", visibility=V.RESTRICTED)
    Milestone.objects.create(campaign=c, title="x",
                             due_at=timezone.now() + timezone.timedelta(days=2))
    cal.publish_campaign(c)
    assert _cal_events(c).count() == 0


def test_visibility_to_restricted_cancels_existing_events(django_user_model):
    c = _active_campaign(name="Flips")
    cal.publish_campaign(c)
    assert _cal_events(c).filter(source_object_id=f"campaign:{c.pk}").exists()
    c.visibility = V.RESTRICTED
    c.save(update_fields=["visibility"])
    cal.publish_campaign(c)
    assert not _cal_events(c).exists()


def test_sync_campaigns_is_idempotent(django_user_model):
    c = _active_campaign(name="Sweep")
    Milestone.objects.create(campaign=c, title="m",
                             due_at=timezone.now() + timezone.timedelta(days=4))
    first = cal.sync()
    count_after_first = CalendarEvent.objects.filter(source_system="campaigns").count()
    cal.sync()
    count_after_second = CalendarEvent.objects.filter(source_system="campaigns").count()
    assert first >= 1
    assert count_after_first == count_after_second  # no duplication


def test_completed_campaign_cancels_calendar(django_user_model, django_capture_on_commit_callbacks):
    director = _user(django_user_model, "eve:close", rbac.ROLE_DIRECTOR)
    c = _active_campaign(name="Wrap", commander=director)
    _objective(c, status=OS.MET)
    cal.publish_campaign(c)
    assert _cal_events(c).exists()
    with django_capture_on_commit_callbacks(execute=True):
        services.set_status(c, CS.COMPLETED, director, reason="done", via_closeout=True)
    assert not _cal_events(c).exists()


# ===========================================================================
#  Evidence (doc 04 §9)
# ===========================================================================
def _login(client, user):
    client.force_login(user)


def test_evidence_rejects_http_and_empty(client, django_user_model):
    officer = _user(django_user_model, "eve:ev", rbac.ROLE_OFFICER)
    c = _active_campaign(commander=officer)
    _login(client, officer)
    url = reverse("campaigns:evidence_create", args=[c.pk])

    client.post(url, {"attached_kind": "campaign", "url": "http://insecure.example"})
    assert CampaignEvidence.objects.count() == 0

    client.post(url, {"attached_kind": "campaign", "url": "", "note": ""})
    assert CampaignEvidence.objects.count() == 0

    client.post(url, {"attached_kind": "campaign", "url": "https://ok.example", "note": "ref"})
    assert CampaignEvidence.objects.filter(campaign=c, url="https://ok.example").count() == 1


def test_evidence_delete_permissions(client, django_user_model):
    officer = _user(django_user_model, "eve:ev2", rbac.ROLE_OFFICER)
    stranger = _user(django_user_model, "eve:stranger", rbac.ROLE_MEMBER)
    c = _active_campaign(commander=officer)
    ev = CampaignEvidence.objects.create(
        campaign=c, attached_kind="campaign", attached_id=c.pk, note="mine", added_by=officer
    )
    auto = CampaignEvidence.objects.create(
        campaign=c, attached_kind="campaign", attached_id=c.pk, note="auto", added_by=None
    )

    # A non-author, non-manager member cannot delete.
    _login(client, stranger)
    assert client.post(reverse("campaigns:evidence_delete", args=[ev.pk])).status_code == 403
    # Auto-written evidence is immutable, even for a manager.
    _login(client, officer)
    assert client.post(reverse("campaigns:evidence_delete", args=[auto.pk])).status_code == 403
    # The author/manager removes their own row.
    client.post(reverse("campaigns:evidence_delete", args=[ev.pk]))
    assert not CampaignEvidence.objects.filter(pk=ev.pk).exists()


# ===========================================================================
#  Volunteering (doc 10 §6.5)
# ===========================================================================
def test_volunteer_creates_self_task_and_activity(client, django_user_model):
    member = _user(django_user_model, "eve:vol", rbac.ROLE_MEMBER)
    c = _active_campaign()
    o = _objective(c, status=OS.ACTIVE, help_wanted=True)
    _login(client, member)
    client.post(reverse("campaigns:volunteer", args=[o.pk]))

    task = o.linked_tasks().get()
    assert task.assignee_id == member.pk
    assert CampaignActivity.objects.filter(
        campaign=c, verb="objective.volunteer", target_id=o.pk
    ).exists()
    # No pingboard alert: doc 09 defines no volunteer event (activity-only nudge).
    assert _campaign_alerts().count() == 0


def test_volunteer_is_idempotent(client, django_user_model):
    member = _user(django_user_model, "eve:vol2", rbac.ROLE_MEMBER)
    c = _active_campaign()
    o = _objective(c, status=OS.ACTIVE, help_wanted=True)
    _login(client, member)
    client.post(reverse("campaigns:volunteer", args=[o.pk]))
    client.post(reverse("campaigns:volunteer", args=[o.pk]))
    assert o.linked_tasks().filter(assignee=member).count() == 1


# ===========================================================================
#  Officer workspace (doc 10 §6.7)
# ===========================================================================
def test_workspace_gating(client, django_user_model):
    officer = _user(django_user_model, "eve:wo", rbac.ROLE_OFFICER)
    plain = _user(django_user_model, "eve:plain", rbac.ROLE_MEMBER)
    url = reverse("campaigns:workspace")

    _login(client, plain)
    assert client.get(url).status_code == 403

    _login(client, officer)
    assert client.get(url).status_code == 200


def test_workspace_owner_access_and_contents(client, django_user_model):
    owner = _user(django_user_model, "eve:owner3", rbac.ROLE_MEMBER)
    c = _active_campaign()
    _objective(c, owner=owner, status=OS.ACTIVE,
               due_at=timezone.now() - timezone.timedelta(days=2))
    assert services.workspace_access(owner) is True
    _login(client, owner)
    resp = client.get(reverse("campaigns:workspace"))
    assert resp.status_code == 200
    queues = services.workspace_queues(owner)
    assert len(queues["overdue"]) == 1


# ===========================================================================
#  Pilot Command-Center panel (doc 10 §6.5)
# ===========================================================================
def test_pilot_panel_content_and_hide(django_user_model):
    owner = _user(django_user_model, "eve:pilot", rbac.ROLE_MEMBER)
    c = _active_campaign(name="Push")
    _objective(c, owner=owner, status=OS.ACTIVE)
    panel = services.pilot_panel(owner)
    assert panel["has_content"] is True
    assert c in panel["campaigns"]

    from apps.identity.views import _campaigns_panel

    assert _campaigns_panel(owner) is not None


def test_pilot_panel_empty_for_pilot_with_no_visible_campaigns(django_user_model):
    lonely = _user(django_user_model, "eve:lonely", rbac.ROLE_MEMBER)
    Campaign.objects.create(name="Secret", status=CS.ACTIVE, visibility=V.DIRECTORS)
    panel = services.pilot_panel(lonely)
    assert panel["has_content"] is False


def test_dashboard_panel_template_renders(django_user_model):
    from django.template.loader import render_to_string

    owner = _user(django_user_model, "eve:render", rbac.ROLE_MEMBER)
    c = _active_campaign(name="Renderable", rationale="why it matters to us")
    _objective(c, owner=owner, status=OS.ACTIVE)
    _objective(c, title="Help", status=OS.ACTIVE, help_wanted=True)  # not owned by owner
    html = render_to_string(
        "campaigns/_dashboard_panel.html", {"campaigns_panel": services.pilot_panel(owner)}
    )
    assert "Renderable" in html
    assert "I can help" in html


# ===========================================================================
#  Adversarial review fixes (findings #3, #5, #17, #20)
# ===========================================================================
def _degrade(c, obj):
    obj.due_at = timezone.now() - timezone.timedelta(days=1)
    obj.save(update_fields=["due_at"])
    services.recompute(c)
    c.refresh_from_db()
    notify.health_changed(c)


def _recover(c, obj):
    obj.due_at = timezone.now() + timezone.timedelta(days=5)
    obj.save(update_fields=["due_at"])
    services.recompute(c)
    c.refresh_from_db()
    notify.health_changed(c)


def test_health_recurrence_after_recovery_redelivers(django_user_model, monkeypatch):
    # degrade → recover → degrade with the SAME reason set must deliver a fresh alert each time; the
    # per-firing stamp on the idempotency key stops the permanent id-key match swallowing the
    # recurrence (#3). Pingboard's separate content-window duplicate guard is short-lived and long
    # expired by the time a real recurrence fires hours later — neutralised here to isolate the fix.
    monkeypatch.setattr("apps.pingboard.ratelimit.is_duplicate", lambda *a, **k: False)
    c = _active_campaign(name="Recur")
    obj = _objective(c, status=OS.ACTIVE, current_value=Decimal(1), target_value=Decimal(10),
                     baseline_value=Decimal(0))
    _degrade(c, obj)
    _recover(c, obj)
    _degrade(c, obj)
    at_risk = _campaign_alerts().filter(idempotency_key__contains=":at_risk:")
    assert at_risk.count() == 2  # the recurrence delivered a second, distinct alert
    assert at_risk.values("idempotency_key").distinct().count() == 2


def test_directors_visibility_publishes_director_tier(django_user_model):
    # A directors-visibility campaign publishes a director-tier calendar row, not officer-tier, so
    # officers 404'd off the campaign never see its calendar (#5).
    c = _active_campaign(name="Brass", visibility=V.DIRECTORS)
    cal.publish_campaign(c)
    ev = _cal_events(c).get(source_object_id=f"campaign:{c.pk}")
    assert ev.visibility == "director"


def test_health_alert_appends_commander_and_sponsor(django_user_model):
    # A member-commander must receive an individually-targeted health leg even though the role-kind
    # broadcast goes to officers (doc 09 line 120, #17).
    commander = _user(django_user_model, "eve:memcmd", rbac.ROLE_MEMBER)
    c = _active_campaign(name="Frontline", visibility=V.MEMBERS, commander=commander)
    _objective(c, status=OS.ACTIVE, due_at=timezone.now() - timezone.timedelta(days=1),
               current_value=Decimal(1), target_value=Decimal(10), baseline_value=Decimal(0))
    services.recompute(c)
    c.refresh_from_db()
    notify.health_changed(c)
    health = list(_campaign_alerts().filter(idempotency_key__startswith="campaigns:health:"))
    users_legs = [a for a in health if a.audience.get("kind") == "users"]
    assert users_legs
    assert any(commander.pk in a.audience.get("ids", []) for a in users_legs)


def test_operation_link_is_idempotent_and_unlinks(django_user_model):
    # Link is idempotent per the (campaign, operation_id) uniqueness; unlink removes it (#20).
    from apps.operations.models import Operation

    officer = _user(django_user_model, "eve:oplink", rbac.ROLE_OFFICER)
    c = _active_campaign(name="OpsLink")
    op = Operation.objects.create(name="Home Defence")
    first = services.link_operation(c, officer, op.pk, note="primary fleet")
    again = services.link_operation(c, officer, op.pk)
    assert first.pk == again.pk
    assert c.linked_operations.count() == 1
    assert services.unlink_operation(c, officer, op.pk) is True
    assert c.linked_operations.count() == 0


def test_operation_link_views(client, django_user_model):
    from apps.operations.models import Operation

    officer = _user(django_user_model, "eve:opview", rbac.ROLE_OFFICER, corp_member=True)
    c = _active_campaign(name="OpsView", commander=officer)
    op = Operation.objects.create(name="Convoy Escort")
    client.force_login(officer)
    resp = client.post(reverse("campaigns:operation_link", args=[c.pk]), {"operation_id": op.pk})
    assert resp.status_code == 302 and c.linked_operations.filter(operation_id=op.pk).exists()
    resp = client.post(reverse("campaigns:operation_unlink", args=[c.pk]), {"operation_id": op.pk})
    assert resp.status_code == 302 and not c.linked_operations.exists()
