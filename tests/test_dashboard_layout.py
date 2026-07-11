"""PCC-4 — Command Center panel personalisation (dashboard_layout)."""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.identity.models import RoleAssignment
from apps.identity.views import _hidden_panels
from apps.pilots.services import get_prefs
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac

pytestmark = pytest.mark.django_db


def _member(django_user_model, cid=9001, with_char=True):
    user = django_user_model.objects.create(username=f"eve:{cid}")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    if with_char:
        EveCharacter.objects.create(character_id=cid, user=user, name=f"P{cid}",
                                    is_main=True, is_corp_member=True)
    return user


# --- save view ---------------------------------------------------------------
def test_save_layout_stores_hidden(client, django_user_model):
    user = _member(django_user_model, with_char=False)
    client.force_login(user)
    resp = client.post(reverse("identity:save_dashboard_layout"),
                       {"show": ["raffle", "combat_log"]})
    assert resp.status_code == 302
    hidden = set(get_prefs(user).dashboard_layout["hidden"])
    assert hidden == {"onboarding", "pilot_stats", "doctrines", "campaigns", "capsuleer"}


def test_save_layout_all_shown_is_default(client, django_user_model):
    user = _member(django_user_model, with_char=False)
    client.force_login(user)
    client.post(reverse("identity:save_dashboard_layout"),
                {"show": ["raffle", "combat_log", "onboarding", "pilot_stats", "doctrines",
                          "campaigns", "capsuleer"]})
    assert get_prefs(user).dashboard_layout["hidden"] == []


def test_save_layout_ignores_unknown_keys(client, django_user_model):
    user = _member(django_user_model, with_char=False)
    client.force_login(user)
    client.post(reverse("identity:save_dashboard_layout"), {"show": ["bogus"]})
    # bogus isn't a real panel, so every real panel ends up hidden; none is 'bogus'
    hidden = set(get_prefs(user).dashboard_layout["hidden"])
    assert "bogus" not in hidden
    assert hidden == {"raffle", "combat_log", "onboarding", "pilot_stats", "doctrines",
                      "campaigns", "capsuleer"}


# --- helper ------------------------------------------------------------------
def test_hidden_panels_helper_filters_unknown(django_user_model):
    user = _member(django_user_model, with_char=False)
    prefs = get_prefs(user)
    prefs.dashboard_layout = {"hidden": ["doctrines", "not_a_panel"]}
    prefs.save()
    assert _hidden_panels(user) == {"doctrines"}


def test_hidden_panels_default_empty(django_user_model):
    user = _member(django_user_model, with_char=False)
    assert _hidden_panels(user) == set()


# --- dashboard render --------------------------------------------------------
def test_dashboard_has_customize_form(client, django_user_model, sde):
    user = _member(django_user_model)
    client.force_login(user)
    resp = client.get("/dashboard/")
    assert resp.status_code == 200
    assert b"Customize panels" in resp.content
    assert resp.context["hidden_panels"] == set()  # default: nothing hidden


def test_dashboard_reflects_hidden_panel(client, django_user_model, sde):
    user = _member(django_user_model)
    prefs = get_prefs(user)
    prefs.dashboard_layout = {"hidden": ["doctrines"]}
    prefs.save()
    client.force_login(user)
    resp = client.get("/dashboard/")
    assert resp.status_code == 200
    assert "doctrines" in resp.context["hidden_panels"]
