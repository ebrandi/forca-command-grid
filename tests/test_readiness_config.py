"""Phase 1 — the readiness configuration layer + Dimensions admin page.

Covers: defaults/merge/validation, the weighted index (default config reproduces the
Phase-0 equal-weight number; weight/enable edits move it), version-bump, and the
Director-gated Dimensions & weights page (render, save+audit, reject, reset).
"""
from __future__ import annotations

import copy

import pytest

from apps.characters.models import CharacterSkillSnapshot
from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit, SkillRequirement
from apps.identity.models import RoleAssignment
from apps.readiness import config
from apps.readiness.services import compute_readiness
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac

GUNNERY = 3300
RIFTER = 587


def _doctrine(name="Core", priority=100, req_level=3):
    cat, _ = DoctrineCategory.objects.get_or_create(key="dps", label="DPS")
    d = Doctrine.objects.create(name=name, category=cat, priority=priority)
    fit = DoctrineFit.objects.create(doctrine=d, name=name, ship_type_id=RIFTER)
    SkillRequirement.objects.create(fit=fit, skill_type_id=GUNNERY, min_level=req_level, optimal_level=req_level)
    return d


def _char(django_user_model, cid, gunnery_level):
    user = django_user_model.objects.create(username=f"eve:{cid}")
    ch = EveCharacter.objects.create(character_id=cid, user=user, name=f"P{cid}", is_corp_member=True)
    CharacterSkillSnapshot.objects.create(
        character=ch, is_latest=True, skills={str(GUNNERY): {"trained_level": gunnery_level, "sp": 0}}
    )
    return ch


def _director(django_user_model, username="dir"):
    user = django_user_model.objects.create(username=username)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_DIRECTOR))
    return user


# --- config layer ------------------------------------------------------------
@pytest.mark.django_db
def test_config_defaults_and_merge():
    dims = config.get("dimensions")
    assert set(dims) == {
        "doctrine", "skill", "stock", "logistics",
        "financial", "srp", "activity", "recruitment", "leadership", "infrastructure",
        "strategic", "fleet_comp", "support", "staging",
    }
    # The original four ship enabled at equal weight (reproducing the Phase-0 index);
    # every net-new dimension ships disabled (preview only) until leadership enables.
    for key in ("doctrine", "skill", "stock", "logistics"):
        assert dims[key]["weight"] == 1.0 and dims[key]["enabled"]
    for key in ("financial", "srp", "activity", "recruitment", "leadership",
                "infrastructure", "strategic", "fleet_comp", "support", "staging"):
        assert not dims[key]["enabled"]
    # Unknown domain rejected.
    with pytest.raises(config.ConfigError):
        config.get("nope")
    # A stored override merges over defaults (and bumps the version).
    assert config.config_version() == 0
    doc = copy.deepcopy(config.DEFAULTS["dimensions"])
    doc["doctrine"]["weight"] = 2.5
    config.set("dimensions", doc, user=None)
    assert config.get("dimensions")["doctrine"]["weight"] == 2.5
    assert config.dimension_weights()["doctrine"] == 2.5
    assert config.config_version() == 1


@pytest.mark.django_db
def test_set_validation_rejects_bad_input():
    bad_threshold = copy.deepcopy(config.DEFAULTS["dimensions"])
    bad_threshold["doctrine"]["thresholds"] = {"amber": 40, "red": 60}  # red ≥ amber
    with pytest.raises(config.ConfigError):
        config.set("dimensions", bad_threshold, user=None)

    negative = copy.deepcopy(config.DEFAULTS["dimensions"])
    negative["skill"]["weight"] = -1
    with pytest.raises(config.ConfigError):
        config.set("dimensions", negative, user=None)

    # A rejected write leaves the version untouched (no partial write).
    assert config.config_version() == 0


@pytest.mark.django_db
def test_default_config_reproduces_equal_weight_index(django_user_model, sde):
    _doctrine("Core", 100, 3)
    _char(django_user_model, 9001, 5)  # flies
    _char(django_user_model, 9002, 1)  # known, can't
    # doctrine 50, skill 50, stock None (excluded), logistics 100 → mean(50,50,100)=67.
    result = compute_readiness(use_cache=False)
    assert result["dimensions"]["doctrine"] == 50
    assert result["dimensions"]["skill"] == 50
    assert result["index"] == 67


