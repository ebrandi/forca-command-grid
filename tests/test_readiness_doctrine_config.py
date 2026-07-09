"""Milestone — G5: doctrine readiness classification (model + admin + config-gated findings).

Key invariant: with no DoctrineReadinessConfig rows the doctrine dimension is byte-
identical (no extra findings), so the index and the golden v1 payload are unchanged.
Classifying a doctrine mandatory/retiring adds findings without touching the score.
"""
from __future__ import annotations

import datetime as dt

import pytest

from apps.characters.models import CharacterSkillSnapshot
from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit, SkillRequirement
from apps.identity.models import RoleAssignment
from apps.readiness.models import DoctrineReadinessConfig
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac

GUNNERY = 3300
RIFTER = 587


def _director(django_user_model, name="dir"):
    user = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_DIRECTOR))
    return user


def _doctrine(name="Core", req_level=3, priority=100):
    cat, _ = DoctrineCategory.objects.get_or_create(key="dps", label="DPS")
    d = Doctrine.objects.create(name=name, category=cat, priority=priority)
    fit = DoctrineFit.objects.create(doctrine=d, name=name, ship_type_id=RIFTER)
    SkillRequirement.objects.create(fit=fit, skill_type_id=GUNNERY, min_level=req_level, optimal_level=req_level)
    return d


def _char(cid, level):
    ch = EveCharacter.objects.create(character_id=cid, name=f"P{cid}", is_main=True, is_corp_member=True)
    CharacterSkillSnapshot.objects.create(
        character=ch, is_latest=True, skills={str(GUNNERY): {"trained_level": level, "sp": 0}}
    )
    return ch


def _compute_doctrine():
    from apps.readiness.services import compute_dimension

    return compute_dimension("doctrine")


# --- index neutrality (the load-bearing invariant) ---------------------------
@pytest.mark.django_db
def test_no_config_means_no_extra_findings(django_user_model, sde):
    d = _doctrine("Core", 3)
    _char(8001, 1)  # can't fly → a base gap exists
    result = _compute_doctrine()
    # Only the base gap finding(s) — none with a config-driven kpi_key.
    assert all(f.kpi_key not in ("doctrine.mandatory_coverage", "doctrine.retirement")
               for f in result.findings)
    assert not DoctrineReadinessConfig.objects.exists()
    assert d.name  # fixture sanity


# --- mandatory escalation ----------------------------------------------------
@pytest.mark.django_db
def test_mandatory_undercrewed_doctrine_adds_high_finding(django_user_model, sde):
    d = _doctrine("Core", 3)
    _char(8001, 1)  # can't fly → under-crewed gap
    DoctrineReadinessConfig.objects.create(doctrine=d, is_mandatory=True)
    result = _compute_doctrine()
    mand = [f for f in result.findings if f.kpi_key == "doctrine.mandatory_coverage"]
    assert len(mand) == 1
    assert mand[0].severity == "high"
    assert "Core" in mand[0].label


@pytest.mark.django_db
def test_fully_crewed_mandatory_doctrine_has_no_finding(django_user_model, sde):
    d = _doctrine("Core", 3)
    _char(8001, 5)  # can fly → no gap → mandatory not flagged
    DoctrineReadinessConfig.objects.create(doctrine=d, is_mandatory=True)
    result = _compute_doctrine()
    assert not [f for f in result.findings if f.kpi_key == "doctrine.mandatory_coverage"]


# --- retirement --------------------------------------------------------------
@pytest.mark.django_db
def test_retired_doctrine_adds_finding(django_user_model, sde):
    d = _doctrine("Old", 3)
    _char(8001, 5)
    DoctrineReadinessConfig.objects.create(doctrine=d, retirement_date=dt.date(2020, 1, 1))
    result = _compute_doctrine()
    ret = [f for f in result.findings if f.kpi_key == "doctrine.retirement"]
    assert len(ret) == 1 and "Old" in ret[0].label


@pytest.mark.django_db
def test_future_retirement_date_no_finding(django_user_model, sde):
    d = _doctrine("Future", 3)
    _char(8001, 5)
    DoctrineReadinessConfig.objects.create(doctrine=d, retirement_date=dt.date(2099, 1, 1))
    result = _compute_doctrine()
    assert not [f for f in result.findings if f.kpi_key == "doctrine.retirement"]


# --- admin page --------------------------------------------------------------
@pytest.mark.django_db
def test_admin_page_director_only(client, django_user_model, sde):
    member = django_user_model.objects.create(username="m")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_OFFICER))
    client.force_login(member)
    assert client.get("/ops/admin/readiness/doctrines/").status_code == 403


@pytest.mark.django_db
def test_admin_classify_and_unclassify(client, django_user_model, sde):
    from apps.admin_audit.models import AuditLog

    d = _doctrine("Core", 3)
    client.force_login(_director(django_user_model))
    # Classify.
    client.post("/ops/admin/readiness/doctrines/", {
        f"doc_{d.id}_mandatory": "on", f"doc_{d.id}_retire": "2030-06-01", f"doc_{d.id}_min": "10",
    })
    cfg = DoctrineReadinessConfig.objects.get(doctrine=d)
    assert cfg.is_mandatory and cfg.min_pilots == 10
    assert cfg.retirement_date == dt.date(2030, 6, 1)
    assert AuditLog.objects.filter(action="readiness.doctrine_config.update").exists()
    # Un-classify (all default) → row removed, restoring the engine fast path.
    client.post("/ops/admin/readiness/doctrines/", {})
    assert not DoctrineReadinessConfig.objects.filter(doctrine=d).exists()


@pytest.mark.django_db
def test_admin_rejects_bad_date(client, django_user_model, sde):
    d = _doctrine("Core", 3)
    client.force_login(_director(django_user_model, "dir2"))
    client.post("/ops/admin/readiness/doctrines/", {f"doc_{d.id}_retire": "not-a-date"})
    assert not DoctrineReadinessConfig.objects.filter(doctrine=d).exists()
