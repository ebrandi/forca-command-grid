"""REC-1 (roadmap 2.10) — designated notification/mail relay character.

Acceptance: leadership can select and rotate the relay character; both relays use the
designated character (not the first-with-scope); the fallback is deterministic; the
console only accepts a character that actually holds a relay scope.
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.recommendations import relay
from apps.recommendations.relay import (
    MAIL_SCOPE,
    NOTIF_SCOPE,
    eligible_relay_characters,
    relay_character,
)
from core import rbac
from tests._raffle_utils import enrol_pilot

pytestmark = pytest.mark.django_db

OTHER_SCOPE = "esi-skills.read_skills.v1"


def test_no_designated_falls_back_deterministically(django_user_model):
    enrol_pilot(django_user_model, 2001, scopes=[NOTIF_SCOPE])
    enrol_pilot(django_user_model, 1001, scopes=[NOTIF_SCOPE])
    # Fallback is the lowest character_id with a valid token (deterministic, not "first found").
    assert relay_character(NOTIF_SCOPE).character_id == 1001


def test_designated_character_is_used(django_user_model):
    enrol_pilot(django_user_model, 1001, scopes=[NOTIF_SCOPE])
    enrol_pilot(django_user_model, 2001, scopes=[NOTIF_SCOPE])
    relay.set_designated_relay_character(2001)
    assert relay_character(NOTIF_SCOPE).character_id == 2001


def test_designated_without_the_scope_falls_back(django_user_model):
    enrol_pilot(django_user_model, 1001, scopes=[NOTIF_SCOPE])
    enrol_pilot(django_user_model, 2001, scopes=[MAIL_SCOPE])  # designated, but no notif scope
    relay.set_designated_relay_character(2001)
    assert relay_character(NOTIF_SCOPE).character_id == 1001   # falls back
    assert relay_character(MAIL_SCOPE).character_id == 2001    # used for the scope it holds


def test_eligible_lists_only_scope_holders(django_user_model):
    enrol_pilot(django_user_model, 1001, scopes=[NOTIF_SCOPE, MAIL_SCOPE])
    enrol_pilot(django_user_model, 2001, scopes=[OTHER_SCOPE])  # no relay scope
    elig = eligible_relay_characters()
    ids = {e["character_id"] for e in elig}
    assert 1001 in ids and 2001 not in ids
    row = next(e for e in elig if e["character_id"] == 1001)
    assert row["has_notifications"] and row["has_mail"]


def test_both_relays_delegate_to_the_helper(django_user_model):
    from apps.recommendations import mail_relay
    from apps.recommendations import notifications as notif
    enrol_pilot(django_user_model, 1001, scopes=[NOTIF_SCOPE, MAIL_SCOPE])
    enrol_pilot(django_user_model, 2001, scopes=[NOTIF_SCOPE, MAIL_SCOPE])
    relay.set_designated_relay_character(2001)
    assert notif._token_character(0).character_id == 2001
    assert mail_relay._token_character(0).character_id == 2001


def test_console_set_rotate_clear_and_validate(client, django_user_model):
    user, _ = enrol_pilot(django_user_model, 9001, roles=(rbac.ROLE_DIRECTOR,), scopes=[NOTIF_SCOPE])
    enrol_pilot(django_user_model, 9002, scopes=[MAIL_SCOPE])
    client.force_login(user)
    url = reverse("admin_audit:relay_settings")
    assert client.get(url).status_code == 200

    client.post(url, {"character_id": "9001"})
    assert relay.designated_relay_character_id() == 9001
    client.post(url, {"character_id": "9002"})  # rotate
    assert relay.designated_relay_character_id() == 9002
    client.post(url, {"character_id": "424242"})  # not eligible → rejected
    assert relay.designated_relay_character_id() == 9002
    client.post(url, {"character_id": ""})  # clear → fallback
    assert relay.designated_relay_character_id() is None


def test_ciphertext_erased_token_is_not_eligible(django_user_model):
    # A token that still has the scope but whose refresh ciphertext was erased (would
    # fail at relay time) must not be offered as designatable.
    from apps.sso.models import AuthToken

    enrol_pilot(django_user_model, 3001, scopes=[NOTIF_SCOPE])
    t = AuthToken.objects.get(character__character_id=3001)
    t._refresh_token = ""
    t.save(update_fields=["_refresh_token"])
    assert 3001 not in {e["character_id"] for e in eligible_relay_characters()}


def test_console_requires_director(client, django_user_model):
    user, _ = enrol_pilot(django_user_model, 8001, roles=(rbac.ROLE_MEMBER,))
    client.force_login(user)
    r = client.get(reverse("admin_audit:relay_settings"))
    assert r.status_code in (302, 403)
