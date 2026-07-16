"""REWARD-01 regression: combat-rank reward lifecycle transitions must be row-locked.

Each of approve/mark_paid/reject/cancel now re-reads the event under ``select_for_update``
inside a transaction and gates on the LOCKED status, so a concurrent transition landing
between the caller loading the row and acting on it can't slip past a stale in-memory guard
(which previously let two officer POSTs both write a terminal status + duplicate audit rows).
This mirrors the sibling SRP / mentorship reward flows.
"""
from __future__ import annotations

import pytest

from apps.identity.models import RoleAssignment
from apps.killboard import rewards
from apps.killboard.models import RankRewardEvent
from apps.sso.services import ensure_role
from core import rbac


def _user(django_user_model, name, *roles):
    user = django_user_model.objects.create(username=name)
    for r in roles:
        RoleAssignment.objects.create(user=user, role=ensure_role(r))
    return user


def _event(user=None, status=RankRewardEvent.Status.PENDING):
    return RankRewardEvent.objects.create(
        character_id=42, character_name="Winner", user=user, rank_name="Ensign",
        status=status,
    )


@pytest.mark.django_db
def test_transition_rechecks_locked_status_not_stale_instance(django_user_model):
    """The core fix: gate on a freshly-locked re-read, not the passed-in instance. Simulates
    a concurrent approval landing after the caller loaded the row — the stale copy still reads
    PENDING, but approve() must refuse the double transition."""
    officer = _user(django_user_model, "rw_fc", rbac.ROLE_OFFICER)
    winner = _user(django_user_model, "rw_win", rbac.ROLE_MEMBER)
    ev = _event(user=winner)
    stale = RankRewardEvent.objects.get(pk=ev.pk)  # the caller's in-memory instance

    # A concurrent request approves it first (direct DB write; the stale copy is unaware).
    RankRewardEvent.objects.filter(pk=ev.pk).update(status=RankRewardEvent.Status.APPROVED)
    assert stale.status == RankRewardEvent.Status.PENDING  # still stale in memory

    with pytest.raises(rewards.InvalidTransition):
        rewards.approve(stale, officer)

    ev.refresh_from_db()
    assert ev.status == RankRewardEvent.Status.APPROVED  # not double-transitioned


@pytest.mark.django_db
def test_happy_path_and_double_call_guard(django_user_model):
    """Normal transitions still work, and a repeat call raises InvalidTransition."""
    officer = _user(django_user_model, "rw_fc2", rbac.ROLE_OFFICER)
    payer = _user(django_user_model, "rw_dir", rbac.ROLE_DIRECTOR)
    winner = _user(django_user_model, "rw_win2", rbac.ROLE_MEMBER)
    ev = _event(user=winner)

    rewards.approve(ev, officer)
    assert ev.status == RankRewardEvent.Status.APPROVED  # caller instance synced
    ev.refresh_from_db()
    assert ev.status == RankRewardEvent.Status.APPROVED and ev.approved_by_id == officer.id

    # Second approve is refused.
    with pytest.raises(rewards.InvalidTransition):
        rewards.approve(ev, officer)

    rewards.mark_paid(ev, payer, reference="wallet: 1.2b")
    ev.refresh_from_db()
    assert ev.status == RankRewardEvent.Status.PAID and ev.payment_reference == "wallet: 1.2b"

    # A paid reward can't be paid again, cancelled, or rejected.
    with pytest.raises(rewards.InvalidTransition):
        rewards.mark_paid(ev, payer)
    with pytest.raises(rewards.InvalidTransition):
        rewards.cancel(ev, officer)
    with pytest.raises(rewards.InvalidTransition):
        rewards.reject(ev, officer)


@pytest.mark.django_db
def test_self_action_denied_before_lock(django_user_model):
    """Separation of duties still holds: a pilot can't approve or pay their own reward."""
    from django.core.exceptions import PermissionDenied

    winner = _user(django_user_model, "rw_self", rbac.ROLE_DIRECTOR)  # director on their own reward
    ev = _event(user=winner)
    with pytest.raises(PermissionDenied):
        rewards.approve(ev, winner)
    with pytest.raises(PermissionDenied):
        rewards.mark_paid(ev, winner)
    ev.refresh_from_db()
    assert ev.status == RankRewardEvent.Status.PENDING  # untouched
