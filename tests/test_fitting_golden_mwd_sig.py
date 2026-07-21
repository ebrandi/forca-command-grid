"""Golden fits: propulsion-module signature bloom joins the sig stacking chain (engine v2).

Regression coverage for the MWD-signature stacking bug (WS-15 sigfix). A propulsion
module's signature bloom is client-internal in CCP's data — moduleBonusMicrowarpdrive
(effect 6730) ships an EMPTY modifierInfo, so the graph never applies it and the evaluator
(``_mobility``) does. The bug: it multiplied the bloom on as a SEPARATE factor over the
graph-evaluated signature, so it escaped the stacking penalty that ``signatureRadius`` (552,
a non-stackable attribute) applies to every OTHER percentage modifier — rig sig penalties, a
projected painter, … The fix injects the bloom as a synthetic penalised postPercent source
(``graph.inject_penalised_percent``) so ``graph._calculate`` folds it into the SAME sorted
penalised chain. pyfa (independent reference) groups the MWD + shield-rig sig penalties
together; this file proves we now do too.

Every expectation is hand-derived from the slice's base attributes and the documented EVE
stacking maths — never read back from the engine. The stacking penalty on the i-th strongest
modifier of a chain is S(i) = exp(-(i/2.67)²), reproduced below exactly as graph._calculate
applies it (``_PENALTY_FACTOR ** (i*i)``).

Fixture: tests/fixtures/fitting/caracal_mwd_sig.json — a Caracal, the +500% compact
50MN MWD, the +10%-drawback Medium Core Defense Field Extender II shield rig (reduced to +5%
by Shield Rigging V, from the shared skills_core slice), and a +30% Target Painter II.
"""
from __future__ import annotations

import math

import pytest

from apps.fitting.engine.types import (
    FitInput,
    ModuleInput,
    ModuleState,
    OperatingProfile,
    ProjectedInput,
    SkillProfile,
    SlotKind,
)

from ._fitting_graph_utils import load_graph_fixture

pytestmark = pytest.mark.django_db

# Reproduces graph._PENALTY_FACTOR: S(i) = exp(-(i/2.67)²) = _PENALTY ** (i*i).
_PENALTY = math.exp(-((1.0 / 2.67) ** 2))

SIG_RADIUS = 552
SIG_RADIUS_BONUS = 554       # MWD / painter signatureRadiusBonus (%)
RIG_DRAWBACK = 1138          # shield-rig drawback (% sig penalty), reduced by Shield Rigging
MWD_SIG_ROLE = 1803          # hull role bonus reducing the MWD sig penalty (absent on Caracal)


def _S(i: int) -> float:
    """Stacking effectiveness of the i-th strongest (0-based) chain member."""
    return _PENALTY ** (i * i)


def _attr(type_id, attr_id):
    from apps.sde.models import SdeTypeAttribute
    return SdeTypeAttribute.objects.get(type_id=type_id, attribute_id=attr_id).value


def _has(type_id, attr_id) -> bool:
    from apps.sde.models import SdeTypeAttribute
    return SdeTypeAttribute.objects.filter(type_id=type_id, attribute_id=attr_id).exists()


@pytest.fixture()
def ids():
    return load_graph_fixture("caracal_mwd_sig")


def _mwd(ids):
    return ModuleInput(ids["50MN Y-T8 Compact Microwarpdrive"], SlotKind.MED,
                       ModuleState.ACTIVE)


def _cdfe(ids):
    return ModuleInput(ids["Medium Core Defense Field Extender II"], SlotKind.RIG,
                       ModuleState.ONLINE)


def _sig(ship, modules=(), projected=(), propulsion=False):
    """Full pass-4 signature_radius telemetry through the production adapter path, All-V so
    the shield rig's drawback reduction (Shield Rigging V) is in effect."""
    from apps.fitting.engine.adapter import FittingEngine

    fit = FitInput(ship_type_id=ship, modules=tuple(modules), projected=tuple(projected))
    res = FittingEngine().evaluate(fit, SkillProfile.omniscient(),
                                   OperatingProfile(propulsion_active=propulsion))
    return res.telemetry["mobility"]["signature_radius"]


def test_fixture_base_attributes_are_what_the_derivations_assume(ids):
    """Pin the slice's base attributes so any fixture drift fails loudly before it silently
    changes an expected number below."""
    car = ids["Caracal"]
    mwd = ids["50MN Y-T8 Compact Microwarpdrive"]
    cdfe = ids["Medium Core Defense Field Extender II"]
    painter = ids["Target Painter II"]
    assert _attr(car, SIG_RADIUS) == 125.0
    assert not _has(car, MWD_SIG_ROLE)               # Caracal has no MWD sig-penalty role bonus
    assert _attr(mwd, SIG_RADIUS_BONUS) == 500.0     # +500% bloom
    assert _attr(cdfe, RIG_DRAWBACK) == 10.0         # base +10%, halved to +5% by Shield Rigging V
    assert _attr(painter, SIG_RADIUS_BONUS) == 30.0  # +30%


