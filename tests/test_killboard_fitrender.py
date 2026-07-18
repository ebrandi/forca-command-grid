"""KB-21 — in-house per-slot fit render + local EFT/ESI export."""
from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apps.killboard import fitrender
from apps.killboard.ingest import ingest_killmail
from apps.market.models import MarketPrice

HOME = 98000001  # FORCA home corp in test settings


def _seed_prices(prices: dict[int, int]) -> None:
    for type_id, sell_min in prices.items():
        MarketPrice.objects.create(
            type_id=type_id, location=None, profile=MarketPrice.Profile.JITA_SELL,
            sell_min=Decimal(sell_min),
        )


# A Rifter (587: 4 hi / 3 med / 3 low / 3 rig in the sample SDE) loss with items spread
# across slots so the render exercises every bucket + empty-slot padding.
BODY = {
    "killmail_id": 100001,
    "killmail_time": "2026-06-20T12:00:00Z",
    "solar_system_id": 30002053,  # Otitoh -> region 10000002
    "victim": {
        "character_id": 2001,
        "corporation_id": HOME,
        "ship_type_id": 587,
        "damage_taken": 1000,
        "items": [
            {"item_type_id": 484, "flag": 27, "quantity_destroyed": 1},   # high 0
            {"item_type_id": 484, "flag": 28, "quantity_dropped": 1},     # high 1
            {"item_type_id": 2046, "flag": 19, "quantity_destroyed": 1},  # med 0
            {"item_type_id": 2046, "flag": 11, "quantity_destroyed": 1},  # low 0
            {"item_type_id": 2046, "flag": 92, "quantity_destroyed": 1},  # rig 0
            {"item_type_id": 484, "flag": 87, "quantity_destroyed": 2},   # drone bay
            {"item_type_id": 192, "flag": 5, "quantity_dropped": 100},    # cargo
        ],
    },
    "attackers": [
        {"character_id": 3001, "corporation_id": 99, "ship_type_id": 587,
         "final_blow": True, "damage_done": 1000},
    ],
}


def _doctrine_fit_with_modules(modules):
    from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit

    cat = DoctrineCategory.objects.create(key="dps", label="DPS")
    doctrine = Doctrine.objects.create(name="Rifter Doctrine", category=cat, priority=90)
    return DoctrineFit.objects.create(
        doctrine=doctrine, name="Rifter", ship_type_id=587, modules=modules
    )


# --- slot_bucket --------------------------------------------------------------
@pytest.mark.parametrize("flag,bucket", [
    (27, "high"), (34, "high"),
    (19, "med"), (26, "med"),
    (11, "low"), (18, "low"),
    (92, "rig"), (99, "rig"),
    (125, "subsystem"), (132, "subsystem"),
    (87, "drone"),
    (89, "implant"),
    (5, "cargo"),
    (0, "other"), (1000, "other"),
])
def test_slot_bucket(flag, bucket):
    assert fitrender.slot_bucket(flag) == bucket


# --- build_fit ----------------------------------------------------------------
@pytest.mark.django_db
def test_build_fit_groups_orders_and_values(sde):
    _seed_prices({587: 380000, 484: 12000, 2046: 8000, 192: 5})
    km = ingest_killmail(100001, "h1", body=BODY)

    fit = fitrender.build_fit(km)
    by_key = {s["key"]: s for s in fit["sections"]}

    assert fit["has_slot_data"] is True
    # Every populated bucket present.
    assert set(by_key) >= {"high", "med", "low", "rig", "drone", "cargo"}

    # High slots: 2 occupied (flags 27, 28), hull capacity 4 -> 2 empties padded.
    high = by_key["high"]
    assert high["capacity"] == 4
    assert high["filled"] == 2
    assert len(high["items"]) == 4
    assert [it["empty"] for it in high["items"]] == [False, False, True, True]
    # Ordered by flag: the flag-27 row (destroyed) before the flag-28 row (dropped).
    assert high["items"][0]["destroyed"] == 1 and high["items"][0]["flag"] == 27
    assert high["items"][1]["dropped"] == 1 and high["items"][1]["flag"] == 28
    # Per-item ISK is populated from unit_value.
    assert high["items"][0]["value"] == Decimal("12000")

    # Cargo has no fixed capacity -> occupied-only, no empties.
    cargo = by_key["cargo"]
    assert cargo["capacity"] is None
    assert cargo["count"] == 1
    assert all(not it["empty"] for it in cargo["items"])
    assert cargo["items"][0]["qty"] == 100
    assert cargo["items"][0]["value"] == Decimal("500")  # 100 * 5


