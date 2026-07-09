"""Jump-capable ship capability profiles — the single source of truth.

Every routing decision (can this hull cyno into high-sec? use stargates? which
isotope does it burn? how much fuel per light-year?) is answered from a
``ShipProfile``, never from a hull name string. There are no ``if ship ==
"Rhea"`` checks anywhere in the planner: the capability matrix here is what the
route-mode resolver and fuel calculator consult.

Fuel and range figures are the *real* EVE dogma attributes, verified 2026-07
against CCP's Static Data Export and the EVE University wiki:

* ``jumpDriveConsumptionAmount`` (attr 868) → :attr:`ShipProfile.base_fuel_per_ly`
* ``jumpDriveRange`` (attr 867)            → :attr:`ShipProfile.base_range_ly`
* ``jumpDriveConsumptionType`` (attr 866)  → :attr:`ShipProfile.isotope_type_id`

See docs/navigation/jump-ship-capabilities.md and jump-fuel-calculation.md for
the derivation and sources.
"""
from __future__ import annotations

from dataclasses import dataclass

# --- Isotope fuel types (SDE type ids) --------------------------------------
HELIUM = 16274
NITROGEN = 17888
OXYGEN = 17887
HYDROGEN = 17889
ISOTOPE_NAMES = {
    HELIUM: "Helium Isotopes",
    NITROGEN: "Nitrogen Isotopes",
    OXYGEN: "Oxygen Isotopes",
    HYDROGEN: "Hydrogen Isotopes",
}

# --- Cyno type a hull jumps *to* --------------------------------------------
CYNO_NORMAL = "normal"
CYNO_COVERT = "covert"
CYNO_INDUSTRIAL = "industrial"
CYNO_LABELS = {
    CYNO_NORMAL: "Cynosural Field",
    CYNO_COVERT: "Covert Cyno",
    CYNO_INDUSTRIAL: "Industrial Cyno",
}

# --- Security bands ---------------------------------------------------------
HIGHSEC, LOWSEC, NULLSEC = "highsec", "lowsec", "nullsec"


@dataclass(frozen=True)
class ShipProfile:
    """What a hull (or route profile) can legally do, and what its jump drive costs.

    The four ``can_*_highsec`` / ``can_start_from_highsec`` flags encode the two
    distinct high-sec rules that trip pilots (and the old planner) up:

    * **Cyno rule** — no ship can light a cyno in high-sec, so *no* jump-drive
      ship can jump *into* or *out of* high-sec (``can_cyno_into_highsec`` and
      ``can_start_from_highsec`` are always False). A high-sec leg is therefore
      always a *stargate* leg.
    * **Gate rule** — a *separate* restriction on who may use stargates into
      high-sec. Jump Freighters and Black Ops can (``can_gate_highsec``); true
      capitals/supers cannot. Supercarriers and Titans can't use stargates at
      all (``can_use_gates`` is False for them).
    """

    key: str
    label: str
    type_id: int
    ship_class: str          # machine key: jump_freighter / black_ops / rorqual / carrier / …
    class_label: str
    isotope_type_id: int
    base_fuel_per_ly: float  # dogma 868
    base_range_ly: float     # dogma 867
    fatigue_factor: float    # jump-fatigue distance multiplier (JF/industrial 0.10, blops 0.25)
    cyno_type: str
    has_jump_drive: bool = True
    jf_skill: bool = False       # the Jump Freighters skill fuel reduction applies (JF hulls only)
    rig_eligible: bool = False   # Jump Drive Economizer rigs fit (Jump Freighters + Rorqual only)
    can_use_gates: bool = True   # can traverse a stargate at all (low/null)
    can_gate_highsec: bool = False  # can enter/transit HIGH-sec on stargates
    # Invariants shared by every jump ship, stated explicitly rather than assumed:
    # a cyno can't be lit in high-sec, so a jump can neither end nor start there.
    can_cyno_into_highsec: bool = False
    can_start_from_highsec: bool = False

    # -- convenience -----------------------------------------------------------
    @property
    def isotope_name(self) -> str:
        return ISOTOPE_NAMES.get(self.isotope_type_id, "Isotopes")

    @property
    def cyno_label(self) -> str:
        return CYNO_LABELS.get(self.cyno_type, "Cyno")

    @property
    def reaches_highsec(self) -> bool:
        """Can this hull reach a high-sec system at all? Only via stargates."""
        return self.can_gate_highsec

    def can_jump_to_band(self, band: str) -> bool:
        """A jump-drive leg can only touch low/null (never high, w-space, Pochven)."""
        return self.has_jump_drive and band in (LOWSEC, NULLSEC)

    def can_gate_band(self, band: str) -> bool:
        if band == HIGHSEC:
            return self.can_gate_highsec
        return self.can_use_gates

    # Back-compat: the old ship table was a plain dict; a couple of callers still
    # subscript it (``ship["range"]``). Support read-only item access so those
    # keep working without a flag-day change.
    def __getitem__(self, item: str):  # pragma: no cover - trivial shim
        alias = {"range": "base_range_ly", "fuel": "base_fuel_per_ly"}.get(item, item)
        return getattr(self, alias)


