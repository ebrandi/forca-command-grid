"""KB-30 — per-pilot subscriptions (personal, RBAC-aware notification feeds).

Covers: per-event-type matching (my_kill/my_loss via linked pilots, my_loss_srp_pending via
the precomputed needs_srp flag, filter_match via the reused killfeed_rules clause engine);
the cursor-consumer's resume + no-back-fire semantics; per-channel delivery (notify via the
Pingboard per-user alert, email via the locmem backend, webhook with timeout/retry/dead-letter,
and the pull-only RSS feed with token auth + regenerate); the SSRF guard at save AND send;
the per-user cap; a disabled subscription staying silent; the UI CRUD + test-fire; the
rank_up / watchlist_hit push hooks; and that no officer-only datum ever reaches a payload.
"""
from __future__ import annotations

import secrets
from decimal import Decimal

import pytest
from django.core import mail
from django.test import Client, override_settings
from django.utils import timezone

from apps.killboard import subscriptions as subs
from apps.killboard.models import (
    KillboardStreamEvent,
    KillboardSubscription,
    KillboardSubscriptionEvent,
    Killmail,
    KillmailParticipant,
    SubscriptionChannel,
    SubscriptionEventType,
)
from apps.pingboard import config as pingboard_config
from apps.pingboard.models import Alert
from tests._raffle_utils import HOME_CORP, enrol_pilot

pytestmark = pytest.mark.django_db

VICTIM = Killmail.HomeRole.VICTIM
ATTACKER = Killmail.HomeRole.ATTACKER
ET = SubscriptionEventType
CH = SubscriptionChannel

# A public IP literal — getaddrinfo resolves it without a DNS/network call, so the SSRF
# guard's save+send checks run offline and deterministically.
PUBLIC_URL = "https://93.184.216.34/hook"


@pytest.fixture(autouse=True)
def _reset_pingboard():
    pingboard_config.reset("notifications")
    yield
    pingboard_config.reset("notifications")


# --------------------------------------------------------------------------- #
#  Builders
# --------------------------------------------------------------------------- #
def _member(django_user_model, char_id=2001, **kw):
    return enrol_pilot(django_user_model, char_id, **kw)


def _km(km_id, *, role=VICTIM, victim_char=2001, victim_corp=None, value="1000000",
        system=30002053, ship=587, when=None):
    if victim_corp is None:
        victim_corp = HOME_CORP if role == VICTIM else 999
    return Killmail.objects.create(
        killmail_id=km_id, killmail_hash=f"h{km_id}", killmail_time=when or timezone.now(),
        solar_system_id=system, victim_ship_type_id=ship, total_value=Decimal(value),
        involves_home_corp=True, home_corp_role=role,
        victim_character_id=victim_char, victim_corporation_id=victim_corp, sec_band="lowsec",
    )


def _ev(km, **ov):
    d = dict(
        killmail=km, killmail_hash=km.killmail_hash, kill_time=km.killmail_time,
        home_role=km.home_corp_role, sec_band=km.sec_band, system_id=km.solar_system_id,
        ship_class="Frigate", victim_ship_type_id=km.victim_ship_type_id,
        victim_character_id=km.victim_character_id, victim_corporation_id=km.victim_corporation_id,
        total_value=km.total_value, needs_srp=False, deviated=False,
    )
    d.update(ov)
    return KillboardStreamEvent.objects.create(**d)


def _attacker(km, char_id, *, seq=0, corp=HOME_CORP, final=True):
    return KillmailParticipant.objects.create(
        killmail=km, role=ATTACKER, seq=seq, character_id=char_id, corporation_id=corp,
        ship_type_id=587, final_blow=final, damage_done=100,
    )


def _sub(user, event_type, channel, **kw):
    return KillboardSubscription.objects.create(
        user=user, event_type=event_type, channel=channel, **kw
    )


