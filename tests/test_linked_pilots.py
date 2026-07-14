"""Linked Pilots: the active-pilot context, switching, unlinking and the main pilot (LP-2/6/7).

The thing these tests exist to protect is an *identity* guarantee. A user with several pilots is
acting as exactly one of them at a time, and the app must never be confused about which — not
after a switch, not after a refresh, not when someone forges a character id in a POST body, and
not when a pilot is unlinked in another tab.

Pilot data is never merged. Selecting a pilot changes WHICH pilot the app acts as; it never
widens the app's view to the union of an account's pilots.
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.identity.models import RoleAssignment
from apps.sso import linking
from apps.sso.models import AuthToken, EveCharacter
from apps.sso.services import ensure_role
from core import pilots, rbac


def _account(django_user_model, username="eve:1"):
    user = django_user_model.objects.create(username=username)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    return user


def _pilot(user, character_id, name, *, main=False, corp_member=True, director=False):
    return EveCharacter.objects.create(
        character_id=character_id, user=user, name=name, is_main=main,
        is_corp_member=corp_member, is_corp_director=director,
    )


def _with_token(character, scopes=("publicData",)):
    from django.utils import timezone

    token = AuthToken(
        character=character, scopes=list(scopes),
        access_expires_at=timezone.now() + timezone.timedelta(hours=1),
    )
    token.refresh_token = "r"
    token.access_token = "a"
    token.save()
    return token


# --- resolution ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_a_fresh_session_starts_on_the_main_pilot(client, django_user_model):
    user = _account(django_user_model)
    _pilot(user, 1, "Alt")
    main = _pilot(user, 2, "Main", main=True)
    client.force_login(user)

    resp = client.get(reverse("identity:linked_pilots"))
    assert resp.context["cards"][0]["character"].character_id == main.character_id
    assert resp.context["cards"][0]["is_active"] is True


@pytest.mark.django_db
def test_the_selected_pilot_survives_navigation_and_refresh(client, django_user_model):
    user = _account(django_user_model)
    _pilot(user, 1, "Main", main=True)
    alt = _pilot(user, 2, "Alt")
    client.force_login(user)

    client.post(reverse("identity:pilot_switch"), {"character_id": alt.character_id})

    # A different page, then the same page again: still the alt, both times. The choice lives
    # in the server-side session, not in the URL or a form field.
    for _ in range(2):
        resp = client.get(reverse("identity:linked_pilots"))
        active = [c for c in resp.context["cards"] if c["is_active"]]
        assert [c["character"].character_id for c in active] == [alt.character_id]


@pytest.mark.django_db
def test_an_account_with_no_pilots_does_not_crash(client, django_user_model):
    """Possible in the wild: an officer detaches someone's only character. The app must render,
    not 500 — and must not silently grant that account anything."""
    user = django_user_model.objects.create(username="eve:orphan")
    client.force_login(user)
    resp = client.get(reverse("identity:linked_pilots"))
    assert resp.status_code == 200
    assert resp.context["pilot_count"] == 0


# --- switching ----------------------------------------------------------------------------
@pytest.mark.django_db
def test_switching_changes_the_active_pilot_and_is_audited(client, django_user_model):
    from apps.admin_audit.models import AuditLog

    user = _account(django_user_model)
    main = _pilot(user, 1, "Main", main=True)
    alt = _pilot(user, 2, "Alt")
    client.force_login(user)

    resp = client.post(reverse("identity:pilot_switch"), {"character_id": alt.character_id})
    assert resp.status_code == 302

    entry = AuditLog.objects.filter(action="pilot.switched").get()
    assert entry.metadata == {"from": main.character_id, "to": alt.character_id}
    assert entry.target_id == str(alt.character_id)


@pytest.mark.django_db
def test_switching_stamps_last_used(client, django_user_model):
    user = _account(django_user_model)
    _pilot(user, 1, "Main", main=True)
    alt = _pilot(user, 2, "Alt")
    assert alt.last_used_at is None
    client.force_login(user)

    client.post(reverse("identity:pilot_switch"), {"character_id": alt.character_id})
    alt.refresh_from_db()
    assert alt.last_used_at is not None


@pytest.mark.django_db
def test_switching_is_refused_by_GET(client, django_user_model):
    """A switch answered on GET would be firable from an <img src> on any site — a CSRF that
    silently changes which pilot you are acting as."""
    user = _account(django_user_model)
    _pilot(user, 1, "Main", main=True)
    client.force_login(user)
    assert client.get(reverse("identity:pilot_switch")).status_code == 405


# --- ownership: the security boundary -----------------------------------------------------
@pytest.mark.django_db
def test_you_cannot_switch_to_a_pilot_you_do_not_own(client, django_user_model):
    """The IDOR case. Mallory posts Alice's character id at the switch endpoint."""
    from apps.admin_audit.models import AuditLog

    alice = _account(django_user_model, "eve:alice")
    alices_pilot = _pilot(alice, 100, "Alice", main=True)

    mallory = _account(django_user_model, "eve:mallory")
    mallorys_pilot = _pilot(mallory, 200, "Mallory", main=True)

    client.force_login(mallory)
    client.post(reverse("identity:pilot_switch"), {"character_id": alices_pilot.character_id})

    # Still Mallory's own pilot — the forged id resolved to nothing.
    resp = client.get(reverse("identity:linked_pilots"))
    active = [c["character"].character_id for c in resp.context["cards"] if c["is_active"]]
    assert active == [mallorys_pilot.character_id]
    assert AuditLog.objects.filter(action="pilot.switch_denied").exists()


