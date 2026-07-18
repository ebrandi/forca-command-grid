"""KB-20 — R2Z2 realtime killmail fallback: adapter, consumer, and source-health.

The fallback is a supplementary latency-reducer, never a replacement for the primary feeds,
so these tests pin: it is OFF by default and makes zero HTTP when disabled; it ingests only
home-corp killmails; it resumes/advances its cursor safely (incl. gaps and long outages);
and ingestion stays idempotent.
"""
from __future__ import annotations

import pytest
import responses
from django.test import override_settings

from apps.killboard import killstream
from apps.killboard.ingest_health import ingest_status
from apps.killboard.models import IngestSourceHealth, Killmail, KillstreamState
from core.esi.adapters import r2z2

HOME = 98000001  # config/settings/test.py FORCA_HOME_CORP_ID
SEQ_URL = "https://r2z2.zkillboard.com/ephemeral/sequence.json"


def _pkg_url(seq: int) -> str:
    return f"https://r2z2.zkillboard.com/ephemeral/{seq}.json"


def _package(seq, killmail_id, *, victim_corp=None, attacker_corps=(), system_id=30002053):
    """An R2Z2 package with the ESI body under ``esi`` (the real shape, verified live)."""
    return {
        "killmail_id": killmail_id,
        "hash": f"h{killmail_id}",
        "sequence_id": seq,
        "uploaded_at": 1,
        "zkb": {"hash": f"h{killmail_id}", "totalValue": 1000.0, "npc": False},
        "esi": {
            "killmail_id": killmail_id,
            "killmail_time": "2026-07-18T10:00:00Z",
            "solar_system_id": system_id,
            "victim": {"corporation_id": victim_corp, "ship_type_id": 587, "items": []},
            "attackers": [{"character_id": 10 + i, "corporation_id": c}
                          for i, c in enumerate(attacker_corps)],
        },
    }


def _enable(**extra) -> KillstreamState:
    ks = KillstreamState.load()
    ks.enabled = True
    for k, v in extra.items():
        setattr(ks, k, v)
    ks.save()
    return ks


# --- adapter ----------------------------------------------------------------
@responses.activate
def test_latest_sequence():
    responses.add(responses.GET, SEQ_URL, json={"sequence": 500}, status=200)
    assert r2z2.latest_sequence() == 500


@responses.activate
def test_fetch_package_200_and_404():
    responses.add(responses.GET, _pkg_url(101), json=_package(101, 111), status=200)
    responses.add(responses.GET, _pkg_url(102), status=404)
    assert r2z2.fetch_package(101)["killmail_id"] == 111
    assert r2z2.fetch_package(102) is None


def test_package_to_ingest():
    kid, khash, esi = r2z2.package_to_ingest(_package(101, 111, victim_corp=HOME))
    assert (kid, khash) == (111, "h111")
    assert esi["solar_system_id"] == 30002053
    assert r2z2.package_to_ingest({"killmail_id": 1}) is None  # no hash / esi
    assert r2z2.package_to_ingest({}) is None
    assert r2z2.package_to_ingest(None) is None


# --- consumer ---------------------------------------------------------------
@responses.activate
@pytest.mark.django_db
@override_settings(FORCA_HOME_CORP_ID=HOME)
def test_disabled_is_noop_and_makes_no_http():
    assert KillstreamState.load().enabled is False
    res = killstream.consume_killstream(sleep_s=0)
    assert res["status"] == "disabled"
    assert res["ingested"] == 0
    assert len(responses.calls) == 0  # never touched the network


@responses.activate
@pytest.mark.django_db
@override_settings(FORCA_HOME_CORP_ID=0)
def test_no_op_without_home_corp():
    _enable()
    res = killstream.consume_killstream(sleep_s=0)
    assert res["status"] == "disabled"
    assert len(responses.calls) == 0


@responses.activate
@pytest.mark.django_db
@override_settings(FORCA_HOME_CORP_ID=HOME)
def test_first_run_initializes_cursor_to_tip():
    _enable()  # no cursor yet
    responses.add(responses.GET, SEQ_URL, json={"sequence": 900}, status=200)
    res = killstream.consume_killstream(sleep_s=0)
    assert res["status"] == "initialized"
    ks = KillstreamState.load()
    assert ks.last_sequence == 900  # started fresh at the tip; no history replay
    assert Killmail.objects.count() == 0