# --------------------------------------------------------------------------- #
#  Matching per event type
# --------------------------------------------------------------------------- #
def test_my_loss_matches_linked_pilot(django_user_model):
    user, _ = _member(django_user_model, 2001)
    km = _km(1, role=VICTIM, victim_char=2001)
    _ev(km)
    sub = _sub(user, ET.MY_LOSS, CH.RSS, rss_token=secrets.token_urlsafe(16))
    result = subs.dispatch_subscriptions()
    assert result["matched"] == 1
    assert sub.feed_events.count() == 1


def test_my_loss_does_not_match_other_pilot(django_user_model):
    user, _ = _member(django_user_model, 2001)
    km = _km(1, role=VICTIM, victim_char=9999)  # someone else's loss
    _ev(km)
    _sub(user, ET.MY_LOSS, CH.RSS, rss_token=secrets.token_urlsafe(16))
    assert subs.dispatch_subscriptions()["matched"] == 0


def test_my_kill_matches_via_attacker_participant(django_user_model):
    user, _ = _member(django_user_model, 2001)
    km = _km(1, role=ATTACKER, victim_char=666)
    _attacker(km, 2001, seq=0)      # my pilot on the kill
    _attacker(km, 3002, seq=1)      # a corpmate
    _ev(km, home_role=ATTACKER, victim_character_id=666)
    sub = _sub(user, ET.MY_KILL, CH.RSS, rss_token=secrets.token_urlsafe(16))
    assert subs.dispatch_subscriptions()["matched"] == 1
    assert sub.feed_events.count() == 1


def test_my_kill_no_match_when_pilot_absent(django_user_model):
    user, _ = _member(django_user_model, 2001)
    km = _km(1, role=ATTACKER, victim_char=666)
    _attacker(km, 3002, seq=0)      # only a corpmate, not my pilot
    _ev(km, home_role=ATTACKER, victim_character_id=666)
    _sub(user, ET.MY_KILL, CH.RSS, rss_token=secrets.token_urlsafe(16))
    assert subs.dispatch_subscriptions()["matched"] == 0


def test_my_loss_srp_pending_requires_flag(django_user_model):
    user, _ = _member(django_user_model, 2001)
    km_no = _km(1, role=VICTIM, victim_char=2001)
    _ev(km_no, needs_srp=False)
    km_yes = _km(2, role=VICTIM, victim_char=2001)
    _ev(km_yes, needs_srp=True)
    sub = _sub(user, ET.MY_LOSS_SRP_PENDING, CH.RSS, rss_token=secrets.token_urlsafe(16))
    assert subs.dispatch_subscriptions()["matched"] == 1
    item = sub.feed_events.get()
    assert item.killmail_id == 2  # only the SRP-eligible loss


def test_filter_match_uses_clause_engine(django_user_model):
    user, _ = _member(django_user_model, 2001)
    big = _km(1, role=ATTACKER, victim_char=666, value="900000000")
    _ev(big, home_role=ATTACKER, total_value=Decimal("900000000"))
    small = _km(2, role=ATTACKER, victim_char=777, value="1000")
    _ev(small, home_role=ATTACKER, total_value=Decimal("1000"))
    # Kills worth >= 500M ISK (min_loss_value 0 mutes losses; direction is kills only).
    sub = _sub(user, ET.FILTER_MATCH, CH.RSS, rss_token=secrets.token_urlsafe(16),
               params={"min_kill_value": "500000000", "min_loss_value": "0"})
    assert subs.dispatch_subscriptions()["matched"] == 1
    assert sub.feed_events.get().killmail_id == 1


def test_filter_match_secband_clause(django_user_model):
    user, _ = _member(django_user_model, 2001)
    null_km = _km(1, role=ATTACKER, victim_char=666, value="10000000")
    _ev(null_km, home_role=ATTACKER, sec_band="nullsec", total_value=Decimal("10000000"))
    hi_km = _km(2, role=ATTACKER, victim_char=777, value="10000000")
    _ev(hi_km, home_role=ATTACKER, sec_band="highsec", total_value=Decimal("10000000"))
    sub = _sub(user, ET.FILTER_MATCH, CH.RSS, rss_token=secrets.token_urlsafe(16),
               params={"min_kill_value": "1", "sec_bands": ["nullsec"]})
    subs.dispatch_subscriptions()
    assert [i.killmail_id for i in sub.feed_events.all()] == [1]


