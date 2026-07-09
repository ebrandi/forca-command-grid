"""Manual leadership grants: the enrolment-gated happy path and the audited,
Director-only, config-guarded emergency override to a not-yet-enrolled pilot.
"""
from __future__ import annotations

import pytest

from apps.raffle import services
from apps.raffle.models import RaffleManualGrant, RaffleTicketLedgerEntry
from core import rbac
from tests._raffle_utils import enrol_pilot, make_contest, make_user


def _enable_override():
    cfg = services.active_config()
    cfg.allow_manual_override = True
    cfg.save(update_fields=["allow_manual_override", "updated_at"])
    return cfg


@pytest.mark.django_db
def test_grant_to_eligible_pilot_writes_ledger(django_user_model):
    contest = make_contest()
    user, _ = enrol_pilot(django_user_model, 3001)
    officer = make_user(django_user_model, "officer", rbac.ROLE_OFFICER)

    grant = services.grant_manual_tickets(
        contest, officer, character_id=3001, amount=5, reason="Great tackling",
        category="pvp",
    )
    assert isinstance(grant, RaffleManualGrant)
    assert grant.amount == 5
    assert grant.override_used is False
    assert grant.ledger_entry is not None

    entry = grant.ledger_entry
    assert entry.source_key == "manual"
    assert entry.source_ref == f"manual:{grant.pk}"
    assert entry.amount == 5
    assert entry.user_id == user.id
    assert entry.status == RaffleTicketLedgerEntry.Status.APPROVED


@pytest.mark.django_db
def test_grant_to_ineligible_pilot_is_blocked_by_default(django_user_model):
    contest = make_contest()
    officer = make_user(django_user_model, "officer", rbac.ROLE_OFFICER)
    # 3002 has no FORCA account.
    with pytest.raises(services.GrantBlocked):
        services.grant_manual_tickets(
            contest, officer, character_id=3002, amount=3, reason="nope",
        )
    assert RaffleManualGrant.objects.filter(contest=contest).count() == 0
    assert RaffleTicketLedgerEntry.objects.filter(contest=contest).count() == 0


@pytest.mark.django_db
def test_override_requires_config_flag(django_user_model):
    """override=True by a Director is still refused unless the config enables it."""
    contest = make_contest()
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    with pytest.raises(services.GrantBlocked):
        services.grant_manual_tickets(
            contest, director, character_id=3005, amount=2, reason="early", override=True,
        )


@pytest.mark.django_db
def test_override_requires_director(django_user_model):
    """Even with the flag on and override=True, a non-Director cannot override."""
    contest = make_contest()
    _enable_override()
    officer = make_user(django_user_model, "officer", rbac.ROLE_OFFICER)
    with pytest.raises(services.GrantBlocked):
        services.grant_manual_tickets(
            contest, officer, character_id=3006, amount=2, reason="early", override=True,
        )


@pytest.mark.django_db
def test_director_override_grant_succeeds_and_marks_override(django_user_model):
    contest = make_contest()
    _enable_override()
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)

    grant = services.grant_manual_tickets(
        contest, director, character_id=3007, amount=4, reason="pre-enrolment recognition",
        override=True,
    )
    assert grant.override_used is True
    assert grant.character_id == 3007
    assert grant.user_id is None  # not enrolled → no account owns the tickets
    entry = grant.ledger_entry
    assert entry.amount == 4
    assert entry.metadata["override"] is True


@pytest.mark.django_db
def test_grant_rejects_bad_input(django_user_model):
    contest = make_contest()
    user, _ = enrol_pilot(django_user_model, 3008)
    officer = make_user(django_user_model, "officer", rbac.ROLE_OFFICER)
    with pytest.raises(services.GrantBlocked):
        services.grant_manual_tickets(contest, officer, character_id=3008, amount=0, reason="x")
    with pytest.raises(services.GrantBlocked):
        services.grant_manual_tickets(contest, officer, character_id=3008, amount=5, reason="  ")