@pytest.mark.django_db
@pytest.mark.parametrize("forged", ["999999", "abc", "", "-1", "1 OR 1=1"])
def test_a_junk_character_id_never_resolves(client, django_user_model, forged):
    user = _account(django_user_model)
    mine = _pilot(user, 1, "Mine", main=True)
    client.force_login(user)

    client.post(reverse("identity:pilot_switch"), {"character_id": forged})
    resp = client.get(reverse("identity:linked_pilots"))
    active = [c["character"].character_id for c in resp.context["cards"] if c["is_active"]]
    assert active == [mine.character_id]


@pytest.mark.django_db
def test_a_session_pointing_at_an_unlinked_pilot_falls_back_safely(client, django_user_model):
    """The pilot is detached (by an officer, or in another tab) while a session still names it.
    The stale hint must not resolve, and must not be left to resolve again next request."""
    user = _account(django_user_model)
    main = _pilot(user, 1, "Main", main=True)
    alt = _pilot(user, 2, "Alt")
    client.force_login(user)
    client.post(reverse("identity:pilot_switch"), {"character_id": alt.character_id})
    assert client.session[pilots.SESSION_KEY] == alt.character_id

    alt.user = None
    alt.save(update_fields=["user"])

    resp = client.get(reverse("identity:linked_pilots"))
    active = [c["character"].character_id for c in resp.context["cards"] if c["is_active"]]
    assert active == [main.character_id]
    assert pilots.SESSION_KEY not in client.session  # the stale hint was dropped, not kept


# --- main pilot ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_promoting_a_main_demotes_the_previous_one(client, django_user_model):
    user = _account(django_user_model)
    old_main = _pilot(user, 1, "Old", main=True)
    alt = _pilot(user, 2, "Alt")
    client.force_login(user)

    client.post(reverse("identity:pilot_main"), {"character_id": alt.character_id})

    old_main.refresh_from_db()
    alt.refresh_from_db()
    user.refresh_from_db()
    assert alt.is_main is True
    assert old_main.is_main is False  # exactly one main, always
    assert user.main_character_id == alt.character_id


# --- unlinking ----------------------------------------------------------------------------
@pytest.mark.django_db
def test_unlinking_severs_the_link_and_destroys_the_tokens(client, django_user_model):
    user = _account(django_user_model)
    _pilot(user, 1, "Main", main=True)
    alt = _pilot(user, 2, "Alt")
    token = _with_token(alt)
    client.force_login(user)

    client.post(reverse("identity:pilot_unlink"), {"character_id": alt.character_id})

    alt.refresh_from_db()
    token.refresh_from_db()
    assert alt.user_id is None                  # the LINK is severed …
    assert EveCharacter.objects.filter(character_id=2).exists()  # … the pilot's record is kept
    assert token.revoked_at is not None
    assert token._refresh_token == ""           # ciphertext erased, not merely marked revoked
    assert token._access_token == ""


@pytest.mark.django_db
def test_the_last_pilot_cannot_be_unlinked(client, django_user_model):
    """An EVE pilot IS the credential — releasing the only one would lock the human out of
    their own account, with their history still inside it."""
    user = _account(django_user_model)
    only = _pilot(user, 1, "Only", main=True)
    client.force_login(user)

    client.post(reverse("identity:pilot_unlink"), {"character_id": only.character_id})

    only.refresh_from_db()
    assert only.user_id == user.pk  # still linked
    assert user.characters.count() == 1


@pytest.mark.django_db
def test_the_service_refuses_the_last_pilot_even_without_the_view(django_user_model):
    """The rule lives in the service, not only in the button's disabled attribute."""
    user = _account(django_user_model)
    only = _pilot(user, 1, "Only", main=True)
    with pytest.raises(linking.LastPilotError):
        linking.unlink(user, only)


