"""Documented static PI domain data that is NOT in the schematic tables.

Production *recipes* (P0→P1→P2→P3→P4, quantities, cycle times) come from the SDE
via ``pi_schematics.json`` / EveRef — never hand-authored here. The two things the
SDE does not express cleanly are seeded here, clearly documented and easy to verify:

1. ``PLANET_TYPES`` — metadata + didactic blurbs for the 8 planet types.
2. ``PLANET_RESOURCES`` — the fixed "which P0 can I extract on which planet" matrix.
   This 8×5 table has been stable for years. Names are the exact SDE type names so
   the seeder can resolve them to ``type_id``s (note: "Microorganisms", one word).

Plus human guidance text for the setup guide (Journey 6).
"""
from __future__ import annotations

from django.utils.translation import gettext_lazy as _

# --- The 8 planet types (didactic) -----------------------------------------
# type_ids are the canonical classic planets (the 5601x rows in the SDE are
# structure-variant duplicates and are intentionally excluded).
PLANET_TYPES = [
    {"type_id": 11, "slug": "temperate", "name": "Temperate", "order": 1,
     "best_for": "The all-rounder. Great first planet.",
     "blurb": "Balanced and forgiving — food, industrial and electronics feedstock. "
              "If you're new, start here."},
    {"type_id": 2016, "slug": "barren", "name": "Barren", "order": 2,
     "best_for": "Robotics & electronics feedstock.",
     "blurb": "Reliable all-rounder rich in metals and organics. Backbone of most "
              "electronics and robotics chains."},
    {"type_id": 2015, "slug": "lava", "name": "Lava", "order": 3,
     "best_for": "Construction metals & heavy industry.",
     "blurb": "Metals and magma — the source of construction materials and many "
              "high-end refined commodities."},
    {"type_id": 2063, "slug": "plasma", "name": "Plasma", "order": 4,
     "best_for": "High-end electronics & metals.",
     "blurb": "Harsh but valuable: heavy metals and non-CS crystals that feed "
              "advanced electronics and superconductors."},
    {"type_id": 12, "slug": "ice", "name": "Ice", "order": 5,
     "best_for": "Coolant, life-support & fuels.",
     "blurb": "Cold storage of gases, water and microorganisms — coolant, oxygen "
              "and biotech chains start here."},
    {"type_id": 13, "slug": "gas", "name": "Gas", "order": 6,
     "best_for": "Fuels, oxidisers & industrials.",
     "blurb": "The only source of Reactive Gas. Gas planets drive oxidising "
              "compounds, oxygen and fuel chains."},
    {"type_id": 2014, "slug": "oceanic", "name": "Oceanic", "order": 7,
     "best_for": "Biotech & strong food chains.",
     "blurb": "Water-rich and organic — the best planet for biomass, proteins and "
              "the biotech P3/P4 lines."},
    {"type_id": 2017, "slug": "storm", "name": "Storm", "order": 8,
     "best_for": "Electronics feedstock (plasmoids).",
     "blurb": "Electrically charged: plasmoids, ionic solutions and noble gas for "
              "the electronics and superconductor chains."},
]

# --- Planet → extractable P0 matrix (exact SDE names) ----------------------
# Each planet yields exactly 5 of the 15 raw resources. Verified against the
# in-game planet resource list. To change: edit here — one row per planet.
PLANET_RESOURCES = {
    "temperate": ["Aqueous Liquids", "Autotrophs", "Carbon Compounds",
                  "Complex Organisms", "Microorganisms"],
    "barren":    ["Aqueous Liquids", "Base Metals", "Carbon Compounds",
                  "Microorganisms", "Noble Metals"],
    "gas":       ["Aqueous Liquids", "Base Metals", "Ionic Solutions",
                  "Noble Gas", "Reactive Gas"],
    "ice":       ["Aqueous Liquids", "Heavy Metals", "Microorganisms",
                  "Noble Gas", "Planktic Colonies"],
    "lava":      ["Base Metals", "Felsic Magma", "Heavy Metals",
                  "Non-CS Crystals", "Suspended Plasma"],
    "oceanic":   ["Aqueous Liquids", "Carbon Compounds", "Complex Organisms",
                  "Microorganisms", "Planktic Colonies"],
    "plasma":    ["Base Metals", "Heavy Metals", "Noble Metals",
                  "Non-CS Crystals", "Suspended Plasma"],
    "storm":     ["Aqueous Liquids", "Base Metals", "Ionic Solutions",
                  "Noble Gas", "Suspended Plasma"],
}

