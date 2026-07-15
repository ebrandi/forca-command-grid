"""Phase 0 — the provider/registry/pipeline engine.

The keystone is ``test_engine_v2_equals_v1``: a frozen copy of the v1 composition
(over the same verbatim source functions) must equal the new pipeline output on a
non-trivial fixture — byte-for-byte, the merge gate for the refactor. Plus registry
discovery, provider isolation, and the score-helper units.
"""
from __future__ import annotations

import pytest

from apps.characters.models import CharacterSkillSnapshot
from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit, SkillRequirement
from apps.readiness.engine import base, pipeline, registry
from apps.readiness.engine.base import (
    DimensionResult,
    combine,
    ratio_score,
    status_for,
    threshold_score,
)
from apps.readiness.services import compute_readiness
from apps.sso.models import EveCharacter

GUNNERY = 3300
RIFTER = 587


def _doctrine(name, priority, req_level):
    cat, _ = DoctrineCategory.objects.get_or_create(key="dps", label="DPS")
    d = Doctrine.objects.create(name=name, category=cat, priority=priority)
    fit = DoctrineFit.objects.create(doctrine=d, name=name, ship_type_id=RIFTER)
    SkillRequirement.objects.create(
        fit=fit, skill_type_id=GUNNERY, min_level=req_level, optimal_level=req_level
    )
    return d


def _char(django_user_model, cid, gunnery_level=None):
    user = django_user_model.objects.create(username=f"eve:{cid}")
    ch = EveCharacter.objects.create(
        character_id=cid, user=user, name=f"P{cid}", is_corp_member=True
    )
    if gunnery_level is not None:
        CharacterSkillSnapshot.objects.create(
            character=ch, is_latest=True,
            skills={str(GUNNERY): {"trained_level": gunnery_level, "sp": 0}},
        )
    return ch


def _v1_reference(characters) -> dict:
    """A FROZEN copy of the pre-refactor ``_compute_readiness_uncached`` composition.

    It calls the (verbatim) source functions and combines them exactly as v1 did:
    equal-weight mean of non-None scores, gaps = doctrine ++ stock sorted by weight,
    capped at 8. The new pipeline must reproduce this on any fixture.
    """
    from apps.readiness.dimensions.sources import doctrine_and_skill, stock_and_logistics

    dims, coverage, doctrine_gaps, _per_doctrine = doctrine_and_skill(characters)
    stock_dims, stock_gaps = stock_and_logistics()
    dims = {**dims, **stock_dims}
    scored = [v for v in dims.values() if v is not None]
    index = round(sum(scored) / len(scored)) if scored else 0
    gaps = sorted(doctrine_gaps + stock_gaps, key=lambda g: -g["weight"])[:8]
    # The gap dicts the source functions hand back now also carry the Seam-B scaffold key +
    # params (``label_key``/``label_params``/``task_title_key``/``task_title_params``) for the
    # persisted risk register. Those are an INTERNAL hand-off to ``Finding``; the v1 dashboard
    # payload is built by ``Finding.as_gap()`` and must keep exactly the six frozen v1 keys.
    # Projecting to that key set here is what makes this assertion prove it — if a *_key ever
    # leaked into the payload, ``set(actual) - set(expected)`` and this equality would catch it.
    v1_gap_keys = ("kind", "ref_id", "label", "weight", "task_type", "task_title")
    gaps = [{k: g[k] for k in v1_gap_keys} for g in gaps]
    return {"index": index, "dimensions": dims, "coverage": coverage, "gaps": gaps}


@pytest.mark.django_db
def test_engine_v2_equals_v1(django_user_model, sde):
    # A non-trivial fixture: two doctrines of different priority, pilots who can fly
    # both / one / none, and an unknown (no skill import) who must not be counted.
    _doctrine("Core DPS", 100, 3)
    _doctrine("Heavy", 50, 5)
    _char(django_user_model, 8001, 5)   # flies both
    _char(django_user_model, 8002, 3)   # flies Core only
    _char(django_user_model, 8003, 1)   # flies neither (known)
    _char(django_user_model, 8004, None)  # unknown — excluded from denominators

    from apps.readiness.dimensions.sources import corp_characters

    expected = _v1_reference(corp_characters())
    actual = compute_readiness(use_cache=False)

    # (a) Composition check: the pipeline's orchestration equals the frozen v1
    #     composition over the (verbatim) source functions. The payload also carries
    #     an additive ``kpis`` per-KPI breakdown (FR4) absent from the frozen v1 shape,
    #     so the golden equality is taken over v1's keys (``kpis`` is checked in the
    #     snapshot/KPI test instead).
    # v2 gaps additionally carry the Seam-B render keys (label_key/label_params/
    # task_title_key/task_title_params) so the dashboard can re-localise; _v1_reference
    # strips its gaps to the six frozen v1 keys, so strip the engine's gaps the same way
    # before the golden equality (mirrors the additive top-level ``kpis`` handling below).
    _v1_gap_keys = ("kind", "ref_id", "label", "weight", "task_type", "task_title")
    actual_cmp = {**actual, "gaps": [{k: g[k] for k in _v1_gap_keys} for g in actual["gaps"]]}
    assert {k: actual_cmp[k] for k in expected} == expected
    assert set(actual) - set(expected) == {"kpis"}

    # (b) Independent oracle: numbers derived BY HAND from the v1 formulas (NOT from
    #     the moved source code), so a future formula drift in sources.py — which
    #     would move oracle (a) and the engine together — is still caught here.
    #     Core (prio 100, req L3): pilots 8001/8002 fly, 8003 can't, 8004 unknown →
    #     2/3 ready; Heavy (prio 50, req L5): only 8001 → 1/3 ready.
    #       doctrine = round(100·(100·2/3 + 50·1/3)/150) = round(55.56) = 56
    #       skill    = round(100·2/3) = 67   (8001,8002 fly something; 8003 known-not)
    #       stock    = None (no stockpile targets);  logistics = 100 (no open hauls)
    #       index    = round((56+67+100)/3) = 74
    assert actual["index"] == 74
    assert actual["dimensions"] == {
        "doctrine": 56, "skill": 67, "stock": None, "logistics": 100,
    }
    assert actual["coverage"] == {"characters": 4, "known": 3, "ready_any": 2}
    assert [(g["kind"], g["weight"], g["label"]) for g in actual["gaps"]] == [
        ("doctrine", 33.33, "Core DPS: 2/3 can fly"),
        ("doctrine", 33.33, "Heavy: 1/3 can fly"),
    ]


