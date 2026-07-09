"""4.6 — SRP auto-draft from attendance + losses.

Acceptance: when leadership arms auto-draft, an eligible loss becomes a SUBMITTED claim
(never auto-paid) filed on the pilot's behalf; future-only (only losses after the arm
time); only for enrolled pilots with a valid ESI token; already-claimed losses skipped;
inert when disabled.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.utils import timezone

from apps.killboard.models import Killmail
from apps.srp import services
from apps.srp.auto_draft import auto_draft_claims
from apps.srp.models import SrpClaim
from apps.sso.models import EveCharacter
from tests._raffle_utils import add_token

pytestmark = pytest.mark.django_db
RIFTER = 587


def _program(**kw):
    p = services.active_program()
    p.enabled = True
    p.require_doctrine = False   # keep eligibility minimal so the test targets auto-draft, not gates
    p.require_fleet_op = False
    for k, v in kw.items():
        setattr(p, k, v)
    p.save()
    return p


def _member(django_user_model, cid, *, with_token=True):
    u = django_user_model.objects.create(username=f"eve:{cid}")
    ch = EveCharacter.objects.create(character_id=cid, user=u, name=f"P{cid}",
                                     is_main=True, is_corp_member=True)
    if with_token:
        add_token(ch)
    return u, ch


def _loss(cid, km_id, when=None):
    return Killmail.objects.create(
        killmail_id=km_id, killmail_time=when or timezone.now(), solar_system_id=30000142,
        victim_character_id=cid, victim_ship_type_id=RIFTER,
        involves_home_corp=True, home_corp_role=Killmail.HomeRole.VICTIM,
    )


def test_auto_draft_creates_submitted_claim(django_user_model):
    _program(auto_draft_enabled=True, auto_draft_since=timezone.now() - dt.timedelta(hours=1))
    u, _ch = _member(django_user_model, 5001)
    _loss(5001, 1)
    assert auto_draft_claims()["drafted"] == 1
    claim = SrpClaim.objects.get()
    assert claim.status == SrpClaim.Status.SUBMITTED      # a DRAFT — never auto-paid/approved
    assert claim.auto_drafted is True and claim.claimant_id == u.id


def test_disabled_is_noop(django_user_model):
    _program(auto_draft_enabled=False)
    _member(django_user_model, 5002)
    _loss(5002, 2)
    assert auto_draft_claims()["status"] == "disabled"
    assert not SrpClaim.objects.exists()


def test_future_only_baseline(django_user_model):
    _program(auto_draft_enabled=True, auto_draft_since=timezone.now() - dt.timedelta(hours=1))
    _member(django_user_model, 5003)
    _loss(5003, 3, when=timezone.now() - dt.timedelta(days=2))  # before the baseline
    assert auto_draft_claims()["drafted"] == 0 and not SrpClaim.objects.exists()


def test_requires_valid_token(django_user_model):
    _program(auto_draft_enabled=True, auto_draft_since=timezone.now() - dt.timedelta(hours=1))
    _member(django_user_model, 5004, with_token=False)  # enrolled but no valid ESI token
    _loss(5004, 4)
    assert auto_draft_claims()["drafted"] == 0 and not SrpClaim.objects.exists()


def test_requires_enrolled(django_user_model):
    _program(auto_draft_enabled=True, auto_draft_since=timezone.now() - dt.timedelta(hours=1))
    _loss(5005, 5)  # a loss by a character with no linked FORCA account
    assert auto_draft_claims()["drafted"] == 0 and not SrpClaim.objects.exists()


def test_skips_already_claimed(django_user_model):
    _program(auto_draft_enabled=True, auto_draft_since=timezone.now() - dt.timedelta(hours=1))
    u, _ch = _member(django_user_model, 5006)
    km = _loss(5006, 6)
    SrpClaim.objects.create(killmail=km, claimant=u, status=SrpClaim.Status.SUBMITTED)
    assert auto_draft_claims()["drafted"] == 0
    assert SrpClaim.objects.count() == 1  # no duplicate


def test_no_starvation_older_claimed_losses(django_user_model):
    # Review MED: newer UNCLAIMED losses must still be drafted even when older CLAIMED losses
    # would fill the [:limit] slice. The unclaimed filter runs in SQL before the limit.
    _program(auto_draft_enabled=True, auto_draft_since=timezone.now() - dt.timedelta(days=10))
    u, _ch = _member(django_user_model, 5007)
    for i, kmid in enumerate((10, 11)):  # two older, already-claimed losses
        km = _loss(5007, kmid, when=timezone.now() - dt.timedelta(days=5, hours=i))
        SrpClaim.objects.create(killmail=km, claimant=u, status=SrpClaim.Status.SUBMITTED)
    _loss(5007, 12, when=timezone.now() - dt.timedelta(hours=1))  # a newer unclaimed loss
    assert auto_draft_claims(limit=2)["drafted"] == 1  # pre-fix this starved to 0
    assert SrpClaim.objects.filter(killmail_id=12).exists()


def test_arming_stamps_future_only_baseline(client, django_user_model):
    from django.urls import reverse

    from apps.identity.models import RoleAssignment
    from apps.srp.forms import SrpProgramForm
    from apps.sso.services import ensure_role
    from core import rbac
    program = _program(auto_draft_enabled=False)
    assert program.auto_draft_since is None
    officer = django_user_model.objects.create(username="off")
    RoleAssignment.objects.create(user=officer, role=ensure_role(rbac.ROLE_OFFICER))
    client.force_login(officer)
    data = {}
    for k, v in SrpProgramForm(instance=program).initial.items():
        if isinstance(v, bool):
            if v:
                data[k] = "on"
        elif v is not None:
            data[k] = str(v)
    data["auto_draft_enabled"] = "on"  # arm it
    resp = client.post(reverse("srp:settings"), data)
    assert resp.status_code in (302, 200)
    program.refresh_from_db()
    assert program.auto_draft_enabled is True and program.auto_draft_since is not None