@pytest.mark.django_db
def test_reweighting_and_disabling_move_the_index(django_user_model, sde):
    _doctrine("Core", 100, 3)
    _char(django_user_model, 9101, 5)
    _char(django_user_model, 9102, 1)
    assert compute_readiness(use_cache=False)["index"] == 67  # equal-weight baseline

    # Weight doctrine 3×: (3·50 + 50 + 100)/5 = 60.
    doc = copy.deepcopy(config.DEFAULTS["dimensions"])
    doc["doctrine"]["weight"] = 3.0
    config.set("dimensions", doc, user=None)
    assert compute_readiness(use_cache=False)["index"] == 60

    # Disable logistics: it drops out of the index entirely → mean(50,50)=50.
    doc = copy.deepcopy(config.DEFAULTS["dimensions"])
    doc["logistics"]["enabled"] = False
    config.set("dimensions", doc, user=None)
    result = compute_readiness(use_cache=False)
    assert "logistics" not in result["dimensions"]
    assert result["index"] == 50


@pytest.mark.django_db
def test_snapshot_stamps_config_version_and_weights(django_user_model, sde):
    _doctrine("Core", 100, 3)
    _char(django_user_model, 9201, 5)
    doc = copy.deepcopy(config.DEFAULTS["dimensions"])
    doc["doctrine"]["weight"] = 2.0
    config.set("dimensions", doc, user=None)  # version → 1

    compute_readiness(persist=True, use_cache=False)
    from apps.readiness.models import ReadinessSnapshot

    snap = ReadinessSnapshot.objects.latest("created_at")
    assert snap.config_version == 1
    assert snap.weights["doctrine"] == 2.0


# --- admin page --------------------------------------------------------------
@pytest.mark.django_db
def test_dimensions_page_is_director_only(client, django_user_model):
    member = django_user_model.objects.create(username="m")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_OFFICER))  # officer, not director
    client.force_login(member)
    assert client.get("/ops/admin/readiness/dimensions/").status_code == 403


@pytest.mark.django_db
def test_dimensions_page_renders_rows(client, django_user_model):
    client.force_login(_director(django_user_model))
    html = client.get("/ops/admin/readiness/dimensions/").content.decode()
    assert "Dimensions &amp; weights" in html
    for key in ("doctrine", "skill", "stock", "logistics"):
        assert f"dim_{key}_weight" in html
    assert "config v0" in html


@pytest.mark.django_db
def test_dimensions_save_persists_and_audits(client, django_user_model):
    from apps.admin_audit.models import AuditLog

    client.force_login(_director(django_user_model, "dir2"))
    resp = client.post("/ops/admin/readiness/dimensions/", {
        "dim_doctrine_enabled": "on", "dim_doctrine_weight": "2.0",
        "dim_doctrine_amber": "60", "dim_doctrine_red": "40",
        "dim_skill_enabled": "on", "dim_skill_weight": "1.0",
        "dim_skill_amber": "60", "dim_skill_red": "40",
        "dim_stock_enabled": "on", "dim_stock_weight": "1.0",
        "dim_stock_amber": "55", "dim_stock_red": "35",
        # logistics checkbox omitted → disabled.
        "dim_logistics_weight": "1.0", "dim_logistics_amber": "60", "dim_logistics_red": "40",
    })
    assert resp.status_code == 302
    dims = config.get("dimensions")
    assert dims["doctrine"]["weight"] == 2.0
    assert dims["logistics"]["enabled"] is False
    assert config.config_version() == 1
    assert AuditLog.objects.filter(action="readiness.config.update", target_id="dimensions").exists()


@pytest.mark.django_db
def test_dimensions_save_rejects_bad_thresholds(client, django_user_model):
    client.force_login(_director(django_user_model, "dir3"))
    resp = client.post("/ops/admin/readiness/dimensions/", {
        # doctrine amber(30) ≤ red(50) → invalid.
        "dim_doctrine_enabled": "on", "dim_doctrine_weight": "1.0",
        "dim_doctrine_amber": "30", "dim_doctrine_red": "50",
        "dim_skill_enabled": "on", "dim_skill_weight": "1.0",
        "dim_skill_amber": "60", "dim_skill_red": "40",
        "dim_stock_enabled": "on", "dim_stock_weight": "1.0",
        "dim_stock_amber": "55", "dim_stock_red": "35",
        "dim_logistics_enabled": "on", "dim_logistics_weight": "1.0",
        "dim_logistics_amber": "60", "dim_logistics_red": "40",
    }, follow=True)
    assert b"red &lt; amber" in resp.content or b"red < amber" in resp.content
    # Nothing persisted — version untouched.
    assert config.config_version() == 0


@pytest.mark.django_db
def test_reset_restores_defaults(client, django_user_model):
    director = _director(django_user_model, "dir4")
    doc = copy.deepcopy(config.DEFAULTS["dimensions"])
    doc["doctrine"]["weight"] = 5.0
    config.set("dimensions", doc, user=director)
    assert config.get("dimensions")["doctrine"]["weight"] == 5.0

    client.force_login(director)
    resp = client.post("/ops/admin/readiness/dimensions/reset/")
    assert resp.status_code == 302
    assert config.get("dimensions")["doctrine"]["weight"] == 1.0
