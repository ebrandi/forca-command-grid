"""Recommendation engine, action queue, alerts, and dashboard tests."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.admin_audit.models import AuditLog
from apps.killboard.ingest import ingest_killmail
from apps.recommendations import engine
from apps.recommendations.models import ActionQueueItem, Alert, Recommendation
from apps.recommendations.notify import dispatch_alerts
from apps.recommendations.services import act_on_recommendation, build_action_queue
from apps.stockpile.models import Stockpile
from apps.stockpile.services import record_manual_stock


@pytest.mark.django_db
def test_stock_shortage_recommendation(sde):
    sp = Stockpile.objects.create(name="Staging")
    record_manual_stock(sp, type_id=587, quantity_current=4, quantity_target=40)

    engine.run_all()
    rec = Recommendation.objects.get(type=Recommendation.Type.STOCK_SHORTAGE, subject_id="587")
    assert "Build or buy 36" in rec.message
    assert rec.suggested_action["quantity"] == 36
    assert rec.confidence == Recommendation.Confidence.HIGH


@pytest.mark.django_db
def test_rerun_with_identical_finding_is_idempotent(sde):
    """An unchanged finding must be a no-op on rerun — NOT supersede-and-recreate.
    Recreating an identical NEW rec every run is what re-fired its alert every 30 min
    (the notification loop). Still exactly one open rec; no supersede churn."""
    sp = Stockpile.objects.create(name="Staging")
    record_manual_stock(sp, type_id=587, quantity_current=4, quantity_target=40)
    engine.run_all()
    engine.run_all()
    open_recs = Recommendation.objects.filter(
        type=Recommendation.Type.STOCK_SHORTAGE,
        subject_id="587",
        state=Recommendation.State.NEW,
    )
    assert open_recs.count() == 1
    assert Recommendation.objects.filter(state=Recommendation.State.SUPERSEDED).count() == 0


@pytest.mark.django_db
def test_combat_loss_pattern(sde):
    recent = (timezone.now() - timedelta(days=1)).isoformat()
    for i in range(3):
        body = {
            "killmail_id": 9000 + i,
            "killmail_time": recent,
            "solar_system_id": 30002053,
            "victim": {"corporation_id": 98000001, "ship_type_id": 587, "items": []},
            "attackers": [{"character_id": 1, "corporation_id": 99}],
        }
        ingest_killmail(9000 + i, f"h{i}", body=body)

    drafts = engine.eval_combat_loss_pattern(threshold=3)
    assert any(d["inputs"]["ship_type_id"] == 587 and d["inputs"]["count"] == 3 for d in drafts)


@pytest.mark.django_db
def test_identical_combat_loss_does_not_reloop_alerts(sde):
    """The reported bug: a rolling-window loss finding ("Lost N × ship in the last 7d")
    re-broadcast every 30 min. An unchanged finding must NOT recreate a rec (so there's
    nothing new to dispatch); a genuine change (a new loss) supersedes + re-alerts once."""
    def _loss(i):
        ingest_killmail(9000 + i, f"h{i}", body={
            "killmail_id": 9000 + i,
            "killmail_time": (timezone.now() - timedelta(days=1)).isoformat(),
            "solar_system_id": 30002053,
            "victim": {"corporation_id": 98000001, "ship_type_id": 587, "items": []},
            "attackers": [{"character_id": 1, "corporation_id": 99}],
        })

    for i in range(3):
        _loss(i)
    engine.run_all()
    assert dispatch_alerts(min_severity=1) >= 1          # alerted once

    # Rerun with the SAME losses → no new rec, nothing new to broadcast (was the loop).
    engine.run_all()
    assert dispatch_alerts(min_severity=1) == 0
    assert Recommendation.objects.filter(
        type=Recommendation.Type.COMBAT_LOSS_PATTERN, state=Recommendation.State.NEW
    ).count() == 1
    assert Recommendation.objects.filter(state=Recommendation.State.SUPERSEDED).count() == 0

    # A genuine change (a 4th loss → new count → new message) supersedes + re-alerts once.
    _loss(3)
    engine.run_all()
    assert dispatch_alerts(min_severity=1) == 1
    assert Recommendation.objects.filter(
        type=Recommendation.Type.COMBAT_LOSS_PATTERN, state=Recommendation.State.SUPERSEDED
    ).count() == 1


@pytest.mark.django_db
def test_action_queue_and_close_loop(sde, django_user_model):
    sp = Stockpile.objects.create(name="Staging")
    record_manual_stock(sp, type_id=587, quantity_current=4, quantity_target=40)
    engine.run_all()
    assert build_action_queue() >= 1

    officer = django_user_model.objects.create(username="off")
    rec = Recommendation.objects.filter(type=Recommendation.Type.STOCK_SHORTAGE).first()
    act_on_recommendation(rec, officer, "action")
    rec.refresh_from_db()
    assert rec.state == Recommendation.State.ACTIONED
    assert rec.closed_by_id == officer.id
    assert ActionQueueItem.objects.get(recommendation=rec).status == ActionQueueItem.Status.DONE
    assert AuditLog.objects.filter(action="recommendation.action", actor=officer).exists()


@pytest.mark.django_db
def test_dispatch_alerts_for_high_severity(sde):
    sp = Stockpile.objects.create(name="Staging")
    record_manual_stock(sp, type_id=587, quantity_current=0, quantity_target=80)  # deficit 80 -> sev high
    engine.run_all()
    sent = dispatch_alerts(min_severity=50)
    assert sent >= 1
    assert Alert.objects.filter(channel=Alert.Channel.IN_APP).exists()


@pytest.mark.django_db
def test_dispatch_alerts_fans_out_to_pingboard_discord(sde, monkeypatch):
    """The retired NotificationChannel webhook loop is replaced by Pingboard: a dispatched
    rec fans out to armed Discord — but, being officer/leadership content, only to a
    channel designated a leadership channel (a corp-wide channel is skipped at the sink).
    The in-app row is unchanged."""
    from apps.pingboard.models import ChannelProvider

    posts = []
    monkeypatch.setattr(
        "apps.pingboard.providers.discord.requests.post",
        lambda url, json=None, timeout=None, allow_redirects=None: posts.append(json["content"])
        or type("R", (), {"status_code": 204})(),
    )
    # A leadership channel (ceiling raised to high_command) receives officer digests.
    provider = ChannelProvider(kind="discord", label="command", enabled=True,
                               max_classification="high_command")
    provider.secret = "https://discord.com/api/webhooks/1/tok"
    provider.save()

    sp = Stockpile.objects.create(name="Staging")
    record_manual_stock(sp, type_id=587, quantity_current=0, quantity_target=80)
    engine.run_all()
    assert dispatch_alerts(min_severity=50) >= 1
    assert Alert.objects.filter(channel=Alert.Channel.IN_APP).exists()  # in-app row still written
    assert posts  # and the rec fanned out to the leadership Discord channel


@pytest.mark.django_db
def test_officer_dashboard_permissions(client, django_user_model):
    from apps.identity.models import RoleAssignment
    from apps.sso.services import ensure_role
    from core import rbac

    assert client.get("/recommendations/officer/").status_code == 302  # anon

    member = django_user_model.objects.create(username="m")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(member)
    assert client.get("/recommendations/officer/").status_code == 403  # member, not officer
    # The personal page merged into the Daily Briefing — members are redirected.
    assert client.get("/recommendations/mine/").status_code == 302

    officer = django_user_model.objects.create(username="o")
    RoleAssignment.objects.create(user=officer, role=ensure_role(rbac.ROLE_OFFICER))
    client.force_login(officer)
    assert client.get("/recommendations/officer/").status_code == 200


@pytest.mark.django_db
def test_composite_score_factors_confidence_and_isk():
    """REC-6: ranking is severity × confidence × ISK, not severity alone."""
    from apps.recommendations.services import composite_score

    base = dict(type=Recommendation.Type.STOCK_SHORTAGE, message="x", required_permission="officer", severity=50)
    high = Recommendation.objects.create(**base, confidence=Recommendation.Confidence.HIGH, isk_impact=0)
    low = Recommendation.objects.create(**base, confidence=Recommendation.Confidence.LOW, isk_impact=0)
    rich = Recommendation.objects.create(**base, confidence=Recommendation.Confidence.HIGH, isk_impact=1_000_000_000)
    # Same severity: higher confidence outranks lower; ISK at stake breaks a tie up.
    assert composite_score(high) > composite_score(low)
    assert composite_score(rich) > composite_score(high)


@pytest.mark.django_db
def test_rec_link_to_project(client, django_user_model, sde):
    """REC-5: an officer links a rec's action item to a build project; targets are
    validated and members can't link."""
    from apps.identity.models import RoleAssignment
    from apps.industry.models import IndustryProject
    from apps.sso.services import ensure_role
    from core import rbac

    rec = Recommendation.objects.create(
        type=Recommendation.Type.BUILD_VS_BUY, message="m", required_permission="officer", severity=20
    )
    project = IndustryProject.objects.create(name="Capital build", status=IndustryProject.Status.ACTIVE)

    officer = django_user_model.objects.create(username="o2")
    RoleAssignment.objects.create(user=officer, role=ensure_role(rbac.ROLE_OFFICER))
    client.force_login(officer)
    assert client.post(f"/recommendations/{rec.pk}/link/", {"project_id": project.pk}).status_code == 302
    item = ActionQueueItem.objects.get(recommendation=rec)
    assert item.linked_project_id == project.pk

    # A bogus project id is rejected (linkage cleared rather than dangling).
    client.post(f"/recommendations/{rec.pk}/link/", {"project_id": 999999})
    item.refresh_from_db()
    assert item.linked_project_id is None

    member = django_user_model.objects.create(username="m2")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(member)
    assert client.post(f"/recommendations/{rec.pk}/link/", {"project_id": project.pk}).status_code == 403
