"""Phase C: per-pilot ESI industry-job + blueprint import (my_industry scope)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from apps.erp import esi_import
from apps.erp.models import Blueprint, CharacterIndustryJob
from apps.sso.models import EveCharacter

RIFTER = 587


class _PathClient:
    """Fake ESIClient returning job rows or blueprint rows depending on the path."""

    def __init__(self, jobs=None, blueprints=None):
        self.jobs = jobs or []
        self.blueprints = blueprints or []

    def get_paged(self, path, token=None, params=None):
        return self.jobs if "industry/jobs" in path else self.blueprints


def _char(django_user_model, character_id=4242, name="Pilot"):
    user = django_user_model.objects.create(username=f"u{character_id}")
    return EveCharacter.objects.create(
        character_id=character_id, user=user, name=name, is_main=True, is_corp_member=True
    )


@pytest.fixture
def _granted(monkeypatch):
    monkeypatch.setattr(esi_import, "_char_token", lambda ch, scope: "tok")


@pytest.mark.django_db
def test_scope_catalogued_both_sides():
    from django.conf import settings

    from apps.sso.scopes import FEATURES

    assert "my_industry" in settings.EVE_SSO_FEATURE_SCOPES
    assert any(f.key == "my_industry" and f.audience == "pilot" for f in FEATURES)
    assert "esi-industry.read_character_jobs.v1" in settings.EVE_SSO_FEATURE_SCOPES["my_industry"]


@pytest.mark.django_db
def test_sync_character_jobs(_granted, sde, django_user_model):
    ch = _char(django_user_model)
    rows = [
        {"job_id": 7001, "activity_id": 1, "blueprint_type_id": 601, "product_type_id": 600,
         "runs": 3, "status": "active", "cost": 1234.56,
         "start_date": "2026-06-20T00:00:00Z", "end_date": "2026-06-28T00:00:00Z"},
        {"job_id": 7002, "activity_id": 8, "blueprint_type_id": 599, "product_type_id": 600,
         "runs": 1, "status": "delivered", "start_date": "2026-06-10T00:00:00Z",
         "end_date": "2026-06-11T00:00:00Z"},
    ]
    res = esi_import.sync_character_industry_jobs(ch, client=_PathClient(jobs=rows))
    assert res["status"] == "ok" and res["jobs"] == 2
    j = CharacterIndustryJob.objects.get(job_id=7001)
    assert j.character_id == 4242 and j.runs == 3 and j.cost == Decimal("1234.56")
    assert j.activity_label == "Manufacturing" and j.is_active
    assert CharacterIndustryJob.objects.get(job_id=7002).activity_label == "Invention"

    # Snapshot-replace scoped to this character only.
    other = _char(django_user_model, character_id=5555, name="Other")
    CharacterIndustryJob.objects.create(character_id=other.character_id, job_id=9999,
                                        blueprint_type_id=1, product_type_id=1)
    esi_import.sync_character_industry_jobs(ch, client=_PathClient(jobs=rows[:1]))
    assert CharacterIndustryJob.objects.filter(character_id=4242).count() == 1
    assert CharacterIndustryJob.objects.filter(character_id=5555).count() == 1  # untouched


@pytest.mark.django_db
def test_sync_character_blueprints(_granted, sde, django_user_model):
    ch = _char(django_user_model)
    rows = [{"item_id": 8001, "type_id": 601, "material_efficiency": 10, "time_efficiency": 20,
             "quantity": -1, "runs": -1, "location_id": 60003760}]
    res = esi_import.sync_character_blueprints(ch, client=_PathClient(blueprints=rows))
    assert res["status"] == "ok" and res["blueprints"] == 1
    bp = Blueprint.objects.get(item_id=8001)
    assert bp.owner_type == Blueprint.Owner.CHARACTER and bp.owner_id == 4242
    assert bp.me == 10 and bp.is_original and bp.source == "esi"


@pytest.mark.django_db
def test_no_scope_is_noop(monkeypatch, sde, django_user_model):
    ch = _char(django_user_model)
    monkeypatch.setattr(esi_import, "_char_token", lambda c, scope: None)
    assert esi_import.sync_character_industry_jobs(ch)["status"] == "no_token"
    assert esi_import.sync_character_blueprints(ch)["status"] == "no_token"
    assert CharacterIndustryJob.objects.count() == 0


@pytest.mark.django_db
def test_sync_all_only_granted_pilots(monkeypatch, sde, django_user_model):
    granted = _char(django_user_model, character_id=4242)
    _char(django_user_model, character_id=5555)  # no token
    monkeypatch.setattr(esi_import, "_char_token",
                        lambda ch, scope: "tok" if ch.character_id == 4242 else None)
    client = _PathClient(
        jobs=[{"job_id": 7001, "activity_id": 1, "blueprint_type_id": 601,
               "product_type_id": 600, "runs": 1, "status": "active"}],
        blueprints=[{"item_id": 8001, "type_id": 601, "quantity": -1, "runs": -1}],
    )
    res = esi_import.sync_all_character_industry(client=client)
    assert res["characters"] == 1 and res["jobs"] == 1 and res["blueprints"] == 1
    assert CharacterIndustryJob.objects.filter(character_id=granted.character_id).count() == 1
    assert CharacterIndustryJob.objects.filter(character_id=5555).count() == 0