# --------------------------------------------------------------------------- #
# (c) rigs alone — the two shield rigs stack-penalise among themselves.
#     Also proves Shield Rigging V reduced each rig's drawback 10% → 5% (else this fails,
#     catching a skills_core / drawback-reduction regression). Establishes the rig chain the
#     MWD must JOIN in case (a).
# --------------------------------------------------------------------------- #
def test_two_shield_rigs_alone(ids):
    base = _attr(ids["Caracal"], SIG_RADIUS)                 # 125
    rig = 0.05                                               # +5% each (10% base × Shield Rigging V)
    # Both rigs equal magnitude → chain [5%, 5%]: first at full, second × S(1).
    expected = base * (1.0 + rig * _S(0)) * (1.0 + rig * _S(1))
    assert expected == pytest.approx(136.9536, abs=1e-3)
    assert _sig(ids["Caracal"], [_cdfe(ids), _cdfe(ids)], propulsion=False) \
        == round(expected, 1)


# --------------------------------------------------------------------------- #
# (b) MWD alone — a single-member chain never penalises; the +500% bloom applies in full.
# --------------------------------------------------------------------------- #
def test_mwd_alone_unpenalised(ids):
    base = _attr(ids["Caracal"], SIG_RADIUS)                 # 125
    bloom = _attr(ids["50MN Y-T8 Compact Microwarpdrive"], SIG_RADIUS_BONUS) / 100.0  # 5.0
    expected = base * (1.0 + bloom * _S(0))                  # 125 × 6.0 = 750
    assert expected == 750.0
    assert _sig(ids["Caracal"], [_mwd(ids)], propulsion=True) == round(expected, 1)


# --------------------------------------------------------------------------- #
# (a) MWD + two shield rigs — THE BUG. All three sig % modifiers share ONE stacking chain,
#     sorted by magnitude: [500%, 5%, 5%]. The MWD, strongest, takes S(0); the rigs take
#     S(1)/S(2). Combined factor 6.43935 (NOT the old 6.5734 = penalised-rigs × separate ×6).
# --------------------------------------------------------------------------- #
def test_mwd_and_two_shield_rigs_share_one_stacking_chain(ids):
    base = _attr(ids["Caracal"], SIG_RADIUS)                 # 125
    # Sorted chain (strongest first): MWD +500% then the two rigs +5% each.
    chain = [5.00, 0.05, 0.05]
    factor = 1.0
    for i, v in enumerate(chain):
        factor *= 1.0 + v * _S(i)
    # 6.0 × (1 + 0.05·0.869141) × (1 + 0.05·0.570617) = 6.0 × 1.043457 × 1.028531 = 6.439350
    assert factor == pytest.approx(6.439350, abs=1e-5)
    expected = base * factor
    assert expected == pytest.approx(804.9187, abs=1e-3)
    assert _sig(ids["Caracal"], [_mwd(ids), _cdfe(ids), _cdfe(ids)], propulsion=True) \
        == round(expected, 1)

    # The OLD (buggy) behaviour penalised the rigs among themselves and multiplied the MWD on
    # OUTSIDE the chain — strictly larger signature. The fix must be strictly smaller than that
    # and strictly larger than a naive fully-unpenalised product.
    old_buggy = base * (1.0 + 0.05 * _S(0)) * (1.0 + 0.05 * _S(1)) * 6.0   # 821.72
    naive_unpenalised = base * 6.0 * 1.05 * 1.05                            # 826.87
    assert expected < old_buggy < naive_unpenalised


# --------------------------------------------------------------------------- #
# (d) EWAR interplay — a projected target painter's sig bonus and the MWD bloom share the
#     chain too. Sorted [500%, 30%]: MWD at S(0), painter at S(1). (Projecting a painter onto
#     the fit itself is the test construct that exercises the stacking interaction; the painter
#     is an unbonused attacker, so its base +30% enters the chain, targetPainterResistance 1.0.)
# --------------------------------------------------------------------------- #
def test_mwd_and_projected_painter_share_one_stacking_chain(ids):
    base = _attr(ids["Caracal"], SIG_RADIUS)                 # 125
    bloom = _attr(ids["50MN Y-T8 Compact Microwarpdrive"], SIG_RADIUS_BONUS) / 100.0  # 5.0
    paint = _attr(ids["Target Painter II"], SIG_RADIUS_BONUS) / 100.0                 # 0.30
    # Sorted chain [500%, 30%]: MWD strongest (S0), painter second (S1).
    expected = base * (1.0 + bloom * _S(0)) * (1.0 + paint * _S(1))
    assert expected == pytest.approx(945.5520, abs=1e-3)
    got = _sig(ids["Caracal"], [_mwd(ids)],
               projected=[ProjectedInput(ids["Target Painter II"])], propulsion=True)
    assert got == round(expected, 1)
    # Must be penalised: strictly below the naive unpenalised product (125 × 6.0 × 1.30 = 975).
    assert got < base * (1.0 + bloom) * (1.0 + paint)
