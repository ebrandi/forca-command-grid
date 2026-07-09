"""Gap B — per-dimension KPI depth (display KPIs; dimension scores unchanged).

Doctrine all/primary/upcoming coverage, combat flyable_members + recent_pvp, industrial
module_ammo_stock, strategic cyno_coverage split. These enrich the drill-down / per-KPI
history without changing the dimension score (so the index + golden gate are unaffected).
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.utils import timezone

from apps.characters.models import CharacterSkillSnapshot
from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit, SkillRequirement
from apps.readiness.models import DoctrineReadinessConfig, StrategicRoleTarget
from apps.readiness.services import compute_dimension
from apps.sde.models import SdeCategory, SdeGroup, SdeType
from apps.sso.models import EveCharacter

GUNNERY, RIFTER = 3300, 587


def _kpi_keys(result):
    return {k.key for k in (result.kpis if result else [])}


def _doctrine(name, req_level=3, priority=100):
    cat, _ = DoctrineCategory.objects.get_or_create(key="dps", label="DPS")
    d = Doctrine.objects.create(name=name, category=cat, priority=priority)
    fit = DoctrineFit.objects.create(doctrine=d, name=name, ship_type_id=RIFTER)
    SkillRequirement.objects.create(fit=fit, skill_type_id=GUNNERY, min_level=req_level, optimal_level=req_level)
    return d


def _char(cid, gunnery=5):
    ch = EveCharacter.objects.create(character_id=cid, name=f"P{cid}", is_main=True, is_corp_member=True)
    CharacterSkillSnapshot.objects.create(
        character=ch, is_latest=True, skills={str(GUNNERY): {"trained_level": gunnery, "sp": 0}})
    return ch


# --- doctrine all/primary/upcoming -------------------------------------------
@pytest.mark.django_db
def test_doctrine_coverage_kpis(sde):
    core = _doctrine("Core", 3)
    upcoming = _doctrine("Muninn Fleet", 3)
    _char(9001, gunnery=5)
    DoctrineReadinessConfig.objects.create(doctrine=core, is_primary=True)
    DoctrineReadinessConfig.objects.create(doctrine=upcoming, is_upcoming=True)
    keys = _kpi_keys(compute_dimension("doctrine"))
    assert {"doctrine.all_coverage", "doctrine.primary_coverage", "doctrine.upcoming_coverage"} <= keys


@pytest.mark.django_db
def test_doctrine_only_all_coverage_without_flags(sde):
    _doctrine("Core", 3)
    _char(9002, gunnery=5)
    keys = _kpi_keys(compute_dimension("doctrine"))
    assert "doctrine.all_coverage" in keys
    assert "doctrine.primary_coverage" not in keys  # no primary doctrines marked


# --- combat flyable + recent_pvp ---------------------------------------------
@pytest.mark.django_db
def test_combat_kpis_with_recent_pvp(sde, django_user_model):
    from apps.killboard.models import Killmail, KillmailParticipant

    _doctrine("Core", 3)
    ch = _char(9101, gunnery=5)
    km = Killmail.objects.create(killmail_id=1, killmail_hash="h1",
                                 killmail_time=timezone.now() - dt.timedelta(days=5),
                                 involves_home_corp=True, solar_system_id=30000142,
                                 victim_ship_type_id=587)
    KillmailParticipant.objects.create(killmail=km, character_id=ch.character_id, role="attacker")
    keys = _kpi_keys(compute_dimension("skill"))
    assert {"combat.flyable_members", "combat.recent_pvp"} <= keys
    rp = next(k for k in compute_dimension("skill").kpis if k.key == "combat.recent_pvp")
    assert rp.score == 100  # the one member is on a recent kill


# --- industrial module_ammo_stock --------------------------------------------
@pytest.mark.django_db
def test_module_ammo_stock_kpi(sde):
    from apps.stockpile.models import Stockpile, StockpileItem

    cat, _ = SdeCategory.objects.get_or_create(category_id=7, defaults={"name": "Module"})
    grp, _ = SdeGroup.objects.get_or_create(group_id=60, defaults={"category": cat, "name": "AfterburnerS"})
    SdeType.objects.get_or_create(type_id=438, defaults={"name": "1MN Afterburner", "group": grp, "published": True})
    sp = Stockpile.objects.create(name="Home", kind=Stockpile.Kind.CORP)
    StockpileItem.objects.create(stockpile=sp, type_id=438, quantity_current=5, quantity_target=10)
    kpis = compute_dimension("stock").kpis
    ma = next((k for k in kpis if k.key == "stock.module_ammo_stock"), None)
    assert ma is not None and ma.score == 50  # 5 of 10


# --- strategic cyno split ----------------------------------------------------
@pytest.mark.django_db
def test_strategic_cyno_coverage_split(sde):
    _char(9201, gunnery=5)  # qualifies for the skill-detected role
    StrategicRoleTarget.objects.create(
        role_key="cyno", label="Cyno", desired_count=1,
        detection=StrategicRoleTarget.Detection.SKILLS, detection_params={"skills": {str(GUNNERY): 5}})
    keys = _kpi_keys(compute_dimension("strategic"))
    assert "strategic.cyno_coverage" in keys
    assert "strategic.capital_bench" not in keys  # only a cyno role configured
