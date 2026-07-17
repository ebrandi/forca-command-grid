"""P5 — Production Capacity board: RBAC, audited POSTs, privacy, CSV, rendering."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.admin_audit.models import AuditLog
from apps.characters.models import CharacterSkillSnapshot
from apps.erp.models import CorpIndustryJob
from apps.industry import capacity
from apps.industry.models import MrpConfig, ProductionResource
from apps.sso.models import EveCharacter, EveScopeGrant
from core import rbac

pytestmark = pytest.mark.django_db

MASS_PROD, ADV_MASS_PROD = 3387, 24625


def _officer(django_user_model, name="qm"):
    from apps.identity.models import RoleAssignment
    from apps.sso.services import ensure_role

    user = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_OFFICER))
    return user


def _member(django_user_model, name="pilot"):
    from apps.identity.models import RoleAssignment
    from apps.sso.services import ensure_role

    user = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    return user


def _pilot(cid, *, user=None, consent=True, name=None):
    char = EveCharacter.objects.create(
        character_id=cid, name=name or f"Pilot {cid}", is_corp_member=True, user=user,
    )
    if consent:
        EveScopeGrant.objects.create(
            character=char, scope=f"esi-industry.read_character_jobs.v1#{cid}",
            feature_key="my_industry", active=True,
        )
    return char


def _snapshot(char, levels):
    CharacterSkillSnapshot.objects.create(
        character=char, is_latest=True, as_of=timezone.now(),
        skills={str(s): {"trained_level": v, "sp": 0} for s, v in levels.items()},
    )


def _corp_job(job_id, installer_id, *, activity_id=1, status="active"):
    return CorpIndustryJob.objects.create(
        job_id=job_id, installer_id=installer_id, activity_id=activity_id,
        blueprint_type_id=588, product_type_id=587, runs=1, status=status,
        end_date=timezone.now() + timedelta(days=1),
    )


def _armed():
    cfg = MrpConfig.active()
    cfg.capacity_enabled = True
    cfg.save()
    return cfg


# --------------------------------------------------------------------------- #
#  RBAC
# --------------------------------------------------------------------------- #
def test_member_forbidden_on_board_and_every_post(client, django_user_model):
    client.force_login(_member(django_user_model))
    assert client.get("/industry/capacity/").status_code == 403
    assert client.post("/industry/capacity/settings/").status_code == 403
    assert client.post("/industry/capacity/derive/").status_code == 403
    assert client.post("/industry/capacity/resource/1/").status_code == 403


def test_officer_can_load_the_board(client, django_user_model):
    client.force_login(_officer(django_user_model))
    assert client.get("/industry/capacity/").status_code == 200


# --------------------------------------------------------------------------- #
#  Pool numbers + privacy
# --------------------------------------------------------------------------- #
def test_pool_summary_numbers(client, django_user_model):
    client.force_login(_officer(django_user_model))
    char = _pilot(5001)
    _snapshot(char, {MASS_PROD: 4, ADV_MASS_PROD: 1})     # 6 mfg slots
    cfg = _armed()
    capacity.derive_resources(cfg)
    _corp_job(9001, 5001)
    _corp_job(9002, 5001)

    resp = client.get("/industry/capacity/")
    mfg = next(p for p in resp.context["pool_summary"] if p["activity_class"] == "manufacturing")
    assert mfg["theoretical"] == 6
    assert mfg["committed"] == 2
    assert mfg["remaining"] == 4


def test_aggregation_privacy_names_one_counts_two(client, django_user_model):
    client.force_login(_officer(django_user_model))
    consenting = _pilot(5101, name="Namely")
    _snapshot(consenting, {MASS_PROD: 2})
    cfg = _armed()
    capacity.derive_resources(cfg)
    _corp_job(9101, 5101)                                 # named
    _corp_job(9102, 999999)                               # non-consenting → unmeasured

    resp = client.get("/industry/capacity/")
    assert resp.status_code == 200
    assert "Namely" in resp.content.decode()
    assert resp.context["unmeasured_jobs"] == 1


def test_member_strip_shows_only_own_characters(client, django_user_model):
    me = _member(django_user_model, "me")
    mine = _pilot(5201, user=me, name="Mine")
    _snapshot(mine, {MASS_PROD: 2})
    other = _member(django_user_model, "other")
    theirs = _pilot(5202, user=other, name="Theirs")
    _snapshot(theirs, {MASS_PROD: 2})
    cfg = _armed()
    capacity.derive_resources(cfg)

    client.force_login(me)
    resp = client.get("/industry/jobs/")
    assert resp.status_code == 200
    caps = resp.context["my_capacity"]
    assert caps and all(c["character_id"] == 5201 for c in caps)
    assert "Theirs" not in resp.content.decode()


# --------------------------------------------------------------------------- #
#  Audited POSTs + GET-never-derives
# --------------------------------------------------------------------------- #
def test_settings_post_saved_and_audited(client, django_user_model):
    client.force_login(_officer(django_user_model))
    resp = client.post("/industry/capacity/settings/",
                       {"capacity_enabled": "on", "capacity_skill_stale_days": "10"})
    assert resp.status_code == 302
    cfg = MrpConfig.active()
    assert cfg.capacity_enabled is True
    assert cfg.capacity_skill_stale_days == 10
    assert AuditLog.objects.filter(action="industry.capacity.settings").exists()


def test_resource_override_post_saved_and_audited(client, django_user_model):
    client.force_login(_officer(django_user_model))
    char = _pilot(5301)
    _snapshot(char, {MASS_PROD: 2})
    capacity.derive_resources(_armed())
    res = ProductionResource.objects.get(character=char, activity_class="manufacturing")
    resp = client.post(f"/industry/capacity/resource/{res.pk}/",
                       {"manual_slots_override": "3", "is_paused": "on"})
    assert resp.status_code == 302
    res.refresh_from_db()
    assert res.manual_slots_override == 3
    assert res.is_paused is True
    assert AuditLog.objects.filter(action="industry.capacity.resource_override").exists()


def test_get_never_derives_but_post_does(client, django_user_model):
    client.force_login(_officer(django_user_model))
    char = _pilot(5401)
    _snapshot(char, {MASS_PROD: 2})
    _armed()

    client.get("/industry/capacity/")                     # a GET must not write
    assert not ProductionResource.objects.filter(character=char).exists()

    resp = client.post("/industry/capacity/derive/")      # explicit POST derives
    assert resp.status_code == 302
    assert ProductionResource.objects.filter(character=char).exists()
    assert AuditLog.objects.filter(action="industry.capacity.derive").exists()


# --------------------------------------------------------------------------- #
#  CSV
# --------------------------------------------------------------------------- #
def test_capacity_csv_machine_keys(client, django_user_model):
    client.force_login(_officer(django_user_model))
    head = client.get("/industry/capacity/?export=csv").content.decode().splitlines()[0]
    for col in ("character_id", "activity_class", "slots", "used", "remaining", "as_of"):
        assert col in head


def test_material_plan_csv_gains_bottleneck_last(client, django_user_model, priced_sde):
    from apps.doctrines.models import Doctrine, DoctrineFit
    from apps.store.models import FitOffer

    client.force_login(_officer(django_user_model))
    doctrine = Doctrine.objects.create(name="D")
    fit = DoctrineFit.objects.create(doctrine=doctrine, name="Alpha", ship_type_id=587)
    FitOffer.objects.create(fit=fit, target_stock=6)
    head = client.get("/industry/mrp/?export=csv").content.decode().splitlines()[0]
    assert head.split(",")[-1] == "bottleneck"