# --- Catalogue builders -----------------------------------------------------
def _jf(key, label, type_id, isotope, fuel) -> ShipProfile:
    return ShipProfile(
        key=key, label=label, type_id=type_id, ship_class="jump_freighter",
        class_label="Jump Freighter", isotope_type_id=isotope, base_fuel_per_ly=fuel,
        base_range_ly=5.0, fatigue_factor=0.10, cyno_type=CYNO_INDUSTRIAL,
        jf_skill=True, rig_eligible=True, can_use_gates=True, can_gate_highsec=True,
    )


def _blops(key, label, type_id, isotope) -> ShipProfile:
    return ShipProfile(
        key=key, label=label, type_id=type_id, ship_class="black_ops",
        class_label="Black Ops Battleship", isotope_type_id=isotope, base_fuel_per_ly=700.0,
        base_range_ly=4.0, fatigue_factor=0.25, cyno_type=CYNO_COVERT,
        jf_skill=False, rig_eligible=False, can_use_gates=True, can_gate_highsec=True,
    )


def _cap(key, label, type_id, isotope, ship_class, class_label, base_range,
         *, fuel=3000.0, gates=True, fatigue=1.0, cyno=CYNO_NORMAL, rig=False) -> ShipProfile:
    # True capitals may use low/null stargates but never high-sec; supercapitals
    # (gates=False) can't use stargates at all. None can reach high-sec.
    return ShipProfile(
        key=key, label=label, type_id=type_id, ship_class=ship_class, class_label=class_label,
        isotope_type_id=isotope, base_fuel_per_ly=fuel, base_range_ly=base_range,
        fatigue_factor=fatigue, cyno_type=cyno, jf_skill=False, rig_eligible=rig,
        can_use_gates=gates, can_gate_highsec=False,
    )


