"""4.8 — Batched, staleness-filtered affiliation refresh.

Acceptance: the periodic affiliation sweep uses the batched ESI endpoint (one
POST per <=1000 characters, not one GET per character), skips characters refreshed
recently (staleness cutoff), reconciles auto-roles once per affected user (not once
per character), and leaves prior values intact when a batch fails — retiring the
per-character ESI fan-out that threatened the shared error budget at alt scale.
"""
from __future__ import annotations

import pytest
import responses
from django.utils import timezone

from apps.corporation.models import EveCorporation
from apps.sso import services
from apps.sso.models import EveCharacter
from apps.sso.services import ROLE_SCOPE
from tests._raffle_utils import HOME_CORP, add_token, make_user

pytestmark = pytest.mark.django_db
ESI = "https://esi.evetech.net"
OTHER_CORP = 98000999


def _corp(corp_id):
    return EveCorporation.objects.get_or_create(corporation_id=corp_id)[0]


def _char(user, cid, *, corp=HOME_CORP, is_member=True, updated=None, director_checked=None):
    return EveCharacter.objects.create(
        character_id=cid, user=user, name=f"P{cid}",
        is_corp_member=is_member, corporation=_corp(corp),
        affiliation_updated_at=updated, director_checked_at=director_checked,
    )


def _resp(rows):
    responses.add(responses.POST, f"{ESI}/characters/affiliation/", json=rows, status=200)


@responses.activate
def test_batch_updates_stale_characters(django_user_model, settings):
    settings.FORCA_HOME_CORP_ID = HOME_CORP
    u = make_user(django_user_model, "u1")
    a = _char(u, 5001, corp=OTHER_CORP, is_member=False)   # will be found in home corp
    b = _char(u, 5002, corp=HOME_CORP)                     # will be found to have left
    _resp([
        {"character_id": 5001, "corporation_id": HOME_CORP, "alliance_id": 99000001},
        {"character_id": 5002, "corporation_id": OTHER_CORP, "alliance_id": None},
    ])
    res = services.refresh_affiliations_batched()
    assert res["batches"] == 1 and res["updated"] == 2 and res["checked"] == 2
    a.refresh_from_db()
    b.refresh_from_db()
    assert a.is_corp_member is True and a.corporation_id == HOME_CORP and a.alliance_id == 99000001
    assert b.is_corp_member is False and b.corporation_id == OTHER_CORP
    assert a.affiliation_updated_at is not None


@responses.activate
def test_staleness_filter_skips_fresh_characters(django_user_model, settings):
    settings.FORCA_HOME_CORP_ID = HOME_CORP
    u = make_user(django_user_model, "u2")
    fresh = _char(u, 5101, corp=OTHER_CORP, is_member=False, updated=timezone.now())
    stale = _char(u, 5102, corp=OTHER_CORP, is_member=False, updated=None)
    # The response would move BOTH into the home corp — but the fresh one must be skipped.
    _resp([
        {"character_id": 5101, "corporation_id": HOME_CORP, "alliance_id": None},
        {"character_id": 5102, "corporation_id": HOME_CORP, "alliance_id": None},
    ])
    res = services.refresh_affiliations_batched(staleness_hours=4)
    assert res["checked"] == 1 and res["updated"] == 1
    fresh.refresh_from_db()
    stale.refresh_from_db()
    assert fresh.is_corp_member is False and fresh.corporation_id == OTHER_CORP  # untouched
    assert stale.is_corp_member is True and stale.corporation_id == HOME_CORP


@responses.activate
def test_role_sync_runs_once_per_user(django_user_model, settings, monkeypatch):
    settings.FORCA_HOME_CORP_ID = HOME_CORP
    u = make_user(django_user_model, "u3")
    _char(u, 5201, corp=OTHER_CORP, is_member=False)
    _char(u, 5202, corp=OTHER_CORP, is_member=False)  # same user, two alts
    _resp([
        {"character_id": 5201, "corporation_id": HOME_CORP, "alliance_id": None},
        {"character_id": 5202, "corporation_id": HOME_CORP, "alliance_id": None},
    ])
    calls = []
    monkeypatch.setattr(services, "sync_roles_for_user", lambda user, **kw: calls.append(user.pk))
    res = services.refresh_affiliations_batched()
    assert res["users_synced"] == 1
    assert calls == [u.pk]  # once, not twice


@responses.activate
def test_failed_batch_leaves_values_intact(django_user_model, settings):
    settings.FORCA_HOME_CORP_ID = HOME_CORP
    u = make_user(django_user_model, "u4")
    c = _char(u, 5301, corp=OTHER_CORP, is_member=False)
    responses.add(responses.POST, f"{ESI}/characters/affiliation/", status=500)
    res = services.refresh_affiliations_batched()
    assert res["batches"] == 0 and res["updated"] == 0
    c.refresh_from_db()
    assert c.is_corp_member is False and c.corporation_id == OTHER_CORP  # unchanged


