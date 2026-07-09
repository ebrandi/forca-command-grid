"""Command Intelligence — the deterministic engine (no LLM).

The provably-correct half of the two-layer engine (docs 05, 09): constraint
binding-metric goldens asserted field-by-field, the honest-``unknown`` contract
(never a fabricated number), provider isolation (one throwing provider never fails
the run), and the perturb-and-recompute impact estimator. Pure arithmetic over
synthetic snapshot dicts — DB-free.
"""
from __future__ import annotations

# Importing the package self-registers every constraint provider (the app's
# ready() does this at startup; the explicit import keeps the unit test standalone).
import apps.command_intel.constraints  # noqa: F401
from apps.command_intel import impact as impact_mod
from apps.command_intel.engine import pipeline, registry


def _by_key(constraints, key):
    for c in constraints:
        if c.key == key:
            return c
    raise AssertionError(f"constraint {key!r} not in {[c.key for c in constraints]}")


def _ferox_snapshot():
    """A primary Ferox doctrine: 22 pilots qualified, only 18 hulls staged."""
    return {"sources": {"doctrine": {"doctrines": [
        {"slug": "ferox", "name": "Ferox Fleet", "primary": True,
         "flyable": 22, "hulls_in_stock": 18, "min_pilots": 22},
    ]}}}


# --- golden constraints ------------------------------------------------------
def test_fleet_size_binds_on_scarcest_input():
    # Liebig minimum of (22 pilots, 18 hulls) -> 18, limited by hulls; demand is the
    # doctrine's own min_pilots (22), so headroom is -4 and a primary shortfall is
    # critical.
    c = _by_key(pipeline.compute_constraints(_ferox_snapshot(), {}), "fleet_size.ferox")
    assert c.binding_metric == 18
    assert c.limiting_factor == "hulls_in_stock"
    assert c.headroom == -4
    assert c.severity == "critical"
    assert c.status == "computed"


def test_fuel_runway_binds_on_soonest_structure():
    snap = {"sources": {"infrastructure": {"low_fuel_structures": [
        {"name": "Astrahus Alpha", "fuel_days_left": 9},
        {"name": "Fortizar Bravo", "fuel_days_left": 4},
    ]}}}
    c = _by_key(pipeline.compute_constraints(snap, {}), "fuel_runway")
    assert c.binding_metric == 4            # min(9, 4)
    assert c.limiting_factor == "fuel_days_left"
    assert c.severity == "high"             # 4 days: below high (7), above critical (3)
    assert c.status == "computed"


def test_srp_solvency_months_of_cover():
    snap = {"sources": {"srp": {
        "budget_isk": 10_000_000_000, "open_liability_isk": 2_000_000_000,
        "spent_period_isk": 4_000_000_000,
    }}}
    c = _by_key(pipeline.compute_constraints(snap, {}), "srp_solvency")
    assert c.binding_metric == 2.0          # (10B - 2B) / 4B = 2 months
    assert c.limiting_factor == "srp_budget"
    assert c.severity == "watch"            # 2 months sits on the watch band
    assert c.status == "computed"


def test_isk_runway_positive_cashflow_is_honest():
    # Positive 30-day net -> no burn, hence NO runway number to report. The honest
    # answer is severity 'info' with binding_metric None (never a fabricated runway).
    snap = {"sources": {"finance": {"balance_isk": 100_000_000_000, "net_30d_isk": 5_000_000_000}}}
    c = _by_key(pipeline.compute_constraints(snap, {}), "isk_runway")
    assert c.binding_metric is None
    assert c.severity == "info"
    assert c.limiting_factor == "balance_isk"


# --- honest-unknown ----------------------------------------------------------
def test_constraint_unknown_when_a_required_input_is_missing():
    # Doctrine with no `flyable` -> cannot compute a fleet size; reported unknown,
    # NOT a guessed number.
    snap = {"sources": {"doctrine": {"doctrines": [
        {"slug": "ferox", "hulls_in_stock": 18, "min_pilots": 22},
    ]}}}
    c = _by_key(pipeline.compute_constraints(snap, {}), "fleet_size.ferox")
    assert c.status == "unknown"
    assert c.binding_metric is None
    assert c.limiting_factor is None


def test_thin_snapshot_yields_unknowns_and_never_raises():
    # Slices present but empty -> the always-on runway providers answer 'unknown'
    # rather than raising.
    snap = {"sources": {"srp": {}, "finance": {}, "infrastructure": {}}}
    results = pipeline.compute_constraints(snap, {})
    assert _by_key(results, "fuel_runway").status == "unknown"
    assert _by_key(results, "srp_solvency").status == "unknown"
    assert _by_key(results, "isk_runway").status == "unknown"


# --- provider isolation ------------------------------------------------------
def test_provider_isolation_skips_a_throwing_provider():
    class _Boom:
        key = "_boom_test"
        label = "Boom"
        category = "combat"
        default_enabled = True

        def compute(self, snapshot, cfg):
            raise RuntimeError("provider blew up")

    registry.register_constraint(_Boom())
    try:
        results = pipeline.compute_constraints(_ferox_snapshot(), {})
    finally:
        registry.unregister_constraint("_boom_test")

    keys = {c.key for c in results}
    assert "fleet_size.ferox" in keys                 # healthy providers still ran
    assert not any(k.startswith("_boom") for k in keys)  # the thrower contributed nothing


# --- impact estimation -------------------------------------------------------
def test_estimate_impact_perturb_and_recompute_moves_the_binding():
    snap = _ferox_snapshot()
    ferox = _by_key(pipeline.compute_constraints(snap, {}), "fleet_size.ferox")
    impact = impact_mod.estimate_impact(ferox, snap)
    assert impact["status"] == "computed"
    assert impact["binding_before"] == 18
    assert impact["binding_after"] == 22      # staging the missing hulls clears the shortfall
    assert impact["expected_delta"]["constraints"]["fleet_size.ferox"] == 4.0


def test_candidate_impacts_cover_open_constraints():
    snap = _ferox_snapshot()
    constraints = pipeline.compute_constraints(snap, {})
    impacts = impact_mod.candidate_impacts(constraints, snap)
    keys = {i["constraint_key"] for i in impacts}
    assert "fleet_size.ferox" in keys                 # the watch+ constraint got a relief
    assert all(i["status"] == "computed" for i in impacts)
