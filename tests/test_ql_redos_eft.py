"""EFT paste parser: input bounds and linear-time line patterns (CodeQL py/polynomial-redos).

``services.import_eft`` is reached straight from a POST body (``apps/fitting/views.py``
``import_eft`` reads ``request.POST["eft"]``), so every per-line pattern it runs is an
attacker-chosen-input sink. ``_MAX_LINES`` has always bounded the number of lines, but
nothing bounded the LENGTH of one line, and ``_MUT_REF_RE`` used to be written
``\\s*\\[(\\d+)\\]\\s*$`` — an unanchored leading ``\\s*`` that the engine retries from every
offset of a whitespace run, i.e. quadratic in line length. One 200 KB line of interior
spaces took ~17 s of pure CPU inside a single gunicorn worker (which serves four threads and
never releases the GIL during ``re``), so a handful of pastes could stall the pool.

This module pins both halves of the hardening:

* the *shape* tests time the patterns against adversarial whitespace runs, so reintroducing a
  leading ``\\s*`` (or any other ambiguous prefix) fails the build rather than silently
  restoring the quadratic;
* the *bound* tests pin ``_MAX_LINE_LEN`` — that it truncates a pathological line, and that it
  is wide enough for the widest line ``export_eft`` can legitimately emit;
* the *tokenizer* tests pin how a line is split into module / charge / quantity / mutation
  reference over realistic EFT input, so a future regex edit that quietly changes parsing
  (the failure mode that silently mis-saves real fits) is caught here.
"""
from __future__ import annotations

import time

import pytest

from apps.fitting import services
from apps.fitting.services import _MAX_LINE_LEN, _MUT_REF_RE, _QTY_RE

from ._fitting_graph_utils import load_graph_fixture

# Long enough that the old quadratic behaviour (~17 s) cannot hide inside the budget, short
# enough that a linear scan is sub-millisecond.
_ADVERSARIAL_RUN = 200_000
# Generous by two orders of magnitude against the measured linear cost (~1 ms), and still ~17x
# under the measured quadratic cost, so this is a shape assertion and not a benchmark.
_LINEAR_BUDGET_S = 1.0


def _elapsed(fn, *args) -> float:
    start = time.perf_counter()
    fn(*args)
    return time.perf_counter() - start


# --------------------------------------------------------------------------- #
# Pattern shape: linear time on adversarial whitespace
# --------------------------------------------------------------------------- #
def test_mutation_ref_pattern_is_linear_on_a_long_whitespace_run():
    """``_MUT_REF_RE`` must not backtrack over an interior whitespace run.

    ``"A" + " " * n + "A"`` survives the parser's ``.strip()`` (the whitespace is interior),
    reaches ``search``/``sub`` in ``import_eft``, and matches nothing — the worst case for a
    pattern whose first atom can repeat over whitespace.
    """
    line = "A" + " " * _ADVERSARIAL_RUN + "A"
    assert _MUT_REF_RE.search(line) is None
    assert _elapsed(_MUT_REF_RE.search, line) < _LINEAR_BUDGET_S
    assert _elapsed(_MUT_REF_RE.sub, "", line) < _LINEAR_BUDGET_S


def test_mutation_ref_pattern_is_linear_on_a_long_digit_run():
    """The ``(\\d+)`` group must not backtrack either: a long digit run followed by a
    non-``]`` character is the other way to make this pattern retry."""
    line = "[" + "1" * _ADVERSARIAL_RUN + "A"
    assert _MUT_REF_RE.search(line) is None
    assert _elapsed(_MUT_REF_RE.search, line) < _LINEAR_BUDGET_S


def test_quantity_pattern_is_linear_on_a_long_whitespace_run():
    """``_QTY_RE`` is already linear (its leading ``\\s`` is a single character, and ``\\d+``
    and ``\\s*`` match disjoint classes). Pinned here so it stays that way."""
    line = "A" + " " * _ADVERSARIAL_RUN + "A"
    assert _QTY_RE.search(line) is None
    assert _elapsed(_QTY_RE.search, line) < _LINEAR_BUDGET_S
    assert _elapsed(_QTY_RE.search, " x" + "1" * _ADVERSARIAL_RUN + "A") < _LINEAR_BUDGET_S