@pytest.mark.django_db
def test_persisted_snapshot_carries_phase0_columns(django_user_model, sde):
    _doctrine("Core DPS", 100, 3)
    _char(django_user_model, 8101, 5)
    compute_readiness(persist=True, use_cache=False)
    from apps.readiness.models import ReadinessSnapshot

    snap = ReadinessSnapshot.objects.latest("created_at")
    # config_version is 0 (default unset config) with equal dimension weights.
    assert snap.config_version == 0
    assert snap.weights == {"doctrine": 1.0, "skill": 1.0, "stock": 1.0, "logistics": 1.0}
    assert snap.dimensions["doctrine"] is not None
    # The per-KPI column now carries the doctrine/combat display KPIs (Gap B) — keyed,
    # dimension-namespaced, with score/value/status.
    assert "doctrine.all_coverage" in snap.kpis
    assert "combat.flyable_members" in snap.kpis
    assert {"value", "score", "status"} <= set(snap.kpis["doctrine.all_coverage"])


def test_registry_discovery():
    # The four built-in providers self-register (the app's ready() imports them).
    import apps.readiness.dimensions  # noqa: F401  (ensure import side effect)

    keys = registry.keys()
    for expected in ("doctrine", "skill", "stock", "logistics"):
        assert expected in keys
        assert registry.get(expected) is not None


@pytest.mark.django_db
def test_provider_isolation_degrades_one_dimension(django_user_model, sde):
    _doctrine("Core DPS", 100, 3)
    _char(django_user_model, 8201, 5)

    class _Boom:
        key = "boom"
        label = "Boom"
        default_weight = 1.0
        data_sources: list[str] = []

        def compute(self, ctx):
            raise RuntimeError("provider blew up")

    from apps.readiness.dimensions import sources

    registry.register(_Boom())
    try:
        ctx = sources.build_context({})
        result = pipeline.run(ctx, {})
    finally:
        registry.unregister("boom")

    # The failing provider yields a None score (excluded from the index), and the
    # run still completes with the healthy dimensions scored.
    assert result["dimensions"]["boom"] is None
    assert result["dimensions"]["doctrine"] is not None
    assert 0 <= result["index"] <= 100


def test_ratio_score():
    assert ratio_score(1, 2) == 50
    assert ratio_score(3, 3) == 100
    assert ratio_score(0, 4) == 0
    assert ratio_score(0, 0) is None  # honest score: empty sample is unavailable


def test_threshold_score_directions():
    # higher_is_better: amber edge → 100, red edge → 0, linear between, clamped.
    assert threshold_score(10, amber=10, red=0) == 100
    assert threshold_score(0, amber=10, red=0) == 0
    assert threshold_score(5, amber=10, red=0) == 50
    assert threshold_score(20, amber=10, red=0) == 100  # clamp high
    # lower_is_better: amber (low, good) → 100, red (high, bad) → 0.
    assert threshold_score(0, amber=0, red=10, direction="lower_is_better") == 100
    assert threshold_score(10, amber=0, red=10, direction="lower_is_better") == 0
    assert threshold_score(5, amber=0, red=10, direction="lower_is_better") == 50
    assert threshold_score(None, amber=10, red=0) is None


def test_status_for():
    assert status_for(90) == base.GREEN
    assert status_for(60) == base.AMBER
    assert status_for(10) == base.RED
    assert status_for(None) == base.UNAVAILABLE


def test_combine_weighted_mean_excludes_unavailable():
    rows = [
        DimensionResult(key="a", score=100),
        DimensionResult(key="b", score=50),
        DimensionResult(key="c", score=None),  # unavailable — not a zero
    ]
    # Equal default weights → plain mean of the two available scores.
    assert combine(rows, {}) == 75
    # A configured weight shifts the mean.
    assert combine(rows, {"weights": {"a": 3.0, "b": 1.0}}) == round((300 + 50) / 4)
    # Nothing available → 0.
    assert combine([DimensionResult(key="x", score=None)], {}) == 0
