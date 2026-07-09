"""Re-login must be idempotent: a returning pilot whose character was detached
or erased still owns an ``eve:<id>`` account, so the callback must reuse it
instead of crashing on a duplicate username (the cause of the login 500)."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import AnonymousUser

from apps.sso.models import EveCharacter
from apps.sso.services import resolve_login_account


@pytest.mark.django_db
def test_first_login_creates_account(django_user_model):
    account = resolve_login_account(AnonymousUser(), 555, "Pilot")
    assert account.username == "eve:555"
    assert not account.has_usable_password()


@pytest.mark.django_db
def test_relogin_reuses_account_when_no_character(django_user_model):
    first = resolve_login_account(AnonymousUser(), 555, "Pilot")
    # No EveCharacter row exists (e.g. erased), but the account persists.
    again = resolve_login_account(AnonymousUser(), 555, "Pilot")
    assert again.id == first.id  # reused, not a duplicate-username crash


@pytest.mark.django_db
def test_relogin_reuses_account_when_character_detached(django_user_model):
    first = resolve_login_account(AnonymousUser(), 555, "Pilot")
    # Erasure detaches the character (user=None) but keeps the account.
    EveCharacter.objects.create(character_id=555, user=None, name="Pilot")
    again = resolve_login_account(AnonymousUser(), 555, "Pilot")
    assert again.id == first.id


@pytest.mark.django_db
def test_uses_account_the_character_is_linked_to(django_user_model):
    user = django_user_model.objects.create(username="custom-name")
    EveCharacter.objects.create(character_id=556, user=user, name="X")
    assert resolve_login_account(AnonymousUser(), 556, "X").id == user.id


@pytest.mark.django_db
def test_authenticated_user_links_additional_character(django_user_model):
    user = django_user_model.objects.create(username="already-in")
    # Linking an extra character while logged in keeps the current account.
    assert resolve_login_account(user, 999, "Alt").id == user.id