@responses.activate
def test_limit_caps_characters_checked(django_user_model, settings):
    settings.FORCA_HOME_CORP_ID = HOME_CORP
    u = make_user(django_user_model, "u5")
    for cid in (5401, 5402, 5403):
        _char(u, cid, corp=OTHER_CORP, is_member=False)
    _resp([{"character_id": cid, "corporation_id": HOME_CORP, "alliance_id": None}
           for cid in (5401, 5402, 5403)])
    res = services.refresh_affiliations_batched(limit=1)
    assert res["checked"] == 1


@responses.activate
def test_empty_when_nothing_stale(django_user_model, settings):
    settings.FORCA_HOME_CORP_ID = HOME_CORP
    u = make_user(django_user_model, "u6")
    _char(u, 5501, updated=timezone.now())  # fresh
    res = services.refresh_affiliations_batched(staleness_hours=4)
    assert res == {"checked": 0, "batches": 0, "updated": 0, "users_synced": 0}


@responses.activate
def test_single_char_refresh_still_works(django_user_model, settings):
    # The login path still uses the single-character GET — must stay intact.
    settings.FORCA_HOME_CORP_ID = HOME_CORP
    u = make_user(django_user_model, "u7")
    c = _char(u, 5601, corp=OTHER_CORP, is_member=False)
    responses.add(responses.GET, f"{ESI}/characters/5601/",
                  json={"corporation_id": HOME_CORP, "name": "P"}, status=200)
    services.refresh_affiliation(c)
    c.refresh_from_db()
    assert c.is_corp_member is True and c.corporation_id == HOME_CORP


@responses.activate
def test_affiliation_sweep_does_no_director_esi(django_user_model, settings):
    # A member with the (default) roles scope would trigger a Director /roles/ + co-check
    # GET if the sweep ran check_director=True. Only the affiliation POST is mocked, so any
    # such GET would raise ConnectionError and fail the test — proving the sweep is ESI-free
    # for the Director check (review MED-1: decoupled into reconcile_director_roles).
    settings.FORCA_HOME_CORP_ID = HOME_CORP
    u = make_user(django_user_model, "u8")
    c = _char(u, 5701, corp=OTHER_CORP, is_member=False)
    add_token(c, scopes=[ROLE_SCOPE])
    _resp([{"character_id": 5701, "corporation_id": HOME_CORP, "alliance_id": None}])
    res = services.refresh_affiliations_batched()
    assert res["updated"] == 1
    c.refresh_from_db()
    assert c.is_corp_member is True  # member role reconciled DB-only


def test_director_reconcile_selects_stale_members_and_stamps(
    django_user_model, settings, monkeypatch
):
    settings.FORCA_HOME_CORP_ID = HOME_CORP
    u = make_user(django_user_model, "dr1")
    a = _char(u, 5801, corp=HOME_CORP, is_member=True)   # stale (never checked)
    b = _char(u, 5802, corp=HOME_CORP, is_member=True)   # same user, also stale
    nonmember = _char(u, 5803, corp=OTHER_CORP, is_member=False)  # must be ignored
    calls = []
    monkeypatch.setattr(services, "sync_roles_for_user",
                        lambda user, **kw: calls.append((user.pk, kw.get("check_director"))))
    res = services.reconcile_director_roles()
    assert res["checked"] == 2 and res["users_synced"] == 1
    assert calls == [(u.pk, True)]  # full director check, once for the user
    a.refresh_from_db()
    b.refresh_from_db()
    nonmember.refresh_from_db()
    assert a.director_checked_at is not None and b.director_checked_at is not None
    assert nonmember.director_checked_at is None


def test_director_reconcile_staleness_and_cap(django_user_model, settings, monkeypatch):
    settings.FORCA_HOME_CORP_ID = HOME_CORP
    u = make_user(django_user_model, "dr2")
    fresh = _char(u, 5901, corp=HOME_CORP, is_member=True, director_checked=timezone.now())
    _char(u, 5902, corp=HOME_CORP, is_member=True)   # stale
    _char(u, 5903, corp=HOME_CORP, is_member=True)   # stale
    monkeypatch.setattr(services, "sync_roles_for_user", lambda user, **kw: None)
    res = services.reconcile_director_roles(staleness_hours=5, limit=1)
    assert res["checked"] == 1  # capped to one of the two stale members
    fresh.refresh_from_db()
    assert fresh.director_checked_at is not None  # fresh one never re-selected