# --------------------------------------------------------------------------- #
#  Cursor: resume + no back-fire
# --------------------------------------------------------------------------- #
def test_cursor_does_not_refire(django_user_model):
    user, _ = _member(django_user_model, 2001)
    km = _km(1, role=VICTIM, victim_char=2001)
    _ev(km)
    sub = _sub(user, ET.MY_LOSS, CH.RSS, rss_token=secrets.token_urlsafe(16))
    assert subs.dispatch_subscriptions()["matched"] == 1
    # A second sweep with no new events must not re-deliver the same one.
    assert subs.dispatch_subscriptions()["matched"] == 0
    assert sub.feed_events.count() == 1


def test_new_subscription_does_not_backfire_history(django_user_model):
    """A subscription created at the current tip never fires on the buffered backlog."""
    user, _ = _member(django_user_model, 2001)
    km = _km(1, role=VICTIM, victim_char=2001)
    ev = _ev(km)
    sub = _sub(user, ET.MY_LOSS, CH.RSS, rss_token=secrets.token_urlsafe(16), last_seq=ev.seq)
    assert subs.dispatch_subscriptions()["matched"] == 0
    assert sub.feed_events.count() == 0


def test_disabled_subscription_does_not_fire(django_user_model):
    user, _ = _member(django_user_model, 2001)
    km = _km(1, role=VICTIM, victim_char=2001)
    _ev(km)
    sub = _sub(user, ET.MY_LOSS, CH.RSS, rss_token=secrets.token_urlsafe(16), enabled=False)
    assert subs.dispatch_subscriptions()["matched"] == 0
    assert sub.feed_events.count() == 0


@override_settings(KILLBOARD_SUBSCRIPTIONS_ENABLED=False)
def test_feature_flag_disables_dispatch(django_user_model):
    user, _ = _member(django_user_model, 2001)
    km = _km(1, role=VICTIM, victim_char=2001)
    _ev(km)
    _sub(user, ET.MY_LOSS, CH.RSS, rss_token=secrets.token_urlsafe(16))
    assert subs.dispatch_subscriptions()["status"] == "disabled"


def test_non_member_owner_is_skipped(django_user_model):
    # A user with a linked pilot but no member role must not receive board data.
    user, _ = _member(django_user_model, 2001, roles=())
    km = _km(1, role=VICTIM, victim_char=2001)
    _ev(km)
    sub = _sub(user, ET.MY_LOSS, CH.RSS, rss_token=secrets.token_urlsafe(16))
    assert subs.dispatch_subscriptions()["matched"] == 0
    assert sub.feed_events.count() == 0


# --------------------------------------------------------------------------- #
#  Channel delivery — notify (Pingboard per-user alert)
# --------------------------------------------------------------------------- #
def test_notify_channel_creates_user_alert(django_user_model):
    user, _ = _member(django_user_model, 2001)
    km = _km(1, role=VICTIM, victim_char=2001)
    _ev(km)
    _sub(user, ET.MY_LOSS, CH.NOTIFY)
    subs.dispatch_subscriptions()
    alerts = Alert.objects.filter(source_service="killboard", source_object_id=f"kbsub:{_only_sub().id}")
    assert alerts.count() == 1
    assert alerts.first().audience == {"kind": "user", "id": user.id}


def _only_sub():
    return KillboardSubscription.objects.get()


