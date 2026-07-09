"""T2 invention planning: probability, expected attempts, and cost per BPC.

Everything here is SDE-driven (:class:`SdeInventionProduct`, invention rows of
:class:`SdeBlueprintMaterial` for datacores, invention rows of
:class:`SdeBlueprintSkill` for skills, and :class:`SdeDecryptor`) and honest about
its assumptions: skill levels and the chosen decryptor are inputs, never guessed,
and the functions return the exact multipliers used so the UI can show the maths.

The invention success formula (the community-standard one CCP uses):

    P = P_base x (1 + (science_1 + science_2)/30 + encryption/40) x decryptor_mult

capped at 1.0. ``science_1``/``science_2`` are the two datacore-science skills
(0-5), ``encryption`` the racial Encryption Methods skill (0-5). Expected attempts
per success = 1 / P; cost per successful BPC = attempts x (datacores + decryptor);
cost per T2 unit = that / runs-per-success. A T2 BPC is ME2/TE4 by default, shifted
by the decryptor's ME/TE modifiers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from apps.market.pricing import price_for
from apps.sde.models import (
    SdeBlueprintMaterial,
    SdeBlueprintSkill,
    SdeDecryptor,
    SdeInventionProduct,
)

# A freshly invented T2 blueprint copy starts at ME 2 / TE 4 (before decryptor).
BASE_T2_ME = 2
BASE_T2_TE = 4


@dataclass
class Datacore:
    type_id: int
    quantity: int
    unit_price: Decimal


@dataclass
class InventionPath:
    """Everything needed to invent ``product_type_id`` (the manufactured T2/T3 item)."""

    product_type_id: int
    t1_blueprint_type_id: int
    t2_blueprint_type_id: int
    base_probability: float
    base_runs: int
    datacores: list[Datacore] = field(default_factory=list)
    skills: list[dict] = field(default_factory=list)  # {skill_type_id, level}

    @property
    def datacore_cost(self) -> Decimal:
        return sum((d.unit_price * d.quantity for d in self.datacores), start=Decimal("0"))


def invention_path(product_type_id: int, *, price=price_for) -> InventionPath | None:
    """Resolve the SDE invention path for a manufactured product, or ``None``.

    ``None`` means the item is not inventable from the data we have (e.g. it's a T1
    item, or invention reference data hasn't been imported yet).
    """
    ip = (
        SdeInventionProduct.objects.filter(product_type_id=product_type_id)
        .order_by("id")
        .first()
    )
    if ip is None:
        return None
    datacores = [
        Datacore(type_id=row.material_type_id, quantity=row.quantity,
                 unit_price=price(row.material_type_id))
        for row in SdeBlueprintMaterial.objects.filter(
            product_type_id=product_type_id, activity=SdeBlueprintMaterial.INVENTION
        )
    ]
    skills = [
        {"skill_type_id": s.skill_type_id, "level": s.level}
        for s in SdeBlueprintSkill.objects.filter(
            product_type_id=product_type_id, activity=SdeBlueprintSkill.INVENTION
        )
    ]
    return InventionPath(
        product_type_id=product_type_id,
        t1_blueprint_type_id=ip.t1_blueprint_type_id,
        t2_blueprint_type_id=ip.t2_blueprint_type_id,
        base_probability=float(ip.probability),
        base_runs=ip.runs or 1,
        datacores=datacores,
        skills=skills,
    )


def skill_multiplier(science_1: int = 0, science_2: int = 0, encryption: int = 0) -> float:
    """The skill portion of the invention probability multiplier (>= 1.0)."""
    s1 = max(0, min(5, science_1))
    s2 = max(0, min(5, science_2))
    enc = max(0, min(5, encryption))
    return 1.0 + (s1 + s2) / 30.0 + enc / 40.0


def effective_probability(
    base_probability: float,
    *,
    science_1: int = 0,
    science_2: int = 0,
    encryption: int = 0,
    decryptor_multiplier: float = 1.0,
) -> float:
    """Success probability after skills and (optionally) a decryptor, capped at 1.0."""
    p = base_probability * skill_multiplier(science_1, science_2, encryption) * decryptor_multiplier
    return max(0.0, min(1.0, p))


def expected_attempts(probability: float) -> float:
    """Mean invention jobs per success (geometric). ``inf`` if probability is 0."""
    if probability <= 0:
        return float("inf")
    return 1.0 / probability


def _decryptor(decryptor_type_id: int | None) -> SdeDecryptor | None:
    if not decryptor_type_id:
        return None
    return SdeDecryptor.objects.filter(type_id=decryptor_type_id).first()


def plan(
    product_type_id: int,
    *,
    science_1: int = 0,
    science_2: int = 0,
    encryption: int = 0,
    decryptor_type_id: int | None = None,
    price=price_for,
) -> dict | None:
    """Full invention plan for one item, with every assumption made explicit.

    Returns ``None`` if the item isn't inventable. Money values are ``Decimal``;
    probability/attempts are floats. Nothing here is presented as guaranteed — the
    caller should surface ``assumptions`` alongside the numbers.
    """
    path = invention_path(product_type_id, price=price)
    if path is None:
        return None

    dec = _decryptor(decryptor_type_id)
    dec_mult = float(dec.probability_multiplier) if dec else 1.0
    dec_cost = price(dec.type_id) if dec else Decimal("0")
    run_mod = dec.run_modifier if dec else 0
    me = BASE_T2_ME + (dec.me_modifier if dec else 0)
    te = BASE_T2_TE + (dec.te_modifier if dec else 0)

    prob = effective_probability(
        path.base_probability, science_1=science_1, science_2=science_2,
        encryption=encryption, decryptor_multiplier=dec_mult,
    )
    attempts = expected_attempts(prob)
    runs_per_success = max(1, path.base_runs + run_mod)

    per_attempt_cost = path.datacore_cost + dec_cost
    # Expected cost to obtain one *successful* BPC = attempts x per-attempt inputs.
    if attempts == float("inf"):
        cost_per_bpc = None
        cost_per_run = None
    else:
        cost_per_bpc = per_attempt_cost * Decimal(str(round(attempts, 4)))
        cost_per_run = cost_per_bpc / runs_per_success

    return {
        "product_type_id": product_type_id,
        "inventable": True,
        "t2_blueprint_type_id": path.t2_blueprint_type_id,
        "base_probability": path.base_probability,
        "probability": prob,
        "expected_attempts": attempts,
        "runs_per_success": runs_per_success,
        "resulting_me": me,
        "resulting_te": te,
        "datacores": [
            {"type_id": d.type_id, "quantity": d.quantity, "unit_price": d.unit_price,
             "line_cost": d.unit_price * d.quantity}
            for d in path.datacores
        ],
        "datacore_cost": path.datacore_cost,
        "decryptor": (
            {"type_id": dec.type_id, "name": dec.name, "unit_price": dec_cost,
             "probability_multiplier": float(dec.probability_multiplier),
             "run_modifier": dec.run_modifier, "me_modifier": dec.me_modifier,
             "te_modifier": dec.te_modifier}
            if dec else None
        ),
        "cost_per_attempt": per_attempt_cost,
        "cost_per_bpc": cost_per_bpc,          # expected cost for one successful BPC
        "cost_per_run": cost_per_run,          # invention cost attributable to one T2 unit
        "skills": path.skills,
        "assumptions": {
            "science_1_level": max(0, min(5, science_1)),
            "science_2_level": max(0, min(5, science_2)),
            "encryption_level": max(0, min(5, encryption)),
            "skill_multiplier": skill_multiplier(science_1, science_2, encryption),
            "note": "Probability uses the standard invention formula; skills and decryptor "
                    "are your inputs. Datacore/decryptor prices are Jita-sell estimates.",
        },
    }