@pytest.mark.django_db
def test_unlinking_the_active_pilot_switches_to_another(client, django_user_model):
    user = _account(django_user_model)
    main = _pilot(user, 1, "Main", main=True)
    alt = _pilot(user, 2, "Alt")
    client.force_login(user)
    client.post(reverse("identity:pilot_switch"), {"character_id": alt.character_id})

    client.post(reverse("identity:pilot_unlink"), {"character_id": alt.character_id})

    resp = client.get(reverse("identity:linked_pilots"))
    active = [c["character"].character_id for c in resp.context["cards"] if c["is_active"]]
    assert active == [main.character_id]


@pytest.mark.django_db
def test_unlinking_the_main_promotes_a_new_one(client, django_user_model):
    user = _account(django_user_model)
    main = _pilot(user, 1, "Main", main=True)
    alt = _pilot(user, 2, "Alt")
    client.force_login(user)

    client.post(reverse("identity:pilot_unlink"), {"character_id": main.character_id})

    alt.refresh_from_db()
    assert alt.is_main is True  # the account is never left without a main


@pytest.mark.django_db
def test_you_cannot_unlink_someone_elses_pilot(client, django_user_model):
    alice = _account(django_user_model, "eve:alice")
    alices_pilot = _pilot(alice, 100, "Alice", main=True)
    _pilot(alice, 101, "Alice Alt")

    mallory = _account(django_user_model, "eve:mallory")
    _pilot(mallory, 200, "Mallory", main=True)
    _pilot(mallory, 201, "Mallory Alt")

    client.force_login(mallory)
    client.post(reverse("identity:pilot_unlink"), {"character_id": alices_pilot.character_id})

    alices_pilot.refresh_from_db()
    assert alices_pilot.user_id == alice.pk  # untouched


# --- the selector's ordering + health -----------------------------------------------------
@pytest.mark.django_db
def test_the_selector_puts_the_active_pilot_first(client, django_user_model):
    user = _account(django_user_model)
    _pilot(user, 1, "Aaa", main=True)
    zzz = _pilot(user, 2, "Zzz")
    client.force_login(user)
    client.post(reverse("identity:pilot_switch"), {"character_id": zzz.character_id})

    resp = client.get(reverse("identity:linked_pilots"))
    order = [c["character"].name for c in resp.context["cards"]]
    assert order[0] == "Zzz"  # active first, even though it sorts last alphabetically


@pytest.mark.django_db
def test_a_dead_token_is_reported_but_never_blocks_a_switch(client, django_user_model):
    """A broken pilot must stay switchable — you reauthorise it BY being it."""
    from django.test import override_settings

    user = _account(django_user_model)
    _pilot(user, 1, "Main", main=True)
    broken = _pilot(user, 2, "Broken")  # deliberately no token at all
    client.force_login(user)

    with override_settings(EVE_SSO_DEFAULT_SCOPES=["publicData"]):
        health = linking.link_health(broken)
        assert health["status"] == linking.STATUS_REAUTH_REQUIRED
        assert health["healthy"] is False

    resp = client.post(reverse("identity:pilot_switch"), {"character_id": broken.character_id})
    assert resp.status_code == 302
    resp = client.get(reverse("identity:linked_pilots"))
    active = [c["character"].character_id for c in resp.context["cards"] if c["is_active"]]
    assert active == [broken.character_id]


@pytest.mark.django_db
def test_missing_scopes_are_reported_as_such(django_user_model):
    from django.test import override_settings

    user = _account(django_user_model)
    pilot = _pilot(user, 1, "Partial", main=True)
    _with_token(pilot, scopes=["publicData"])

    with override_settings(EVE_SSO_DEFAULT_SCOPES=["publicData", "esi-skills.read_skills.v1"]):
        health = linking.link_health(pilot)
    assert health["status"] == linking.STATUS_SCOPES_MISSING
    assert health["missing_scopes"] == ["esi-skills.read_skills.v1"]


@pytest.mark.django_db
def test_health_of_the_whole_roster_is_one_query(django_user_model, django_assert_num_queries):
    """The selector renders on every authenticated page. A query per pilot would be a per-page
    tax that grows with a user's alt count."""
    from django.test import override_settings

    user = _account(django_user_model)
    roster = [_pilot(user, i, f"P{i}", main=(i == 1)) for i in range(1, 8)]
    for pilot in roster:
        _with_token(pilot)

    with override_settings(EVE_SSO_DEFAULT_SCOPES=["publicData"]):
        with django_assert_num_queries(1):
            healthy = linking.healthy_ids(roster)
    assert healthy == {p.character_id for p in roster}
