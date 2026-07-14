"""Adding a pilot through EVE SSO: the link flow and the ways it must refuse (LP-5).

Linking is the only way a pilot enters an account. It requires a full EVE SSO authorisation for
that specific pilot — never a typed name, never a character id. CCP publishes no way to discover
which characters share an EVE account, and this feature does not pretend otherwise: proving
control of a pilot IS the authorisation.

The dangerous shape here is **callback confusion**. One registered callback URL serves both
"I am signing in" and "I am adding a pilot" (EVE validates redirect_uri against the CCP developer
application, so a second callback would force every operator to reconfigure their app). The
intent is therefore bound to the OAuth state SERVER-SIDE, in the session, and the link flow is
additionally pinned to the account that started it.
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.identity.models import RoleAssignment
from apps.sso import views as sso_views
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac


class _FakeToken:
    access_token = "at"
    refresh_token = "rt"
    expires_in = 1200
    token_type = "Bearer"


def _account(django_user_model, username="eve:1"):
    user = django_user_model.objects.create(username=username)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    return user


def _pilot(user, character_id, name, *, main=False):
    return EveCharacter.objects.create(
        character_id=character_id, user=user, name=name, is_main=main,
        is_corp_member=True, owner_hash=f"owner-{character_id}",
    )


@pytest.fixture
def sso(monkeypatch):
    """Stand in for EVE SSO: the code exchange and the JWT validation.

    ``claims_for`` decides which pilot comes back from CCP — the whole point being that the
    HUMAN picks the character on CCP's screen, so the app must cope with any of them coming back.
    """
    state = {"character_id": 2, "name": "New Pilot", "owner": None}

    def fake_exchange(code, verifier, client=None):
        return _FakeToken()

    def fake_validate(access_token, jwks_client=None, client=None):
        cid = state["character_id"]
        return {
            "sub": f"CHARACTER:EVE:{cid}",
            "name": state["name"],
            "owner": state["owner"] or f"owner-{cid}",
            "scp": ["publicData"],
        }

    monkeypatch.setattr(sso_views.oauth, "exchange_code", fake_exchange)
    monkeypatch.setattr(sso_views.oauth, "validate_access_token", fake_validate)
    # Affiliation refresh + the post-login warm task both reach for ESI/Celery; neither is what
    # these tests are about.
    monkeypatch.setattr("apps.sso.services.refresh_affiliation", lambda c: None)
    monkeypatch.setattr("apps.sso.services.sync_roles_for_user", lambda u, **kw: None)
    return state


def _begin_link(client, **post):
    return client.post(reverse("sso:link"), post)


def _finish(client):
    """Come back from EVE with the state the session is holding."""
    return client.get(
        reverse("sso:callback"),
        {"code": "authcode", "state": client.session["eve_sso_state"]},
    )


# --- the happy path -----------------------------------------------------------------------
@pytest.mark.django_db
def test_linking_a_new_pilot_attaches_it_to_the_signed_in_account(client, django_user_model, sso):
    from apps.admin_audit.models import AuditLog

    user = _account(django_user_model)
    _pilot(user, 1, "Main", main=True)
    client.force_login(user)

    resp = _begin_link(client)
    assert resp.status_code == 302
    assert client.session["eve_sso_flow"] == "link"
    assert client.session["eve_sso_link_user"] == user.pk

    resp = _finish(client)
    assert resp.status_code == 302
    assert resp.url == reverse("identity:linked_pilots")

    linked = EveCharacter.objects.get(character_id=2)
    assert linked.user_id == user.pk
    assert linked.is_main is False  # linking never demotes the existing main
    assert AuditLog.objects.filter(action="pilot.linked", target_id="2").exists()


@pytest.mark.django_db
def test_relinking_a_pilot_you_already_hold_is_a_reauthorisation(client, django_user_model, sso):
    """Reauthorise IS the link flow — a fresh token replaces the dead one. It must not be
    reported as a new link, and it must not fail."""
    from apps.admin_audit.models import AuditLog

    user = _account(django_user_model)
    _pilot(user, 1, "Main", main=True)
    mine = _pilot(user, 2, "Mine")
    client.force_login(user)

    sso["character_id"] = mine.character_id
    _begin_link(client, character_id=mine.character_id)
    _finish(client)

    assert AuditLog.objects.filter(action="pilot.reauthorised", target_id="2").exists()
    assert not AuditLog.objects.filter(action="pilot.linked").exists()


@pytest.mark.django_db
def test_authorising_a_different_pilot_than_you_meant_to_says_so(client, django_user_model, sso):
    """CCP's screen is where the human picks the character, so they can pick the wrong one.
    Linking it silently — when they clicked "Reauthorise" on a specific pilot — is the kind of
    surprise that destroys trust in an identity feature."""
    user = _account(django_user_model)
    _pilot(user, 1, "Main", main=True)
    intended = _pilot(user, 2, "Intended")
    client.force_login(user)

    _begin_link(client, character_id=intended.character_id)
    sso["character_id"] = 3  # …but they authorised someone else entirely
    sso["name"] = "Someone Else"
    resp = _finish(client)

    messages = [str(m) for m in resp.wsgi_request._messages]
    assert any("Someone Else" in m and "not the pilot you selected" in m for m in messages)


# --- refusals -----------------------------------------------------------------------------
@pytest.mark.django_db
def test_a_pilot_on_another_account_is_refused_without_naming_that_account(
    client, django_user_model, sso
):
    from apps.admin_audit.models import AuditLog

    alice = _account(django_user_model, "eve:alice")
    _pilot(alice, 50, "Alice Pilot", main=True)

    mallory = _account(django_user_model, "eve:mallory")
    _pilot(mallory, 1, "Mallory", main=True)
    client.force_login(mallory)

    sso["character_id"] = 50
    sso["name"] = "Alice Pilot"
    _begin_link(client)
    resp = _finish(client)

    assert EveCharacter.objects.get(character_id=50).user_id == alice.pk  # still Alice's
    entry = AuditLog.objects.get(action="pilot.link_rejected")
    assert entry.metadata["reason"] == "ownership_conflict"

    text = " ".join(str(m) for m in resp.wsgi_request._messages)
    assert "Alice Pilot" in text          # the pilot they just authorised — they know it exists
    assert "alice" not in text.lower().replace("alice pilot", "")  # …but never the other account


@pytest.mark.django_db
def test_a_link_started_by_one_account_cannot_complete_into_another(
    client, django_user_model, sso
):
    """Session fixation's cousin. If the session changes identity mid-flow, the freshly
    authorised pilot must NOT land in whichever account happens to be signed in at the end."""
    alice = _account(django_user_model, "eve:alice")
    _pilot(alice, 1, "Alice", main=True)
    mallory = _account(django_user_model, "eve:mallory")
    _pilot(mallory, 9, "Mallory", main=True)

    client.force_login(alice)
    _begin_link(client)  # Alice starts a link…

    # …and the session becomes Mallory before the callback lands. The link-owner pin is what
    # stops the new pilot being attached to Mallory.
    session = client.session
    state = session["eve_sso_state"]
    client.force_login(mallory)
    hijacked = client.session
    hijacked["eve_sso_state"] = state
    hijacked["eve_sso_verifier"] = session["eve_sso_verifier"]
    hijacked["eve_sso_flow"] = "link"
    hijacked["eve_sso_link_user"] = alice.pk  # the pin still names Alice
    hijacked.save()

    resp = client.get(reverse("sso:callback"), {"code": "authcode", "state": state})
    assert resp.status_code == 400
    assert not EveCharacter.objects.filter(character_id=2).exists()


@pytest.mark.django_db
def test_linking_requires_POST(client, django_user_model):
    """A GET that begins an OAuth authorisation is login-CSRF: any page on the internet could
    start this flow in the victim's session from an <img> tag."""
    user = _account(django_user_model)
    _pilot(user, 1, "Main", main=True)
    client.force_login(user)
    assert client.get(reverse("sso:link")).status_code == 405


