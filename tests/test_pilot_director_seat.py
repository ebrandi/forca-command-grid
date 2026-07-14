"""The in-game Director seat: where it comes from, and where it must be withdrawn (LP-4).

``EveCharacter.is_corp_director`` is the evidence the authority ceiling reads. A stale ``True``
is a real escalation — it lets a pilot wield Director authority they can no longer substantiate —
so every path that ends a pilot's claim to the role has to clear it, not just the account grant.
"""
from __future__ import annotations

import pytest

from apps.identity.models import RoleAssignment
from apps.sso import services
from apps.sso.models import AuthToken, EveCharacter
from apps.sso.services import ensure_role
from core import rbac


def _user(django_user_model, username, *roles):
    user = django_user_model.objects.create(username=username)
    for role in roles:
        RoleAssignment.objects.create(user=user, role=ensure_role(role))
    return user


def _pilot(user, character_id, **kw):
    kw.setdefault("is_corp_member", True)
    return EveCharacter.objects.create(
        character_id=character_id, user=user, name=f"P{character_id}", **kw
    )


@pytest.mark.django_db
def test_the_reconcile_records_the_seat_per_pilot(django_user_model, monkeypatch):
    """The ESI check has always been per-character; only the ACCOUNT grant was ever stored. The
    ceiling needs to know WHICH pilot is the director."""
    user = _user(django_user_model, "eve:d", rbac.ROLE_MEMBER)
    director = _pilot(user, 1, is_main=True)
    plain = _pilot(user, 2)

    monkeypatch.setattr(
        services, "character_is_corp_director", lambda c: c.character_id == 1
    )
    services._reconcile_roles_for_user(user, check_director=True)

    director.refresh_from_db()
    plain.refresh_from_db()
    assert director.is_corp_director is True
    assert plain.is_corp_director is False
    assert rbac.has_role(user, rbac.ROLE_DIRECTOR) is True  # the account grant still follows


@pytest.mark.django_db
def test_an_unknown_esi_answer_leaves_the_seat_alone(django_user_model, monkeypatch):
    """ESI down, or the role scope not granted. The account grant is deliberately NOT flapped
    off in that case (anti-flap), and the seat must not be either — otherwise a brief ESI outage
    would silently strip every director's authority."""
    user = _user(django_user_model, "eve:d2", rbac.ROLE_MEMBER, rbac.ROLE_DIRECTOR)
    director = _pilot(user, 1, is_main=True, is_corp_director=True)
    token = AuthToken(character=director, scopes=[services.ROLE_SCOPE])
    token.refresh_token = "r"
    token.save()

    monkeypatch.setattr(services, "character_is_corp_director", lambda c: None)
    services._reconcile_roles_for_user(user, check_director=True)

    director.refresh_from_db()
    assert director.is_corp_director is True
    assert rbac.has_role(user, rbac.ROLE_DIRECTOR) is True


@pytest.mark.django_db
def test_leaving_the_corp_withdraws_the_seat(django_user_model):
    user = _user(django_user_model, "eve:left", rbac.ROLE_MEMBER, rbac.ROLE_DIRECTOR)
    pilot = _pilot(user, 1, is_main=True, is_corp_member=False, is_corp_director=True)

    services._reconcile_roles_for_user(user, check_director=False)

    pilot.refresh_from_db()
    assert pilot.is_corp_director is False
    assert rbac.has_role(user, rbac.ROLE_DIRECTOR) is False


@pytest.mark.django_db
def test_detaching_a_pilot_withdraws_its_seat(django_user_model):
    """An officer detaches a character (a sale, a recovery). The seat is evidence about a link
    that no longer exists — left set, it would be sitting on the row waiting for whoever
    re-claims the character."""
    officer = _user(django_user_model, "eve:off", rbac.ROLE_OFFICER)
    owner = _user(django_user_model, "eve:owner", rbac.ROLE_MEMBER)
    pilot = _pilot(owner, 1, is_main=True, is_corp_director=True)

    services.detach_character(pilot, actor=officer, reason="sold")

    pilot.refresh_from_db()
    assert pilot.user_id is None
    assert pilot.is_corp_director is False


@pytest.mark.django_db
def test_unlinking_a_pilot_withdraws_its_seat(django_user_model):
    from apps.sso import linking

    user = _user(django_user_model, "eve:u", rbac.ROLE_MEMBER)
    _pilot(user, 1, is_main=True)
    alt = _pilot(user, 2, is_corp_director=True)

    linking.unlink(user, alt)

    alt.refresh_from_db()
    assert alt.user_id is None
    assert alt.is_corp_director is False


@pytest.mark.django_db
def test_the_backfill_gives_todays_directors_their_seat(django_user_model):
    """The upgrade path (migration sso.0006).

    ``is_corp_director`` is new and defaults to False, but the ceiling reads it — and the only
    thing that sets it is a SIX-HOURLY ESI reconcile. Without the backfill, every director in the
    corporation loses Director access at deploy and gets it back some time in the next six hours.
    That is an outage, not a rollout.

    This asserts the backfill's *logic* against live data (running the historical migration in a
    test would need a migrator harness the suite does not have): every corp-member pilot of every
    non-expired Director grant ends up with the seat, which reproduces today's account-wide
    semantics exactly — nobody gains anything, nobody loses anything, on the day of the deploy.
    """
    from django.db.models import Q
    from django.utils import timezone

    director = _user(django_user_model, "eve:dir", rbac.ROLE_MEMBER, rbac.ROLE_DIRECTOR)
    main = _pilot(director, 1, is_main=True)
    corp_alt = _pilot(director, 2)
    outside_alt = _pilot(director, 3, is_corp_member=False)

    member = _user(django_user_model, "eve:mem", rbac.ROLE_MEMBER)
    members_pilot = _pilot(member, 4, is_main=True)

    # --- the migration's body, verbatim ---
    now = timezone.now()
    director_user_ids = (
        RoleAssignment.objects.filter(role__key="director")
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        .values_list("user_id", flat=True)
    )
    EveCharacter.objects.filter(
        user_id__in=list(director_user_ids), is_corp_member=True, is_corp_director=False
    ).update(is_corp_director=True)

    for pilot in (main, corp_alt, outside_alt, members_pilot):
        pilot.refresh_from_db()

    assert main.is_corp_director is True
    assert corp_alt.is_corp_director is True       # today's semantics: the WHOLE account is a director
    assert outside_alt.is_corp_director is False   # …but never a pilot outside the corp
    assert members_pilot.is_corp_director is False  # …and never a non-director's pilot

    # The consequence that matters: the director keeps their access on the day of the deploy.
    from core import pilots

    pilots.attach(director, main)
    assert rbac.has_role(director, rbac.ROLE_DIRECTOR) is True
