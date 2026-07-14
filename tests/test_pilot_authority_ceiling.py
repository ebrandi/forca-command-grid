"""Authority follows the ACTIVE pilot, and never leaks between pilots (LP-4).

Before Linked Pilots, roles were account-level and derived from the UNION of a user's characters:
link one Director alt and the whole account — every pilot on it — wielded Director authority.
That is a privilege-escalation path, and it is what these tests close.

The model is sudo's: a role grant says what the *human* is trusted with; the pilot you are
currently flying decides what may be *exercised*. Effective authority is the lesser of the two.

    effective_rank(user) = min( account_rank, ceiling(active_pilot) )

    active pilot outside the corp        → ceiling = public   (no corp standing at all)
    in the corp, not an in-game Director → ceiling = officer  (Director is out of reach)
    in the corp, an in-game Director     → no practical ceiling

Two deliberate exemptions, both tested below: ``is_superuser`` and the ROLE_ADMIN grant. Those
are the platform operator, not a corp rank — ceilinging them would let an admin lock themselves
out by switching to an alt, and the rank ladder makes a cap low enough to withdraw Director
(30) also withdraw Admin (40).
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.identity.models import RoleAssignment
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import pilots, rbac

HOME_CORP = 98000001


def _user(django_user_model, username, *roles, superuser=False):
    user = django_user_model.objects.create(username=username, is_superuser=superuser)
    for role in roles:
        RoleAssignment.objects.create(user=user, role=ensure_role(role))
    return user


def _pilot(user, character_id, name, *, main=False, corp_member=True, director=False):
    return EveCharacter.objects.create(
        character_id=character_id, user=user, name=name, is_main=main,
        is_corp_member=corp_member, is_corp_director=director,
    )


def _flying(user, character):
    """The user, with ``character`` resolved as their active pilot (what the middleware does)."""
    pilots.attach(user, character)
    return user


# --- the ceiling itself -------------------------------------------------------------------
@pytest.mark.django_db
def test_a_director_flying_a_plain_member_alt_is_not_a_director(django_user_model):
    """The brief's own example. Pilot A is an in-game Director; Pilot B is a normal member.
    Switching to B must remove director-only access."""
    user = _user(django_user_model, "eve:d", rbac.ROLE_MEMBER, rbac.ROLE_DIRECTOR)
    pilot_a = _pilot(user, 1, "Director Main", main=True, director=True)
    pilot_b = _pilot(user, 2, "Member Alt")

    assert rbac.has_role(_flying(user, pilot_a), rbac.ROLE_DIRECTOR) is True
    assert rbac.has_role(_flying(user, pilot_b), rbac.ROLE_DIRECTOR) is False
    # …but B is still a member. The ceiling removes authority; it does not erase the account.
    assert rbac.has_role(_flying(user, pilot_b), rbac.ROLE_MEMBER) is True


@pytest.mark.django_db
def test_a_pilot_outside_the_corporation_carries_no_standing_at_all(django_user_model):
    """Pilot A is in FORCA; Pilot B is in an unrelated corporation. B sees only what B may."""
    user = _user(django_user_model, "eve:o", rbac.ROLE_MEMBER, rbac.ROLE_OFFICER)
    inside = _pilot(user, 1, "Inside", main=True)
    outside = _pilot(user, 2, "Outside", corp_member=False)

    assert rbac.has_role(_flying(user, inside), rbac.ROLE_OFFICER) is True
    assert rbac.has_role(_flying(user, outside), rbac.ROLE_OFFICER) is False
    assert rbac.has_role(_flying(user, outside), rbac.ROLE_MEMBER) is False
    assert rbac.effective_rank(_flying(user, outside)) == rbac.ROLE_RANK[rbac.ROLE_PUBLIC]


@pytest.mark.django_db
def test_officer_authority_follows_the_human_across_their_corp_pilots(django_user_model):
    """A documented consequence (LP-4). Officer is a trust grant to a PERSON and there is no
    per-character evidence that could narrow it — unlike Director, which EVE itself assigns to a
    character. So an officer stays an officer on any pilot that is actually in the corp."""
    user = _user(django_user_model, "eve:off", rbac.ROLE_MEMBER, rbac.ROLE_OFFICER)
    main = _pilot(user, 1, "Main", main=True)
    alt = _pilot(user, 2, "Corp Alt")

    assert rbac.has_role(_flying(user, main), rbac.ROLE_OFFICER) is True
    assert rbac.has_role(_flying(user, alt), rbac.ROLE_OFFICER) is True


@pytest.mark.django_db
def test_a_lateral_capability_is_ceilinged_like_a_rank(django_user_model):
    """A recruiter flying an alt in another corporation is not recruiting for us from that seat."""
    user = _user(django_user_model, "eve:rec", rbac.ROLE_MEMBER, rbac.ROLE_RECRUITER)
    role = ensure_role(rbac.ROLE_RECRUITER)
    from apps.identity.models import Permission

    perm, _ = Permission.objects.get_or_create(key=rbac.PERM_RECRUITMENT_MANAGE)
    role.permissions.add(perm)

    inside = _pilot(user, 1, "Inside", main=True)
    outside = _pilot(user, 2, "Outside", corp_member=False)

    assert rbac.has_perm(_flying(user, inside), rbac.PERM_RECRUITMENT_MANAGE) is True
    assert rbac.has_perm(_flying(user, outside), rbac.PERM_RECRUITMENT_MANAGE) is False


# --- the exemptions -----------------------------------------------------------------------
@pytest.mark.django_db
def test_a_superuser_is_never_ceilinged(django_user_model):
    """The platform break-glass. If switching to an alt could strip it, an operator could lock
    themselves out of their own instance."""
    user = _user(django_user_model, "eve:root", superuser=True)
    outside = _pilot(user, 1, "Outside", main=True, corp_member=False)
    assert rbac.effective_rank(_flying(user, outside)) == rbac.ROLE_RANK[rbac.ROLE_ADMIN]


@pytest.mark.django_db
def test_the_admin_role_is_never_ceilinged(django_user_model):
    """Same reasoning as is_superuser, plus a structural one: the ranks are a ladder, so any cap
    low enough to withdraw Director (30) would also withdraw Admin (40)."""
    user = _user(django_user_model, "eve:admin", rbac.ROLE_ADMIN)
    outside = _pilot(user, 1, "Outside", main=True, corp_member=False)
    assert rbac.has_role(_flying(user, outside), rbac.ROLE_ADMIN) is True


@pytest.mark.django_db
def test_an_account_with_no_pilots_keeps_its_grants(django_user_model):
    """A ceiling exists to stop authority leaking from one pilot to another. With no pilots there
    is nothing to leak from — and a hand-made operator account must not be locked out."""
    user = _user(django_user_model, "eve:nochars", rbac.ROLE_MEMBER, rbac.ROLE_OFFICER)
    pilots.attach(user, None)
    assert rbac.has_role(user, rbac.ROLE_OFFICER) is True


@pytest.mark.django_db
def test_outside_a_request_authority_is_account_wide(django_user_model):
    """A Celery worker reconciling Discord roles is asking what the HUMAN is entitled to, not
    what some browser session happens to have selected. No pilot is resolved there, and the
    account's own grants stand — which is a different question from 'this account has no pilots',
    and must not be confused with it."""
    user = _user(django_user_model, "eve:bg", rbac.ROLE_MEMBER, rbac.ROLE_DIRECTOR)
    _pilot(user, 1, "Director Main", main=True, director=True)
    _pilot(user, 2, "Member Alt")

    # Freshly loaded, exactly as a task would load it: nothing has attached a pilot.
    reloaded = django_user_model.objects.get(pk=user.pk)
    assert pilots.has_resolved_pilot(reloaded) is False
    assert rbac.has_role(reloaded, rbac.ROLE_DIRECTOR) is True


# --- end to end, through the real gates ---------------------------------------------------
@pytest.mark.django_db
def test_switching_to_a_member_alt_closes_the_director_console(client, django_user_model):
    """Not a unit test of the ceiling — the real thing, through the real middleware and the real
    view decorators, over HTTP."""
    user = _user(django_user_model, "eve:dir", rbac.ROLE_MEMBER, rbac.ROLE_DIRECTOR)
    _pilot(user, 1, "Director Main", main=True, director=True)
    alt = _pilot(user, 2, "Member Alt")
    client.force_login(user)

    assert client.get("/ops/admin/members/").status_code == 200

    client.post(reverse("identity:pilot_switch"), {"character_id": alt.character_id})
    assert client.get("/ops/admin/members/").status_code == 403

    # …and switching back restores it. The authority was withheld, not destroyed.
    client.post(reverse("identity:pilot_switch"), {"character_id": 1})
    assert client.get("/ops/admin/members/").status_code == 200


@pytest.mark.django_db
def test_switching_to_an_outside_alt_confines_you_to_the_recruitment_surface(
    client, django_user_model, settings
):
    """MembershipGateMiddleware sends a non-member to onboarding. Once authority comes from the
    active pilot, flying an out-of-corp alt IS being a non-member — so the gate must catch it,
    and Linked Pilots must stay reachable or the user is stranded with no way back."""
    settings.FORCA_HOME_CORP_ID = HOME_CORP
    user = _user(django_user_model, "eve:m", rbac.ROLE_MEMBER)
    _pilot(user, 1, "Inside", main=True)
    outside = _pilot(user, 2, "Outside", corp_member=False)
    client.force_login(user)

    assert client.get(reverse("identity:dashboard")).status_code == 200

    client.post(reverse("identity:pilot_switch"), {"character_id": outside.character_id})
    assert client.get(reverse("identity:dashboard")).status_code == 302  # → onboarding

    # The way back must not be behind the gate that just moved.
    assert client.get(reverse("identity:linked_pilots")).status_code == 200
    client.post(reverse("identity:pilot_switch"), {"character_id": 1})
    assert client.get(reverse("identity:dashboard")).status_code == 200


@pytest.mark.django_db
def test_the_switch_redirect_does_not_dump_you_on_a_page_you_may_no_longer_see(
    client, django_user_model
):
    """Switching from a director-only page to a member alt must land on the dashboard with an
    explanation — not bounce you into a 403."""
    user = _user(django_user_model, "eve:dir2", rbac.ROLE_MEMBER, rbac.ROLE_DIRECTOR)
    _pilot(user, 1, "Director Main", main=True, director=True)
    alt = _pilot(user, 2, "Member Alt")
    client.force_login(user)

    resp = client.post(
        reverse("identity:pilot_switch"),
        {"character_id": alt.character_id, "next": "/ops/admin/members/"},
    )
    assert resp.status_code == 302
    assert resp.url == reverse("identity:dashboard")


@pytest.mark.django_db
def test_the_switch_redirect_keeps_you_where_you_were_when_it_is_still_allowed(
    client, django_user_model
):
    user = _user(django_user_model, "eve:m2", rbac.ROLE_MEMBER)
    _pilot(user, 1, "Main", main=True)
    alt = _pilot(user, 2, "Corp Alt")
    client.force_login(user)

    resp = client.post(
        reverse("identity:pilot_switch"),
        {"character_id": alt.character_id, "next": "/killboard/"},
    )
    assert resp.status_code == 302
    assert resp.url == "/killboard/"


@pytest.mark.django_db
@pytest.mark.parametrize("hostile", [
    "https://evil.example/phish",
    "//evil.example/phish",
    "javascript:alert(1)",
])
def test_the_switch_redirect_is_not_an_open_redirect(client, django_user_model, hostile):
    user = _user(django_user_model, "eve:m3", rbac.ROLE_MEMBER)
    _pilot(user, 1, "Main", main=True)
    alt = _pilot(user, 2, "Alt")
    client.force_login(user)

    resp = client.post(
        reverse("identity:pilot_switch"),
        {"character_id": alt.character_id, "next": hostile},
    )
    assert resp.status_code == 302
    assert resp.url == reverse("identity:dashboard")
