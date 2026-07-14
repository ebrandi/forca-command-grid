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

# The shipped ``de`` catalogue has no msgstr for the engine scaffolds yet, so a plain
# ``translation.override("de")`` would still hand back the English msgid and the Seam-B bug would
# stay invisible. Seeding the msgstrs — what a translator filling in that .po entry does — is what
# makes the seam testable. Reuses the catalogue helper from the campaigns suite.
from tests.test_campaigns_services import _translated_de


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


# ===========================================================================
#  Seam B: the engine's prose is PERSISTED by a locale-less worker and read
#  back by officers of eight locales. It must render in the READER's language.
# ===========================================================================
_DE_MESSAGE = (
    "Bestand an %(type_name)s ist %(current)s gegenüber einem Ziel von %(target)s. "
    "Bauen oder kaufen: %(deficit)s."
)
_DE_LOGIC = "aktuell < Ziel um den Fehlbetrag"

_SEEDED_DE = {
    "Stock of %(type_name)s is %(current)s against a target of %(target)s. "
    "Build or buy %(deficit)s.": _DE_MESSAGE,
    "current < target by deficit": _DE_LOGIC,
}


def _shortage_rec():
    sp = Stockpile.objects.create(name="Staging")
    record_manual_stock(sp, type_id=587, quantity_current=4, quantity_target=40)
    engine.run_all()
    return Recommendation.objects.get(type=Recommendation.Type.STOCK_SHORTAGE, subject_id="587")


@pytest.mark.django_db
def test_engine_persists_scaffold_key_and_english_prose(sde):
    """The write side (the beat, under English) stores BOTH halves: key+params and the prose."""
    rec = _shortage_rec()

    assert rec.message_key == "stock_shortage.message"
    assert rec.logic_summary_key == "stock_shortage.logic"
    # Params are plain JSON-safe values — a lazy proxy in a JSONField is a TypeError at save time,
    # so this is the invariant that keeps the write path from blowing up.
    assert rec.message_params == {
        "type_name": engine._tname(587), "current": 4, "target": 40, "deficit": 36,
    }
    assert rec.logic_summary_params == {}

    # English output is unchanged — the prose column is still exactly what it always was.
    rec.refresh_from_db()
    assert rec.message == f"Stock of {engine._tname(587)} is 4 against a target of 40. Build or buy 36."
    assert rec.logic_summary == "current < target by deficit"


@pytest.mark.django_db
def test_recommendation_renders_in_the_readers_locale_not_the_writers(sde):
    """THE POINT. Written by the (English, locale-less) worker → read back by a German officer."""
    rec = _shortage_rec()

    with _translated_de(**_SEEDED_DE):
        # A different reader, a different locale, the SAME row.
        fresh = Recommendation.objects.get(pk=rec.pk)
        assert fresh.message_i18n == (
            f"Bestand an {engine._tname(587)} ist 4 gegenüber einem Ziel von 40. "
            "Bauen oder kaufen: 36."
        )
        assert fresh.logic_summary_i18n == _DE_LOGIC
        # The stored prose is untouched: it is the audit record, not the display value.
        assert fresh.message.startswith("Stock of ")


@pytest.mark.django_db
def test_worker_locale_never_leaks_into_the_stored_prose(sde):
    """The original trap: if the beat happened to run under a non-English locale, a naive ``_()``
    would freeze GERMAN into the row and every English reader would then see it. ``messages.english``
    pins the persisted column to English regardless of the worker's active locale."""
    with _translated_de(**_SEEDED_DE):
        rec = _shortage_rec()  # the engine runs with ``de`` active

    rec.refresh_from_db()
    assert rec.message == f"Stock of {engine._tname(587)} is 4 against a target of 40. Build or buy 36."
    assert rec.logic_summary == "current < target by deficit"
    # …and it still renders German for a German reader, because the key survived.
    with _translated_de(**_SEEDED_DE):
        assert Recommendation.objects.get(pk=rec.pk).logic_summary_i18n == _DE_LOGIC


@pytest.mark.django_db
def test_legacy_row_without_a_key_renders_its_stored_english_verbatim():
    """Nothing is backfilled. A row written before this change (or by a caller outside the engine,
    e.g. apps.admin_audit.tasks) has no key and must degrade to its stored English — NEVER to blank."""
    legacy = Recommendation.objects.create(
        type=Recommendation.Type.STOCK_SHORTAGE,
        subject_type="type", subject_id="587",
        message="Stock of Rifter is 4 against a target of 40. Build or buy 36.",
        logic_summary="current < target by deficit",
    )
    assert legacy.message_key == ""
    assert legacy.message_params == {}

    with _translated_de(**_SEEDED_DE):
        fresh = Recommendation.objects.get(pk=legacy.pk)
        # The seeded German msgstr exists — but with no key there is nothing to resolve, so the
        # stored English stands. This is the fallback that keeps legacy rows readable.
        assert fresh.message_i18n == "Stock of Rifter is 4 against a target of 40. Build or buy 36."
        assert fresh.logic_summary_i18n == "current < target by deficit"


@pytest.mark.django_db
def test_a_broken_translation_degrades_to_english_rather_than_blanking(sde):
    """A translator who drops or renames a %(param)s must not blank an officer's dashboard."""
    rec = _shortage_rec()
    broken = {
        "Stock of %(type_name)s is %(current)s against a target of %(target)s. "
        "Build or buy %(deficit)s.": "Bestand an %(nicht_vorhanden)s.",
    }
    with _translated_de(**broken):
        fresh = Recommendation.objects.get(pk=rec.pk)
        assert fresh.message_i18n == fresh.message  # falls back, never raises, never blank
        assert fresh.message_i18n != ""


@pytest.mark.django_db
def test_idempotency_check_still_compares_the_english_prose(sde):
    """``persist_drafts`` dedupes on ``message``. That column stays English for every writer, so a
    beat that ran under a different locale cannot supersede-and-recreate (and re-alert) the row."""
    sp = Stockpile.objects.create(name="Staging")
    record_manual_stock(sp, type_id=587, quantity_current=4, quantity_target=40)
    engine.run_all()
    with _translated_de(**_SEEDED_DE):
        engine.run_all()  # a German-locale worker must still see its own finding as unchanged

    assert Recommendation.objects.filter(
        type=Recommendation.Type.STOCK_SHORTAGE, subject_id="587",
        state=Recommendation.State.NEW,
    ).count() == 1
