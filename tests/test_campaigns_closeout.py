"""Campaign Command close-out, reporting, recognition and participation tests (doc 04 §11–§12,
doc 11 §2.4–§2.6, doc 12 §4.1 Contributions/§4.2 close-out flow).

The close-out POST paths (happy completion, required-field guards, the director override for open
mandatory objectives, follow-up task spawning), the report page's terminal-only gate and the
lessons library's officer gate, plus recognition separation-of-duties, opt-out anonymisation of
the participation panel, and the recognition notification emission.
"""
from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from apps.admin_audit.models import AuditLog
from apps.campaigns import services
from apps.campaigns.models import Campaign
from apps.pilots.models import ContributionEvent, PilotPreference
from apps.tasks.models import Task

from ._campaign_utils import (
    CS,
    OS,
    _advance,
    _campaign,
    _director,
    _member,
    _objective,
    _officer,
)

pytestmark = pytest.mark.django_db


def _active_campaign(django_user_model, commander, driver, **obj_kwargs):
    campaign = _campaign(commander=commander)
    obj = _objective(campaign, status=OS.ACTIVE, **obj_kwargs)
    _advance(campaign, CS.ACTIVE, driver)
    return campaign, obj


# --------------------------------------------------------------------------- #
#  Close-out POST paths
# --------------------------------------------------------------------------- #
def test_close_completed_records_outcome_and_audits(client, django_user_model,
                                                    django_capture_on_commit_callbacks):
    director = _director(django_user_model)
    campaign, obj = _active_campaign(django_user_model, director, director)
    client.force_login(director)
    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post(reverse("campaigns:close", args=[campaign.pk]), {
            "final_status": "completed",
            f"resolve_{obj.pk}": "met",
            "outcome_summary": "Fleet stood up on time.",
            "lessons_learned": "Start the SRP drive earlier.",
        })
    assert resp.status_code == 302
    campaign.refresh_from_db()
    # Close-out completes then auto-archives as its final step (doc 04 T11, #9); closure metadata
    # and the completion audit are stamped at the COMPLETED transition and survive the archive.
    assert campaign.status == CS.ARCHIVED
    assert campaign.closed_by_id == director.pk and campaign.closed_at is not None
    assert campaign.outcome_summary == "Fleet stood up on time."
    assert campaign.lessons_learned == "Start the SRP drive earlier."
    assert AuditLog.objects.filter(
        action="campaigns.completed", target_id=str(campaign.pk)
    ).exists()


def test_close_requires_outcome_and_lessons(client, django_user_model):
    director = _director(django_user_model)
    campaign, obj = _active_campaign(django_user_model, director, director)
    client.force_login(director)
    # No lessons on a completion → rejected, campaign stays active (nothing partially applied).
    resp = client.post(reverse("campaigns:close", args=[campaign.pk]), {
        "final_status": "completed", f"resolve_{obj.pk}": "met",
        "outcome_summary": "done",
    })
    assert resp.status_code == 200
    campaign.refresh_from_db()
    assert campaign.status == CS.ACTIVE


def test_close_override_blocks_non_director_allows_director(client, django_user_model,
                                                            django_capture_on_commit_callbacks):
    officer = _officer(django_user_model)
    director = _director(django_user_model)
    campaign, mandatory = _active_campaign(
        django_user_model, officer, director, is_mandatory=True
    )  # mandatory objective left open

    client.force_login(officer)
    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post(reverse("campaigns:close", args=[campaign.pk]), {
            "final_status": "completed", "outcome_summary": "x", "lessons_learned": "y",
            "override_reason": "push it through",
        })
    campaign.refresh_from_db()
    assert campaign.status == CS.ACTIVE  # a non-director cannot override the open mandatory objective

    client.force_login(director)
    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post(reverse("campaigns:close", args=[campaign.pk]), {
            "final_status": "completed", "outcome_summary": "x", "lessons_learned": "y",
            "override_reason": "director signs off the shortfall",
        })
    assert resp.status_code == 302
    campaign.refresh_from_db()
    assert campaign.status == CS.ARCHIVED  # completed then auto-archived (#9)
    assert AuditLog.objects.filter(
        action="campaigns.completed", target_id=str(campaign.pk), metadata__override=True
    ).exists()


def test_close_spawns_followup_tasks(client, django_user_model,
                                     django_capture_on_commit_callbacks):
    director = _director(django_user_model)
    campaign, obj = _active_campaign(django_user_model, director, director)
    client.force_login(director)
    with django_capture_on_commit_callbacks(execute=True):
        client.post(reverse("campaigns:close", args=[campaign.pk]), {
            "final_status": "completed", f"resolve_{obj.pk}": "met",
            "outcome_summary": "x", "lessons_learned": "y",
            "followup": str(obj.pk),
        })
    assert Task.objects.filter(
        related_type="campaign_objective", title__startswith="Follow-up:"
    ).exists()


# --------------------------------------------------------------------------- #
#  Report & lessons gating
# --------------------------------------------------------------------------- #
def test_report_is_terminal_only(client, django_user_model):
    director = _director(django_user_model)
    campaign, obj = _active_campaign(django_user_model, director, director)
    client.force_login(director)
    assert client.get(reverse("campaigns:report", args=[campaign.pk])).status_code == 404

    services.close_campaign(
        campaign, director, final_status="completed",
        resolutions={obj.pk: {"status": "met"}},
        outcome_summary="ok", lessons_learned="learned",
    )
    assert client.get(reverse("campaigns:report", args=[campaign.pk])).status_code == 200