# --------------------------------------------------------------------------- #
# Input bound: one line cannot be arbitrarily long
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_import_eft_truncates_a_pathological_line():
    """A single multi-hundred-KB line is bounded before any pattern sees it, and the remnant
    still surfaces as an unresolved name (bounded, never silently dropped)."""
    junk = "A" + " " * _ADVERSARIAL_RUN + "A"
    parsed = services.import_eft(f"[Rifter, Bound]\n{junk}")

    assert parsed["unresolved"], "the truncated line must still be reported, not dropped"
    assert max(len(n) for n in parsed["unresolved"]) <= _MAX_LINE_LEN


@pytest.mark.django_db
def test_import_eft_bounded_paste_completes_promptly():
    """The whole parse of a maximally pathological paste stays well inside a request budget:
    ``_MAX_LINES`` lines, each a whitespace run that used to cost seconds on its own."""
    junk = "A" + " " * 20_000 + "A"
    text = "[Rifter, Bound]\n" + "\n".join([junk] * 200)
    assert _elapsed(services.import_eft, text) < 5.0


# --------------------------------------------------------------------------- #
# Input bound: the cap cannot reject anything FORCA itself can emit
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_line_cap_clears_the_widest_line_export_eft_can_emit():
    """``_MAX_OVERRIDES`` (32) mutated attributes render as ONE pyfa attribute line — the
    widest line the exporter can produce. It must round-trip through the cap untouched."""
    from apps.sde.models import SdeDogmaAttribute

    ids = load_graph_fixture("mutated")
    attr_ids = list(SdeDogmaAttribute.objects.order_by("-attribute_id")
                    .values_list("attribute_id", flat=True)[:services._MAX_OVERRIDES])
    assert len(attr_ids) == services._MAX_OVERRIDES
    overrides = {str(aid): 1.0 + i / 100 for i, aid in enumerate(attr_ids)}
    items = [{"type_id": ids["Gyrostabilizer II"], "slot": "low", "state": "active",
              "charge_type_id": None, "quantity": 1, "attr_overrides": overrides}]

    eft = services.export_eft(ids["Rifter"], items, "WideBlock")
    assert max(len(ln) for ln in eft.splitlines()) <= _MAX_LINE_LEN

    parsed = services.import_eft(eft)
    gyro = next(it for it in parsed["items"] if it["type_id"] == ids["Gyrostabilizer II"])
    assert gyro["attr_overrides"] == overrides


# --------------------------------------------------------------------------- #
# Tokenizer equivalence: parsing of realistic lines is unchanged
# --------------------------------------------------------------------------- #
@pytest.fixture()
def ids():
    return load_graph_fixture("mutated")