@pytest.mark.django_db
def test_linking_requires_an_authenticated_session(client):
    resp = client.post(reverse("sso:link"))
    assert resp.status_code == 302
    assert reverse("sso:login") in resp.url


@pytest.mark.django_db
def test_a_stale_link_intent_cannot_hijack_a_later_login(client, django_user_model, sso):
    """Begin a link, abandon it, then log in normally. The abandoned intent must not turn the
    login into a link — the flow is popped and rewritten on every begin."""
    user = _account(django_user_model)
    _pilot(user, 1, "Main", main=True)
    client.force_login(user)

    _begin_link(client)
    assert client.session["eve_sso_flow"] == "link"

    client.get(reverse("sso:login"))
    assert client.session["eve_sso_flow"] == "login"
    assert "eve_sso_link_user" not in client.session


@pytest.mark.django_db
def test_the_oauth_state_is_single_use(client, django_user_model, sso):
    """Replaying a callback must not link the pilot twice, or link anything at all."""
    user = _account(django_user_model)
    _pilot(user, 1, "Main", main=True)
    client.force_login(user)

    _begin_link(client)
    state = client.session["eve_sso_state"]
    first = client.get(reverse("sso:callback"), {"code": "authcode", "state": state})
    assert first.status_code == 302

    replay = client.get(reverse("sso:callback"), {"code": "authcode", "state": state})
    assert replay.status_code == 400  # the state was popped on first use


@pytest.mark.django_db
def test_a_wrong_state_is_refused(client, django_user_model, sso):
    user = _account(django_user_model)
    _pilot(user, 1, "Main", main=True)
    client.force_login(user)

    _begin_link(client)
    resp = client.get(reverse("sso:callback"), {"code": "authcode", "state": "not-the-state"})
    assert resp.status_code == 400
    assert not EveCharacter.objects.filter(character_id=2).exists()


@pytest.mark.django_db
def test_an_ordinary_login_is_still_an_ordinary_login(client, django_user_model, sso):
    """The regression that matters most: the login flow shares this callback, and must be
    completely unaffected by the link branch existing."""
    resp = client.get(reverse("sso:login"))
    assert resp.status_code == 302
    assert client.session["eve_sso_flow"] == "login"

    sso["character_id"] = 77
    sso["name"] = "Brand New"
    resp = _finish(client)

    assert resp.status_code == 302
    character = EveCharacter.objects.get(character_id=77)
    assert character.user is not None
    assert character.user.username == "eve:77"  # a fresh account was created for them
