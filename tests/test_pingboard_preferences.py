"""PNG-2 — per-pilot notification preferences (per-category mute on DM channels)."""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.identity.models import RoleAssignment
from apps.pingboard import preferences
from apps.pingboard.dispatch import RecipientResolver
from apps.pingboard.models import PilotChannelPreference, PilotContactChannel
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac


def _member(django_user_model, uid):
    u = django_user_model.objects.create(username=f"u{uid}")
    RoleAssignment.objects.create(user=u, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(character_id=uid, user=u, name=f"P{uid}",
                                is_main=True, is_corp_member=True)
    return u


def _link_verified(user, kind="telegram", handle="999"):
    return PilotContactChannel.objects.create(
        user=user, kind=kind, handle=handle, verified=True
    )


# --- resolver-level mute ------------------------------------------------------
@pytest.mark.django_db
def test_muted_category_is_dropped_from_dm_recipients(django_user_model):
    u = _member(django_user_model, 6001)
    _link_verified(u)
    PilotChannelPreference.objects.create(user=u, kind="telegram", category="mining", muted=True)
    r = RecipientResolver()
    # muted category → no DM recipient
    assert r.resolve({"kind": "corp"}, "telegram", "mining") == []
    # a different category is still delivered
    pvp = r.resolve({"kind": "corp"}, "telegram", "pvp_fleet")
    assert [rc.user_id for rc in pvp] == [u.id]


@pytest.mark.django_db
def test_mute_does_not_affect_in_app_or_eve_mail(django_user_model):
    u = _member(django_user_model, 6002)
    _link_verified(u)
    PilotChannelPreference.objects.create(user=u, kind="telegram", category="mining", muted=True)
    r = RecipientResolver()
    assert [rc.user_id for rc in r.resolve({"kind": "corp"}, "in_app", "mining")] == [u.id]
    assert [rc.user_id for rc in r.resolve({"kind": "corp"}, "eve_mail", "mining")] == [u.id]


@pytest.mark.django_db
def test_emergency_is_never_muted(django_user_model):
    u = _member(django_user_model, 6003)
    _link_verified(u)
    # even an (impossible via UI) stored emergency mute is ignored
    PilotChannelPreference.objects.create(user=u, kind="telegram", category="emergency", muted=True)
    r = RecipientResolver()
    assert [rc.user_id for rc in r.resolve({"kind": "corp"}, "telegram", "emergency")] == [u.id]


@pytest.mark.django_db
def test_emergency_priority_bypasses_mute_even_for_other_category(django_user_model):
    u = _member(django_user_model, 6003_1)
    _link_verified(u)
    # pilot muted home_defence, but this alert is emergency *priority* → still delivered
    PilotChannelPreference.objects.create(user=u, kind="telegram", category="home_defence", muted=True)
    r = RecipientResolver()
    assert r.resolve({"kind": "corp"}, "telegram", "home_defence", "normal") == []
    got = r.resolve({"kind": "corp"}, "telegram", "home_defence", "emergency")
    assert [rc.user_id for rc in got] == [u.id]


@pytest.mark.django_db
def test_mute_is_per_channel_kind(django_user_model):
    u = _member(django_user_model, 6004)
    _link_verified(u, kind="telegram", handle="111")
    _link_verified(u, kind="slack", handle="U9")
    PilotChannelPreference.objects.create(user=u, kind="telegram", category="mining", muted=True)
    r = RecipientResolver()
    assert r.resolve({"kind": "corp"}, "telegram", "mining") == []
    # slack was not muted for mining → still delivered
    assert [rc.user_id for rc in r.resolve({"kind": "corp"}, "slack", "mining")] == [u.id]


@pytest.mark.django_db
def test_blank_category_applies_no_mute_backcompat(django_user_model):
    u = _member(django_user_model, 6005)
    _link_verified(u)
    PilotChannelPreference.objects.create(user=u, kind="telegram", category="mining", muted=True)
    # a legacy caller that does not thread a category through mutes nothing
    assert [rc.user_id for rc in RecipientResolver().resolve({"kind": "corp"}, "telegram")] == [u.id]


# --- preferences service ------------------------------------------------------
@pytest.mark.django_db
def test_set_preferences_replaces_mute_list(django_user_model):
    u = _member(django_user_model, 6006)
    preferences.set_preferences(u, "telegram", ["mining", "industry_job"])
    assert preferences.muted_pairs(u) == {("telegram", "mining"), ("telegram", "industry_job")}
    # replacing with a smaller set clears the dropped mute
    preferences.set_preferences(u, "telegram", ["mining"])
    assert preferences.muted_pairs(u) == {("telegram", "mining")}


@pytest.mark.django_db
def test_set_preferences_ignores_emergency_and_bad_kind(django_user_model):
    u = _member(django_user_model, 6007)
    preferences.set_preferences(u, "telegram", ["emergency", "system", "mining"])
    assert preferences.muted_pairs(u) == {("telegram", "mining")}
    preferences.set_preferences(u, "not_a_kind", ["mining"])
    assert ("not_a_kind", "mining") not in preferences.muted_pairs(u)


# --- view ---------------------------------------------------------------------
@pytest.mark.django_db
def test_my_channels_renders_pref_matrix_for_verified_dm(client, django_user_model):
    u = _member(django_user_model, 6008)
    _link_verified(u)
    client.force_login(u)
    resp = client.get(reverse("pingboard:my_channels"))
    assert resp.status_code == 200
    assert b"What reaches each channel" in resp.content


@pytest.mark.django_db
def test_channel_prefs_post_saves_mutes(client, django_user_model):
    u = _member(django_user_model, 6009)
    _link_verified(u)
    client.force_login(u)
    # deliver only pvp_fleet + home_defence → everything else muted
    resp = client.post(reverse("pingboard:channel_prefs"),
                       {"kind": "telegram", "deliver": ["pvp_fleet", "home_defence"]})
    assert resp.status_code == 302
    muted = {c for k, c in preferences.muted_pairs(u) if k == "telegram"}
    assert "mining" in muted and "pvp_fleet" not in muted and "home_defence" not in muted
    assert "emergency" not in muted  # never mutable


@pytest.mark.django_db
def test_channel_prefs_rejects_unknown_kind(client, django_user_model):
    u = _member(django_user_model, 6010)
    client.force_login(u)
    resp = client.post(reverse("pingboard:channel_prefs"), {"kind": "bogus", "deliver": ["mining"]})
    assert resp.status_code == 302
    assert not PilotChannelPreference.objects.filter(user=u).exists()