@responses.activate
@pytest.mark.django_db
@override_settings(FORCA_HOME_CORP_ID=HOME)
def test_catch_up_ingests_only_home_corp(sde, monkeypatch):
    monkeypatch.setattr("core.esi.names.backfill_killmail_names", lambda: 0)
    _enable(last_sequence=100)
    responses.add(responses.GET, SEQ_URL, json={"sequence": 103}, status=200)
    # 101: home is the victim -> ingest; 102: unrelated -> skip; 103: home is an attacker -> ingest.
    responses.add(responses.GET, _pkg_url(101),
                  json=_package(101, 111, victim_corp=HOME), status=200)
    responses.add(responses.GET, _pkg_url(102),
                  json=_package(102, 222, victim_corp=99, attacker_corps=[88]), status=200)
    responses.add(responses.GET, _pkg_url(103),
                  json=_package(103, 333, victim_corp=99, attacker_corps=[HOME]), status=200)

    res = killstream.consume_killstream(sleep_s=0)

    assert res["status"] == "ok"
    assert res["scanned"] == 3
    assert res["ingested"] == 2
    assert set(Killmail.objects.values_list("killmail_id", flat=True)) == {111, 333}
    assert Killmail.objects.get(killmail_id=111).source == "killstream"
    ks = KillstreamState.load()
    assert ks.last_sequence == 103
    assert ks.last_run_ingested == 2
    # health row updated
    assert IngestSourceHealth.objects.get(source="killstream").last_count == 2


@responses.activate
@pytest.mark.django_db
@override_settings(FORCA_HOME_CORP_ID=HOME)
def test_404_below_tip_is_skipped_not_stalled(sde, monkeypatch):
    """A missing sequence below the tip must be skipped so it can never stall the cursor."""
    monkeypatch.setattr("core.esi.names.backfill_killmail_names", lambda: 0)
    _enable(last_sequence=100)
    responses.add(responses.GET, SEQ_URL, json={"sequence": 103}, status=200)
    responses.add(responses.GET, _pkg_url(101), json=_package(101, 111, victim_corp=HOME), status=200)
    responses.add(responses.GET, _pkg_url(102), status=404)  # gap below tip
    responses.add(responses.GET, _pkg_url(103), json=_package(103, 333, victim_corp=HOME), status=200)

    res = killstream.consume_killstream(sleep_s=0)

    assert res["status"] == "ok"
    assert set(Killmail.objects.values_list("killmail_id", flat=True)) == {111, 333}
    assert KillstreamState.load().last_sequence == 103  # advanced past the gap


@responses.activate
@pytest.mark.django_db
@override_settings(FORCA_HOME_CORP_ID=HOME)
def test_404_at_tip_means_caught_up(sde, monkeypatch):
    """A 404 at the tip (file not flushed yet) stops the run without advancing past it."""
    monkeypatch.setattr("core.esi.names.backfill_killmail_names", lambda: 0)
    _enable(last_sequence=100)
    # sequence.json claims 102, but 102 isn't published yet.
    responses.add(responses.GET, SEQ_URL, json={"sequence": 102}, status=200)
    responses.add(responses.GET, _pkg_url(101), json=_package(101, 111, victim_corp=HOME), status=200)
    responses.add(responses.GET, _pkg_url(102), status=404)

    res = killstream.consume_killstream(sleep_s=0)

    assert res["status"] == "ok"
    assert Killmail.objects.filter(killmail_id=111).exists()
    # cursor stays at 101 so 102 is retried next run (not skipped)
    assert KillstreamState.load().last_sequence == 101


@responses.activate
@pytest.mark.django_db
@override_settings(FORCA_HOME_CORP_ID=HOME)
def test_large_gap_skips_ahead():
    """A cursor far behind the tip (long outage, past ephemeral retention) jumps ahead
    instead of walking millions of sequences — the primaries + EVE Ref backfill the gap."""
    _enable(last_sequence=1)
    tip = 1 + killstream.KILLSTREAM_MAX_GAP + 5000
    start = tip - killstream.KILLSTREAM_MAX_GAP
    responses.add(responses.GET, SEQ_URL, json={"sequence": tip}, status=200)
    responses.add(responses.GET, _pkg_url(start), json=_package(start, 501, victim_corp=99), status=200)
    responses.add(responses.GET, _pkg_url(start + 1), json=_package(start + 1, 502, victim_corp=99), status=200)

    res = killstream.consume_killstream(max_fetch=2, sleep_s=0)

    assert res["status"] == "ok"
    assert res["gap_skipped"] == start - 2
    assert KillstreamState.load().last_sequence == start + 1  # walked from the skip point


