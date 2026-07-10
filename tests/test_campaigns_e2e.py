"""Campaign Command end-to-end reference scenario (doc 12 §5, brief §11, requirements §21).

A single ``django_db`` test that walks the "Establish Armour Battleship Deployment Readiness"
campaign from template instantiation through approval, start, automatic + manual measurement, a
blocked dependency flipping health, a deadline sweep, a pilot contribution, verification, close-out
(lessons + recognition + follow-up) and archival — asserting the audited verbs and the
notification / calendar effects along the way. Auto sources are faked at their ``measure`` seam;
notifications are spied at the single ``notify._emit`` chokepoint so real Alerts (and their dedup)
still happen while the test can count emissions.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.admin_audit.models import AuditLog
from apps.campaigns import metrics, services
from apps.campaigns.models import (
    Campaign,
    CampaignActivity,
    CampaignRecognition,
    Milestone,
    ObjectiveSample,
)
from apps.pilots.models import ContributionEvent
from apps.tasks import services as task_services
from apps.tasks.models import Task

from ._campaign_utils import CS, OS, _director, _member, _officer

pytestmark = pytest.mark.django_db


def _fake_measure(value):
    return lambda params: metrics.Measurement(
        value=Decimal(value), as_of=timezone.now(), detail={}
    )


def test_reference_campaign_end_to_end(client, django_user_model, monkeypatch,
                                       django_capture_on_commit_callbacks):
    director = _director(django_user_model, suffix="dir", cid=2001)
    officer = _officer(django_user_model, suffix="cmdr", cid=2002)
    pilot = _member(django_user_model, suffix="pilot", cid=2003)

    # Spy on the single notification chokepoint (keeps real Alerts + dedup, records keys).
    import apps.campaigns.notify as notify_mod

    emitted: list[str] = []
    real_emit = notify_mod._emit

    def spy_emit(campaign, key, **kw):
        emitted.append(key)
        return real_emit(campaign, key, **kw)

    monkeypatch.setattr(notify_mod, "_emit", spy_emit)

    # Fake the four backing auto sources (doc 12 §3 seam list).
    monkeypatch.setattr(metrics.get_source("doctrine.qualified_pilots"), "measure", _fake_measure(20))
    monkeypatch.setattr(metrics.get_source("stockpile.on_hand"), "measure", _fake_measure(30))
    monkeypatch.setattr(metrics.get_source("srp.reserve"), "measure", _fake_measure(15000000000))
    monkeypatch.setattr(metrics.get_source("operations.completed"), "measure", _fake_measure(0))

    # 1 · Create from template (officer POST) ------------------------------------------------
    client.force_login(officer)
    start = timezone.now()
    resp = client.post(reverse("campaigns:new"), {
        "template_key": "armour_bs_deployment", "name": "Armour BS Readiness",
        "start_at": start.strftime("%Y-%m-%dT%H:%M"),
        "target_end_at": (start + timezone.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M"),
    })
    assert resp.status_code == 302
    campaign = Campaign.objects.get(name="Armour BS Readiness")
    assert campaign.objectives.count() == 12
    assert campaign.workstreams.count() == 9
    assert campaign.status == CS.DRAFT
    assert AuditLog.objects.filter(action="campaigns.created_from_template",
                                   target_id=str(campaign.pk)).exists()
    set_status_url = reverse("campaigns:set_status", args=[campaign.pk])

    # 2 · Propose --------------------------------------------------------------------------
    with django_capture_on_commit_callbacks(execute=True):
        client.post(set_status_url, {"to": "proposed"})
    campaign.refresh_from_db()
    assert campaign.status == CS.PROPOSED
    assert CampaignActivity.objects.filter(
        campaign=campaign, verb="status.changed", after__status="proposed").exists()
    assert "campaigns.approval_needed" in emitted

    # 3 · Approve (pilot rejected, director approves) --------------------------------------
    # A plain member cannot even see a proposed campaign, so the POST is refused (404, the
    # no-existence-oracle rule) — either way the pilot never approves.
    client.force_login(pilot)
    assert client.post(set_status_url, {"to": "approved"}).status_code in (403, 404)
    client.force_login(director)
    with django_capture_on_commit_callbacks(execute=True):
        client.post(set_status_url, {"to": "approved"})
    campaign.refresh_from_db()
    assert campaign.status == CS.APPROVED
    assert AuditLog.objects.filter(action="campaigns.approved", target_id=str(campaign.pk)).exists()
    assert "campaigns.approved" in emitted

    # 4 · Start (commander) → active, calendar published, started broadcast -----------------
    client.force_login(officer)
    with django_capture_on_commit_callbacks(execute=True):
        client.post(set_status_url, {"to": "active"})
    campaign.refresh_from_db()
    assert campaign.status == CS.ACTIVE
    assert "campaigns.started" in emitted
    from apps.pingboard.models import CalendarEvent

    assert CalendarEvent.objects.filter(
        source_system="campaigns", source_object_id=f"campaign:{campaign.pk}").exists()

    # 5 · Auto measurement — refresh twice (idempotent) ------------------------------------
    services.run_metric_refresh()
    doc_obj = campaign.objectives.filter(metric_source="doctrine.qualified_pilots").first()
    doc_obj.refresh_from_db()
    assert doc_obj.current_value == Decimal(20)
    stock_obj = campaign.objectives.filter(metric_source="stockpile.on_hand").first()
    stock_obj.refresh_from_db()
    assert stock_obj.current_value == Decimal(30)
    samples = ObjectiveSample.objects.filter(objective__campaign=campaign).count()
    assert samples == 8  # eight auto objectives, one sample each
    services.run_metric_refresh()  # second run inside the min interval → no new samples
    assert ObjectiveSample.objects.filter(objective__campaign=campaign).count() == samples
    campaign.refresh_from_db()
    assert campaign.progress_pct > 0
    assert client.get(reverse("campaigns:explain", args=[campaign.pk])).status_code == 200

    # 6 · Manual update (commander) --------------------------------------------------------
    manual_obj = campaign.objectives.filter(metric_source="").first()
    resp = client.post(reverse("campaigns:objective_update_value", args=[manual_obj.pk]),
                       {"value": "2", "note": "confirmed 2 FCs across TZs"})
    manual_obj.refresh_from_db()
    assert manual_obj.current_value == Decimal(2)
    assert manual_obj.measurement_source == "manual"
    assert manual_obj.last_manual_value_by_id == officer.pk

    # 7 · Blocked dependency flips health (dedup on re-run) --------------------------------
    mandatory = campaign.objectives.filter(is_mandatory=True).first()
    milestone = campaign.milestones.first()
    services.add_dependency(campaign, "objective", mandatory.pk, "milestone", milestone.pk,
                            user=director)
    # A milestone is missed only once its due date has passed (#27), so age it into the past first.
    Milestone.objects.filter(pk=milestone.pk).update(
        due_at=timezone.now() - timezone.timedelta(days=1))
    milestone.refresh_from_db()
    emitted.clear()
    with django_capture_on_commit_callbacks(execute=True):
        services.set_milestone_status(milestone, director, Milestone.MilestoneStatus.MISSED)
    campaign.refresh_from_db()
    assert campaign.health == Campaign.Health.BLOCKED
    assert campaign.health_reasons  # a matching reason entry is populated
    health_emits = emitted.count("campaigns.health_changed")
    assert health_emits >= 1
    with django_capture_on_commit_callbacks(execute=True):
        services.recompute(campaign)
    assert emitted.count("campaigns.health_changed") == health_emits  # unchanged signature → no re-ping

    # 8 · Deadline sweep — one alert, idempotent on re-run ---------------------------------
    soon = campaign.objectives.filter(metric_source="stockpile.on_hand").first()
    soon.due_at = timezone.now() + timezone.timedelta(hours=20)
    soon.save(update_fields=["due_at"])
    emitted.clear()
    services.run_deadline_sweep()
    after_first = emitted.count("campaigns.deadline_soon")
    services.run_deadline_sweep()
    assert after_first >= 1
    assert emitted.count("campaigns.deadline_soon") == after_first  # suppressed on the second run
    from apps.pingboard.models import Alert

    assert Alert.objects.filter(
        source_service="campaigns",
        idempotency_key=f"campaigns:due:objective:{soon.pk}:24h").count() == 1

    # 9 · Pilot completes a linked task → contribution + participation ---------------------
    campaign.recognition_mode = Campaign.RecognitionMode.COUNTS
    campaign.recognition_public = True
    campaign.save(update_fields=["recognition_mode", "recognition_public"])
    task_obj = campaign.objectives.filter(metric_source="").exclude(pk=manual_obj.pk).first()
    linked = services.create_objective_task(task_obj, officer, title="Stage the route test",
                                            assignee=pilot)
    with django_capture_on_commit_callbacks(execute=True):
        task_services.set_status(linked, pilot, Task.Status.DONE)
    assert CampaignActivity.objects.filter(
        campaign=campaign, target_id=task_obj.pk, verb__startswith="objective.task_").exists()
    assert ContributionEvent.objects.filter(
        user=pilot, gap_ref=f"campaign_objective:{task_obj.pk}").exists()
    panel = services.participation_panel(campaign, director)
    assert pilot.pk in {row["user"].pk for row in panel["rows"] if row["user"]}

    # 10 · Verification — pilot claim doesn't count until an officer verifies ---------------
    ver_obj = campaign.objectives.filter(requires_verification=True).first()
    ver_obj.owner = pilot
    ver_obj.status = OS.ACTIVE
    ver_obj.save(update_fields=["owner", "status"])
    client.force_login(pilot)
    client.post(reverse("campaigns:objective_set_status", args=[ver_obj.pk]), {"to": "met"})
    ver_obj.refresh_from_db()
    assert ver_obj.status == OS.MET and ver_obj.verified_by_id is None
    client.force_login(officer)
    client.post(reverse("campaigns:objective_verify", args=[ver_obj.pk]))
    ver_obj.refresh_from_db()
    assert ver_obj.verified_by_id == officer.pk

    # 11 · Unblock + drive remaining mandatory objectives to met+verified ------------------
    services.resolve_dependency(campaign.dependencies.first(), director,
                                reason="external blocker cleared")
    for obj in campaign.objectives.filter(is_mandatory=True):
        obj.refresh_from_db()
        if obj.status != OS.MET:
            services.set_objective_status(obj, director, OS.MET)
        obj.refresh_from_db()
        if obj.requires_verification and obj.verified_by_id is None:
            services.verify_objective(obj, officer)

    # 12 · Close-out (commander) — completed, lessons, recognition, follow-up ---------------
    client.force_login(officer)
    close_data = {
        "final_status": "completed",
        "outcome_summary": "Fleet stood up in staging on schedule.",
        "lessons_learned": "Kick off the SRP drive and route validation earlier.",
        "followup": str(task_obj.pk),
        "rec_user": str(pilot.pk), "rec_category": "logistics",
        "rec_points": "5", "rec_reason": "Hauled and staged the fleet",
    }
    for obj in campaign.objectives.all():
        obj.refresh_from_db()
        if obj.status not in (OS.MET, OS.MISSED, OS.DROPPED):
            close_data[f"resolve_{obj.pk}"] = "met"
    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post(reverse("campaigns:close", args=[campaign.pk]), close_data)
    assert resp.status_code == 302
    campaign.refresh_from_db()
    # Close-out completes then auto-archives as its final step, so the campaign lands ARCHIVED —
    # read-only forever (doc 04 T11, #9).
    assert campaign.status == CS.ARCHIVED
    assert campaign.outcome_summary.startswith("Fleet stood up")
    assert campaign.lessons_learned
    assert CampaignRecognition.objects.filter(campaign=campaign, user=pilot).exists()
    # Officer-visibility campaign → the follow-up task carries a neutral title and is assigned (never
    # left in the corp-wide claimable open pool, #1).
    followups = Task.objects.filter(
        related_type="campaign_objective", title="Campaign follow-up task")
    assert followups.exists()
    assert not followups.filter(is_open=True).exists()
    assert AuditLog.objects.filter(action="campaigns.completed", target_id=str(campaign.pk)).exists()
    assert AuditLog.objects.filter(action="campaigns.recognition_adjusted").exists()
    assert "campaigns.completed" in emitted

    # 13 · Already archived by close-out — re-posting archive is an idempotent no-op ---------
    with django_capture_on_commit_callbacks(execute=True):
        client.post(set_status_url, {"to": "archived"})
    campaign.refresh_from_db()
    assert campaign.status == CS.ARCHIVED
    client.force_login(director)
    assert client.get(reverse("campaigns:report", args=[campaign.pk])).status_code == 200
    before = ObjectiveSample.objects.filter(objective__campaign=campaign).count()
    services.run_metric_refresh()  # archived campaigns are excluded from the beat
    assert ObjectiveSample.objects.filter(objective__campaign=campaign).count() == before
    lessons = client.get(reverse("campaigns:lessons"))
    assert lessons.status_code == 200
    assert b"Armour BS Readiness" in lessons.content