@pytest.mark.django_db
def test_tokenizer_pins_qty_mutation_ref_and_charge(ids):
    """One paste covering every line form the rack loop understands: module+charge, a bare
    charge with ``xN``, a mutation ``[N]`` reference, ``xN`` and ``[N]`` together, and a plain
    line with neither."""
    eft = (
        "[Rifter, Pin]\n"
        "150mm Light AutoCannon II, Republic Fleet EMP S\n"
        "Gyrostabilizer II\n"
        "Gyrostabilizer II [1]\n"
        "Abyssal Gyrostabilizer x2 [2]\n"
        "Republic Fleet EMP S x7\n"
        "\n"
        "[1] Gyrostabilizer II\n"
        "  Unknown Mutaplasmid\n"
        "  damageMultiplier 1.35\n"
        "\n"
        "[2] Abyssal Gyrostabilizer\n"
        "  Unknown Mutaplasmid\n"
        "  damageMultiplier 1.5, speedMultiplier 0.9\n"
    )
    parsed = services.import_eft(eft)

    assert parsed["ship_type_id"] == ids["Rifter"]
    assert parsed["fit_name"] == "Pin"
    assert parsed["unresolved"] == []

    gun, gyro = ids["150mm Light AutoCannon II"], ids["Gyrostabilizer II"]
    abyssal, ammo = ids["Abyssal Gyrostabilizer"], ids["Republic Fleet EMP S"]

    # module + charge, comma-split, charge resolved onto the module
    assert {"type_id": gun, "slot": "high", "state": "active",
            "charge_type_id": ammo, "quantity": 1} in parsed["items"]
    # plain line: no quantity, no overrides
    assert {"type_id": gyro, "slot": "low", "state": "active",
            "charge_type_id": None, "quantity": 1} in parsed["items"]
    # trailing " [1]" is consumed as a mutation reference, not as part of the name
    assert {"type_id": gyro, "slot": "low", "state": "active", "charge_type_id": None,
            "quantity": 1, "attr_overrides": {"64": 1.35}} in parsed["items"]
    # " x2 [2]": a rack module expands to N single items, each carrying the overrides
    expanded = [it for it in parsed["items"] if it["type_id"] == abyssal]
    assert expanded == [{"type_id": abyssal, "slot": "low", "state": "active",
                         "charge_type_id": None, "quantity": 1,
                         "attr_overrides": {"64": 1.5, "204": 0.9}}] * 2
    # bare charge line with "xN" → cargo stack of N
    assert {"type_id": ammo, "slot": "cargo", "state": "offline",
            "charge_type_id": None, "quantity": 7} in parsed["items"]


@pytest.mark.django_db
def test_tokenizer_pins_charge_split_on_a_resolvable_module(ids):
    """The text after the comma is the charge, and it is looked up separately: a known module
    with an unknown charge keeps the module and reports the charge name verbatim — including
    when an ``xN`` suffix follows the charge."""
    eft = ("[Rifter, Pin]\n"
           "150mm Light AutoCannon II, No Such Ammo\n"
           "150mm Light AutoCannon II, Other Missing Ammo x2\n")
    parsed = services.import_eft(eft)

    assert parsed["unresolved"] == ["No Such Ammo", "Other Missing Ammo"]
    guns = [it for it in parsed["items"] if it["type_id"] == ids["150mm Light AutoCannon II"]]
    assert len(guns) == 3            # 1 from the first line, 2 from the "x2" expansion
    assert all(it["charge_type_id"] is None and it["slot"] == "high" for it in guns)


@pytest.mark.django_db
def test_tokenizer_pins_unresolved_names_verbatim():
    """With nothing resolvable in the SDE, ``unresolved`` exposes the module-name tokenizer
    directly: the quantity and mutation-reference suffixes are stripped and the comma splits
    module from charge, with no stray whitespace left behind.

    Only the MODULE name is reported when it is itself unresolvable (``import_eft`` skips the
    entry before it ever looks the charge up) — pinned as-is, it is the existing contract.
    """
    eft = (
        "[No Such Hull, Pin]\n"
        "Alpha Module, Beta Charge\n"
        "Gamma Module x3\n"
        "Delta Module [4]\n"
        "Epsilon Module, Zeta Charge x5 [6]\n"
        "Eta Module\n"
    )
    parsed = services.import_eft(eft)

    assert parsed["ship_type_id"] is None
    assert parsed["unresolved"] == [
        "Alpha Module", "Gamma Module", "Delta Module", "Epsilon Module", "Eta Module",
    ]


@pytest.mark.django_db
def test_tokenizer_pins_no_trailing_whitespace_before_a_mutation_ref():
    """Extra spaces in front of a ``[N]`` reference (or an ``xN``) are absorbed, exactly as
    before the pattern was re-anchored — the module name must not keep them."""
    eft = ("[No Such Hull, Pin]\n"
           "Wide Spaced Module   [7]\n"
           "Other Module    x9\n"
           "Third Module   x2   [8]\n")
    parsed = services.import_eft(eft)
    assert parsed["unresolved"] == ["Wide Spaced Module", "Other Module", "Third Module"]
