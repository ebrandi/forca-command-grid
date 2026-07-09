"""Pingboard Phase 0 — config, rendering, rate limiting, models, recipient resolution."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from apps.pingboard import config, ratelimit, rendering
from apps.pingboard.dispatch import RecipientResolver
from apps.pingboard.models import ChannelProvider
from apps.sso.models import EveCharacter


# --- config layer ------------------------------------------------------------
@pytest.mark.django_db
def test_config_defaults_and_roundtrip():
    gen = config.get("general")
    assert gen["enabled"] is True
    assert gen["dispatch_floor"]["emergency"] == "director"
    assert gen["dispatch_floor"]["normal"] == "officer"
    # write + version bump + read-back
    v0 = config.config_version()
    config.set("anti_abuse", {"max_urgent_per_day": 3}, user=None)
    assert config.get("anti_abuse")["max_urgent_per_day"] == 3
    assert config.config_version() == v0 + 1
    # reset restores the shipped default
    config.reset("anti_abuse", user=None)
    assert config.get("anti_abuse")["max_urgent_per_day"] == 10


@pytest.mark.django_db
def test_config_validation_rejects_bad_role():
    with pytest.raises(config.ConfigError):
        config.set("general", {"dispatch_floor": {"normal": "wizard"}})
    with pytest.raises(config.ConfigError):
        config.set("anti_abuse", {"max_urgent_per_day": -1})


# --- encrypted secret --------------------------------------------------------
@pytest.mark.django_db
def test_channel_secret_is_encrypted_at_rest():
    p = ChannelProvider(kind="discord", label="corp")
    p.secret = "https://discord.com/api/webhooks/1/abc"
    assert p._secret and p._secret != "https://discord.com/api/webhooks/1/abc"
    assert p.secret == "https://discord.com/api/webhooks/1/abc"
    assert p.has_secret is True
    empty = ChannelProvider(kind="discord", label="x")
    assert empty.secret == "" and empty.has_secret is False


# --- sandboxed rendering -----------------------------------------------------
def test_render_substitutes_and_blocks_traversal():
    assert rendering.render("Op {op} at {sys}", {"op": "HD", "sys": "Jita"}) == "Op HD at Jita"
    # unknown variable renders empty, not an error
    assert rendering.render("hi {missing}", {}) == "hi "
    # attribute / index traversal into objects is refused
    for hostile in ("{a.__class__}", "{a[0]}", "{0}"):
        with pytest.raises(rendering.TemplateError):
            rendering.render(hostile, {"a": "x"})


def test_render_escapes_literal_braces():
    assert rendering.render("100{{pct}}", {}) == "100{pct}"


def test_missing_required():
    assert rendering.missing_required(["op", "fc"], {"op": "x", "fc": ""}) == ["fc"]
    assert rendering.missing_required(["op"], {"op": "x"}) == []


# --- rate limiting / dedup ---------------------------------------------------
@pytest.mark.django_db
def test_rate_limit_per_officer(monkeypatch):
    config.set("anti_abuse", {"max_per_officer_per_hour": 2, "max_per_category_per_hour": 100})
    ok1, _ = ratelimit.try_consume_dispatch(1, "announcement", "normal")
    ok2, _ = ratelimit.try_consume_dispatch(1, "announcement", "normal")
    ok3, why = ratelimit.try_consume_dispatch(1, "announcement", "normal")
    assert ok1 and ok2 and not ok3
    assert "officer" in why
    # a different officer has their own bucket
    ok_other, _ = ratelimit.try_consume_dispatch(2, "announcement", "normal")
    assert ok_other


@pytest.mark.django_db
def test_urgent_daily_cap():
    config.set("anti_abuse", {"max_urgent_per_day": 1})
    assert ratelimit.try_consume_dispatch(1, "emergency", "emergency")[0]
    assert not ratelimit.try_consume_dispatch(1, "emergency", "emergency")[0]


@pytest.mark.django_db
def test_duplicate_suppression():
    h = ratelimit.duplicate_hash("pvp_fleet", {"kind": "corp"}, "form up")
    assert ratelimit.is_duplicate(h) is False  # first time
    assert ratelimit.is_duplicate(h) is True   # within window
    # disabling suppression turns it off
    config.set("anti_abuse", {"suppress_duplicates": False})
    assert ratelimit.is_duplicate(ratelimit.duplicate_hash("x", {}, "y")) is False


# --- recipient resolution ----------------------------------------------------
def _corp_user(uid, cid, *, main=True, corp=True, superuser=False):
    User = get_user_model()
    u = User.objects.create(username=f"eve:{cid}", is_superuser=superuser)
    EveCharacter.objects.create(character_id=cid, user=u, name=f"P{cid}", is_main=main, is_corp_member=corp)
    return u


@pytest.mark.django_db
def test_resolve_corp_and_eve_mail(user, character):
    r = RecipientResolver()
    inapp = r.resolve({"kind": "corp"}, "in_app")
    assert len(inapp) == 1 and inapp[0].recipient_type == "user"
    mail = r.resolve({"kind": "corp"}, "eve_mail")
    assert len(mail) == 1 and mail[0].recipient_ref == "1001" and mail[0].recipient_type == "character"


@pytest.mark.django_db
def test_resolve_users_role_and_broadcast():
    officer = _corp_user(1, 2001, superuser=True)
    _corp_user(2, 2002)  # plain corp member
    r = RecipientResolver()
    # explicit users
    assert {rc.user_id for rc in r.resolve({"kind": "users", "ids": [officer.id]}, "in_app")} == {officer.id}
    # role: only the officer/superuser qualifies
    role_users = {rc.user_id for rc in r.resolve({"kind": "role", "role": "officer"}, "in_app")}
    assert role_users == {officer.id}
    # broadcast channels resolve to no per-user recipients
    assert r.resolve({"kind": "corp"}, "discord") == []
    assert r.estimate({"kind": "corp"}) == 2


@pytest.mark.django_db
def test_non_corp_members_excluded_from_corp_audience():
    _corp_user(1, 3001, corp=True)
    _corp_user(2, 3002, corp=False)  # not a corp member
    assert RecipientResolver().estimate({"kind": "corp"}) == 1
