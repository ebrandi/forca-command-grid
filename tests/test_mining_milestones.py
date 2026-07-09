"""MIN-4 (roadmap 3.10) — mining participation milestones.

Cumulative-m³ milestones give the industrial backbone visible progression, and crossing a
NEW one (future-only, after the baseline snapshot) earns recognition — never ISK.
"""
from __future__ import annotations

import datetime as dt

import pytest

from apps.identity.models import RoleAssignment
from apps.mining.models import MiningLedgerEntry, MiningMilestone, MiningObserver
from apps.mining.services import cumulative_m3, mining_milestones, scan_mining_milestones
from apps.pilots.models import ContributionEvent
from apps.sde.models import SdeCategory, SdeGroup, SdeType
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac

pytestmark = pytest.mark.django_db
DAY = dt.date(2026, 7, 1)


def _ore(volume=1.0):  # volume 1 → quantity == m³, easy thresholds
    cat, _ = SdeCategory.objects.get_or_create(category_id=25, defaults={"name": "Asteroid"})
    grp, _ = SdeGroup.objects.get_or_create(group_id=450, defaults={"category": cat, "name": "Veld"})
    SdeType.objects.get_or_create(type_id=18, defaults={"group": grp, "name": "Veldspar", "volume": volume})


def _miner(django_user_model, uid, cid):
    user = django_user_model.objects.create(username=f"eve:{uid}")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(character_id=cid, user=user, name=f"P{cid}", is_main=True,
                                is_corp_member=True)
    user.main_character_id = cid
    user.save()
    return user


def _mine(cid, qty, day=DAY):
    obs, _ = MiningObserver.objects.get_or_create(observer_id=1)
    MiningLedgerEntry.objects.create(observer=obs, character_id=cid, character_name=f"P{cid}",
                                     type_id=18, quantity=qty, day=day)


def _milestone_events():
    return ContributionEvent.objects.filter(
        kind=ContributionEvent.Kind.MINING, ref_type="mining_milestone"
    )


def test_cumulative_m3_and_milestones(django_user_model):
    _ore()
    _miner(django_user_model, 1, 100)
    _mine(100, 1_500_000)
    assert cumulative_m3([100]) == 1_500_000
    ms = mining_milestones([100])
    assert ms["reached"] == [1_000_000]
    assert ms["next_threshold"] == 10_000_000
    assert ms["remaining"] == 8_500_000


def test_first_scan_baselines_without_credit(django_user_model):
    _ore()
    _miner(django_user_model, 2, 200)
    _mine(200, 2_000_000)  # already past 1M before the baseline
    assert scan_mining_milestones()["baselined_now"] is True
    m = MiningMilestone.objects.get(user__username="eve:2", threshold_m3=1_000_000)
    assert m.credited is False  # future-only: pre-baseline milestones are not credited
    assert not _milestone_events().exists()


def test_new_crossing_after_baseline_credits(django_user_model):
    _ore()
    user = _miner(django_user_model, 3, 300)
    _mine(300, 500_000)          # below 1M
    scan_mining_milestones()     # baseline run — nothing reached
    _mine(300, 600_000, day=dt.date(2026, 7, 2))  # now 1.1M — crosses 1M AFTER baseline
    assert scan_mining_milestones()["awarded"] == 1
    m = MiningMilestone.objects.get(user=user, threshold_m3=1_000_000)
    assert m.credited is True
    assert _milestone_events().get(user=user).points == 5


def test_scan_is_idempotent(django_user_model):
    _ore()
    _miner(django_user_model, 4, 400)
    _mine(400, 500_000)
    scan_mining_milestones()     # baseline
    _mine(400, 600_000, day=dt.date(2026, 7, 2))  # cross 1M
    scan_mining_milestones()     # credits once
    scan_mining_milestones()     # again → no re-credit
    assert _milestone_events().count() == 1