# --- Setup guidance (Journey 6) --------------------------------------------
# Per-role practical guidance; rendered on the planet setup guide, not computed.
ROLE_GUIDANCE = {
    "extract": {
        "title": _("Extraction planet"),
        "purpose": _("Pull one or two raw P0 resources and refine them on-site into P1 "
                     "(or straight P2 if you have the feedstock)."),
        "facilities": [
            _("1× Command Center (sets your power/CPU budget — upgrade it fully)."),
            _("1× Extractor Control Unit per resource, with as many extractor heads as "
              "CPU/PG allows (more heads = more yield, but shorter reach)."),
            _("Basic Industry Facilities to convert P0 → P1 (each does 3,000 P0 → 20 P1 "
              "every 30 min)."),
            _("1× Launchpad as the export point and buffer storage."),
        ],
        "routes": _("Route extractor output → storage/launchpad → factories → launchpad. "
                    "Keep the extracted P0 flowing into the BIFs and the finished P1 to the "
                    "launchpad for export."),
        "export": _("Export P1 (or P2) via the customs office. Import nothing."),
    },
    "factory": {
        "title": _("Factory planet"),
        "purpose": _("Import cheaper lower-tier materials and refine them up a tier. "
                     "No extraction — pure manufacturing throughput."),
        "facilities": [
            _("1× Command Center (upgrade fully for CPU/PG)."),
            _("1× Launchpad to receive imports and hold exports."),
            _("As many Advanced Industry Facilities as your power/CPU allows (each does "
              "40+40 input → 5 output every hour for P2)."),
            _("Storage facilities to buffer inputs between imports."),
        ],
        "routes": _("Import inputs to the launchpad → route to each factory → route "
                    "finished goods back to the launchpad."),
        "export": _("Import the P1/P2 inputs; export the higher-tier product."),
    },
    "storage": {
        "title": _("Storage / staging planet"),
        "purpose": _("A buffer or consolidation point. Rare for most pilots — usually a "
                     "launchpad on another planet does the job."),
        "facilities": [
            _("1× Command Center"),
            _("1× Launchpad"),
            _("Storage facilities as needed."),
        ],
        "routes": _("Consolidate goods here before a single haul out."),
        "export": _("Export in bulk on your haul day."),
    },
}

# Common mistakes — shown on the setup guide + wizard help.
COMMON_MISTAKES = [
    (_("Not upgrading the Command Center"),
     _("Your Command Center level sets your entire CPU/PG budget. Upgrade it to level 5 "
       "before you place facilities, or you'll run out of power halfway through.")),
    (_("Routing errors"),
     _("A facility with no inbound route sits idle; a route to the wrong storage stalls the "
       "chain. Follow every product from extractor → factory → launchpad.")),
    (_("Too many extractor heads"),
     _("More heads raise yield but shorten the extraction radius and cost CPU/PG. Balance "
       "heads against the program length you actually want to run.")),
    (_("Insufficient storage"),
     _("If a launchpad or storage fills up, upstream facilities stop. Size storage for the "
       "time between your resets.")),
    (_("Wrong schematic"),
     _("Setting a factory to the wrong schematic wastes materials. Double-check inputs match "
       "what you're actually routing in.")),
    (_("Overbuilding factories"),
     _("Ten factories starved of feedstock earn less than three that run continuously. Match "
       "factory count to your extraction rate.")),
    (_("Ignoring customs (POCO) tax"),
     _("Export/import tax at the customs office is charged on every launch. In hostile space "
       "it can dwarf your margin — factor it in before you commit.")),
]

# In-game build checklist — a short, ordered checklist for the detail page.
BUILD_CHECKLIST = [
    _("Train Command Center Upgrades to unlock the planet tiers you need."),
    _("Buy and deploy a Command Center on each planet, then upgrade it fully."),
    _("On extraction planets: place the ECU, add heads, and start a program."),
    _("Place your factories and set the correct schematic on each."),
    _("Wire the routes: extractor → storage → factory → launchpad."),
    _("Confirm the customs office tax for your space and your standings."),
    _("Set a reset cadence that matches your extractor program length."),
    _("Haul exports to your chosen market hub (or hand to corp buyback)."),
]
