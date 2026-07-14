"""The "new goal" form renders.

It did not — every pilot who clicked "new goal" got an HTTP 500, in every language.

``goal_new`` renders ``goal_form.html`` with ``goal=None``, and the motivation textarea read::

    {{ owner_motivation|default:goal.motivation|default:'' }}

Django resolves a filter's *arguments* eagerly, so ``goal.motivation`` was looked up on
``None`` before ``default`` could ever supply a fallback: VariableDoesNotExist → 500. A
failure in a filter's *input* is swallowed and replaced by ``string_if_invalid``; a failure
in its *argument* is not. That asymmetry is the entire bug, and it is why the code reads as
if it were already guarded.

It survived because nothing ever rendered the page — the suite covered the POST that creates
a goal, never a plain GET of the empty form.
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.identity.models import RoleAssignment
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac


@pytest.fixture
def member(django_user_model):
    """A corp member — the membership gate redirects anyone else to onboarding."""
    u = django_user_model.objects.create(username="eve:9001", first_name="Goal Pilot")
    RoleAssignment.objects.create(user=u, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(
        character_id=9001, user=u, name="Goal Pilot", is_main=True, is_corp_member=True
    )
    return u


@pytest.mark.django_db
def test_new_goal_form_renders(client, member):
    client.force_login(member)
    resp = client.get(reverse("capsuleer:goal_new"))
    assert resp.status_code == 200, "the new-goal form must render with no goal in context"
    assert b'name="motivation"' in resp.content


@pytest.mark.django_db
def test_edit_goal_form_prefills_the_motivation(client, member):
    from apps.capsuleer.models import CareerGoal

    goal = CareerGoal.objects.create(user=member, title="Fly logi", motivation="carry the fleet")
    client.force_login(member)
    resp = client.get(reverse("capsuleer:goal_edit", args=[goal.pk]))
    assert resp.status_code == 200
    assert b"carry the fleet" in resp.content, "editing a goal must still pre-fill its motivation"
