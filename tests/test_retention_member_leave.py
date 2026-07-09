"""0.4 / ADM-1: on-member-leave data retention — report-mode-first, armed enforcement."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.admin_audit.models import AppSetting, DataRetentionPolicy
from apps.admin_audit.services import enforce_member_leave, on_leave_armed
from apps.characters.models import CharacterSkillSnapshot
from apps.sso.models import AuthToken, EveCharacter
from apps.stockpile.models import Asset, AssetLocation

_ARMED_KEY = "retention:on_leave_armed"


def _policy(dc, mode=DataRetentionPolicy.OnLeave.DELETE, active=True):
    return DataRetentionPolicy.objects.update_or_create(
        data_class=dc,
        defaults={"on_member_leave": mode, "active": active, "retention_days": 365},
    )[0]


def _arm():
    AppSetting.objects.update_or_create(key=_ARMED_KEY, defaults={"value": {"armed": True}})


def _member(django_user_model, cid, *, is_member, user=None):
    user = user or django_user_model.objects.create(username=f"eve:{cid}")
    ch = EveCharacter.objects.create(character_id=cid, user=user, name=f"P{cid}",
                                     is_main=not user.characters.exists(), is_corp_member=is_member)
    t = AuthToken(character=ch, scopes=["esi-skills.read_skills.v1"],
                  access_expires_at=timezone.now() + timedelta(hours=1))
    t.refresh_token = "r"
    t.access_token = "a"
    t.save()
    CharacterSkillSnapshot.objects.create(character=ch, is_latest=True,
                                          skills={"3300": {"trained_level": 5}})
    loc = AssetLocation.objects.get_or_create(location_id=60003760)[0]
    Asset.objects.create(owner_type=Asset.Owner.CHARACTER, owner_id=cid, location=loc,
                         type_id=34, quantity=5)
    return user, ch


@pytest.mark.django_db
def test_report_mode_counts_without_deleting(django_user_model):
    _policy("token")
    _policy("skill_snapshot")
    _policy("asset_snapshot")
    _member(django_user_model, 95000001, is_member=False)   # departed account

    report = enforce_member_leave()  # unarmed → report only
    assert report["dry_run"] is True
    assert report["departed_accounts"] == 1
    assert report["counts"] == {"token": 1, "skill_snapshot": 1, "asset_snapshot": 1}
    # Report mode deletes NOTHING.
    assert AuthToken.objects.count() == 1
    assert CharacterSkillSnapshot.objects.count() == 1
    assert Asset.objects.count() == 1


@pytest.mark.django_db
def test_armed_enforcement_honours_mode_per_class(django_user_model):
    _policy("token", DataRetentionPolicy.OnLeave.DELETE)
    _policy("skill_snapshot", DataRetentionPolicy.OnLeave.DELETE)
    _policy("asset_snapshot", DataRetentionPolicy.OnLeave.RETAIN)  # keep assets
    _member(django_user_model, 95000002, is_member=False)
    _arm()
    assert on_leave_armed() is True

    report = enforce_member_leave()
    assert report["dry_run"] is False
    assert AuthToken.objects.count() == 0               # DELETE → removed
    assert CharacterSkillSnapshot.objects.count() == 0  # DELETE → removed
    assert Asset.objects.count() == 1                   # RETAIN → kept


@pytest.mark.django_db
def test_active_member_alt_not_swept(django_user_model):
    _policy("token")
    _policy("skill_snapshot")
    _policy("asset_snapshot")
    _arm()
    user = django_user_model.objects.create(username="eve:95000003")
    EveCharacter.objects.create(character_id=95000003, user=user, name="Main",
                                is_main=True, is_corp_member=True)   # still active
    _member(django_user_model, 95000004, is_member=False, user=user)  # departed alt, same account

    report = enforce_member_leave()
    assert report["departed_accounts"] == 0                       # account still active
    assert AuthToken.objects.filter(character_id=95000004).count() == 1  # alt data kept


@pytest.mark.django_db
def test_console_arm_toggle(client, django_user_model):
    from apps.identity.models import RoleAssignment
    from apps.sso.services import ensure_role
    from core import rbac

    d = django_user_model.objects.create(username="dir")
    RoleAssignment.objects.create(user=d, role=ensure_role(rbac.ROLE_DIRECTOR))
    client.force_login(d)

    assert on_leave_armed() is False
    resp = client.post("/ops/admin/retention/settings/", {"on_leave_armed": "on"})
    assert resp.status_code == 302
    assert on_leave_armed() is True