# Grouped for the ship selector; each hull carries the correct isotope so fuel
# and shopping lists are exact per racial hull.
SHIP_GROUPS: list[tuple[str, list[ShipProfile]]] = [
    ("Jump Freighters", [
        _jf("ark", "Ark", 28850, HELIUM, 8800.0),
        _jf("rhea", "Rhea", 28844, NITROGEN, 10000.0),
        _jf("anshar", "Anshar", 28848, OXYGEN, 9400.0),
        _jf("nomad", "Nomad", 28846, HYDROGEN, 8200.0),
    ]),
    ("Black Ops", [
        _blops("redeemer", "Redeemer", 22428, HELIUM),
        _blops("sin", "Sin", 22430, OXYGEN),
        _blops("widow", "Widow", 22436, NITROGEN),
        _blops("panther", "Panther", 22440, HYDROGEN),
        _blops("marshal", "Marshal", 44996, HELIUM),
    ]),
    ("Capital Industrial", [
        _cap("rorqual", "Rorqual", 28352, OXYGEN, "rorqual", "Capital Industrial Ship", 5.0,
             fuel=4000.0, gates=True, fatigue=0.10, cyno=CYNO_INDUSTRIAL, rig=True),
    ]),
    ("Carriers", [
        _cap("archon", "Archon", 23757, HELIUM, "carrier", "Carrier", 3.5),
        _cap("chimera", "Chimera", 23915, NITROGEN, "carrier", "Carrier", 3.5),
        _cap("thanatos", "Thanatos", 23911, OXYGEN, "carrier", "Carrier", 3.5),
        _cap("nidhoggur", "Nidhoggur", 24483, HYDROGEN, "carrier", "Carrier", 3.5),
    ]),
    ("Force Auxiliaries", [
        _cap("apostle", "Apostle", 37604, HELIUM, "fax", "Force Auxiliary", 3.5),
        _cap("minokawa", "Minokawa", 37605, NITROGEN, "fax", "Force Auxiliary", 3.5),
        _cap("ninazu", "Ninazu", 37607, OXYGEN, "fax", "Force Auxiliary", 3.5),
        _cap("lif", "Lif", 37606, HYDROGEN, "fax", "Force Auxiliary", 3.5),
    ]),
    ("Dreadnoughts", [
        _cap("revelation", "Revelation", 19720, HELIUM, "dread", "Dreadnought", 3.5),
        _cap("phoenix", "Phoenix", 19726, NITROGEN, "dread", "Dreadnought", 3.5),
        _cap("moros", "Moros", 19724, OXYGEN, "dread", "Dreadnought", 3.5),
        _cap("naglfar", "Naglfar", 19722, HYDROGEN, "dread", "Dreadnought", 3.5),
    ]),
    ("Supercarriers", [
        _cap("aeon", "Aeon", 23919, HELIUM, "super", "Supercarrier", 3.0, gates=False),
        _cap("wyvern", "Wyvern", 23917, NITROGEN, "super", "Supercarrier", 3.0, gates=False),
        _cap("nyx", "Nyx", 23913, OXYGEN, "super", "Supercarrier", 3.0, gates=False),
        _cap("hel", "Hel", 22852, HYDROGEN, "super", "Supercarrier", 3.0, gates=False),
    ]),
    ("Titans", [
        _cap("avatar", "Avatar", 11567, HELIUM, "titan", "Titan", 3.0, gates=False),
        _cap("leviathan", "Leviathan", 3764, NITROGEN, "titan", "Titan", 3.0, gates=False),
        _cap("erebus", "Erebus", 671, OXYGEN, "titan", "Titan", 3.0, gates=False),
        _cap("ragnarok", "Ragnarok", 23773, HYDROGEN, "titan", "Titan", 3.0, gates=False),
    ]),
]

SHIP_PROFILES: dict[str, ShipProfile] = {
    p.key: p for _, group in SHIP_GROUPS for p in group
}

# Legacy class-level keys the old UI / freight pricing / tests still pass
# (``ship=jf``, ``ship=carrier`` …). Each resolves to a representative hull of
# that class so old callers keep working; new code uses the specific hull keys.
_LEGACY_ALIASES = {
    "jf": "rhea", "blops": "redeemer", "carrier": "archon", "fax": "apostle",
    "dread": "revelation", "super": "aeon", "titan": "avatar", "rorqual": "rorqual",
}

# Public lookup: real hull keys + legacy aliases.
SHIPS_BY_KEY: dict[str, ShipProfile] = dict(SHIP_PROFILES)
for _alias, _target in _LEGACY_ALIASES.items():
    SHIPS_BY_KEY.setdefault(_alias, SHIP_PROFILES[_target])

DEFAULT_SHIP_KEY = "rhea"

# Back-compat list (a couple of callers iterate the old ``JUMP_SHIPS``).
JUMP_SHIPS: list[ShipProfile] = [p for _, group in SHIP_GROUPS for p in group]


def profile_for(key: str | None) -> ShipProfile:
    """Resolve a ship key (hull or legacy alias) to a profile, defaulting to a JF."""
    return SHIPS_BY_KEY.get((key or "").strip(), SHIP_PROFILES[DEFAULT_SHIP_KEY])


# --- Jump Drive Economizer rigs (Jump Freighters + Rorqual) -----------------
# Fuel reduction for fitting the three Jump Drive Economizer rig tiers, ordered
# best-first (Prototype 10% / Experimental 7% / Limited 4%) with EVE's standard
# stacking penalty. ``jde_fuel_multiplier(n)`` returns the factor to multiply
# fuel by when ``n`` rigs (0–3) are fitted.
_JDE_BONUSES = (0.10, 0.07, 0.04)
_JDE_STACK = (1.0, 0.869, 0.571)


def jde_fuel_multiplier(n_rigs: int) -> float:
    n = max(0, min(len(_JDE_BONUSES), int(n_rigs or 0)))
    mult = 1.0
    for bonus, stack in zip(_JDE_BONUSES[:n], _JDE_STACK[:n], strict=False):
        mult *= 1.0 - bonus * stack
    return mult
