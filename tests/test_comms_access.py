"""Tests for the external comms access-sync core (Phase 5.0).

Covers the entitlement resolver, the reconcile engine's safety rails (managed-set boundary,
additive-default vs authoritative, dry-run, pin, inert-when-unarmed), ledger idempotency,
and the guarded targeted-reconcile hook.
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.comms_access import config
from apps.comms_access.entitlements import entitlements
from apps.comms_access.models import (
    AccessSyncLedger,
    CommsAccount,
    EntitlementMapping,
    MappingMode,
    SyncResult,
)
from apps.comms_access.providers.base import AccessProvider, ApplyResult
from apps.comms_access.reconcile import reconcile_account
from apps.corporation.models import EveCorporation, FriendlyCorporation
from apps.identity.models import Permission, RoleAssignment
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac

FRIENDLY_CORP = 98007777


# --- helpers -----------------------------------------------------------------
def _user_with_role(django_user_model, uid, role_key):
    u = django_user_model.objects.create(username=f"ca-{uid}")
    RoleAssignment.objects.create(user=u, role=ensure_role(role_key))
    return u


def _friendly_pilot(django_user_model, uid):
    u = django_user_model.objects.create(username=f"ca-{uid}")
    corp, _ = EveCorporation.objects.get_or_create(corporation_id=FRIENDLY_CORP)
    FriendlyCorporation.objects.get_or_create(corporation_id=FRIENDLY_CORP, defaults={"active": True})
    EveCharacter.objects.create(
        character_id=90000000 + uid, user=u, name="P", is_main=True,
        is_corp_member=False, corporation=corp,
    )
    return u


def _account(user, *, platform="discord", verified=True, pinned=False):
    return CommsAccount.objects.create(
        user=user, platform=platform, verified=verified, pinned=pinned,
        external_id="123", external_handle="pilot#0001",
    )


def _mapping(entitlement, ref, *, mode=MappingMode.ADDITIVE, dry_run=False, platform="discord"):
    return EntitlementMapping.objects.create(
        platform=platform, entitlement_key=entitlement, target_ref=ref,
        mode=mode, dry_run=dry_run, enabled=True,
    )


def _go_live():
    """Enable the feature and turn off the global dry-run so mappings can apply."""
    config.set("general", {"enabled": True, "global_dry_run": False, "revoke_grace_minutes": 0})


class FakeProvider(AccessProvider):
    platform = "discord"

    def __init__(self, current=None, *, fail=False):
        super().__init__({})
        self._current = set(current or [])
        self.fail = fail
        self.apply_calls = []

    def validate_configuration(self):
        return True, ""

    def read_current(self, account):
        return set(self._current)

    def apply(self, account, *, add, remove):
        self.apply_calls.append((set(add), set(remove)))
        if self.fail:
            return ApplyResult(ok=False, error="boom")
        self._current = (self._current | set(add)) - set(remove)
        return ApplyResult(ok=True, applied_add=set(add), applied_remove=set(remove))


# --- entitlement resolver ----------------------------------------------------
@pytest.mark.django_db
def test_resolver_member(django_user_model):
    u = _user_with_role(django_user_model, 1, rbac.ROLE_MEMBER)
    assert entitlements(u) == {"member"}


@pytest.mark.django_db
def test_resolver_director_gets_baseline_capabilities(django_user_model):
    # A director outranks the officer baseline for recruitment.manage / fleet.manage,
    # so the resolver credits recruiter + fc without an explicit lateral grant.
    u = _user_with_role(django_user_model, 2, rbac.ROLE_DIRECTOR)
    assert entitlements(u) == {"member", "officer", "director", "recruiter", "fc"}


@pytest.mark.django_db
def test_resolver_lateral_recruiter_is_least_privilege(django_user_model):
    # A pure recruiter (rank 0) gets ONLY the recruiter entitlement — not member.
    perm, _ = Permission.objects.get_or_create(key=rbac.PERM_RECRUITMENT_MANAGE)
    role = ensure_role(rbac.ROLE_RECRUITER)
    role.permissions.add(perm)
    u = django_user_model.objects.create(username="ca-recruiter")
    RoleAssignment.objects.create(user=u, role=role)
    assert entitlements(u) == {"recruiter"}


@pytest.mark.django_db
def test_resolver_alliance_friendly_pilot(django_user_model):
    u = _friendly_pilot(django_user_model, 3)
    assert entitlements(u) == {"alliance"}


@pytest.mark.django_db
def test_resolver_anonymous():
    from django.contrib.auth.models import AnonymousUser

    assert entitlements(AnonymousUser()) == set()


# --- reconcile: additive grant ----------------------------------------------
@pytest.mark.django_db
def test_additive_grants_desired_role(django_user_model):
    _go_live()
    u = _user_with_role(django_user_model, 10, rbac.ROLE_MEMBER)
    acct = _account(u)
    _mapping("member", "R_MEM")
    prov = FakeProvider()

    res = reconcile_account(acct, provider=prov, source_ref="t1")

    assert res.added == {"R_MEM"}
    assert res.removed == set()
    assert prov.apply_calls == [({"R_MEM"}, set())]
    assert AccessSyncLedger.objects.filter(result=SyncResult.APPLIED).count() == 1


@pytest.mark.django_db
def test_additive_never_removes_on_loss(django_user_model):
    _go_live()
    u = _friendly_pilot(django_user_model, 11)  # not a member
    acct = _account(u)
    _mapping("member", "R_MEM", mode=MappingMode.ADDITIVE)
    prov = FakeProvider(current={"R_MEM"})

    res = reconcile_account(acct, provider=prov, source_ref="t2")

    assert res.removed == set()
    assert res.preview_remove == set()
    assert prov.apply_calls == []  # additive mapping never revokes


# --- reconcile: authoritative removal + managed-set boundary -----------------
@pytest.mark.django_db
def test_authoritative_removes_on_loss(django_user_model):
    _go_live()
    u = _friendly_pilot(django_user_model, 12)  # not a member
    acct = _account(u)
    _mapping("member", "R_MEM", mode=MappingMode.AUTHORITATIVE)
    prov = FakeProvider(current={"R_MEM"})

    res = reconcile_account(acct, provider=prov, source_ref="t3")

    assert res.removed == {"R_MEM"}
    assert "R_MEM" not in prov._current


@pytest.mark.django_db
def test_unmanaged_roles_are_never_touched(django_user_model):
    _go_live()
    u = _friendly_pilot(django_user_model, 13)  # not a member → would lose R_MEM
    acct = _account(u)
    _mapping("member", "R_MEM", mode=MappingMode.AUTHORITATIVE)
    prov = FakeProvider(current={"R_MEM", "R_UNMANAGED"})

    res = reconcile_account(acct, provider=prov, source_ref="t4")

    assert res.removed == {"R_MEM"}
    assert "R_UNMANAGED" in prov._current  # boundary: never removed a role we don't manage
    add, remove = prov.apply_calls[0]
    assert "R_UNMANAGED" not in remove


# --- reconcile: dry-run ------------------------------------------------------
@pytest.mark.django_db
def test_per_mapping_dry_run_previews_only(django_user_model):
    _go_live()
    u = _user_with_role(django_user_model, 14, rbac.ROLE_MEMBER)
    acct = _account(u)
    _mapping("member", "R_MEM", dry_run=True)  # this mapping stays in preview
    prov = FakeProvider()

    res = reconcile_account(acct, provider=prov, source_ref="t5")

    assert res.added == set()
    assert res.preview_add == {"R_MEM"}
    assert prov.apply_calls == []
    assert AccessSyncLedger.objects.filter(result=SyncResult.DRY_RUN).count() == 1


@pytest.mark.django_db
def test_global_dry_run_previews_only(django_user_model):
    config.set("general", {"enabled": True, "global_dry_run": True, "revoke_grace_minutes": 0})
    u = _user_with_role(django_user_model, 15, rbac.ROLE_MEMBER)
    acct = _account(u)
    _mapping("member", "R_MEM", dry_run=False)  # mapping live, but global switch wins
    prov = FakeProvider()

    res = reconcile_account(acct, provider=prov, source_ref="t6")

    assert res.added == set()
    assert res.preview_add == {"R_MEM"}
    assert prov.apply_calls == []


# --- reconcile: skips --------------------------------------------------------
@pytest.mark.django_db
def test_pinned_account_is_skipped(django_user_model):
    _go_live()
    u = _user_with_role(django_user_model, 16, rbac.ROLE_MEMBER)
    acct = _account(u, pinned=True)
    _mapping("member", "R_MEM")
    prov = FakeProvider()

    res = reconcile_account(acct, provider=prov)

    assert res.skipped and "pinned" in res.reason
    assert prov.apply_calls == []


@pytest.mark.django_db
def test_unverified_account_is_skipped(django_user_model):
    _go_live()
    u = _user_with_role(django_user_model, 17, rbac.ROLE_MEMBER)
    acct = _account(u, verified=False)
    _mapping("member", "R_MEM")
    res = reconcile_account(acct, provider=FakeProvider())
    assert res.skipped and "verified" in res.reason


@pytest.mark.django_db
def test_inert_when_platform_not_armed(django_user_model):
    # No explicit provider + platform unarmed ⇒ skipped (empty registry / not armed).
    _go_live()
    u = _user_with_role(django_user_model, 18, rbac.ROLE_MEMBER)
    acct = _account(u)
    _mapping("member", "R_MEM")
    res = reconcile_account(acct)  # no provider passed, discord not armed
    assert res.skipped


@pytest.mark.django_db
def test_no_mappings_is_skipped(django_user_model):
    _go_live()
    u = _user_with_role(django_user_model, 19, rbac.ROLE_MEMBER)
    acct = _account(u)
    res = reconcile_account(acct, provider=FakeProvider())
    assert res.skipped and "mapping" in res.reason


# --- ledger idempotency ------------------------------------------------------
@pytest.mark.django_db
def test_ledger_is_idempotent_within_a_run(django_user_model):
    _go_live()
    u = _user_with_role(django_user_model, 20, rbac.ROLE_MEMBER)
    acct = _account(u)
    _mapping("member", "R_MEM")

    reconcile_account(acct, provider=FakeProvider(), source_ref="same-run")
    reconcile_account(acct, provider=FakeProvider(), source_ref="same-run")

    # Same (account, ref, action, source_ref) ⇒ one row, not two.
    assert AccessSyncLedger.objects.filter(target_ref="R_MEM").count() == 1


@pytest.mark.django_db
def test_failed_apply_records_failure(django_user_model):
    _go_live()
    u = _user_with_role(django_user_model, 21, rbac.ROLE_MEMBER)
    acct = _account(u)
    _mapping("member", "R_MEM")
    prov = FakeProvider(fail=True)

    res = reconcile_account(acct, provider=prov, source_ref="t-fail")

    assert res.added == set()
    assert "R_MEM" in res.failed
    assert AccessSyncLedger.objects.filter(result=SyncResult.FAILED).count() == 1


# --- hook --------------------------------------------------------------------
@pytest.mark.django_db
def test_hook_is_noop_when_feature_disabled(django_user_model, settings, monkeypatch):
    settings.COMMS_ACCESS_ENABLED = False
    from apps.comms_access import hooks

    called = []
    monkeypatch.setattr(
        "apps.comms_access.tasks.reconcile_user_task.delay",
        lambda *a, **k: called.append((a, k)),
    )
    u = _user_with_role(django_user_model, 22, rbac.ROLE_MEMBER)
    hooks.enqueue_user_reconcile(u)
    assert called == []  # feature off ⇒ nothing enqueued


@pytest.mark.django_db
def test_hook_enqueues_when_enabled(django_user_model, settings, monkeypatch):
    settings.COMMS_ACCESS_ENABLED = True
    _go_live()  # the console switch now governs too — hook gates on config.feature_active()
    from apps.comms_access import hooks

    called = []
    monkeypatch.setattr(
        "apps.comms_access.tasks.reconcile_user_task.delay",
        lambda *a, **k: called.append((a, k)),
    )
    u = _user_with_role(django_user_model, 23, rbac.ROLE_MEMBER)
    hooks.enqueue_user_reconcile(u, source_ref="role-sync")
    assert len(called) == 1
    assert called[0][0][0] == u.pk


# --- admin console (Director-gated) ------------------------------------------
def _director(django_user_model, uid=9500):
    u = django_user_model.objects.create(username=f"cad-{uid}")
    RoleAssignment.objects.create(user=u, role=ensure_role(rbac.ROLE_DIRECTOR))
    # is_corp_director: since LP-4 the app's Director role is only exercisable from a pilot who
    # holds the in-game Director role, so a director fixture needs the seat that proves it.
    EveCharacter.objects.create(character_id=uid, user=u, name=f"Dir{uid}",
                                is_main=True, is_corp_member=True, is_corp_director=True)
    return u


@pytest.mark.django_db
def test_console_pages_render_for_director(client, django_user_model):
    client.force_login(_director(django_user_model))
    for name in ("comms_access_settings", "comms_access_mappings", "comms_access_status"):
        assert client.get(reverse(f"admin_audit:{name}")).status_code == 200


@pytest.mark.django_db
def test_console_denied_for_member(client, django_user_model):
    client.force_login(_user_with_role(django_user_model, 24, rbac.ROLE_MEMBER))
    assert client.get(reverse("admin_audit:comms_access_settings")).status_code == 403


@pytest.mark.django_db
def test_console_create_mapping(client, django_user_model):
    client.force_login(_director(django_user_model))
    resp = client.post(reverse("admin_audit:comms_access_mapping_create"), {
        "platform": "discord", "entitlement_key": "member", "target_ref": "123456",
        "target_label": "@Member", "mode": "additive", "dry_run": "on", "enabled": "on",
    })
    assert resp.status_code == 302
    m = EntitlementMapping.objects.get(platform="discord", entitlement_key="member")
    assert m.target_ref == "123456" and m.dry_run is True


@pytest.mark.django_db
def test_reconcile_account_uses_prefetched_mappings(django_user_model):
    """The sweep hoists mappings once and passes them in; reconcile_account must honour
    the pre-fetched list (and not depend on a per-account DB query)."""
    _go_live()
    u = _user_with_role(django_user_model, 30, rbac.ROLE_MEMBER)
    acct = _account(u)
    m = _mapping("member", "R_MEM")
    # Delete the row from the DB — if reconcile still grants, it used the passed list.
    EntitlementMapping.objects.filter(pk=m.pk).delete()
    prov = FakeProvider()

    res = reconcile_account(acct, provider=prov, mappings=[m], source_ref="pf1")

    assert res.added == {"R_MEM"}


@pytest.mark.django_db
def test_reconcile_all_sweep_lock_prevents_overlap(django_user_model, settings):
    """A second concurrent full sweep short-circuits while the first holds the lock."""
    from django.core.cache import cache

    from apps.comms_access.tasks import _SWEEP_LOCK_KEY, _SWEEP_LOCK_TTL, reconcile_all

    settings.COMMS_ACCESS_ENABLED = True
    _go_live()
    cache.add(_SWEEP_LOCK_KEY, "1", _SWEEP_LOCK_TTL)  # pretend a sweep is in flight
    try:
        assert reconcile_all() == {"skipped": "already running"}
    finally:
        cache.delete(_SWEEP_LOCK_KEY)


@pytest.mark.django_db
def test_console_arm_platform(client, django_user_model):
    client.force_login(_director(django_user_model))
    client.post(reverse("admin_audit:comms_access_settings"), {
        "domain": "platforms", "discord_armed": "on", "discord_id": "42",
    })
    assert config.platform_armed("discord") is True
    assert config.get("platforms")["discord"]["guild_id"] == "42"
