"""P5 — the armed MRP beat pings the per-code capacity bottleneck variant."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.characters.models import CharacterSkillSnapshot
from apps.doctrines.models import Doctrine, DoctrineFit
from apps.erp.models import CorpIndustryJob
from apps.industry import capacity
from apps.industry.models import MrpConfig
from apps.industry.tasks import run_mrp_beat
from apps.pingboard.models import Alert
from apps.sso.models import EveCharacter, EveScopeGrant
from apps.store.models import FitSupplyNeed

pytestmark = pytest.mark.django_db

MASS_PROD = 3387


def _need(hull=587, qty=3, *, days=1):
    doctrine = Doctrine.objects.create(name="D")
    fit = DoctrineFit.objects.create(doctrine=doctrine, name="Alpha", ship_type_id=hull)
    FitSupplyNeed.objects.create(
        doctrine_fit=fit, quantity_required=qty,
        required_by=timezone.now() + timedelta(days=days),
    )
    return fit


def _armed(auto=True):
    cfg = MrpConfig.active()
    cfg.capacity_enabled = True
    cfg.auto_run_enabled = auto
    cfg.save()
    return cfg


def _measured_pilot(cid, mass_prod):
    char = EveCharacter.objects.create(
        character_id=cid, name=f"Pilot {cid}", is_corp_member=True,
    )
    EveScopeGrant.objects.create(
        character=char, scope="esi-industry.read_character_jobs.v1",
        feature_key="my_industry", active=True,
    )
    CharacterSkillSnapshot.objects.create(
        character=char, is_latest=True, as_of=timezone.now(),
        skills={str(MASS_PROD): {"trained_level": mass_prod, "sp": 0}},
    )
    return char


def test_armed_beat_pings_unmeasured_and_is_idempotent(priced_sde):
    """Zero measured capacity ⇒ the build row is refused (unmeasured) ⇒ the beat
    pings the per-code variant, once per (requirement, day, code)."""
    _need()
    _armed()                                    # no measured pilots

    pinged = run_mrp_beat()
    assert pinged >= 1
    assert Alert.objects.filter(
        template_key="industry.capacity_bottleneck.unmeasured"
    ).exists()

    before = Alert.objects.count()
    run_mrp_beat()                              # same day → nothing new
    assert Alert.objects.count() == before


def test_armed_beat_selects_slots_variant(priced_sde):
    """A measured-but-contended pilot ⇒ the late row carries the slots code ⇒ the
    beat selects the slots variant (per-code selection, not a generic ping)."""
    _measured_pilot(6001, mass_prod=0)           # exactly 1 slot
    CorpIndustryJob.objects.create(              # occupies it for a month (unrelated product)
        job_id=7001, installer_id=6001, activity_id=1, blueprint_type_id=998,
        product_type_id=999, runs=1, status="active",
        end_date=timezone.now() + timedelta(days=30),
    )
    cfg = _armed()
    capacity.derive_resources(cfg)
    _need(qty=1, days=1)                          # due tomorrow, can't land for a month

    run_mrp_beat()
    assert Alert.objects.filter(
        template_key="industry.capacity_bottleneck.slots"
    ).exists()
    assert not Alert.objects.filter(
        template_key="industry.capacity_bottleneck.unmeasured"
    ).exists()


def test_manual_run_never_pings(priced_sde):
    from apps.industry import mrp

    _need()
    _armed()
    mrp.run_mrp(actor=None)                       # a manual run, not the beat
    assert not Alert.objects.filter(
        template_key__startswith="industry.capacity_bottleneck"
    ).exists()


def test_disarmed_beat_returns_zero(priced_sde):
    _need()
    cfg = MrpConfig.active()
    cfg.capacity_enabled = True
    cfg.auto_run_enabled = False                 # beat disarmed
    cfg.save()
    assert run_mrp_beat() == 0
    assert not Alert.objects.filter(
        template_key__startswith="industry.capacity_bottleneck"
    ).exists()
