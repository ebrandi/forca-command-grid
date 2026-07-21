"""KB-35 — point-in-time historical valuation + multi-oracle pricing.

Covers the historical price service (stored hit, on-demand day-file fetch that bulk-caches
the day, nearest-≤ tolerance fallback, live fallback labelling), oracle routing (Fuzzwork
percentile for high-value, Janice only-with-key), the at-kill stamp at ingest, the resumable
backfill command, ranking fairness (boards read the at-kill value), the detail "then vs now"
context, and the SRP basis flag (default = unchanged payouts).
"""
from __future__ import annotations

import bz2
import csv
import datetime as dt
import io
from decimal import Decimal

import pytest
import responses
from django.utils import timezone

from apps.killboard.models import Killmail, KillmailParticipant
from apps.market import historical
from apps.market.everef_history import THE_FORGE, day_url
from apps.market.models import MarketHistory, MarketPrice

HOME = 98000001
ENEMY = 55555


@pytest.fixture(autouse=True)
def _reset_market_memos():
    """The historical fetch memo + oracle cache are process-local; clear them per test."""
    historical.reset_history_fetch_memo()
    historical.reset_oracle_cache()
    yield
    historical.reset_history_fetch_memo()
    historical.reset_oracle_cache()


# --- helpers ----------------------------------------------------------------
def _history(type_id: int, on: dt.date, average, region_id: int = THE_FORGE) -> None:
    MarketHistory.objects.create(
        type_id=type_id, region_id=region_id, date=on,
        average=Decimal(average), highest=Decimal(average), lowest=Decimal(average),
        volume=1000, order_count=5,
    )


def _jita(type_id: int, sell_min) -> None:
    MarketPrice.objects.create(
        type_id=type_id, location=None, profile=MarketPrice.Profile.JITA_SELL,
        sell_min=Decimal(sell_min),
    )


_DAY_COLS = ["average", "date", "highest", "lowest", "order_count", "volume",
             "http_last_modified", "region_id", "type_id"]