@responses.activate
@pytest.mark.django_db
@override_settings(FORCA_HOME_CORP_ID=HOME)
def test_ingestion_is_idempotent(sde, monkeypatch):
    monkeypatch.setattr("core.esi.names.backfill_killmail_names", lambda: 0)
    from apps.killboard.ingest import ingest_killmail

    ingest_killmail(111, "h111", body=_package(0, 111, victim_corp=HOME)["esi"])
    assert Killmail.objects.filter(killmail_id=111).count() == 1

    _enable(last_sequence=100)
    responses.add(responses.GET, SEQ_URL, json={"sequence": 101}, status=200)
    responses.add(responses.GET, _pkg_url(101), json=_package(101, 111, victim_corp=HOME), status=200)
    killstream.consume_killstream(sleep_s=0)

    assert Killmail.objects.filter(killmail_id=111).count() == 1  # no duplicate


@responses.activate
@pytest.mark.django_db
@override_settings(FORCA_HOME_CORP_ID=HOME)
def test_sequence_tip_error_records_failure():
    _enable(last_sequence=100)
    responses.add(responses.GET, SEQ_URL, status=503)
    res = killstream.consume_killstream(sleep_s=0)
    assert res["status"] == "error"
    assert IngestSourceHealth.objects.get(source="killstream").consecutive_failures == 1
    # cursor untouched on a tip failure
    assert KillstreamState.load().last_sequence == 100


# --- source health ----------------------------------------------------------
@pytest.mark.django_db
def test_ingest_source_health_record_transitions():
    IngestSourceHealth.record("zkill_query", count=5)
    row = IngestSourceHealth.objects.get(source="zkill_query")
    assert row.last_count == 5 and row.consecutive_failures == 0 and row.last_success_at is not None

    IngestSourceHealth.record("zkill_query", error="boom")
    row.refresh_from_db()
    assert row.consecutive_failures == 1 and row.last_error == "boom"

    IngestSourceHealth.record("zkill_query", count=2)
    row.refresh_from_db()
    assert row.consecutive_failures == 0 and row.last_error == "" and row.last_count == 2


@pytest.mark.django_db
@override_settings(FORCA_HOME_CORP_ID=HOME)
def test_ingest_status_shape():
    IngestSourceHealth.record("esi_corp", count=3)
    st = ingest_status()
    by = {s["source"]: s for s in st["sources"]}
    assert by["esi_corp"]["primary"] is True and by["esi_corp"]["verdict"] == "fresh"
    assert by["zkill_query"]["verdict"] == "idle"  # scheduled but never ran here
    assert by["killstream"]["primary"] is False and by["killstream"]["verdict"] == "off"


# --- wiring -----------------------------------------------------------------
def test_killstream_beat_is_scheduled():
    from config.celery import app

    scheduled = {entry["task"] for entry in app.conf.beat_schedule.values()}
    assert "killboard.consume_killstream" in scheduled


@pytest.mark.django_db
@override_settings(FORCA_HOME_CORP_ID=HOME)
def test_primary_zkill_poll_records_health(monkeypatch):
    """The zKill query poll records health without changing its import behaviour/return."""
    from apps.killboard import tasks

    monkeypatch.setattr(tasks, "import_from_zkill", lambda et, eid: 4)
    assert tasks.import_home_corp_from_zkill() == 4  # return contract unchanged
    row = IngestSourceHealth.objects.get(source="zkill_query")
    assert row.last_count == 4 and row.last_success_at is not None


# --- concurrency / robustness invariants (review L3) ------------------------
@responses.activate
@pytest.mark.django_db
@override_settings(FORCA_HOME_CORP_ID=HOME)
def test_second_concurrent_run_is_locked_out(monkeypatch):
    """A run that can't acquire the lock returns 'locked' and never touches the cursor."""
    _enable(last_sequence=100)
    monkeypatch.setattr("apps.killboard.killstream.cache.add", lambda *a, **k: False)
    res = killstream.consume_killstream(sleep_s=0)
    assert res["status"] == "locked"
    assert len(responses.calls) == 0  # never fetched
    assert KillstreamState.load().last_sequence == 100


def test_state_writes_never_clobber_enabled():
    """The enabled flag must never be in the fields a run persists, so a run in flight can
    never overwrite an officer toggling the feed off."""
    assert "enabled" not in killstream._STATE_FIELDS