def test_lessons_library_officer_gate(client, django_user_model):
    director = _director(django_user_model)
    officer = _officer(django_user_model)
    member = _member(django_user_model)
    campaign, obj = _active_campaign(django_user_model, director, director)
    # Member-visible so the officer can see it once closed.
    campaign.visibility = Campaign.Visibility.MEMBERS
    campaign.save(update_fields=["visibility"])
    services.close_campaign(
        campaign, director, final_status="completed",
        resolutions={obj.pk: {"status": "met"}},
        outcome_summary="ok", lessons_learned="Rehearse the route sooner.",
    )
    client.force_login(member)
    assert client.get(reverse("campaigns:lessons")).status_code == 403
    client.force_login(officer)
    resp = client.get(reverse("campaigns:lessons"))
    assert resp.status_code == 200
    assert b"Rehearse the route sooner." in resp.content


# --------------------------------------------------------------------------- #
#  Recognition — separation of duties, notify, opt-out anonymisation
# --------------------------------------------------------------------------- #
def test_recognition_separation_of_duties(django_user_model):
    officer = _officer(django_user_model)
    director = _director(django_user_model)
    member = _member(django_user_model)
    campaign = _campaign(commander=officer, recognition_mode=Campaign.RecognitionMode.POINTS)

    # A non-director cannot recognise their own account.
    with pytest.raises(ValidationError):
        services.award_recognition(campaign, officer, officer, category="fc", points=1, reason="me")
    # A director may (break-glass, raffle pattern).
    assert services.award_recognition(campaign, director, director, category="lead", points=2,
                                      reason="carried it").pk
    # An empty reason is rejected.
    with pytest.raises(ValidationError):
        services.award_recognition(campaign, member, officer, category="x", points=1, reason="   ")


def test_recognition_notifies_recipient(django_user_model, monkeypatch,
                                        django_capture_on_commit_callbacks):
    calls = []
    monkeypatch.setattr("apps.campaigns.notify.recognition", lambda row: calls.append(row.pk))
    officer = _officer(django_user_model)
    member = _member(django_user_model)
    campaign = _campaign(commander=officer, recognition_mode=Campaign.RecognitionMode.POINTS)
    with django_capture_on_commit_callbacks(execute=True):
        services.award_recognition(campaign, member, officer, category="haul", points=1,
                                   reason="great hauling")
    assert calls
    assert AuditLog.objects.filter(action="campaigns.recognition_adjusted").exists()


def test_participation_panel_optout_anonymised(django_user_model):
    officer = _officer(django_user_model)
    director = _director(django_user_model)
    p1 = _member(django_user_model, suffix="p1")
    p2 = _member(django_user_model, suffix="p2")
    campaign = _campaign(commander=officer, recognition_mode=Campaign.RecognitionMode.COUNTS,
                         recognition_public=True)
    obj = _objective(campaign, status=OS.ACTIVE)
    now = timezone.now()
    for pilot, ref in ((p1, "t1"), (p2, "t2")):
        ContributionEvent.objects.create(
            user=pilot, kind="task", magnitude=1, unit="tasks", points=1,
            ref_type="task", ref_id=ref, gap_ref=f"campaign_objective:{obj.pk}", occurred_at=now,
        )
    PilotPreference.objects.create(user=p2, public_recognition=False)  # p2 opts out
    services.bust_participation(campaign)

    # A plain member sees themselves named; the opted-out pilot folds into an anonymous count.
    panel = services.participation_panel(campaign, p1)
    named = {row["user"].pk for row in panel["rows"] if row["user"]}
    assert p1.pk in named and p2.pk not in named
    assert panel["other_count"] == 1

    # A director sees every contributor named.
    dpanel = services.participation_panel(campaign, director)
    dnamed = {row["user"].pk for row in dpanel["rows"] if row["user"]}
    assert {p1.pk, p2.pk} <= dnamed and dpanel["other_count"] == 0


def test_participation_hidden_when_mode_none(django_user_model):
    officer = _officer(django_user_model)
    campaign = _campaign(commander=officer)  # default recognition_mode = none
    panel = services.participation_panel(campaign, officer)
    assert panel["mode"] == "none" and panel["has_content"] is False


# --------------------------------------------------------------------------- #
#  Timeline & recognition page render smoke
# --------------------------------------------------------------------------- #
def test_timeline_and_recognition_pages_render(client, django_user_model):
    from apps.campaigns.models import Milestone

    officer = _officer(django_user_model)
    director = _director(django_user_model)
    campaign, _obj = _active_campaign(django_user_model, officer, director)
    Milestone.objects.create(
        campaign=campaign, title="Route validated",
        due_at=timezone.now() + timezone.timedelta(days=3),
    )
    client.force_login(officer)
    assert client.get(reverse("campaigns:timeline", args=[campaign.pk])).status_code == 200
    campaign.recognition_mode = Campaign.RecognitionMode.POINTS
    campaign.save(update_fields=["recognition_mode"])
    assert client.get(reverse("campaigns:recognition", args=[campaign.pk])).status_code == 200
