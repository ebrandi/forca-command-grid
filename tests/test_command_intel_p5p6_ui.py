"""Surface gating for the P5/P6 web pages: the simulator is officer-gated, the pilot
quest-log honours its feature flag, and the delivery console arms config safely."""
from __future__ import annotations

import pytest
from django.core.cache import cache
from django.urls import reverse

from apps.command_intel import config
from apps.identity.models import RoleAssignment
from apps.sso.services import ensure_role
from core import features, rbac


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _user(django_user_model, role, suffix=""):
    user, _ = django_user_model.objects.get_or_create(username=f"ci-{role}{suffix}")
    RoleAssignment.objects.get_or_create(user=user, role=ensure_role(role))
    return user


@pytest.mark.django_db
def test_simulator_is_officer_gated(client, django_user_model, sde):
    client.force_login(_user(django_user_model, rbac.ROLE_OFFICER, "-sim"))
    assert client.get("/command/sim/").status_code == 200


@pytest.mark.django_db
def test_simulator_blocks_a_plain_member(client, django_user_model, sde):
    client.force_login(_user(django_user_model, rbac.ROLE_MEMBER, "-sim"))
    assert client.get("/command/sim/").status_code in (403, 302)


@pytest.mark.django_db
def test_pilot_quest_log_respects_its_feature_flag(client, django_user_model, sde):
    # The old /command/me/ page is a redirect into the merged Daily Briefing, but
    # its off-switch semantics survive: 404 when the pilot slice is disabled.
    member = _user(django_user_model, rbac.ROLE_MEMBER, "-flag")
    client.force_login(member)
    assert client.get("/command/me/").status_code == 302  # default-on → redirect
    features.set_disabled(["command_intel_pilot"])
    assert client.get("/command/me/").status_code == 404   # feature_required 404s when off


@pytest.mark.django_db
def test_delivery_console_arms_config(client, django_user_model, sde):
    director = _user(django_user_model, rbac.ROLE_DIRECTOR, "-notif")
    client.force_login(director)
    url = reverse("admin_audit:command_intel_notifications")
    assert client.get(url).status_code == 200

    # Try to arm Discord for high_command AND sneak in a forbidden tier — the view filters
    # it to the broadcast-safe set before the (also-guarding) validator ever runs.
    resp = client.post(url, {
        "scheduled_enabled": "on",
        "deliver_discord": "on",
        "discord_classifications": ["high_command", "director_eyes_only"],
        "min_severity_to_deliver": "watch",
    })
    assert resp.status_code == 302
    saved = config.get("notifications")
    assert saved["deliver_discord"] is True
    assert saved["discord_classifications"] == ["high_command"]  # forbidden tier dropped
    assert "director_eyes_only" not in saved["discord_classifications"]


@pytest.mark.django_db
def test_me_renders_the_directive_card(client, django_user_model, sde):
    # The quest-log card block is new markup — render it end-to-end (a populated dict,
    # not an empty one: the class of template bug that shipped a 500 to prod once).
    from apps.command_intel import pilot
    from apps.command_intel.models import PilotDirective
    from apps.sso.models import EveCharacter

    member = _user(django_user_model, rbac.ROLE_MEMBER, "-card")
    EveCharacter.objects.create(character_id=4242, user=member, name="Toren",
                                is_main=True, is_corp_member=True)
    PilotDirective.objects.create(
        user=member, slug="fleet_size.ferox/train", constraint_key="fleet_size.ferox",
        category="skill", title="Train into Ferox", detail="Relieves the corp's shortage.",
        leverage=75, points=12, action_url="/skills/",
    )
    # Prime the cache so the view renders the persisted directive without recomputing.
    from django.core.cache import cache as _c
    _c.set(pilot.cache_key(4242), {"directives": []})
    client.force_login(member)
    body = client.get("/command/me/", follow=True).content  # → merged Daily Briefing
    assert b"Train into Ferox" in body
    assert b"Mark done" in body
    assert b"high leverage" in body  # the #1 CI-grounded card badge


@pytest.mark.django_db
def test_simulator_renders_before_after_rows(client, django_user_model, sde):
    from apps.command_intel.models import IntelligenceSnapshot

    IntelligenceSnapshot.objects.create(slices={"doctrine": {"doctrines": [
        {"name": "Ferox", "slug": "ferox", "primary": True,
         "flyable": 30, "hulls_in_stock": 40, "min_pilots": 22},
    ]}})
    client.force_login(_user(django_user_model, rbac.ROLE_OFFICER, "-simrows"))
    body = client.get("/command/sim/?scenario=pilot_attrition&magnitude=12").content
    assert b"Ferox fleet size" in body     # the before/after constraint row rendered
    assert b"before" in body.lower() or b"After" in body