@responses.activate
@pytest.mark.django_db
@override_settings(FORCA_HOME_CORP_ID=HOME)
def test_wall_clock_budget_stops_the_run():
    """A zero-time budget breaks the loop before any package fetch (bounds run duration)."""
    _enable(last_sequence=100)
    responses.add(responses.GET, SEQ_URL, json={"sequence": 200}, status=200)
    res = killstream.consume_killstream(max_runtime_s=0, sleep_s=0)
    assert res["status"] == "ok"
    assert res["scanned"] == 0
    assert KillstreamState.load().last_sequence == 100  # nothing walked


@responses.activate
@pytest.mark.django_db
@override_settings(FORCA_HOME_CORP_ID=HOME)
def test_mid_walk_error_persists_progress(sde, monkeypatch):
    """A network error mid-walk persists the last good sequence (retried next run, not skipped)."""
    monkeypatch.setattr("core.esi.names.backfill_killmail_names", lambda: 0)
    _enable(last_sequence=100)
    responses.add(responses.GET, SEQ_URL, json={"sequence": 103}, status=200)
    responses.add(responses.GET, _pkg_url(101), json=_package(101, 111, victim_corp=HOME), status=200)
    responses.add(responses.GET, _pkg_url(102), status=500)  # server error mid-walk

    res = killstream.consume_killstream(sleep_s=0)

    assert res["status"] == "error"
    assert Killmail.objects.filter(killmail_id=111).exists()
    assert KillstreamState.load().last_sequence == 101  # 102 retried next run
    assert IngestSourceHealth.objects.get(source="killstream").consecutive_failures == 1


@responses.activate
@pytest.mark.django_db
@override_settings(FORCA_HOME_CORP_ID=HOME)
def test_one_bad_mail_does_not_stop_the_stream(sde, monkeypatch):
    monkeypatch.setattr("core.esi.names.backfill_killmail_names", lambda: 0)
    real = killstream.ingest_killmail

    def flaky(kid, khash, **kw):
        if kid == 222:
            raise RuntimeError("bad mail")
        return real(kid, khash, **kw)

    monkeypatch.setattr(killstream, "ingest_killmail", flaky)
    _enable(last_sequence=100)
    responses.add(responses.GET, SEQ_URL, json={"sequence": 103}, status=200)
    responses.add(responses.GET, _pkg_url(101), json=_package(101, 111, victim_corp=HOME), status=200)
    responses.add(responses.GET, _pkg_url(102), json=_package(102, 222, victim_corp=HOME), status=200)
    responses.add(responses.GET, _pkg_url(103), json=_package(103, 333, victim_corp=HOME), status=200)

    res = killstream.consume_killstream(sleep_s=0)

    assert res["status"] == "ok"
    assert set(Killmail.objects.values_list("killmail_id", flat=True)) == {111, 333}  # 222 skipped
    assert KillstreamState.load().last_sequence == 103  # walked past the bad mail


@responses.activate
@pytest.mark.django_db
@override_settings(FORCA_HOME_CORP_ID=HOME)
def test_backlog_resumes_across_runs_under_max_fetch(sde, monkeypatch):
    """A backlog larger than max_fetch is drained across successive runs without gaps."""
    monkeypatch.setattr("core.esi.names.backfill_killmail_names", lambda: 0)
    _enable(last_sequence=100)
    responses.add(responses.GET, SEQ_URL, json={"sequence": 104}, status=200)
    responses.add(responses.GET, SEQ_URL, json={"sequence": 104}, status=200)  # second run's tip read
    for seq, kid in [(101, 111), (102, 222), (103, 333), (104, 444)]:
        responses.add(responses.GET, _pkg_url(seq),
                      json=_package(seq, kid, victim_corp=HOME), status=200)

    r1 = killstream.consume_killstream(max_fetch=2, sleep_s=0)
    assert r1["ingested"] == 2
    assert KillstreamState.load().last_sequence == 102

    r2 = killstream.consume_killstream(max_fetch=2, sleep_s=0)
    assert r2["ingested"] == 2
    assert KillstreamState.load().last_sequence == 104
    assert set(Killmail.objects.values_list("killmail_id", flat=True)) == {111, 222, 333, 444}


@pytest.mark.django_db
@override_settings(FORCA_HOME_CORP_ID=HOME)
def test_ingest_status_reports_down_for_failing_primary():
    """A feed that has only ever failed reads as 'down', not 'idle' (review L5)."""
    IngestSourceHealth.record("zkill_query", error="503 from zkill")
    by = {s["source"]: s for s in ingest_status()["sources"]}
    assert by["zkill_query"]["verdict"] == "down"
    assert by["zkill_query"]["consecutive_failures"] == 1
    assert by["zkill_query"]["last_error"] == "503 from zkill"
