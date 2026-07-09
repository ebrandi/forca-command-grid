"""Static domain constants for Planetary Industry.

These are stable EVE facts (planet type_ids, the five trade hubs, tier metadata).
Production *recipes* are NOT here — those are data (``PiSchematic`` rows) loaded from
the SDE so they refresh when CCP changes them. Only genuinely fixed, tiny lookups live
in code.
"""
from __future__ import annotations

# --- Trade hubs -------------------------------------------------------------
# The codebase has no hub list anywhere (see market app — only THE_FORGE is
# hard-coded). We define the canonical five so a pilot can pick a pricing hub.
# Region ids are what the market history/pricing layer keys on.
THE_FORGE = 10000002

TRADE_HUBS = [
    {"region_id": 10000002, "name": "The Forge", "hub": "Jita", "system_id": 30000142},
    {"region_id": 10000043, "name": "Domain", "hub": "Amarr", "system_id": 30002187},
    {"region_id": 10000032, "name": "Sinq Laison", "hub": "Dodixie", "system_id": 30002659},
    {"region_id": 10000030, "name": "Heimatar", "hub": "Rens", "system_id": 30002510},
    {"region_id": 10000042, "name": "Metropolis", "hub": "Hek", "system_id": 30002053},
]
TRADE_HUBS_BY_REGION = {h["region_id"]: h for h in TRADE_HUBS}


def hub_label(region_id: int | None) -> str:
    """Human label for a pricing region, e.g. ``The Forge (Jita)``."""
    hub = TRADE_HUBS_BY_REGION.get(region_id or THE_FORGE)
    return f"{hub['name']} ({hub['hub']})" if hub else "Custom region"


# --- SDE categories that hold PI materials ----------------------------------
CATEGORY_PLANETARY_RESOURCES = 42   # the 15 P0 raw resources
CATEGORY_PLANETARY_COMMODITIES = 43  # P1..P4 processed/refined/specialised/advanced
PI_CATEGORY_IDS = (CATEGORY_PLANETARY_RESOURCES, CATEGORY_PLANETARY_COMMODITIES)

# Tier is derived from the SDE market group. cat-42 (Planetary Resources) is
# always P0. cat-43 (Planetary Commodities) splits into four groups; we key on the
# stable group_id first and fall back to the "Tier N" in the group name. If neither
# resolves we leave tier blank (a visible, honest gap) rather than guessing.
PI_GROUP_TIER_BY_ID = {
    1032: "P0", 1033: "P0", 1035: "P0",   # raw-resource groups (solid / liquid-gas / organic)
    1042: "P1",   # Basic Commodities - Tier 1
    1034: "P2",   # Refined Commodities - Tier 2
    1040: "P3",   # Specialized Commodities - Tier 3
    1041: "P4",   # Advanced Commodities - Tier 4
}


def tier_for(category_id: int, group_id: int, group_name: str = "") -> str:
    """Best-effort tier for a PI material from its SDE category/group."""
    if group_id in PI_GROUP_TIER_BY_ID:
        return PI_GROUP_TIER_BY_ID[group_id]
    if category_id == CATEGORY_PLANETARY_RESOURCES:
        return "P0"
    for n in (1, 2, 3, 4):
        if f"Tier {n}" in group_name:
            return f"P{n}"
    return ""

# --- Tier metadata (didactic) ----------------------------------------------
TIER_META = {
    "P0": {
        "label": "Raw resource",
        "short": "Pulled straight from a planet by an Extractor Control Unit.",
        "blurb": "The 15 raw materials. You never sell these — you refine them on-planet "
                 "into P1. Extraction rate depends on your ECU program and the planet's "
                 "resource hotspots, so it's the one number you should tune to your colony.",
        "facility": "Extractor Control Unit (ECU) + extractor heads",
    },
    "P1": {
        "label": "Processed material",
        "short": "One P0 refined in a Basic Industry Facility.",
        "blurb": "15 processed materials. A Basic Industry Facility turns 3,000 of one P0 "
                 "into 20 P1 every 30 minutes. P1 is the first thing worth selling, and the "
                 "feedstock for everything above it.",
        "facility": "Basic Industry Facility (BIF)",
    },
    "P2": {
        "label": "Refined commodity",
        "short": "Two P1 combined in an Advanced Industry Facility.",
        "blurb": "Refined commodities combine two P1 inputs. These are the bread-and-butter "
                 "of profitable PI — good ISK/m³ and steady demand (Coolant, Mechanical Parts, "
                 "Consumer Electronics, Enriched Uranium…).",
        "facility": "Advanced Industry Facility (AIF)",
    },
    "P3": {
        "label": "Specialised commodity",
        "short": "Several P2 combined into a high-value good.",
        "blurb": "Specialised commodities (Robotics, Coolant→…, Guidance Systems, "
                 "Transcranial Microcontrollers…). High value per unit but they need a "
                 "multi-planet supply chain or a market to buy the P2 inputs.",
        "facility": "Advanced Industry Facility (AIF)",
    },
    "P4": {
        "label": "Advanced commodity",
        "short": "The end of the chain — mixes P3 (and some P1) into strategic goods.",
        "blurb": "Advanced commodities (Nano-Factory, Broadcast Node, Self-Harmonising "
                 "Power Core, Wetware Mainframe) feed sovereignty structures and capital "
                 "construction. Only worth it with a dedicated multi-character operation.",
        "facility": "High-Tech Production Plant",
    },
}
TIER_ORDER = ["P0", "P1", "P2", "P3", "P4"]


# --- Planning-assumption defaults ------------------------------------------
# These are the honest, tunable planning knobs. Physical throughput in PI depends
# on in-game extractor placement, resource density and how many facilities you fit
# on each planet — none of which the SDE or ESI expose cleanly — so the planner
# treats them as *labelled estimates* the pilot can override per planet. The value
# of the tool is in the (exact) chain, price, tax and hauling maths on top.
DEFAULT_EXTRACTION_RATE_PER_HOUR = 2000     # P0 units/hour on one extraction planet
SECONDS_PER_DAY = 86400

# Fallback "units produced per day on one planet" by the tier that planet exports,
# used when a plan has no per-planet override and we can't derive it from extraction.
# P0/P1 are derived from the extraction rate (exact schematic ratio); P2+ are the
# labelled factory-planet estimates.
DEFAULT_FACTORY_OUTPUT_PER_DAY = {"P2": 720, "P3": 120, "P4": 24}

# Default economic assumptions (percent unless noted). Seeded onto PlanetaryConfig
# and copied onto each plan at creation so a plan is a self-contained snapshot.
DEFAULT_CUSTOMS_EXPORT_TAX = 5.0    # % of item base value charged at the POCO on export
DEFAULT_CUSTOMS_IMPORT_TAX = 5.0    # % on import
DEFAULT_SALES_TAX = 4.5             # % market sales tax
DEFAULT_BROKER_FEE = 3.0           # % broker fee (only on sell orders, not immediate sells)
DEFAULT_HAULING_COST_PER_M3 = 0.0   # ISK/m³ to move goods to the hub (0 = self-haul)
DEFAULT_CORP_BUYBACK_RATE = 90.0    # % of Jita sell paid by corp buyback

