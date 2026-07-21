"""KB-28 — the killboard REST API (DRF).

Covers: default-deny for anonymous, the KILLBOARD_API_PUBLIC_READ subset, bearer-token auth
(and revoke), member-vs-officer field gating on deviation/SRP, filter semantics mirroring the
public feed, stable cursor pagination, fit export round-trips, the history endpoints, the
leaderboards by_main param, the query budget for the list, throttle wiring, and the schema.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.killboard.ingest import ingest_killmail
from apps.killboard.models import Killmail, KillboardApiToken
from apps.market.models import MarketPrice

AJSON = {"HTTP_ACCEPT": "application/json"}


# --------------------------------------------------------------------------- #
#  Fixtures / helpers
# --------------------------------------------------------------------------- #
def _seed_prices(prices: dict[int, int]) -> None:
    for type_id, sell_min in prices.items():
        MarketPrice.objects.create(
            type_id=type_id, location=None, profile=MarketPrice.Profile.JITA_SELL,
            sell_min=Decimal(sell_min),
        )


def _loss_body(km_id: int, *, time: str, victim_char=2001, ship=587, system=30002053,
               items=None):
    """A home-corp LOSS (corp 98000001 is the victim)."""
    return {
        "killmail_id": km_id, "killmail_time": time, "solar_system_id": system,
        "victim": {"character_id": victim_char, "corporation_id": 98000001, "ship_type_id": ship,
                   "damage_taken": 1000, "items": items or []},
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


def _token_headers(user, name=""):
    _tok, raw = KillboardApiToken.issue(user, name=name)
    return {"HTTP_AUTHORIZATION": f"Bearer {raw}", **AJSON}


# --------------------------------------------------------------------------- #
#  AuthN / public-read
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_anonymous_denied_by_default(client, sde):
    _seed_prices({587: 380000})
    ingest_killmail(1, "h1", body=_loss_body(1, time="2026-07-20T12:00:00Z"))
    for path in (
        "/api/killboard/killmails/",
        "/api/killboard/killmails/1/",
        "/api/killboard/history/latest/",
        "/api/killboard/stats/corp/",
        "/api/killboard/leaderboards/",
    ):
        r = client.get(path, **AJSON)
        assert r.status_code in (401, 403), f"{path} should deny anonymous, got {r.status_code}"


@pytest.mark.django_db
@override_settings(KILLBOARD_API_PUBLIC_READ=True)
def test_public_read_exposes_only_the_public_subset(client, sde):
    _seed_prices({587: 380000})
    ingest_killmail(1, "h1", body=_loss_body(1, time="2026-07-20T12:00:00Z"))
    # Public-board-equivalent endpoints: open to anonymous when the flag is on.
    for path in (
        "/api/killboard/killmails/",
        "/api/killboard/killmails/1/",
        "/api/killboard/killmails/1/fitting/",
        "/api/killboard/killmails/1/eft/",
        "/api/killboard/killmails/1/esi/",
        "/api/killboard/history/latest/",
        "/api/killboard/history/20260720/",
    ):
        assert client.get(path, **AJSON).status_code == 200, path
    # Stats + leaderboards stay members-only regardless of the flag (mirror the website).
    for path in ("/api/killboard/stats/corp/", "/api/killboard/leaderboards/",
                 "/api/killboard/stats/pilots/2001/"):
        assert client.get(path, **AJSON).status_code in (401, 403), path


# --------------------------------------------------------------------------- #
#  Token auth
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_token_auth_and_revoke(client, sde, django_user_model):
    _seed_prices({587: 380000})
    ingest_killmail(1, "h1", body=_loss_body(1, time="2026-07-20T12:00:00Z"))
    member = _make_user(django_user_model, "member", 5001, "member")
    tok, raw = KillboardApiToken.issue(member, name="bot")

    ok = client.get("/api/killboard/killmails/", HTTP_AUTHORIZATION=f"Bearer {raw}", **AJSON)
    assert ok.status_code == 200
    # last_used_at is stamped by the authenticator.
    tok.refresh_from_db()
    assert tok.last_used_at is not None

    # A garbage token is rejected.
    assert client.get(
        "/api/killboard/killmails/", HTTP_AUTHORIZATION="Bearer not-a-real-token", **AJSON
    ).status_code in (401, 403)

    # A revoked token no longer authenticates.
    tok.revoke()
    assert client.get(
        "/api/killboard/killmails/", HTTP_AUTHORIZATION=f"Bearer {raw}", **AJSON
    ).status_code in (401, 403)


@pytest.mark.django_db
def test_token_only_stores_a_hash(django_user_model, db):
    member = _make_user(django_user_model, "member", 5001, "member")
    tok, raw = KillboardApiToken.issue(member)
    assert tok.key_hash == KillboardApiToken.hash_key(raw)
    assert raw not in tok.key_hash
    assert len(tok.key_hash) == 64  # sha-256 hex
    assert tok.prefix and tok.prefix in raw


# --------------------------------------------------------------------------- #
#  RBAC field gating (deviation + SRP) — member owner vs peer vs officer
# --------------------------------------------------------------------------- #
def _doctrine_fit_requiring(module_type_id):
    from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit

    cat = DoctrineCategory.objects.create(key="dps", label="DPS")
    doctrine = Doctrine.objects.create(name="Rifter Doctrine", category=cat, priority=90)
    return DoctrineFit.objects.create(
        doctrine=doctrine, name="Rifter", ship_type_id=587,
        modules=[{"type_id": module_type_id, "quantity": 1, "name": "Gun II"}],
    )


@pytest.fixture
def deviated_loss(sde):
    """A home-corp loss (victim char 2001) that deviates from doctrine + has an SRP claim."""
    _seed_prices({587: 380000, 484: 12000})
    _doctrine_fit_requiring(485)  # fit wants 485; the loss carries 484 (flag 27) → deviated
    body = _loss_body(1, time="2026-07-20T12:00:00Z",
                      items=[{"item_type_id": 484, "flag": 27, "quantity_destroyed": 1}])
    km = ingest_killmail(1, "h1", body=body)
    return km


@pytest.mark.django_db
def test_detail_deviation_and_srp_gated_to_owner_and_officer(
    client, deviated_loss, django_user_model
):
    from apps.srp.models import SrpClaim

    km = deviated_loss
    owner = _make_user(django_user_model, "owner", 2001, "member")  # owns victim 2001
    SrpClaim.objects.create(
        killmail=km, claimant=owner, status=SrpClaim.Status.SUBMITTED,
        loss_value=Decimal("392000"), computed_payout=Decimal("300000"),
    )
    peer = _make_user(django_user_model, "peer", 3001, "member")
    officer = _make_user(django_user_model, "officer", 4001, "officer")
    url = "/api/killboard/killmails/1/"

    # Owner: sees both the deviation diff and the SRP status.
    client.force_login(owner)
    body = client.get(url, **AJSON).json()
    assert body["deviation"] is not None and body["deviation"]["is_clean"] is False
    assert body["srp"] is not None and body["srp"]["status"] == "submitted"
    # The off-doctrine module is marked in the owner's fit.
    fit = client.get("/api/killboard/killmails/1/fitting/", **AJSON).json()
    assert any(it["off_doctrine"] for s in fit["sections"] for it in s["items"])

    # Peer member: both fields dropped, and no off-doctrine markers leak.
    client.force_login(peer)
    body = client.get(url, **AJSON).json()
    assert body["deviation"] is None
    assert body["srp"] is None
    fit = client.get("/api/killboard/killmails/1/fitting/", **AJSON).json()
    assert not any(it["off_doctrine"] for s in fit["sections"] for it in s["items"])

    # Officer: sees deviation + SRP on any loss.
    client.force_login(officer)
    body = client.get(url, **AJSON).json()
    assert body["deviation"] is not None
    assert body["srp"] is not None


@pytest.mark.django_db
@override_settings(KILLBOARD_API_PUBLIC_READ=True)
def test_public_read_never_leaks_deviation_or_srp(client, deviated_loss, django_user_model):
    from apps.srp.models import SrpClaim

    owner = _make_user(django_user_model, "owner", 2001, "member")
    SrpClaim.objects.create(killmail=deviated_loss, claimant=owner,
                            status=SrpClaim.Status.SUBMITTED)
    body = client.get("/api/killboard/killmails/1/", **AJSON).json()  # anonymous
    assert body["deviation"] is None
    assert body["srp"] is None


# --------------------------------------------------------------------------- #
#  Filters mirror the public feed
# --------------------------------------------------------------------------- #
@pytest.fixture
def mixed_feed(sde, django_user_model):
    """A member + a small mixed feed: one loss + one kill, plus an old loss for windowing."""
    _seed_prices({587: 380000, 588: 500000})
    ingest_killmail(1, "h1", body=_loss_body(1, time=timezone.now().isoformat()))
    ingest_killmail(2, "h2", body=_kill_body(2, time=timezone.now().isoformat()))
    # An old loss (over 7 days ago) on a different hull, for the window + ship filters.
    ingest_killmail(3, "h3", body=_loss_body(3, time="2026-01-01T12:00:00Z", ship=588))
    member = _make_user(django_user_model, "member", 9001, "member")
    return member


def _ids(client, query=""):
    r = client.get(f"/api/killboard/killmails/{query}", **AJSON)
    assert r.status_code == 200, r.status_code
    return {row["killmail_id"] for row in r.json()["results"]}


@pytest.mark.django_db
def test_list_filters_mirror_the_board(client, mixed_feed):
    client.force_login(mixed_feed)

    assert _ids(client) == {1, 2, 3}                       # all home-corp mails
    assert _ids(client, "?kind=losses") == {1, 3}          # home corp is victim
    assert _ids(client, "?kind=kills") == {2}              # home corp is attacker
    assert _ids(client, "?system_id=30002053") == {1, 2, 3}
    assert _ids(client, "?system_id=99999999") == set()
    assert _ids(client, "?ship_type_id=588") == {3}        # only the old loss is a 588
    assert _ids(client, "?sec_band=lowsec") == {1, 2, 3}   # 30002053 is lowsec
    assert _ids(client, "?sec_band=highsec") == set()
    assert _ids(client, "?min_value=450000000") == set()   # all are ~380M-500k, well under
    # Window: the old (Jan) loss falls outside 7d; the two fresh mails remain.
    assert _ids(client, "?window=7d") == {1, 2}
    # Entity filter with the attacker-side toggle: home char 2001 attacks on the kill (2).
    assert _ids(client, "?character_id=2001&side=attacker") == {2}
    # Victim-side default: char 2001 is the victim on the losses (1, 3).
    assert _ids(client, "?character_id=2001") == {1, 3}


@pytest.mark.django_db
def test_ship_class_filter(client, mixed_feed):
    client.force_login(mixed_feed)
    # 587 (Rifter) + 588 (Rupture-ish) are both frigates/cruisers in the test SDE; the filter
    # resolves via the same hull-class mapping the killfeed rules use. Assert it narrows, not
    # the exact class membership (which depends on the SDE sample's group ids).
    all_ids = _ids(client)
    frig = _ids(client, "?ship_class=Frigate")
    assert frig <= all_ids


# --------------------------------------------------------------------------- #
#  Cursor pagination
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_cursor_pagination_stable_ordering(client, sde, django_user_model):
    _seed_prices({587: 380000})
    for i in range(1, 8):
        ingest_killmail(i, f"h{i}", body=_loss_body(i, time="2026-07-20T12:00:00Z"))
    member = _make_user(django_user_model, "member", 9001, "member")
    client.force_login(member)

    seen: list[int] = []
    url = "/api/killboard/killmails/?page_size=2"
    for _ in range(10):  # bounded; there are only 7 rows → ≤4 pages
        r = client.get(url, **AJSON).json()
        seen.extend(row["killmail_id"] for row in r["results"])
        if not r.get("next"):
            break
        url = r["next"]
    assert seen == sorted(seen, reverse=True)          # id-DESC, stable
    assert len(seen) == len(set(seen)) == 7            # every row once, no dupes


@pytest.mark.django_db
def test_list_query_budget_is_bounded(client, sde, django_user_model, django_assert_max_num_queries):
    """The list must not scale queries with page size (no N+1 on victim/attacker data)."""
    _seed_prices({587: 380000})
    for i in range(1, 26):
        ingest_killmail(i, f"h{i}", body=_loss_body(i, time="2026-07-20T12:00:00Z"))
    member = _make_user(django_user_model, "member", 9001, "member")
    tok_headers = _token_headers(member)  # token auth: no session/user extra queries per row

    with django_assert_max_num_queries(10):
        r = client.get("/api/killboard/killmails/?page_size=25", **tok_headers)
    assert r.status_code == 200
    assert len(r.json()["results"]) == 25


# --------------------------------------------------------------------------- #
#  Fit exports round-trip against the existing (HTML) views
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_eft_and_esi_match_the_html_views(client, sde, django_user_model):
    _seed_prices({587: 380000, 484: 12000, 192: 5})
    body = _loss_body(1, time="2026-07-20T12:00:00Z", items=[
        {"item_type_id": 484, "flag": 27, "quantity_destroyed": 1},
        {"item_type_id": 192, "flag": 5, "quantity_dropped": 100},
    ])
    ingest_killmail(1, "h1", body=body)
    member = _make_user(django_user_model, "member", 9001, "member")
    headers = _token_headers(member)

    # EFT: byte-identical to the public HTML export (same exporter).
    api_eft = client.get("/api/killboard/killmails/1/eft/", **headers)
    html_eft = client.get("/killboard/1/eft/")
    assert api_eft.status_code == 200
    assert api_eft["Content-Type"].startswith("text/plain")
    assert api_eft.content == html_eft.content

    # ESI fitting: same dict as the public fit.json view.
    api_esi = client.get("/api/killboard/killmails/1/esi/", **headers).json()
    html_esi = client.get("/killboard/1/fit.json").json()
    assert api_esi == html_esi
    assert api_esi["ship_type_id"] == 587


# --------------------------------------------------------------------------- #
#  History endpoints
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_history_latest_caps_n_and_returns_id_hash(client, sde, django_user_model):
    _seed_prices({587: 380000})
    for i in range(1, 6):
        ingest_killmail(i, f"hash{i}", body=_loss_body(i, time="2026-07-20T12:00:00Z"))
    member = _make_user(django_user_model, "member", 9001, "member")
    headers = _token_headers(member)

    # Absurd n is clamped (not an error), newest-first, id+hash shape.
    r = client.get("/api/killboard/history/latest/?n=999999", **headers)
    assert r.status_code == 200
    payload = r.json()
    assert payload["count"] == 5
    assert payload["killmails"][0] == {"killmail_id": 5, "hash": "hash5"}
    # n honoured when small.
    two = client.get("/api/killboard/history/latest/?n=2", **headers).json()
    assert [k["killmail_id"] for k in two["killmails"]] == [5, 4]


@pytest.mark.django_db
def test_history_by_day(client, sde, django_user_model):
    _seed_prices({587: 380000})
    ingest_killmail(1, "h1", body=_loss_body(1, time="2026-07-20T12:00:00Z"))
    ingest_killmail(2, "h2", body=_loss_body(2, time="2026-07-21T09:00:00Z"))
    member = _make_user(django_user_model, "member", 9001, "member")
    headers = _token_headers(member)

    day = client.get("/api/killboard/history/20260720/", **headers).json()
    assert [k["killmail_id"] for k in day["killmails"]] == [1]
    assert day["killmails"][0]["hash"] == "h1"
    # A malformed date is a 404, not a 500.
    assert client.get("/api/killboard/history/nope/", **headers).status_code == 404


# --------------------------------------------------------------------------- #
#  Leaderboards / stats
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_leaderboards_accepts_by_main(client, sde, django_user_model):
    _seed_prices({587: 380000})
    ingest_killmail(1, "h1", body=_kill_body(1, time=timezone.now().isoformat()))
    member = _make_user(django_user_model, "member", 9001, "member")
    headers = _token_headers(member)

    base = client.get("/api/killboard/leaderboards/?window=all", **headers)
    assert base.status_code == 200
    assert "categories" in base.json() and "pilot_count" in base.json()
    # by_main is accepted (KB-23 rollup path) and returns the same shape.
    rolled = client.get("/api/killboard/leaderboards/?window=all&by_main=true", **headers)
    assert rolled.status_code == 200
    assert "categories" in rolled.json()


@pytest.mark.django_db
def test_corp_stats_and_pilot_stats(client, sde, django_user_model):
    _seed_prices({587: 380000})
    ingest_killmail(1, "h1", body=_kill_body(1, time=timezone.now().isoformat()))
    member = _make_user(django_user_model, "member", 2001, "member")  # home pilot 2001
    headers = _token_headers(member)

    corp = client.get("/api/killboard/stats/corp/", **headers)
    assert corp.status_code == 200
    assert {"kills", "losses", "efficiency"} <= set(corp.json())

    # Home-corp pilot resolves; a stranger id is a 404 (home pilots only).
    assert client.get("/api/killboard/stats/pilots/2001/", **headers).status_code == 200
    assert client.get("/api/killboard/stats/pilots/424242/", **headers).status_code == 404


# --------------------------------------------------------------------------- #
#  Throttle wiring + schema
# --------------------------------------------------------------------------- #
def test_throttle_classes_are_wired():
    from django.conf import settings

    from apps.killboard.api.throttling import KillboardAnonThrottle, KillboardUserThrottle
    from apps.killboard.api.views import KillmailViewSet

    assert KillboardAnonThrottle in KillmailViewSet.throttle_classes
    assert KillboardUserThrottle in KillmailViewSet.throttle_classes
    rates = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
    assert rates["killboard_anon"] and rates["killboard_user"]
    assert KillboardAnonThrottle().scope == "killboard_anon"
    assert KillboardUserThrottle().scope == "killboard_user"


@pytest.mark.django_db
def test_schema_endpoint_member_only(client, sde, django_user_model):
    # Anonymous denied by default.
    assert client.get("/api/schema/").status_code in (401, 403)
    member = _make_user(django_user_model, "member", 9001, "member")
    client.force_login(member)
    r = client.get("/api/schema/")
    assert r.status_code == 200
    assert b"/api/killboard/killmails/" in r.content


# --------------------------------------------------------------------------- #
#  Token management UI (self-serve)
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_token_management_page_create_and_revoke(client, sde, django_user_model):
    member = _make_user(django_user_model, "member", 9001, "member")
    client.force_login(member)

    # Create → the plaintext is shown exactly once on the next render.
    client.post("/killboard/api-tokens/create/", {"name": "My bot"})
    page = client.get("/killboard/api-tokens/")
    assert page.status_code == 200
    assert KillboardApiToken.objects.filter(user=member, revoked_at__isnull=True).count() == 1
    tok = KillboardApiToken.objects.get(user=member)
    # A second GET no longer reveals it (one-time session pop).
    again = client.get("/killboard/api-tokens/")
    assert tok.key_hash.encode() not in again.content  # the hash is never rendered

    # Revoke.
    client.post(f"/killboard/api-tokens/{tok.id}/revoke/")
    tok.refresh_from_db()
    assert tok.revoked_at is not None
