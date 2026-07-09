"""ESI corp blueprint + industry-job import → erp.Blueprint / CorpIndustryJob.

Covers the snapshot-replace semantics (the corp's owned set is the truth),
idempotency, that manual rows survive an ESI sync, that a spent BPC doesn't count
toward blueprint coverage, and that no granted token is a clean no-op.
"""
from __future__ import annotations

import pytest

from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit
from apps.erp import services
from apps.erp.models import Blueprint, CorpIndustryJob

RIFTER = 587


class _PagedClient:
    """Fake ESIClient exposing only get_paged, returning a fixed page of rows."""

    def __init__(self, rows):
        self.rows = rows

    def get_paged(self, path, token=None, params=None):
        return self.rows


@pytest.fixture
def _granted(monkeypatch):
    """Pretend a Director has granted the scope and a token resolves."""
    from apps.erp import esi_import

    monkeypatch.setattr(esi_import, "_token_character",
                        lambda scope: type("C", (), {"character_id": 1})())
    monkeypatch.setattr("apps.sso.token_service.get_valid_access_token", lambda ch, sc: "tok")


@pytest.mark.django_db
def test_sync_blueprints_snapshot_and_idempotent(_granted, sde):
    from apps.erp.esi_import import sync_corp_blueprints

    # A hand-entered blueprint must survive an ESI sync (different source).
    Blueprint.objects.create(
        owner_type=Blueprint.Owner.CORPORATION, type_id=111, product_type_id=222, source="manual"
    )
    rows = [
        {"item_id": 9001, "type_id": 681, "material_efficiency": 10, "time_efficiency": 20,
         "quantity": -1, "runs": -1, "location_id": 60003760},  # a BPO
        {"item_id": 9002, "type_id": 682, "material_efficiency": 2, "time_efficiency": 4,
         "quantity": -2, "runs": 5, "location_id": 60003760},   # a BPC
    ]
    res = sync_corp_blueprints(corp_id=1, client=_PagedClient(rows))
    assert res["status"] == "ok" and res["blueprints"] == 2

    esi = Blueprint.objects.filter(source="esi")
    assert esi.count() == 2
    bpo = esi.get(item_id=9001)
    assert bpo.me == 10 and bpo.is_original and bpo.is_usable
    bpc = esi.get(item_id=9002)
    assert not bpc.is_original and bpc.runs == 5 and bpc.is_usable
    assert Blueprint.objects.filter(source="manual").count() == 1  # untouched

    # Snapshot-replace: re-syncing the same (smaller) set doesn't accumulate.
    sync_corp_blueprints(corp_id=1, client=_PagedClient(rows[:1]))
    assert Blueprint.objects.filter(source="esi").count() == 1
    assert Blueprint.objects.filter(source="manual").count() == 1


@pytest.mark.django_db
def test_coverage_ignores_spent_bpc(sde):
    cat = DoctrineCategory.objects.create(key="dps", label="DPS")
    d = Doctrine.objects.create(name="Rifter Fleet", category=cat)
    DoctrineFit.objects.create(doctrine=d, name="Rifter", ship_type_id=RIFTER)

    # A spent BPC (0 runs left) does NOT cover the hull.
    spent = Blueprint.objects.create(
        owner_type=Blueprint.Owner.CORPORATION, type_id=999, product_type_id=RIFTER,
        source="esi", quantity=-2, runs=0,
    )
    assert any(g["type_id"] == RIFTER for g in services.blueprint_coverage()["gaps"])

    # Give it runs back -> now covered.
    spent.runs = 3
    spent.save(update_fields=["runs"])
    assert any(c["type_id"] == RIFTER for c in services.blueprint_coverage()["covered"])


@pytest.mark.django_db
def test_sync_industry_jobs_and_in_production(_granted, sde):
    from apps.erp.esi_import import sync_corp_industry_jobs

    rows = [
        {"job_id": 5001, "installer_id": 1001, "activity_id": 1, "blueprint_type_id": 681,
         "product_type_id": RIFTER, "runs": 10, "status": "active",
         "start_date": "2026-06-20T00:00:00Z", "end_date": "2026-06-28T00:00:00Z"},
        {"job_id": 5002, "installer_id": 1002, "activity_id": 1, "blueprint_type_id": 682,
         "product_type_id": 588, "runs": 1, "status": "delivered",
         "start_date": "2026-06-10T00:00:00Z", "end_date": "2026-06-11T00:00:00Z"},
    ]
    res = sync_corp_industry_jobs(corp_id=1, client=_PagedClient(rows))
    assert res["status"] == "ok" and res["jobs"] == 2
    assert CorpIndustryJob.objects.count() == 2

    # in_production surfaces only the active job.
    live = services.in_production()
    assert [p["job"].job_id for p in live] == [5001]
    assert live[0]["activity"] == "Manufacturing" and live[0]["runs"] == 10

    # Snapshot replace on re-sync.
    sync_corp_industry_jobs(corp_id=1, client=_PagedClient(rows[:1]))
    assert CorpIndustryJob.objects.count() == 1


@pytest.mark.django_db
def test_no_token_is_noop(monkeypatch, sde):
    from apps.erp import esi_import

    monkeypatch.setattr(esi_import, "_token_character", lambda scope: None)
    assert esi_import.sync_corp_blueprints(corp_id=1)["status"] == "no_token"
    assert esi_import.sync_corp_industry_jobs(corp_id=1)["status"] == "no_token"
    assert Blueprint.objects.count() == 0 and CorpIndustryJob.objects.count() == 0