# --------------------------------------------------------------------------- #
#  Channel delivery — email (locmem backend)
# --------------------------------------------------------------------------- #
@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   DEFAULT_FROM_EMAIL="forca@example.com")
def test_email_channel_sends(django_user_model):
    user, _ = _member(django_user_model, 2001)
    user.email = "pilot@example.com"
    user.save(update_fields=["email"])
    km = _km(1, role=VICTIM, victim_char=2001)
    _ev(km)
    sub = _sub(user, ET.MY_LOSS, CH.EMAIL)
    subs.dispatch_subscriptions()
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["pilot@example.com"]
    sub.refresh_from_db()
    assert sub.last_fired is not None


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
def test_email_without_address_is_a_no_send(django_user_model):
    user, _ = _member(django_user_model, 2001)
    user.email = ""
    user.save(update_fields=["email"])
    km = _km(1, role=VICTIM, victim_char=2001)
    _ev(km)
    _sub(user, ET.MY_LOSS, CH.EMAIL)
    subs.dispatch_subscriptions()
    assert len(mail.outbox) == 0


# --------------------------------------------------------------------------- #
#  Channel delivery — webhook (timeout, retry, dead-letter)
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, status):
        self.status_code = status


@override_settings(KILLBOARD_SUBSCRIPTION_WEBHOOK_TIMEOUT_S=5.0, KILLBOARD_SUBSCRIPTION_WEBHOOK_RETRIES=1)
def test_webhook_posts_with_timeout_and_no_redirects(django_user_model, monkeypatch):
    user, _ = _member(django_user_model, 2001)
    km = _km(1, role=VICTIM, victim_char=2001)
    _ev(km)
    _sub(user, ET.MY_LOSS, CH.WEBHOOK, webhook_url=PUBLIC_URL)
    calls = []

    def _capture(url, **kw):
        calls.append((url, kw))
        return _Resp(204)

    monkeypatch.setattr("requests.post", _capture)
    subs.dispatch_subscriptions()
    assert len(calls) == 1
    url, kw = calls[0]
    assert url == PUBLIC_URL
    assert kw["timeout"] == 5.0
    assert kw["allow_redirects"] is False
    body = kw["json"]
    assert body["event_type"] == ET.MY_LOSS
    assert "data" in body  # the member-visible public payload


@override_settings(KILLBOARD_SUBSCRIPTION_WEBHOOK_RETRIES=1)
def test_webhook_retries_once_then_succeeds(django_user_model, monkeypatch):
    import requests

    user, _ = _member(django_user_model, 2001)
    km = _km(1, role=VICTIM, victim_char=2001)
    _ev(km)
    _sub(user, ET.MY_LOSS, CH.WEBHOOK, webhook_url=PUBLIC_URL)
    state = {"n": 0}

    def _flaky(url, **kw):
        state["n"] += 1
        if state["n"] == 1:
            raise requests.RequestException("transient")
        return _Resp(200)

    monkeypatch.setattr("requests.post", _flaky)
    subs.dispatch_subscriptions()
    assert state["n"] == 2  # one retry within a single delivery
    sub = _only_sub()
    sub.refresh_from_db()
    assert sub.consecutive_failures == 0
    assert sub.last_fired is not None


@override_settings(KILLBOARD_SUBSCRIPTION_WEBHOOK_MAX_FAILURES=2, KILLBOARD_SUBSCRIPTION_WEBHOOK_RETRIES=0)
def test_webhook_dead_letters_after_repeated_failures(django_user_model, monkeypatch):
    user, _ = _member(django_user_model, 2001)
    sub = _sub(user, ET.MY_LOSS, CH.WEBHOOK, webhook_url=PUBLIC_URL)
    monkeypatch.setattr("requests.post", lambda url, **kw: _Resp(500))
    rendered = {"title": "t", "summary": "s", "link": "", "payload": {}, "killmail_id": None, "seq": None}
    subs.record_and_deliver(sub, rendered)
    sub.refresh_from_db()
    assert sub.enabled is True and sub.consecutive_failures == 1
    subs.record_and_deliver(sub, dict(rendered, title="t2"))
    sub.refresh_from_db()
    assert sub.enabled is False
    assert sub.disabled_reason