@pytest.mark.django_db
def test_build_fit_without_slot_data_renders_occupied_only(sde):
    """A hull with no known slot counts -> no empty-slot outlines (graceful fallback)."""
    from apps.sde.models import SdeType

    SdeType.objects.filter(type_id=587).update(
        hi_slots=None, med_slots=None, low_slots=None, rig_slots=None
    )
    km = ingest_killmail(100001, "h1", body=BODY)
    fit = fitrender.build_fit(km)
    by_key = {s["key"]: s for s in fit["sections"]}

    assert fit["has_slot_data"] is False
    assert by_key["high"]["capacity"] is None
    assert len(by_key["high"]["items"]) == 2  # occupied only, no padding
    assert all(not it["empty"] for it in by_key["high"]["items"])


@pytest.mark.django_db
def test_build_fit_off_doctrine_marker_only_with_deviation(sde):
    km = ingest_killmail(100001, "h1", body=BODY)

    # No deviation -> nothing flagged.
    plain = fitrender.build_fit(km, None)
    assert not any(it["off_doctrine"] for s in plain["sections"] for it in s["items"])

    # With a (gated) deviation naming 484 as extra -> those rows are flagged.
    deviation = SimpleNamespace(extra=[{"type_id": 484, "quantity": 1}])
    marked = fitrender.build_fit(km, deviation)
    flagged = {it["type_id"] for s in marked["sections"] for it in s["items"] if it["off_doctrine"]}
    assert flagged == {484}


# --- esi_fitting --------------------------------------------------------------
@pytest.mark.django_db
def test_esi_fitting_shape(sde):
    km = ingest_killmail(100001, "h1", body=BODY)
    esi = fitrender.esi_fitting(km)

    assert esi["ship_type_id"] == 587
    assert "Killmail 100001" in esi["name"]
    # Items carry the fitting flag + summed quantity, keyed per (type, slot).
    flags = {(i["type_id"], i["flag"]): i["quantity"] for i in esi["items"]}
    assert flags[(484, 27)] == 1
    assert flags[(484, 28)] == 1
    assert flags[(192, 5)] == 100


# --- detail page + export endpoints ------------------------------------------
@pytest.mark.django_db
def test_detail_page_renders_per_slot_fit(client, sde):
    _seed_prices({587: 380000, 484: 12000, 2046: 8000, 192: 5})
    ingest_killmail(100001, "h1", body=BODY)
    html = client.get("/killboard/100001/").content

    assert b"High slots" in html
    assert b"Mid slots" in html
    assert b"200mm AutoCannon I" in html
    assert b"empty" in html          # empty-slot outlines rendered (Rifter has capacity)
    assert b"Copy EFT" in html
    assert b"Copy ESI" in html


@pytest.mark.django_db
def test_eft_export_endpoint(client, sde):
    ingest_killmail(100001, "h1", body=BODY)
    resp = client.get("/killboard/100001/eft/")

    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/plain")
    body = resp.content.decode()
    assert body.startswith("[Rifter, Rifter]")
    assert "200mm AutoCannon I" in body


@pytest.mark.django_db
def test_fit_esi_endpoint(client, sde):
    ingest_killmail(100001, "h1", body=BODY)
    resp = client.get("/killboard/100001/fit.json")

    assert resp.status_code == 200
    data = json.loads(resp.content)
    assert data["ship_type_id"] == 587
    assert any(i["flag"] == 27 and i["type_id"] == 484 for i in data["items"])


@pytest.mark.django_db
def test_refit_to_doctrine_button_gated_on_match_and_membership(client, django_user_model, sde):
    from apps.identity.models import RoleAssignment
    from apps.sso.models import EveCharacter
    from apps.sso.services import ensure_role
    from core import rbac

    km = ingest_killmail(100001, "h1", body=BODY)
    url = "/killboard/100001/"
    marker = b"Refit to doctrine"

    # No doctrine match -> button absent even for a member.
    user = django_user_model.objects.create(username="m")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(character_id=5001, user=user, name="m",
                                is_main=True, is_corp_member=True)
    client.force_login(user)
    assert marker not in client.get(url).content

    # Attach a doctrine fit -> members see the refit button, anonymous does not.
    fit = _doctrine_fit_with_modules([{"type_id": 484, "quantity": 1, "name": "Gun"}])
    km.doctrine_fit = fit
    km.save(update_fields=["doctrine_fit"])

    assert marker in client.get(url).content   # still logged in as member
    client.logout()
    assert marker not in client.get(url).content
