"""KB-29 — the outbound realtime killmail stream (SSE + poll).

Covers: event emission at the single ingest seam (and NOT for stale backfill), the ring-buffer
prune, cursor resume, topic filtering incl. RBAC gating (member sees needs-srp; anonymous
public-read does not and its payload omits the flag), auth denial, the connection-cap 503, the
SSE response framing (content-type + id/event/data lines, parsed from a bounded response), and
that the live-feed page renders the EventSource script only when the feature is enabled.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.killboard import stream
from apps.killboard.ingest import ingest_killmail
from apps.killboard.models import KillboardStreamEvent, Killmail
from apps.market.models import MarketPrice

AJSON = {"HTTP_ACCEPT": "application/json"}
STREAM = "/api/killboard/stream/"


# --------------------------------------------------------------------------- #
#  Fixtures / helpers
# --------------------------------------------------------------------------- #
def _seed_prices(prices: dict[int, int]) -> None:
    for type_id, sell_min in prices.items():
        MarketPrice.objects.create(
            type_id=type_id, location=None, profile=MarketPrice.Profile.JITA_SELL,
            sell_min=Decimal(sell_min),
        )


def _recent(hours_ago: float = 1) -> str:
    return (timezone.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _loss_body(km_id: int, *, time: str, victim_char=2001, ship=587, system=30002053):
    """A home-corp LOSS (corp 98000001 is the victim)."""
    return {
        "killmail_id": km_id, "killmail_time": time, "solar_system_id": system,
        "victim": {"character_id": victim_char, "corporation_id": 98000001, "ship_type_id": ship,
                   "damage_taken": 1000, "items": []},
        "attackers": [{"character_id": 3001, "corporation_id": 99, "ship_type_id": 587,
                       "final_blow": True, "damage_done": 1000}],
    }


def _kill_body(km_id: int, *, time: str, victim_char=4001, ship=587, system=30002053):
    """A home-corp KILL (corp 98000001 is the attacker)."""
    return {
        "killmail_id": km_id, "killmail_time": time, "solar_system_id": system,
        "victim": {"character_id": victim_char, "corporation_id": 99, "ship_type_id": ship,
                   "damage_taken": 1000, "items": []},
        "attackers": [{"character_id": 2001, "corporation_id": 98000001, "ship_type_id": 587,
                       "final_blow": True, "damage_done": 1000}],
    }


def _make_user(django_user_model, username, cid, role, *, is_corp_member=True):
    from apps.identity.models import RoleAssignment
    from apps.sso.models import EveCharacter
    from apps.sso.services import ensure_role

    user = django_user_model.objects.create(username=username)
    if role:
        RoleAssignment.objects.create(user=user, role=ensure_role(role))
    EveCharacter.objects.create(
        character_id=cid, user=user, name=username, is_main=True, is_corp_member=is_corp_member
    )
    return user


def _member_headers(django_user_model):
    from apps.killboard.models import KillboardApiToken

    user = _make_user(django_user_model, "eve:member", 5001, "member")
    _tok, raw = KillboardApiToken.issue(user, name="t")
    return {"HTTP_AUTHORIZATION": f"Bearer {raw}", **AJSON}


def _ingest_one(km_id=1, *, loss=True, time=None) -> Killmail:
    body = (_loss_body if loss else _kill_body)(km_id, time=time or _recent())
    return ingest_killmail(km_id, f"h{km_id}", body=body)


def _mkevent(km: Killmail, **overrides) -> KillboardStreamEvent:
    """A stream event with explicit topic dimensions (bypasses emission for serving tests)."""
    defaults = dict(
        killmail=km, killmail_hash=km.killmail_hash, kill_time=km.killmail_time,
        home_role=Killmail.HomeRole.VICTIM, sec_band="lowsec", system_id=30002053,
        ship_class="Frigate", victim_ship_type_id=587, victim_character_id=2001,
        victim_corporation_id=98000001, total_value=Decimal("1000000"),
        needs_srp=False, deviated=False,
    )
    defaults.update(overrides)
    return KillboardStreamEvent.objects.create(**defaults)


# --------------------------------------------------------------------------- #
#  Emission at the ingest seam
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_emits_one_event_on_fresh_ingest(sde):
    _seed_prices({587: 380000})
    km = _ingest_one(1, loss=True)
    events = list(KillboardStreamEvent.objects.all())
    assert len(events) == 1
    ev = events[0]
    assert ev.killmail_id == km.killmail_id
    assert ev.home_role == Killmail.HomeRole.VICTIM
    assert ev.victim_ship_type_id == 587


@pytest.mark.django_db
def test_kill_vs_loss_role_recorded(sde):
    _seed_prices({587: 380000})
    _ingest_one(1, loss=True)
    _ingest_one(2, loss=False)
    roles = set(KillboardStreamEvent.objects.values_list("home_role", flat=True))
    assert roles == {Killmail.HomeRole.VICTIM, Killmail.HomeRole.ATTACKER}


@pytest.mark.django_db
def test_no_event_for_stale_backfill(sde):
    """A years-old EVE Ref/zKill backfill mail must not flood the live feed."""
    _seed_prices({587: 380000})
    ingest_killmail(9, "h9", body=_loss_body(9, time="2019-01-01T00:00:00Z"))
    assert KillboardStreamEvent.objects.count() == 0


@pytest.mark.django_db
def test_reingest_does_not_double_emit(sde):
    """ingest_killmail is idempotent (early-returns on an existing id) — no second event."""
    _seed_prices({587: 380000})
    body = _loss_body(1, time=_recent())
    ingest_killmail(1, "h1", body=body)
    ingest_killmail(1, "h1", body=body)
    assert KillboardStreamEvent.objects.count() == 1


@pytest.mark.django_db
@override_settings(KILLBOARD_STREAM_ENABLED=False)
def test_no_emission_when_disabled(sde):
    _seed_prices({587: 380000})
    _ingest_one(1, loss=True)
    assert KillboardStreamEvent.objects.count() == 0


# --------------------------------------------------------------------------- #
#  Ring-buffer prune
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
@override_settings(KILLBOARD_STREAM_RETENTION=3)
def test_prune_keeps_newest_n(sde):
    _seed_prices({587: 380000})
    km = _ingest_one(1, loss=True)
    for _ in range(5):
        _mkevent(km)
    # 1 (from ingest) + 5 = 6 rows; prune down to the newest 3.
    result = stream.prune_events()
    assert result["deleted"] == 3
    remaining = list(KillboardStreamEvent.objects.order_by("seq").values_list("seq", flat=True))
    assert len(remaining) == 3
    assert remaining == sorted(remaining)  # the newest three, ascending


# --------------------------------------------------------------------------- #
#  Cursor resume
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_poll_cursor_returns_exactly_after(django_user_model, sde):
    _seed_prices({587: 380000})
    km = _ingest_one(1, loss=True)
    e1, e2, e3 = _mkevent(km), _mkevent(km), _mkevent(km)
    headers = _member_headers(django_user_model)

    r = django_client().get(f"{STREAM}?mode=poll&after_seq={e1.seq}", **headers)
    assert r.status_code == 200
    seqs = [e["seq"] for e in r.json()["events"]]
    assert e1.seq not in seqs
    assert e2.seq in seqs and e3.seq in seqs
    assert seqs == sorted(seqs)


# --------------------------------------------------------------------------- #
#  Topic filtering
# --------------------------------------------------------------------------- #
def _poll(headers, query=""):
    # after_seq=0 asks for the whole retained backlog (a bare poll defaults to the tip — the
    # "stream from now" handshake shared with SSE — and would return nothing).
    r = django_client().get(f"{STREAM}?mode=poll&after_seq=0{query}", **headers)
    assert r.status_code == 200, r.content
    return r.json()["events"]


@pytest.mark.django_db
def test_topic_kills_vs_losses(django_user_model, sde):
    _seed_prices({587: 380000})
    km = _ingest_one(1, loss=True)
    _mkevent(km, home_role=Killmail.HomeRole.ATTACKER)
    _mkevent(km, home_role=Killmail.HomeRole.VICTIM)
    headers = _member_headers(django_user_model)

    kills = _poll(headers, "&topics=kills")
    losses = _poll(headers, "&topics=losses")
    assert all(e["home_role"] == "attacker" for e in kills) and kills
    assert all(e["home_role"] == "victim" for e in losses) and losses


@pytest.mark.django_db
def test_topic_secband_iskband_system_pilot(django_user_model, sde):
    _seed_prices({587: 380000})
    km = _ingest_one(1, loss=True)
    _mkevent(km, sec_band="nullsec", system_id=30009999, victim_character_id=777,
             total_value=Decimal("5000000000"))
    _mkevent(km, sec_band="highsec", system_id=30001111, victim_character_id=888,
             total_value=Decimal("1000"))
    headers = _member_headers(django_user_model)

    assert all(e["sec_band"] == "nullsec" for e in _poll(headers, "&topics=secband:nullsec"))
    assert all(e["system_id"] == 30009999 for e in _poll(headers, "&topics=system:30009999"))
    assert all(e["victim"]["character_id"] == 777 for e in _poll(headers, "&topics=pilot:777"))
    big = _poll(headers, "&topics=iskband:1000000000")
    assert big and all(Decimal(e["value"]) >= 1000000000 for e in big)


@pytest.mark.django_db
def test_topic_shipclass(django_user_model, sde):
    _seed_prices({587: 380000})
    km = _ingest_one(1, loss=True)
    ev = _mkevent(km, ship_class="Cruiser")
    _mkevent(km, ship_class="Frigate")
    headers = _member_headers(django_user_model)
    got = _poll(headers, "&topics=shipclass:Cruiser")
    assert got and all(e["ship_class"] == "Cruiser" for e in got)
    assert ev.seq in [e["seq"] for e in got]


@pytest.mark.django_db
def test_unknown_topic_400(django_user_model, sde):
    _seed_prices({587: 380000})
    _ingest_one(1, loss=True)
    headers = _member_headers(django_user_model)
    r = django_client().get(f"{STREAM}?mode=poll&topics=bogus:1", **headers)
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
#  RBAC-gated topics + payload tiering
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_member_sees_needs_srp_topic_and_flag(django_user_model, sde):
    _seed_prices({587: 380000})
    km = _ingest_one(1, loss=True)
    ev = _mkevent(km, needs_srp=True, deviated=True)
    headers = _member_headers(django_user_model)

    got = _poll(headers, "&topics=needs-srp")
    assert ev.seq in [e["seq"] for e in got]
    # Member payload carries the gated flags.
    row = next(e for e in got if e["seq"] == ev.seq)
    assert row["flags"]["needs_srp"] is True
    assert row["flags"]["deviated"] is True


@pytest.mark.django_db
@override_settings(KILLBOARD_API_PUBLIC_READ=True)
def test_public_read_denied_gated_topic_and_flag_hidden(sde):
    _seed_prices({587: 380000})
    km = _ingest_one(1, loss=True)
    _mkevent(km, needs_srp=True, deviated=True)
    c = django_client()

    # Anonymous (public-read) may NOT subscribe to a member-gated topic.
    denied = c.get(f"{STREAM}?mode=poll&topics=needs-srp", **AJSON)
    assert denied.status_code == 403

    # And the gated flags never appear in the anonymous payload of a public topic.
    ok = c.get(f"{STREAM}?mode=poll&after_seq=0&topics=all", **AJSON)
    assert ok.status_code == 200
    assert ok.json()["events"]  # the created event is returned (so the flag check isn't vacuous)
    for e in ok.json()["events"]:
        assert "needs_srp" not in e["flags"]
        assert "deviated" not in e["flags"]


# --------------------------------------------------------------------------- #
#  Auth denial
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_anonymous_denied_by_default(sde):
    _seed_prices({587: 380000})
    _ingest_one(1, loss=True)
    r = django_client().get(f"{STREAM}?mode=poll", **AJSON)
    assert r.status_code in (401, 403)


# --------------------------------------------------------------------------- #
#  Connection-cap 503
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
@override_settings(KILLBOARD_STREAM_MAX_CLIENTS=1)
def test_stream_capacity_503(django_user_model, sde):
    _seed_prices({587: 380000})
    _ingest_one(1, loss=True)
    headers = _member_headers(django_user_model)

    slot = stream.acquire_slot()  # occupy the single slot out-of-band
    assert slot is not None
    try:
        r = django_client().get(STREAM, HTTP_ACCEPT="text/event-stream",
                                HTTP_AUTHORIZATION=headers["HTTP_AUTHORIZATION"])
        assert r.status_code == 503
        assert r["Retry-After"]
    finally:
        stream.release_slot(slot)


@pytest.mark.django_db
@override_settings(KILLBOARD_STREAM_ENABLED=False)
def test_stream_disabled_503(django_user_model, sde):
    _seed_prices({587: 380000})
    headers = _member_headers(django_user_model)
    r = django_client().get(STREAM, HTTP_ACCEPT="text/event-stream",
                            HTTP_AUTHORIZATION=headers["HTTP_AUTHORIZATION"])
    assert r.status_code == 503


# --------------------------------------------------------------------------- #
#  SSE framing (bounded, finite response)
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
@override_settings(KILLBOARD_STREAM_MAX_LIFETIME_S=0, KILLBOARD_STREAM_POLL_INTERVAL_S=0)
def test_sse_framing(django_user_model, sde):
    """With a zero lifetime the generator drains the backlog once and closes — a finite
    response we can parse for the SSE framing (content-type, id/event/data lines)."""
    _seed_prices({587: 380000})
    km = _ingest_one(1, loss=True)
    ev = _mkevent(km, home_role=Killmail.HomeRole.ATTACKER)
    headers = _member_headers(django_user_model)

    r = django_client().get(
        f"{STREAM}?after_seq=0", HTTP_ACCEPT="text/event-stream",
        HTTP_AUTHORIZATION=headers["HTTP_AUTHORIZATION"],
    )
    assert r.status_code == 200
    assert r["Content-Type"].startswith("text/event-stream")
    assert r["X-Accel-Buffering"] == "no"
    body = b"".join(r.streaming_content).decode()
    assert "retry:" in body
    assert f"id: {ev.seq}" in body
    assert "event: kill" in body
    assert "data: {" in body


# --------------------------------------------------------------------------- #
#  Live-feed page renders the EventSource script only when enabled
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_live_script_present_when_enabled(sde):
    r = django_client().get("/killboard/")
    assert r.status_code == 200
    assert b"EventSource" in r.content
    assert b'id="kb-live"' in r.content


@pytest.mark.django_db
@override_settings(KILLBOARD_STREAM_ENABLED=False)
def test_live_script_absent_when_disabled(sde):
    r = django_client().get("/killboard/")
    assert r.status_code == 200
    assert b"EventSource" not in r.content


# --------------------------------------------------------------------------- #
#  A fresh test client per call (avoids sharing session state across assertions)
# --------------------------------------------------------------------------- #
def django_client():
    from django.test import Client

    return Client()