def _day_archive(rows: list[dict]) -> bytes:
    """Build a market-history-*.csv.bz2 body like EVE Ref's daily file."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=_DAY_COLS)
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return bz2.compress(buf.getvalue().encode())


def _day_row(type_id, average, on: dt.date, region_id: int = THE_FORGE) -> dict:
    return {"average": str(average), "date": on.isoformat(), "highest": str(average),
            "lowest": str(average), "order_count": "5", "volume": "1000",
            "http_last_modified": "x", "region_id": str(region_id), "type_id": str(type_id)}


# --- price_at: stored hit ---------------------------------------------------
@pytest.mark.django_db
def test_price_at_uses_stored_history_within_tolerance():
    on = dt.date(2024, 6, 15)
    _history(587, on, 1_000_000)
    res = historical.price_at(587, on, fetch=False)
    assert res.amount == Decimal("1000000")
    assert res.source == historical.SOURCE_HISTORY


@pytest.mark.django_db
def test_price_at_nearest_earlier_day_within_tolerance():
    # Nothing on the kill day, but a row three days earlier (inside the 7-day window).
    _history(587, dt.date(2024, 6, 12), 900_000)
    res = historical.price_at(587, dt.date(2024, 6, 15), fetch=False)
    assert res.amount == Decimal("900000")
    assert res.source == historical.SOURCE_HISTORY


@pytest.mark.django_db
def test_price_at_ignores_history_outside_tolerance():
    # A row 30 days before the kill is too stale — must not be used.
    _history(587, dt.date(2024, 5, 16), 900_000)
    _jita(587, 500_000)
    res = historical.price_at(587, dt.date(2024, 6, 15), fetch=False)
    assert res.source == historical.SOURCE_LIVE_FALLBACK
    assert res.amount == Decimal("500000")


# --- price_at: on-demand day-file fetch bulk-caches the whole day ------------
@responses.activate
@pytest.mark.django_db
def test_price_at_fetches_and_bulk_caches_the_day():
    on = dt.date(2024, 6, 15)
    responses.add(
        responses.GET, day_url(on),
        body=_day_archive([_day_row(587, 1_000_000, on), _day_row(648, 2_500_000, on),
                           _day_row(999, 42, dt.date(2024, 6, 14))]),  # wrong-day row ignored
        status=200,
    )
    res = historical.price_at(587, on)  # fetch enabled (default)
    assert res.source == historical.SOURCE_HISTORY
    assert res.amount == Decimal("1000000")
    # The WHOLE day was cached, so a second type on the same day needs no further fetch.
    assert MarketHistory.objects.filter(region_id=THE_FORGE, date=on).count() == 2
    assert historical.price_at(648, on, fetch=False).amount == Decimal("2500000")


@responses.activate
@pytest.mark.django_db
def test_price_at_absent_day_falls_back_to_live_labelled():
    on = dt.date(2024, 6, 15)
    responses.add(responses.GET, day_url(on), status=404)  # day not published
    _jita(587, 750_000)
    res = historical.price_at(587, on)
    assert res.source == historical.SOURCE_LIVE_FALLBACK
    assert res.amount == Decimal("750000")


@responses.activate
@pytest.mark.django_db
def test_price_at_unpriced_when_no_signal_anywhere():
    on = dt.date(2024, 6, 15)
    responses.add(responses.GET, day_url(on), status=404)
    res = historical.price_at(99999, on)
    assert res.source == historical.SOURCE_UNPRICED
    assert res.amount == Decimal("0")


@responses.activate
@pytest.mark.django_db
def test_price_at_fetches_a_missing_day_only_once():
    on = dt.date(2024, 6, 15)
    responses.add(responses.GET, day_url(on), status=404)
    _jita(587, 100)
    historical.price_at(587, on)
    historical.price_at(648, on)  # same missing day — must NOT trigger a second request
    assert len(responses.calls) == 1


# --- oracle routing ---------------------------------------------------------
@responses.activate
@pytest.mark.django_db
def test_oracle_routes_high_value_to_fuzzwork_percentile(settings):
    settings.MARKET_ORACLE_FUZZWORK_ENABLED = True
    settings.MARKET_ORACLE_FUZZWORK_THRESHOLD_ISK = 1_000_000_000
    responses.add(
        responses.GET, historical.FUZZWORK_AGGREGATES,
        json={"29984": {"sell": {"percentile": "1800000000.0"}, "buy": {"percentile": "1"}}},
        status=200,
    )
    # Live price 2B is above the 1B threshold → route to the manipulation-resistant percentile.
    res = historical.oracle_price(29984, live=Decimal("2000000000"))
    assert res.source == historical.SOURCE_FUZZWORK
    assert res.amount == Decimal("1800000000.0")


@pytest.mark.django_db
def test_oracle_leaves_cheap_items_on_jita(settings):
    settings.MARKET_ORACLE_FUZZWORK_ENABLED = True
    settings.MARKET_ORACLE_FUZZWORK_THRESHOLD_ISK = 1_000_000_000
    res = historical.oracle_price(587, live=Decimal("500000"))  # below threshold
    assert res.source == historical.SOURCE_LIVE
    assert res.amount == Decimal("500000")


@pytest.mark.django_db
def test_oracle_skips_janice_without_a_key(settings):
    settings.JANICE_API_KEY = ""  # no key → PLEX still routes to jita, never Janice
    _jita(44992, 5_000_000)
    res = historical.oracle_price(44992)
    assert res.source == historical.SOURCE_LIVE
    assert res.amount == Decimal("5000000")


@responses.activate
@pytest.mark.django_db
def test_oracle_uses_janice_for_plex_with_a_key(settings):
    settings.JANICE_API_KEY = "test-key"
    settings.JANICE_TYPE_IDS = [44992]
    responses.add(
        responses.POST, historical.JANICE_APPRAISAL_URL,
        json=[{"immediatePrices": {"sellPrice": 5_400_000.0, "splitPrice": 5_300_000.0}}],
        status=200,
    )
    res = historical.oracle_price(44992, live=Decimal("5000000"))
    assert res.source == historical.SOURCE_JANICE
    assert res.amount == Decimal("5400000.0")


# --- ingest stamps a fresh mail's at-kill value (cheap = live) --------------
@pytest.mark.django_db
def test_ingest_stamps_value_at_kill_live(settings):
    from apps.killboard.ingest import ingest_killmail

    _jita(587, 3_000_000)
    body = {
        "killmail_id": 700001, "killmail_time": "2026-07-20T00:00:00Z",
        "solar_system_id": 30000142,
        "victim": {"character_id": 2001, "corporation_id": ENEMY, "ship_type_id": 587,
                   "items": []},
        "attackers": [{"character_id": 3001, "corporation_id": HOME, "ship_type_id": 587,
                       "final_blow": True, "damage_done": 100}],
    }
    km = ingest_killmail(700001, "h", body=body)
    assert km.value_source == historical.SOURCE_LIVE
    assert km.value_at_kill == km.total_value == Decimal("3000000")


# --- backfill command: batches, stamps, resumable ---------------------------
@pytest.mark.django_db
def test_backfill_command_stamps_and_is_resumable():
    from django.core.management import call_command

    on = dt.date(2024, 6, 15)
    _history(587, on, 1_000_000)
    km_time = dt.datetime(2024, 6, 15, 12, tzinfo=dt.UTC)
    ids = [800001, 800002, 800003]
    for kid in ids:
        Killmail.objects.create(
            killmail_id=kid, killmail_hash=f"h{kid}", killmail_time=km_time,
            solar_system_id=30000142, victim_ship_type_id=587,
            total_value=Decimal("5000000"),  # live "now" differs from the 1M at-kill history
            involves_home_corp=True, home_corp_role=Killmail.HomeRole.VICTIM,
        )

    # First pass with a limit stamps only the first two — proving the batch cursor.
    call_command("backfill_value_at_kill", "--no-fetch", "--limit", "2")
    stamped = Killmail.objects.exclude(value_at_kill__isnull=True).count()
    assert stamped == 2

    # Second pass resumes off the value_at_kill IS NULL cursor and finishes the rest.
    call_command("backfill_value_at_kill", "--no-fetch")
    km = Killmail.objects.get(killmail_id=800003)
    assert km.value_at_kill == Decimal("1000000")  # hull priced from the kill-date history
    assert km.value_source == historical.SOURCE_HISTORY
    assert Killmail.objects.exclude(value_at_kill__isnull=True).count() == 3


@pytest.mark.django_db
def test_revalue_does_not_clobber_value_at_kill():
    """The daily re-value refreshes total_value ("now") but never the at-kill figure."""
    from apps.killboard.valuation import apply_valuation

    _jita(587, 9_000_000)  # live price
    km = Killmail.objects.create(
        killmail_id=810001, killmail_hash="h", killmail_time=timezone.now(),
        solar_system_id=30000142, victim_ship_type_id=587,
        value_at_kill=Decimal("1000000"), value_source=historical.SOURCE_HISTORY,
    )
    apply_valuation(km)
    km.refresh_from_db()
    assert km.total_value == Decimal("9000000")           # now: re-valued live
    assert km.value_at_kill == Decimal("1000000")         # then: preserved
    assert km.value_source == historical.SOURCE_HISTORY


# --- ranking fairness: boards read the at-kill value ------------------------
def _kill_for(pilot, km_id, *, value_at_kill, total_value):
    km = Killmail.objects.create(
        killmail_id=km_id, killmail_hash=f"h{km_id}", killmail_time=timezone.now(),
        solar_system_id=30000142, victim_ship_type_id=587,
        total_value=Decimal(total_value), value_at_kill=Decimal(value_at_kill),
        points=10, involves_home_corp=True, home_corp_role=Killmail.HomeRole.ATTACKER,
        victim_corporation_id=ENEMY,
    )
    KillmailParticipant.objects.create(
        killmail=km, role="victim", seq=0, corporation_id=ENEMY, ship_type_id=587,
    )
    KillmailParticipant.objects.create(
        killmail=km, role="attacker", seq=1, character_id=pilot, corporation_id=HOME,
        ship_type_id=587, final_blow=True, damage_done=100,
    )
    return km


@pytest.mark.django_db
def test_leaderboard_ranks_by_at_kill_value(settings):
    from apps.killboard.leaderboards import leaderboards

    settings.FORCA_HOME_CORP_ID = HOME
    A, B = 1001, 1002
    # By LIVE value B (500M) beats A (100M); by AT-KILL A (300M) beats B (200M).
    _kill_for(A, 1, value_at_kill="300000000", total_value="100000000")
    _kill_for(B, 2, value_at_kill="200000000", total_value="500000000")

    data = leaderboards("all", use_cache=False)
    board = next(c["rows"] for c in data["categories"] if c["key"] == "isk_destroyed")
    assert board[0]["character_id"] == A          # ranked by at-kill, so A is first
    assert board[0]["value"] == Decimal("300000000")


@pytest.mark.django_db
def test_leaderboard_coalesces_null_at_kill_to_live(settings):
    from apps.killboard.leaderboards import leaderboards

    settings.FORCA_HOME_CORP_ID = HOME
    A = 1001
    km = _kill_for(A, 1, value_at_kill="300000000", total_value="1")
    Killmail.objects.filter(pk=km.pk).update(value_at_kill=None)  # not yet backfilled
    data = leaderboards("all", use_cache=False)
    board = next(c["rows"] for c in data["categories"] if c["key"] == "isk_destroyed")
    assert board[0]["value"] == Decimal("1")      # falls back to live total_value


# --- detail "then vs now" context -------------------------------------------
@pytest.mark.django_db
def test_detail_then_vs_now_context(client, sde):
    _jita(587, 5_000_000)  # live "now" hull price
    Killmail.objects.create(
        killmail_id=100777, killmail_hash="h", killmail_time=timezone.now(),
        solar_system_id=30002053, victim_ship_type_id=587,
        total_value=Decimal("1000000"), value_at_kill=Decimal("1000000"),
        value_source=historical.SOURCE_HISTORY,
        involves_home_corp=True, home_corp_role=Killmail.HomeRole.VICTIM,
        victim_corporation_id=HOME, victim_character_id=2001,
    )
    KillmailParticipant.objects.create(
        killmail_id=100777, role="attacker", seq=1, character_id=3001,
        corporation_id=ENEMY, ship_type_id=587, final_blow=True, damage_done=100,
    )
    resp = client.get("/killboard/100777/")
    assert resp.status_code == 200
    val = resp.context["valuation"]
    assert val["then"] == Decimal("1000000")
    assert val["now"] == Decimal("5000000")       # recomputed live
    assert val["delta"] == Decimal("4000000")
    assert val["has_at_kill"] is True
    assert b"At kill" in resp.content


# --- SRP basis flag: default keeps payouts unchanged ------------------------
@pytest.mark.django_db
def test_srp_value_basis_default_is_live(settings):
    from apps.srp import services
    from apps.srp.models import SrpProgram

    _jita(587, 8_000_000)
    km = Killmail.objects.create(
        killmail_id=900001, killmail_hash="h",
        killmail_time=dt.datetime(2024, 6, 15, 12, tzinfo=dt.UTC),
        solar_system_id=30000142, victim_ship_type_id=587,
        destroyed_value=Decimal("8000000"),
        involves_home_corp=True, home_corp_role=Killmail.HomeRole.VICTIM,
        victim_corporation_id=HOME, victim_character_id=2001,
    )
    program = SrpProgram.objects.create(
        name="P", is_active=True, valuation=SrpProgram.Valuation.ACTUAL_LOSS,
    )
    # Default basis (live): the stored destroyed_value is used verbatim (unchanged behaviour).
    assert settings.SRP_VALUE_BASIS == "live"
    assert services.loss_value(km, None, program) == Decimal("8000000")


@pytest.mark.django_db
def test_srp_value_basis_at_kill_uses_history(settings):
    from apps.srp import services
    from apps.srp.models import SrpProgram

    settings.SRP_VALUE_BASIS = "at_kill"
    on = dt.date(2024, 6, 15)
    _history(587, on, 2_000_000)  # cheaper on the day it died
    _jita(587, 8_000_000)         # pricier now
    km = Killmail.objects.create(
        killmail_id=900002, killmail_hash="h",
        killmail_time=dt.datetime(2024, 6, 15, 12, tzinfo=dt.UTC),
        solar_system_id=30000142, victim_ship_type_id=587,
        destroyed_value=Decimal("8000000"),
        involves_home_corp=True, home_corp_role=Killmail.HomeRole.VICTIM,
        victim_corporation_id=HOME, victim_character_id=2001,
    )
    program = SrpProgram.objects.create(
        name="P", is_active=True, valuation=SrpProgram.Valuation.HULL_ONLY,
    )
    # at_kill basis prices the hull from the kill-date history, not the live 8M.
    assert services.loss_value(km, None, program) == Decimal("2000000")
