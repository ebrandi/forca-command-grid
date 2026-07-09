"""4.12 — structure/rig material efficiency wired into the calculator.

Acceptance: the manufacturing calculator applies an Engineering-Complex + rig material
bonus on top of blueprint ME, so material amounts (and cost) match real production; the
default (no structure) is byte-identical to the pre-4.12 blueprint-ME-only result.
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.industry import calc
from apps.industry.bom import material_quantity

pytestmark = pytest.mark.django_db

CRUISER = 600
TRIT = 34


def test_material_quantity_formula():
    # No structure bonus is byte-identical to the pre-4.12 signature.
    assert material_quantity(100, 1, 0) == material_quantity(100, 1, 0, 0.0) == 100
    # 2% structure bonus: ceil(100 * 1.0 * 0.98) = 98.
    assert material_quantity(100, 1, 0, 0.02) == 98
    # Combined with blueprint ME 10: ceil(100 * 0.90 * 0.98) = ceil(88.2) = 89.
    assert material_quantity(100, 1, 10, 0.02) == 89
    # Per-run floor of 1 is preserved.
    assert material_quantity(1, 1, 10, 0.042) == 1
    # Scales by runs.
    assert material_quantity(100, 3, 0, 0.02) == 98 * 3


def test_structure_bonus_reduces_estimate(priced_sde):
    base = calc.manufacturing_estimate(CRUISER, runs=1, me=0)
    boosted = calc.manufacturing_estimate(CRUISER, runs=1, me=0, structure_bonus=0.042)
    base_trit = {m["type_id"]: m["required"] for m in base["materials"]}[TRIT]
    boosted_trit = {m["type_id"]: m["required"] for m in boosted["materials"]}[TRIT]
    assert boosted_trit < base_trit                       # structure ME cut materials
    assert boosted["material_cost"] < base["material_cost"]


def test_default_estimate_unchanged(priced_sde):
    # structure_bonus defaults to 0.0 → identical to before the feature.
    a = calc.manufacturing_estimate(CRUISER, runs=1, me=0)
    b = calc.manufacturing_estimate(CRUISER, runs=1, me=0, structure_bonus=0.0)
    assert a["material_cost"] == b["material_cost"]
    assert {m["type_id"]: m["required"] for m in a["materials"]} == \
           {m["type_id"]: m["required"] for m in b["materials"]}


def _member(client, django_user_model):
    from apps.identity.models import RoleAssignment
    from apps.sso.services import ensure_role
    from core import rbac
    u = django_user_model.objects.create(username="m")
    RoleAssignment.objects.create(user=u, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(u)
    return u


def test_calculator_view_accepts_structure(client, priced_sde, django_user_model):
    _member(client, django_user_model)
    resp = client.get(reverse("industry:calculator") + f"?type={CRUISER}&structure=ec_t2_lowsec")
    assert resp.status_code == 200
    assert resp.context["structure"] == "ec_t2_lowsec"
    assert b"Raitaru" in resp.content  # the preset dropdown rendered


def test_calculator_view_rejects_bad_structure(client, priced_sde, django_user_model):
    _member(client, django_user_model)
    resp = client.get(reverse("industry:calculator") + f"?type={CRUISER}&structure=evil")
    assert resp.status_code == 200
    assert resp.context["structure"] == "none"  # bogus value falls back safely
