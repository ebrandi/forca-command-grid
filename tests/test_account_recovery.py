"""0.8 / SSO-1: officer character-detach / owner-hash reset console."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.admin_audit.models import AuditLog
from apps.identity.models import RoleAssignment
from apps.sso.models import AuthToken, EveCharacter
from apps.sso.services import detach_character, ensure_role
from core import rbac


def _user(dum, username, role, cid=None, owner_hash="oh", with_token=True):
    user = dum.objects.create(username=username)
    RoleAssignment.objects.create(user=user, role=ensure_role(role))
    ch = None
    if cid is not None:
        ch = EveCharacter.objects.create(
            character_id=cid, user=user, name=username, owner_hash=owner_hash,
            is_main=True, is_corp_member=True,
            # The seat that substantiates the account role (LP-4).
            is_corp_director=role == rbac.ROLE_DIRECTOR,
        )
        user.main_character_id = cid
        user.save(update_fields=["main_character_id"])
        if with_token:
            t = AuthToken(character=ch, scopes=["publicData"],
                          access_expires_at=timezone.now() + timedelta(hours=1))
            t.refresh_token = "r"
            t.access_token = "a"
            t.save()
    return user, ch


@pytest.mark.django_db
def test_detach_clears_hash_unlinks_revokes_and_audits(django_user_model):
    officer, _ = _user(django_user_model, "officer", rbac.ROLE_OFFICER, cid=90000010)
    owner, ch = _user(django_user_model, "seller", rbac.ROLE_MEMBER, cid=90000011, owner_hash="OLDHASH")

    result = detach_character(ch, actor=officer, reason="character sold")
    ch.refresh_from_db()
    owner.refresh_from_db()

    assert ch.user_id is None            # unlinked → fresh claim
    assert ch.owner_hash == ""           # next login records a fresh hash
    assert ch.is_main is False
    assert owner.main_character_id is None
    assert result["tokens_revoked"] == 1
    assert AuthToken.objects.filter(character=ch, revoked_at__isnull=False).count() == 1
    assert AuditLog.objects.filter(
        action="sso.character_detached", target_id="90000011"
    ).exists()


@pytest.mark.django_db
def test_recovery_page_officer_only(client, django_user_model):
    member, _ = _user(django_user_model, "m", rbac.ROLE_MEMBER)
    client.force_login(member)
    assert client.get("/ops/admin/access/recovery/").status_code == 403

    officer, _ = _user(django_user_model, "o", rbac.ROLE_OFFICER)
    client.force_login(officer)
    assert client.get("/ops/admin/access/recovery/").status_code == 200


@pytest.mark.django_db
def test_console_detach_flow_and_reason_required(client, django_user_model):
    officer, _ = _user(django_user_model, "off", rbac.ROLE_OFFICER, cid=90000020)
    _owner, ch = _user(django_user_model, "locked", rbac.ROLE_MEMBER, cid=90000021)
    client.force_login(officer)

    # A reason is mandatory (it is audited): a blank reason detaches nothing.
    client.post("/ops/admin/access/recovery/detach/", {"character_id": 90000021, "reason": ""})
    ch.refresh_from_db()
    assert ch.user_id is not None

    # With a reason it detaches.
    client.post("/ops/admin/access/recovery/detach/",
                {"character_id": 90000021, "reason": "legacy row, verified owner"})
    ch.refresh_from_db()
    assert ch.user_id is None and ch.owner_hash == ""


@pytest.mark.django_db
def test_officer_cannot_detach_a_director_linked_character(client, django_user_model):
    officer, _ = _user(django_user_model, "off2", rbac.ROLE_OFFICER, cid=90000030)
    _director, ch = _user(django_user_model, "boss", rbac.ROLE_DIRECTOR, cid=90000031)
    client.force_login(officer)

    client.post("/ops/admin/access/recovery/detach/",
                {"character_id": 90000031, "reason": "nope"})
    ch.refresh_from_db()
    assert ch.user_id is not None  # escalation guard blocked it

    # A Director can.
    boss_admin, _ = _user(django_user_model, "boss2", rbac.ROLE_DIRECTOR, cid=90000032)
    client.force_login(boss_admin)
    client.post("/ops/admin/access/recovery/detach/",
                {"character_id": 90000031, "reason": "director-approved recovery"})
    ch.refresh_from_db()
    assert ch.user_id is None


@pytest.mark.django_db
def test_detach_retires_account_defining_username(django_user_model):
    """Security: detaching the account-defining character retires its eve:<id>
    username so a re-claiming owner mints a FRESH account, never inheriting the
    seller's account (and its manual roles/history)."""
    from apps.sso.services import resolve_login_account

    officer, _ = _user(django_user_model, "off3", rbac.ROLE_OFFICER, cid=90000040)
    cid = 90000041
    seller = django_user_model.objects.create(username=f"eve:{cid}")
    RoleAssignment.objects.create(user=seller, role=ensure_role(rbac.ROLE_MEMBER))
    ch = EveCharacter.objects.create(character_id=cid, user=seller, name="Sold Main",
                                     owner_hash="OLD", is_main=True, is_corp_member=True)
    seller.main_character_id = cid
    seller.save(update_fields=["main_character_id"])

    detach_character(ch, actor=officer, reason="sold")
    seller.refresh_from_db()
    assert seller.username != f"eve:{cid}"
    # A re-claiming login resolves to a FRESH account, not the seller's.
    account = resolve_login_account(None, cid, "New Owner")
    assert account.id != seller.id
    assert account.username == f"eve:{cid}"


@pytest.mark.django_db
def test_detach_clears_scope_grants_and_token_ciphertext(django_user_model):
    """Security: a re-claim must not inherit the prior owner's scope grants, and the
    revoked token's ciphertext is wiped."""
    from apps.sso.models import EveScopeGrant

    officer, _ = _user(django_user_model, "off4", rbac.ROLE_OFFICER, cid=90000050)
    _owner, ch = _user(django_user_model, "grantee", rbac.ROLE_MEMBER, cid=90000051)
    EveScopeGrant.objects.create(character=ch, scope="esi-industry.read_character_jobs.v1",
                                 feature_key="my_industry", active=True)

    detach_character(ch, actor=officer, reason="sold")
    assert not EveScopeGrant.objects.filter(character=ch).exists()
    tok = AuthToken.objects.get(character=ch)
    assert tok.revoked_at is not None
    assert tok._refresh_token == "" and tok._access_token == ""