# --------------------------------------------------------------------------- #
#  SSRF guard — private/loopback/link-local rejected at save AND send
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("url", [
    "http://93.184.216.34/hook",           # not https
    "https://127.0.0.1/hook",              # loopback
    "https://10.1.2.3/hook",               # RFC1918
    "https://192.168.0.5/hook",            # RFC1918
    "https://169.254.169.254/latest/meta", # link-local (cloud metadata)
    "https://[::1]/hook",                  # IPv6 loopback
    "https://[::ffff:169.254.169.254]/x",  # IPv4-mapped link-local
    "https://0.0.0.0/hook",                # unspecified
])
def test_ssrf_guard_rejects_unsafe_targets(url):
    assert subs.webhook_url_error(url) is not None


def test_ssrf_guard_accepts_public_https():
    assert subs.webhook_url_error(PUBLIC_URL) is None


def test_ssrf_guard_blocks_at_send(django_user_model, monkeypatch):
    """A subscription whose URL is private (bypassing the form) never reaches requests.post."""
    user, _ = _member(django_user_model, 2001)
    km = _km(1, role=VICTIM, victim_char=2001)
    _ev(km)
    _sub(user, ET.MY_LOSS, CH.WEBHOOK, webhook_url="https://10.9.9.9/hook")
    called = {"n": 0}
    monkeypatch.setattr("requests.post", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    subs.dispatch_subscriptions()
    assert called["n"] == 0  # blocked before any network call
    sub = _only_sub()
    sub.refresh_from_db()
    assert sub.consecutive_failures == 1


def test_create_view_rejects_private_webhook(django_user_model):
    user, _ = _member(django_user_model, 2001)
    c = Client()
    c.force_login(user)
    r = c.post("/killboard/subscriptions/create/", {
        "event_type": ET.MY_LOSS, "channel": CH.WEBHOOK, "webhook_url": "https://127.0.0.1/x"})
    assert r.status_code == 302
    assert not KillboardSubscription.objects.filter(user=user).exists()


# --------------------------------------------------------------------------- #
#  RSS feed — content, token auth, regenerate
# --------------------------------------------------------------------------- #
def test_rss_feed_serves_items_by_token(django_user_model):
    user, _ = _member(django_user_model, 2001)
    sub = _sub(user, ET.MY_LOSS, CH.RSS, rss_token=secrets.token_urlsafe(16))
    KillboardSubscriptionEvent.objects.create(
        subscription=sub, event_type=ET.MY_LOSS, title="Loss: your Rifter", summary="1 ISK")
    r = Client().get(f"/killboard/subscriptions/feed/{sub.rss_token}/")
    assert r.status_code == 200
    assert r["Content-Type"].startswith("application/atom+xml")
    assert b"Loss: your Rifter" in r.content


def test_rss_feed_bad_token_404(django_user_model):
    user, _ = _member(django_user_model, 2001)
    _sub(user, ET.MY_LOSS, CH.RSS, rss_token=secrets.token_urlsafe(16))
    assert Client().get("/killboard/subscriptions/feed/not-a-real-token/").status_code == 404


def test_rss_feed_no_session_needed(django_user_model):
    user, _ = _member(django_user_model, 2001)
    sub = _sub(user, ET.MY_LOSS, CH.RSS, rss_token=secrets.token_urlsafe(16))
    # Anonymous client (no login) still reads the token-authed feed.
    assert Client().get(f"/killboard/subscriptions/feed/{sub.rss_token}/").status_code == 200


def test_rss_regenerate_revokes_old_url(django_user_model):
    user, _ = _member(django_user_model, 2001)
    sub = _sub(user, ET.MY_LOSS, CH.RSS, rss_token=secrets.token_urlsafe(16))
    old = sub.rss_token
    c = Client()
    c.force_login(user)
    r = c.post(f"/killboard/subscriptions/{sub.id}/rss/regenerate/")
    assert r.status_code == 302
    sub.refresh_from_db()
    assert sub.rss_token != old
    assert Client().get(f"/killboard/subscriptions/feed/{old}/").status_code == 404
    assert Client().get(f"/killboard/subscriptions/feed/{sub.rss_token}/").status_code == 200


def test_rss_feed_denied_when_owner_not_member(django_user_model):
    user, _ = _member(django_user_model, 2001, roles=())
    sub = _sub(user, ET.MY_LOSS, CH.RSS, rss_token=secrets.token_urlsafe(16))
    assert Client().get(f"/killboard/subscriptions/feed/{sub.rss_token}/").status_code == 404


# --------------------------------------------------------------------------- #
#  Per-user cap
# --------------------------------------------------------------------------- #
@override_settings(KILLBOARD_SUBSCRIPTIONS_PER_USER_CAP=2)
def test_per_user_cap_enforced(django_user_model):
    user, _ = _member(django_user_model, 2001)
    _sub(user, ET.MY_LOSS, CH.NOTIFY)
    _sub(user, ET.MY_KILL, CH.NOTIFY)
    c = Client()
    c.force_login(user)
    r = c.post("/killboard/subscriptions/create/", {"event_type": ET.RANK_UP, "channel": CH.NOTIFY})
    assert r.status_code == 302
    assert KillboardSubscription.objects.filter(user=user).count() == 2  # not incremented


# --------------------------------------------------------------------------- #
#  UI CRUD + test-fire
# --------------------------------------------------------------------------- #
def test_subscriptions_page_renders(django_user_model):
    user, _ = _member(django_user_model, 2001)
    c = Client()
    c.force_login(user)
    r = c.get("/killboard/subscriptions/")
    assert r.status_code == 200


def test_create_sets_cursor_at_tip(django_user_model):
    user, _ = _member(django_user_model, 2001)
    km = _km(1, role=VICTIM, victim_char=2001)
    ev = _ev(km)  # a backlog event
    c = Client()
    c.force_login(user)
    r = c.post("/killboard/subscriptions/create/", {"event_type": ET.MY_LOSS, "channel": CH.NOTIFY})
    assert r.status_code == 302
    sub = _only_sub()
    assert sub.last_seq == ev.seq  # created at the tip → the backlog won't fire
    assert subs.dispatch_subscriptions()["matched"] == 0


def test_toggle_and_delete(django_user_model):
    user, _ = _member(django_user_model, 2001)
    sub = _sub(user, ET.MY_LOSS, CH.NOTIFY)
    c = Client()
    c.force_login(user)
    c.post(f"/killboard/subscriptions/{sub.id}/toggle/")
    sub.refresh_from_db()
    assert sub.enabled is False
    c.post(f"/killboard/subscriptions/{sub.id}/delete/")
    assert not KillboardSubscription.objects.filter(id=sub.id).exists()


def test_test_fire_records_and_delivers(django_user_model):
    user, _ = _member(django_user_model, 2001)
    sub = _sub(user, ET.MY_LOSS, CH.NOTIFY)
    c = Client()
    c.force_login(user)
    r = c.post(f"/killboard/subscriptions/{sub.id}/test/")
    assert r.status_code == 302
    item = sub.feed_events.get()
    assert item.payload.get("test") is True
    assert Alert.objects.filter(source_object_id=f"kbsub:{sub.id}").exists()


def test_cannot_toggle_another_users_subscription(django_user_model):
    owner, _ = _member(django_user_model, 2001)
    other, _ = _member(django_user_model, 2002, username="other")
    sub = _sub(owner, ET.MY_LOSS, CH.NOTIFY)
    c = Client()
    c.force_login(other)
    r = c.post(f"/killboard/subscriptions/{sub.id}/toggle/")
    assert r.status_code == 404  # scoped to request.user — not found for anyone else


def test_create_rejects_zero_value_filter(django_user_model):
    user, _ = _member(django_user_model, 2001)
    c = Client()
    c.force_login(user)
    r = c.post("/killboard/subscriptions/create/", {
        "event_type": ET.FILTER_MATCH, "channel": CH.NOTIFY,
        "min_kill_value": "0", "min_loss_value": "0"})
    assert r.status_code == 302
    assert not KillboardSubscription.objects.filter(user=user).exists()  # inert filter rejected


# --------------------------------------------------------------------------- #
#  Push hooks — rank_up / watchlist_hit
# --------------------------------------------------------------------------- #
def test_rank_up_hook_fans_out(django_user_model):
    from apps.killboard.subscriptions import notify_user_event

    user, _ = _member(django_user_model, 2001)
    sub = _sub(user, ET.RANK_UP, CH.RSS, rss_token=secrets.token_urlsafe(16))
    n = notify_user_event(event_type=ET.RANK_UP, user_id=user.id,
                          title="Combat rank: Ace", summary="You reached Ace")
    assert n == 1
    assert sub.feed_events.get().title == "Combat rank: Ace"


def test_rank_up_hook_ignores_other_event_types(django_user_model):
    from apps.killboard.subscriptions import notify_user_event

    user, _ = _member(django_user_model, 2001)
    _sub(user, ET.MY_LOSS, CH.RSS, rss_token=secrets.token_urlsafe(16))  # not a rank_up sub
    assert notify_user_event(event_type=ET.RANK_UP, user_id=user.id, title="x") == 0


def test_watchlist_hit_hook_respects_pin(django_user_model):
    from apps.killboard.models import Watchlist, WatchlistEntry
    from apps.killboard.subscriptions import notify_watchlist_hit

    user, _ = _member(django_user_model, 2001)
    wl_a = Watchlist.objects.create(name="Campers")
    wl_b = Watchlist.objects.create(name="Others")
    entry = WatchlistEntry.objects.create(
        watchlist=wl_a, entity_type=WatchlistEntry.EntityType.CHARACTER, entity_id=555)
    # Pinned to a DIFFERENT watchlist → must not fire.
    sub = _sub(user, ET.WATCHLIST_HIT, CH.RSS, rss_token=secrets.token_urlsafe(16),
               params={"watchlist_id": wl_b.id})
    assert notify_watchlist_hit(entry, {"system_id": 30002053, "km_id": 1}, {}) == 0
    # Repointed to the matching watchlist → fires.
    sub.params = {"watchlist_id": wl_a.id}
    sub.save(update_fields=["params"])
    assert notify_watchlist_hit(entry, {"system_id": 30002053, "km_id": 1}, {}) == 1


# --------------------------------------------------------------------------- #
#  Privacy — no officer-only datum in any payload
# --------------------------------------------------------------------------- #
_ALLOWED_PAYLOAD_KEYS = {
    "seq", "killmail_id", "hash", "kill_time", "home_role", "sec_band", "system_id",
    "ship_class", "value", "victim", "flags",
}
_ALLOWED_FLAG_KEYS = {"solo", "npc", "awox", "needs_srp", "deviated"}


def test_payload_carries_no_officer_only_fields(django_user_model):
    user, _ = _member(django_user_model, 2001)
    km = _km(1, role=VICTIM, victim_char=2001)
    _ev(km, needs_srp=True, deviated=True)
    sub = _sub(user, ET.MY_LOSS, CH.RSS, rss_token=secrets.token_urlsafe(16))
    subs.dispatch_subscriptions()
    payload = sub.feed_events.get().payload
    assert set(payload) <= _ALLOWED_PAYLOAD_KEYS
    assert set(payload["victim"]) <= {"character_id", "corporation_id", "ship_type_id"}
    assert set(payload["flags"]) <= _ALLOWED_FLAG_KEYS
    # The member-visible booleans are present; no payout / deviation-detail / fit keys exist.
    assert payload["flags"]["needs_srp"] is True
    for banned in ("payout", "reward", "srp_amount", "deviation", "missing", "extra", "fit"):
        assert banned not in payload
