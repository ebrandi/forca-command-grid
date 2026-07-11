"""Built-in career templates — the paths a pilot starts a goal from (doc 05 appendix, brief §12).

A :class:`~apps.capsuleer.models.CareerTemplate` carries a ``structure`` JSON document that is
**structure only** (the CampaignTemplate rule, doc 07 §7): milestones, skill/ship targets by
name, knowledge links and honesty assumptions — never a pilot, a date, or a live value.

Representation decision (recorded deviation from the doc 07 §7 *example*, aligned with doc 13.1
and repo reality): skill and ship references are stored by **canonical name**, not by SDE
``type_id``. A fresh database (every test run, a new deploy before the SDE import) has no SDE
rows, so seed-time id resolution cannot be relied on; names are SDE-version-independent and
resolve to type ids at goal instantiation (Stage 2), where entries that fail to resolve are
retained by name and excluded from plan math (doc 13.1). Consequently the ``skill_target`` /
``ship_owned`` / ``doctrine_ready`` milestones inside a template are validated *structurally* by
:func:`validate_structure` here; the strict type-id ``params`` validators (``params.py``) run on
the concrete milestone rows produced at instantiation. Milestones whose params are already
id-free — ``contribution`` / ``combat_first`` / ``practical`` / ``manual`` — are validated by the
real ``params.py`` validators even in a template.

Doctrine-linked templates carry a top-level ``doctrine_resolver`` (``{role_tokens, name_tokens}``,
doc 13.3); their ``doctrine_ready`` milestones use the ``$doctrine`` placeholder and their
doctrine-hull ``ship_owned`` milestone uses ``{"resolve": "doctrine_hull"}`` — both concretised at
instantiation, degrading honestly when no doctrine matches (brief §12).

:func:`sync_builtin_templates` upserts every template here by ``key`` (idempotent, re-runnable from
the data migration), setting ``source=builtin``. Built-ins are edit-locked and can be hidden via the
``capsuleer.templates.disabled_keys`` config, so a re-sync safely refreshes them from code without
clobbering operator intent.
"""
from __future__ import annotations

from django.core.exceptions import ValidationError

from .models import MilestoneKind
from .params import validate_milestone_params, validate_verification
from .taxonomy import Activity

STRUCTURE_VERSION = 1

# The one placeholder token, resolved to the template's live doctrine_id at instantiation.
_DOCTRINE_PLACEHOLDER = "$doctrine"
# ship_owned "resolve" markers a template may use for a hull only known after doctrine resolution.
_SHIP_RESOLVERS = frozenset({"doctrine_hull"})

# Kinds whose template params are already id-free and so are validated by the real params.py
# validators even inside a template; the rest carry name/placeholder references (validated
# structurally here) that resolve to ids at instantiation.
_CONCRETE_PARAM_KINDS = frozenset({
    MilestoneKind.CONTRIBUTION,
    MilestoneKind.COMBAT_FIRST,
    MilestoneKind.PRACTICAL,
    MilestoneKind.MANUAL,
})


# --------------------------------------------------------------------------- #
#  Authoring helpers (keep the 13 blueprints below readable)
# --------------------------------------------------------------------------- #
def _sk(name: str, level: int) -> dict:
    return {"name": name, "level": level}


def _ms(order, title, kind, verification, *, required=True, params=None, link=None) -> dict:
    p = dict(params or {})
    if link:
        p["link"] = link
    return {
        "order": order, "title": title, "kind": kind, "required": required,
        "verification": verification, "params": p,
    }


def _skill(order, title, name, level, *, required=True) -> dict:
    return _ms(order, title, "skill_target", "auto", required=required,
               params={"skills": [_sk(name, level)]})


def _ship(order, title, names, *, required=True) -> dict:
    return _ms(order, title, "ship_owned", "auto", required=required,
               params={"type_names": list(names)})


def _doctrine(order, title, tier, *, required=True) -> dict:
    return _ms(order, title, "doctrine_ready", "auto", required=required,
               params={"doctrine_id": _DOCTRINE_PLACEHOLDER, "tier": tier})


def _contribution(order, title, kinds, count, *, required=True) -> dict:
    return _ms(order, title, "contribution", "auto", required=required,
               params={"kinds": list(kinds), "count": count})


def _combat_first(order, title, key, *, required=True) -> dict:
    return _ms(order, title, "combat_first", "auto", required=required,
               params={"milestone_key": key})


def _practical(order, title, verification, *, required=True, link=None) -> dict:
    return _ms(order, title, "practical", verification, required=required, link=link)


def _manual(order, title, verification, *, required=True) -> dict:
    return _ms(order, title, "manual", verification, required=required)


def _knowledge(*pairs) -> list[dict]:
    return [{"kb_slug": slug, "label": label} for slug, label in pairs]


# --------------------------------------------------------------------------- #
#  The 13 built-in templates (doc 05 appendix T1-T13)
# --------------------------------------------------------------------------- #
_TACKLE_PILOT = {
    "key": "tackle_pilot",
    "name": "Tackle Pilot",
    "category": Activity.TACKLE_SCOUT,
    "difficulty": 1,
    "newbro_friendly": True,
    "solo_group": "group",
    "est_hours_note": "Days of training; mostly learn-by-flying. Typical, not personalised.",
    "cost_note": "Very low — T1 frigates are cheap and expected to die.",
    "risk_note": "High loss rate, low stakes.",
    "income_note": "None direct; fleet loot and SRP.",
    "structure": {
        "version": STRUCTURE_VERSION,
        "summary": "Learn to hold tackle and fly fast frigates in corp fleets.",
        "skill_tiers": {
            "minimum": [_sk("Racial Frigate", 3), _sk("Propulsion Jamming", 3),
                        _sk("Navigation", 3), _sk("High Speed Maneuvering", 3)],
            "recommended": [_sk("Racial Frigate", 4), _sk("Propulsion Jamming", 4),
                            _sk("Navigation", 4), _sk("Evasive Maneuvering", 4),
                            _sk("Acceleration Control", 4), _sk("Target Management", 4)],
            "mastery": [_sk("Racial Frigate", 5), _sk("Propulsion Jamming", 5),
                        _sk("Interceptors", 4)],
        },
        "ship_targets": [
            {"name": "Atron", "role": "tackle", "note": "or another T1 fast frigate for your race"},
            {"name": "Stiletto", "role": "interceptor", "note": "mastery hull (race-appropriate)"},
        ],
        "milestones": [
            _skill(1, "Train Propulsion Jamming III", "Propulsion Jamming", 3),
            _ship(2, "Own a T1 tackle frigate", ["Atron", "Slasher", "Executioner", "Merlin"]),
            _practical(3, "Overview setup lesson", "self", link="overview-setup"),
            _practical(4, "Safe travel and d-scan basics", "self"),
            _contribution(5, "Fly in 3 corp fleets", ["fleet"], 3),
            _combat_first(6, "First corp killmail", "first_kill"),
            _practical(7, "Hold tackle under FC direction", "mentor"),
            _manual(8, "After-action reflection", "self", required=False),
        ],
        "knowledge_links": _knowledge(
            ("overview-setup", "Overview setup"), ("tackle-101", "Tackle 101"),
            ("fleet-comms-basics", "Fleet comms basics"),
        ),
        "assumptions": [
            "Training estimates assume remap-free attributes.",
            "Costs exclude implants.",
        ],
    },
}

_LOGISTICS_PILOT = {
    "key": "logistics_pilot",
    "name": "Logistics Pilot",
    "category": Activity.COMBAT_SUPPORT,
    "difficulty": 2,
    "newbro_friendly": True,
    "solo_group": "group",
    "est_hours_note": "Weeks to T1 logi comfort. Typical, not personalised.",
    "cost_note": "Low-moderate — a T1 logi cruiser and fittings.",
    "risk_note": "Primary-target risk in fleets.",
    "income_note": "None direct; high fleet value and SRP priority.",
    "structure": {
        "version": STRUCTURE_VERSION,
        "summary": "Fly logistics cruisers and keep corp fleets alive.",
        "doctrine_resolver": {"role_tokens": ["logi", "logistics"],
                              "name_tokens": ["logi", "logistics"]},
        "skill_tiers": {
            "minimum": [_sk("Racial Cruiser", 3), _sk("Shield Emission Systems", 4),
                        _sk("Capacitor Management", 4), _sk("Capacitor Systems Operation", 3)],
            "recommended": [_sk("Racial Cruiser", 4), _sk("Capacitor Emission Systems", 4),
                            _sk("Signature Analysis", 4), _sk("Long Range Targeting", 4)],
            "mastery": [_sk("Racial Cruiser", 5), _sk("Capacitor Management", 5),
                        _sk("Logistics Cruisers", 4)],
        },
        "ship_targets": [
            {"name": "Osprey", "role": "logistics",
             "note": "affordable training hull; or the resolved corp logi doctrine hull"},
            {"name": "Basilisk", "role": "logistics", "note": "T2 mastery hull (race-appropriate)"},
        ],
        "milestones": [
            _skill(1, "Train your remote repair system to IV", "Shield Emission Systems", 4),
            _doctrine(2, "Qualify for the corp logistics fit (viable)", "viable"),
            _ship(3, "Own a logistics hull", ["Osprey", "Exequror", "Augoror", "Scythe"]),
            _practical(4, "Overview and watchlist setup for logi", "self",
                       link="logi-overview-watchlist"),
            _practical(5, "Cap-chain exercise", "mentor", link="cap-chain-basics"),
            _contribution(6, "Fly in 3 corp fleets", ["fleet"], 3),
            _practical(7, "Fly logistics in a fleet under an FC", "mentor"),
            _doctrine(8, "Qualify at the optimal fit level", "optimal", required=False),
            _manual(9, "Review session with your mentor", "mentor", required=False),
        ],
        "knowledge_links": _knowledge(
            ("logi-overview-watchlist", "Logi overview & watchlist"),
            ("cap-chain-basics", "Cap-chain basics"), ("broadcast-basics", "Broadcast basics"),
        ),
        "assumptions": [
            "The resolved doctrine's requirements take precedence over the fallback skill list.",
            "Training estimates assume remap-free attributes.",
        ],
    },
}

_MAINLINE_DD = {
    "key": "mainline_dd",
    "name": "Mainline Damage Dealer",
    "category": Activity.COMBAT_LINE,
    "difficulty": 1,
    "newbro_friendly": True,
    "solo_group": "group",
    "est_hours_note": "Low-moderate. Typical, not personalised.",
    "cost_note": "Low — doctrine mainline hulls are corp-standard, often store-supplied.",
    "risk_note": "Normal fleet losses.",
    "income_note": "None direct; fleet loot and SRP.",
    "structure": {
        "version": STRUCTURE_VERSION,
        "summary": "Fly the corp doctrine's mainline DPS hull in fleets.",
        "doctrine_resolver": {"role_tokens": ["dd", "dps", "mainline", "damage"],
                              "name_tokens": []},
        "skill_tiers": {
            "minimum": [_sk("Racial Cruiser", 3), _sk("CPU Management", 3),
                        _sk("Power Grid Management", 3)],
            "recommended": [_sk("Racial Cruiser", 4), _sk("CPU Management", 4),
                            _sk("Power Grid Management", 4)],
            "mastery": [_sk("Racial Cruiser", 5)],
        },
        "ship_targets": [
            {"name": "", "role": "mainline",
             "note": "your doctrine's mainline DPS ship (resolved at instantiation)"},
        ],
        "milestones": [
            _doctrine(1, "Qualify for the mainline fit (viable)", "viable"),
            _ms(2, "Own the doctrine hull", "ship_owned", "auto",
                params={"resolve": "doctrine_hull"}),
            _practical(3, "Anchoring and broadcast basics", "self", link="broadcast-basics"),
            _contribution(4, "Fly in 3 corp fleets", ["fleet"], 3),
            _combat_first(5, "First corp killmail", "first_kill"),
            _practical(6, "Hold anchor and switch targets on call", "mentor", required=False),
            _doctrine(7, "Qualify at the optimal fit level", "optimal", required=False),
        ],
        "knowledge_links": _knowledge(
            ("broadcast-basics", "Broadcast basics"), ("fleet-comms-basics", "Fleet comms basics"),
            ("fitting-basics", "Fitting basics"),
        ),
        "assumptions": [
            "The resolved doctrine's hull and requirements take precedence over the fallbacks.",
        ],
    },
}

_SCOUT = {
    "key": "scout",
    "name": "Scout",
    "category": Activity.TACKLE_SCOUT,
    "difficulty": 2,
    "newbro_friendly": True,
    "solo_group": "mixed",
    "est_hours_note": "Moderate; the skill is mostly human. Typical, not personalised.",
    "cost_note": "Very low (T1 frigates) to moderate (covert ops).",
    "risk_note": "Low ship value, high situational demand.",
    "income_note": "None direct.",
    "structure": {
        "version": STRUCTURE_VERSION,
        "summary": "Be the fleet's eyes: d-scan, intel and route scouting.",
        "skill_tiers": {
            "minimum": [_sk("Racial Frigate", 3), _sk("Astrometrics", 3), _sk("Cloaking", 1),
                        _sk("Evasive Maneuvering", 3)],
            "recommended": [_sk("Racial Frigate", 4), _sk("Astrometrics", 4), _sk("Cloaking", 4),
                            _sk("Evasive Maneuvering", 4)],
            "mastery": [_sk("Racial Frigate", 5), _sk("Astrometrics", 5), _sk("Covert Ops", 4)],
        },
        "ship_targets": [
            {"name": "Heron", "role": "scout", "note": "or another T1 exploration/fast frigate"},
            {"name": "Buzzard", "role": "covert ops", "note": "mastery hull (race-appropriate)"},
        ],
        "milestones": [
            _skill(1, "Train Astrometrics III", "Astrometrics", 3),
            _practical(2, "D-scan drill: find and report", "mentor"),
            _practical(3, "Make safes and insta-undocks in home space", "self"),
            _practical(4, "Intel reporting format lesson", "self", link="intel-reporting"),
            _practical(5, "Scout a route for a fleet or hauler", "mentor"),
            _contribution(6, "Fly in 3 corp fleets", ["fleet"], 3),
            _skill(7, "Train Cloaking IV", "Cloaking", 4, required=False),
            _ship(8, "Own a Covert Ops frigate", ["Buzzard", "Helios", "Anathema", "Cheetah"],
                  required=False),
        ],
        "knowledge_links": _knowledge(
            ("intel-reporting", "Intel reporting"), ("dscan-mastery", "D-scan mastery"),
            ("safe-spots", "Safe spots"),
        ),
        "assumptions": [
            "Scouting for a fleet carries no role signal, so those milestones are mentor-verified.",
        ],
    },
}

_FLEET_COMMANDER = {
    "key": "fleet_commander",
    "name": "Fleet Commander",
    "category": Activity.FLEET_COMMAND,
    "difficulty": 3,
    "newbro_friendly": False,
    "solo_group": "group",
    "est_hours_note": "High, mostly practice and review. Typical, not personalised.",
    "cost_note": "Low — skills plus doctrine hulls you already fly.",
    "risk_note": "Reputational fear is the real barrier; the path normalises supervised failure.",
    "income_note": "None.",
    "structure": {
        "version": STRUCTURE_VERSION,
        "summary": "Grow from fleet member to a signed-off corp fleet commander.",
        "skill_tiers": {
            "minimum": [_sk("Leadership", 4)],
            "recommended": [_sk("Leadership", 5), _sk("Wing Command", 3)],
            "mastery": [_sk("Wing Command", 4), _sk("Fleet Command", 4)],
        },
        "ship_targets": [
            {"name": "", "role": "command",
             "note": "whatever the corp mainline doctrine flies; Command Ships at mastery"},
        ],
        "milestones": [
            _manual(1, "Comms and tooling ready (mumble/discord, broadcasts, fleet finder)", "self"),
            _practical(2, "Doctrine knowledge review with a mentor", "mentor",
                       link="doctrine-theory"),
            _skill(3, "Train Leadership V", "Leadership", 5),
            _contribution(4, "Fly in 10 corp fleets", ["fleet"], 10),
            _practical(5, "Shadow an FC on 3 fleets (backseat/second)", "mentor"),
            _practical(6, "Target-calling and broadcast exercise", "mentor"),
            _practical(7, "Lead a training fleet", "officer"),
            _manual(8, "After-action review of your fleet", "mentor"),
            _manual(9, "Officer sign-off for the corp FC list", "officer"),
            _manual(10, "Plan a progression to larger fleets", "self", required=False),
        ],
        "knowledge_links": _knowledge(
            ("fc-101", "FC 101"), ("doctrine-theory", "Doctrine theory"),
            ("after-action-reviews", "After-action reviews"),
        ),
        "assumptions": [
            "Officer-verified milestones require the goal shared at officers visibility.",
        ],
    },
}

_BLACK_OPS_PILOT = {
    "key": "black_ops_pilot",
    "name": "Black Ops Pilot",
    "category": Activity.BLACK_OPS,
    "difficulty": 3,
    "newbro_friendly": False,
    "solo_group": "group",
    "est_hours_note": "High (long skill tails). Typical, not personalised.",
    "cost_note": "Moderate (bombers) to very high (Black Ops battleships).",
    "risk_note": "Expensive losses are part of the trade.",
    "income_note": "None direct.",
    "structure": {
        "version": STRUCTURE_VERSION,
        "summary": "Fly stealth bombers and covert operations toward Black Ops.",
        "doctrine_resolver": {"role_tokens": ["blops", "bomber", "black ops"],
                              "name_tokens": ["black ops", "blops"]},
        "skill_tiers": {
            "minimum": [_sk("Racial Frigate", 5), _sk("Covert Ops", 3), _sk("Cloaking", 4),
                        _sk("Torpedoes", 3)],
            "recommended": [_sk("Covert Ops", 4), _sk("Cloaking", 4), _sk("Torpedoes", 4),
                            _sk("Cynosural Field Theory", 5)],
            "mastery": [_sk("Racial Battleship", 5), _sk("Black Ops", 4),
                        _sk("Jump Drive Calibration", 4)],
        },
        "ship_targets": [
            {"name": "Purifier", "role": "bomber", "note": "stealth bomber (race-appropriate)"},
            {"name": "Redeemer", "role": "black ops", "note": "mastery hull (race-appropriate)"},
        ],
        "milestones": [
            _skill(1, "Train Cloaking IV", "Cloaking", 4),
            _ship(2, "Own a Stealth Bomber", ["Purifier", "Manticore", "Nemesis", "Hound"]),
            _practical(3, "Cloaky movement and bridge etiquette", "mentor", link="blops-etiquette"),
            _doctrine(4, "Qualify for the Black Ops doctrine (viable)", "viable"),
            _practical(5, "Covert cyno exercise", "mentor"),
            _manual(6, "Participate in a Black Ops operation", "mentor"),
            _skill(7, "Train Cynosural Field Theory V", "Cynosural Field Theory", 5,
                   required=False),
            _contribution(8, "Fly in 5 corp fleets", ["fleet"], 5, required=False),
            _manual(9, "Mastery review: hunter or bridge-capable", "mentor", required=False),
        ],
        "knowledge_links": _knowledge(
            ("blops-etiquette", "Black Ops etiquette"), ("covert-cyno", "Covert cyno"),
            ("hunting-basics", "Hunting basics"),
        ),
        "assumptions": [
            "Degrades to a doctrine-free plan when no Black Ops doctrine exists (brief §12).",
            "The covert cyno alt is a separate goal on that character.",
        ],
    },
}

_MINER_FOUNDATION = {
    "key": "miner_foundation",
    "name": "Mining Foundation",
    "category": Activity.MINING,
    "difficulty": 1,
    "newbro_friendly": True,
    "solo_group": "mixed",
    "est_hours_note": "Low to start. Typical, not personalised.",
    "cost_note": "Near zero (Venture) to modest (Procurer).",
    "risk_note": "Gank exposure in a barge — the tanky-barge lesson exists for a reason.",
    "income_note": "Steady, low risk.",
    "structure": {
        "version": STRUCTURE_VERSION,
        "summary": "Start mining safely and grow into a barge with corp buyback.",
        "skill_tiers": {
            "minimum": [_sk("Mining", 3), _sk("Mining Frigate", 2)],
            "recommended": [_sk("Mining", 4), _sk("Mining Frigate", 3), _sk("Industry", 5),
                            _sk("Astrogeology", 3), _sk("Mining Barge", 3), _sk("Mining Upgrades", 3)],
            "mastery": [_sk("Mining", 5), _sk("Industry", 5), _sk("Astrogeology", 5),
                        _sk("Mining Barge", 5), _sk("Mining Upgrades", 4)],
        },
        "ship_targets": [
            {"name": "Venture", "role": "mining frigate", "note": "trivially cheap starter"},
            {"name": "Procurer", "role": "mining barge", "note": "the tanky choice"},
        ],
        "milestones": [
            _ship(1, "Own a Venture", ["Venture"]),
            _skill(2, "Train Mining III", "Mining", 3),
            _contribution(3, "Mine with the corp (first recorded corp mining)", ["mining"], 1),
            _skill(4, "Train Industry V", "Industry", 5),
            _ship(5, "Own a mining barge", ["Procurer", "Retriever", "Covetor"]),
            _practical(6, "Sell ore through the corp buyback", "self", link="buyback-howto"),
            _contribution(7, "Join a corp mining fleet", ["fleet"], 1, required=False),
            _practical(8, "Ore choices and compression basics", "self", link="ore-guide",
                       required=False),
        ],
        "knowledge_links": _knowledge(
            ("buyback-howto", "Buyback how-to"), ("ore-guide", "Ore guide"),
            ("mining-fleet-etiquette", "Mining fleet etiquette"),
        ),
        "assumptions": [
            "Only corp-observer (moon) mining is recorded; belt/solo mining needs a personal ESI "
            "scope that is not integrated, so those tonnes do not count automatically.",
        ],
    },
}

_T2_INDUSTRIALIST = {
    "key": "t2_industrialist",
    "name": "Tech II Industrialist",
    "category": Activity.INDUSTRY,
    "difficulty": 3,
    "newbro_friendly": False,
    "solo_group": "solo",
    "est_hours_note": "High (science skill tails). Typical, not personalised.",
    "cost_note": "Significant setup — blueprints, datacores, invention materials, lab access.",
    "risk_note": "Market risk, not ship risk.",
    "income_note": "The strongest sustained ISK path in the catalogue.",
    "structure": {
        "version": STRUCTURE_VERSION,
        "summary": "Move from T1 production into invention and Tech II manufacturing.",
        "skill_tiers": {
            "minimum": [_sk("Industry", 5), _sk("Science", 4), _sk("Laboratory Operation", 3),
                        _sk("Advanced Industry", 3)],
            "recommended": [_sk("Science", 4), _sk("Laboratory Operation", 4),
                            _sk("Advanced Industry", 4), _sk("Research", 4), _sk("Metallurgy", 4)],
            "mastery": [_sk("Science", 5), _sk("Laboratory Operation", 5),
                        _sk("Advanced Industry", 5), _sk("Research", 5), _sk("Metallurgy", 5)],
        },
        "ship_targets": [],
        "milestones": [
            _skill(1, "Train Industry V", "Industry", 5),
            _contribution(2, "Deliver a T1 production batch to the corp", ["build"], 1),
            _practical(3, "Set up research/invention access (lab, BPOs, datacores)", "self",
                       link="invention-setup"),
            _skill(4, "Train your invention prerequisites", "Gallente Encryption Methods", 3),
            _practical(5, "Complete your first invention job", "self"),
            _practical(6, "Complete your first T2 manufacturing run", "self"),
            _practical(7, "Price and margin review with the market tools", "self",
                       link="industry-margins"),
            _contribution(8, "Deliver 3 corp production jobs", ["build"], 3, required=False),
            _manual(9, "Production line review with a mentor", "mentor", required=False),
        ],
        "knowledge_links": _knowledge(
            ("invention-setup", "Invention setup"), ("industry-margins", "Industry margins"),
            ("t2-production-chains", "T2 production chains"),
        ),
        "assumptions": [
            "The invention prerequisite defaults to a Gallente line; the plan expander personalises "
            "the encryption and engineering-science skills to the pilot's chosen invention line.",
            "Corp-delivered batches verify automatically; personal invention/manufacturing jobs are "
            "self-certified (jobs are snapshot-replaced upstream).",
        ],
    },
}

_PLANETARY_INDUSTRY = {
    "key": "planetary_industry",
    "name": "Planetary Industrialist",
    "category": Activity.PLANETARY,
    "difficulty": 1,
    "newbro_friendly": True,
    "solo_group": "solo",
    "est_hours_note": "Very low ongoing after setup. Typical, not personalised.",
    "cost_note": "Low (command centers plus customs fees).",
    "risk_note": "Minimal in high-sec; customs-office exposure elsewhere.",
    "income_note": "Modest, passive.",
    "structure": {
        "version": STRUCTURE_VERSION,
        "summary": "Build passive income from planetary production chains.",
        "skill_tiers": {
            "minimum": [_sk("Planetology", 3), _sk("Command Center Upgrades", 3),
                        _sk("Interplanetary Consolidation", 2), _sk("Remote Sensing", 2)],
            "recommended": [_sk("Planetology", 4), _sk("Command Center Upgrades", 4),
                            _sk("Interplanetary Consolidation", 4), _sk("Remote Sensing", 3)],
            "mastery": [_sk("Command Center Upgrades", 5),
                        _sk("Interplanetary Consolidation", 5), _sk("Advanced Planetology", 4)],
        },
        "ship_targets": [
            {"name": "Epithal", "role": "hauler", "note": "the PI-dedicated pickup hull"},
        ],
        "milestones": [
            _skill(1, "Train Command Center Upgrades III", "Command Center Upgrades", 3),
            _practical(2, "Establish your first colony", "self"),
            _practical(3, "Run a P2 production chain", "self", link="pi-chains"),
            _skill(4, "Train Interplanetary Consolidation IV", "Interplanetary Consolidation", 4),
            _practical(5, "Run a multi-planet P3 chain", "self"),
            _practical(6, "Exports, imports and customs fees lesson", "self", link="pi-customs"),
            _manual(7, "Monthly profit review", "self", required=False),
        ],
        "knowledge_links": _knowledge(
            ("pi-chains", "PI chains"), ("pi-customs", "PI customs"), ("pi-planning", "PI planning"),
        ),
        "assumptions": [
            "Colony milestones are self-certified; only existence/count/level is ever observed.",
        ],
    },
}

_HAULER = {
    "key": "hauler",
    "name": "Hauler",
    "category": Activity.HAULING,
    "difficulty": 2,
    "newbro_friendly": True,
    "solo_group": "solo",
    "est_hours_note": "Low-moderate. Typical, not personalised.",
    "cost_note": "Low (T1 industrial) rising with hull class.",
    "risk_note": "Gank corridors — the safety lessons are load-bearing.",
    "income_note": "Freight fees via the corp logistics programme.",
    "structure": {
        "version": STRUCTURE_VERSION,
        "summary": "Move goods safely and run corp courier contracts.",
        "skill_tiers": {
            "minimum": [_sk("Racial Industrial", 3), _sk("Hull Upgrades", 3),
                        _sk("Evasive Maneuvering", 2)],
            "recommended": [_sk("Racial Industrial", 4), _sk("Hull Upgrades", 4),
                            _sk("Evasive Maneuvering", 3)],
            "mastery": [_sk("Racial Industrial", 5), _sk("Transport Ships", 4)],
        },
        "ship_targets": [
            {"name": "Nereus", "role": "industrial", "note": "or another racial T1 industrial"},
            {"name": "Occator", "role": "deep space transport", "note": "mastery hull"},
        ],
        "milestones": [
            _ship(1, "Own a T1 industrial", ["Nereus", "Badger", "Sigil", "Wreathe"]),
            _practical(2, "Tank-versus-cargo fitting lesson", "self", link="hauler-fitting"),
            _practical(3, "Safe travel drill: instadocks, alignment, no autopilot with cargo",
                       "self"),
            _contribution(4, "Complete your first verified corp courier contract", ["haul"], 1),
            _contribution(5, "Complete 5 verified corp deliveries", ["haul"], 5),
            _skill(6, "Train Racial Industrial V", "Racial Industrial", 5, required=False),
            _ship(7, "Own a DST or Blockade Runner",
                  ["Occator", "Bustard", "Impel", "Mastodon", "Viator", "Crane", "Prorator",
                   "Prowler"], required=False),
            _practical(8, "Freight pricing and collateral review", "self", link="freight-pricing",
                       required=False),
        ],
        "knowledge_links": _knowledge(
            ("hauler-fitting", "Hauler fitting"), ("freight-pricing", "Freight pricing"),
            ("gank-avoidance", "Gank avoidance"),
        ),
        "assumptions": [
            "Courier milestones verify via corp courier contracts reaching VERIFIED; where contract "
            "verification is unavailable the state is honest unknown, not assumed complete.",
        ],
    },
}

_EXPLORER = {
    "key": "explorer",
    "name": "Explorer",
    "category": Activity.EXPLORATION,
    "difficulty": 1,
    "newbro_friendly": True,
    "solo_group": "solo",
    "est_hours_note": "Low to start. Typical, not personalised.",
    "cost_note": "Near zero — a fitted T1 exploration frigate.",
    "risk_note": "You will lose ships; carry nothing you can't replace.",
    "income_note": "Lumpy but real (relic loot).",
    "structure": {
        "version": STRUCTURE_VERSION,
        "summary": "Scan down relic and data sites and bring the loot home.",
        "skill_tiers": {
            "minimum": [_sk("Racial Frigate", 3), _sk("Astrometrics", 3), _sk("Archaeology", 3),
                        _sk("Hacking", 3)],
            "recommended": [_sk("Racial Frigate", 4), _sk("Astrometrics", 4), _sk("Archaeology", 4),
                            _sk("Hacking", 4), _sk("Cloaking", 4)],
            "mastery": [_sk("Astrometrics", 5), _sk("Archaeology", 5), _sk("Hacking", 5),
                        _sk("Covert Ops", 4)],
        },
        "ship_targets": [
            {"name": "Heron", "role": "exploration", "note": "or another T1 exploration frigate"},
            {"name": "Buzzard", "role": "covert ops", "note": "mastery hull; Astero is premium"},
        ],
        "milestones": [
            _ship(1, "Own an exploration frigate", ["Heron", "Imicus", "Magnate", "Probe"]),
            _skill(2, "Train Astrometrics III", "Astrometrics", 3),
            _practical(3, "Scan down and complete your first relic or data site", "self",
                       link="scanning-101"),
            _practical(4, "Travel a low-sec loop and come home", "self"),
            _practical(5, "Bookmark and escape drills (safes, cloak-warp)", "self"),
            _skill(6, "Train Cloaking IV", "Cloaking", 4, required=False),
            _manual(7, "Bank 50M ISK from exploration loot", "self", required=False),
            _manual(8, "Share a find: loot review or map note", "self", required=False),
        ],
        "knowledge_links": _knowledge(
            ("scanning-101", "Scanning 101"), ("hacking-minigame", "Hacking minigame"),
            ("exploration-fits", "Exploration fits"),
        ),
        "assumptions": [
            "Site completions leave no ESI trace, so those milestones are self-certified by design.",
        ],
    },
}

_WORMHOLE_EXPLORER = {
    "key": "wormhole_explorer",
    "name": "Wormhole Explorer",
    "category": Activity.WORMHOLES,
    "difficulty": 3,
    "newbro_friendly": False,
    "solo_group": "mixed",
    "advanced_from": "explorer",
    "est_hours_note": "Moderate on skills, high on judgement. Typical, not personalised.",
    "cost_note": "Low-moderate (covert ops recommended).",
    "risk_note": "Everyone in J-space is hunting you; local doesn't exist.",
    "income_note": "Strong (J-space relic sites).",
    "structure": {
        "version": STRUCTURE_VERSION,
        "summary": "Take exploration into wormhole space with the judgement it demands.",
        "skill_tiers": {
            "minimum": [_sk("Astrometrics", 4), _sk("Cloaking", 4), _sk("Archaeology", 3),
                        _sk("Hacking", 3)],
            "recommended": [_sk("Astrometrics", 4), _sk("Cloaking", 4), _sk("Covert Ops", 4),
                            _sk("Archaeology", 4), _sk("Hacking", 4)],
            "mastery": [_sk("Astrometrics", 5), _sk("Cloaking", 5), _sk("Archaeology", 5),
                        _sk("Hacking", 5)],
        },
        "ship_targets": [
            {"name": "Buzzard", "role": "covert ops",
             "note": "or Astero; a T1 exploration frigate is acceptable with the risk stated"},
        ],
        "milestones": [
            _skill(1, "Train Astrometrics IV", "Astrometrics", 4),
            _practical(2, "Wormhole identification: class, mass, lifetime reading", "mentor",
                       link="wormhole-ids"),
            _practical(3, "Map a chain with bookmarks and naming convention", "mentor"),
            _practical(4, "D-scan discipline drill (constant scan while working)", "mentor"),
            _practical(5, "Run sites in J-space and bring the loot home", "self"),
            _ship(6, "Own a Covert Ops frigate", ["Buzzard", "Helios", "Anathema", "Cheetah"],
                  required=False),
            _practical(7, "Home-hole operations / rolling basics (if the corp runs a WH group)",
                       "mentor", required=False),
            _manual(8, "Review with a wormhole mentor", "mentor", required=False),
        ],
        "knowledge_links": _knowledge(
            ("wormhole-ids", "Wormhole IDs"), ("chain-mapping", "Chain mapping"),
            ("jspace-survival", "J-space survival"),
        ),
        "assumptions": [
            "Site and chain milestones leave no ESI trace, so they are human-verified.",
        ],
    },
}

_MENTOR = {
    "key": "mentor",
    "name": "Mentor",
    "category": Activity.MENTORING,
    "difficulty": 2,
    "newbro_friendly": False,
    "solo_group": "group",
    "est_hours_note": "Whatever you choose to give. Typical, not personalised.",
    "cost_note": "None.",
    "risk_note": "None.",
    "income_note": "None (mentorship reward caps exist in the programme config).",
    "structure": {
        "version": STRUCTURE_VERSION,
        "summary": "Give back: register as a mentor and guide new pilots.",
        "skill_tiers": {"minimum": [], "recommended": [], "mastery": []},
        "ship_targets": [],
        "milestones": [
            _manual(1, "Meet the mentor eligibility bar (tenure/character age per programme config)",
                    "officer"),
            _practical(2, "Register a mentor profile with your focus areas", "self"),
            _practical(3, "Take your first mentee pairing", "self"),
            _manual(4, "Guide a mentee through a learning track", "self"),
            _practical(5, "Endorse a mentee's practical milestone in Capsuleer Path", "self"),
            _practical(6, "Contribute or improve a KB lesson page", "self", link="writing-lessons",
                       required=False),
            _manual(7, "Officer recognition of sustained mentoring", "officer", required=False),
        ],
        "knowledge_links": _knowledge(
            ("mentoring-guide", "Mentoring guide"), ("writing-lessons", "Writing lessons"),
        ),
        "assumptions": [
            "This path is about people, not SP — no skill plan is generated for it.",
            "Milestones 1 and 7 require the goal shared at officers visibility.",
        ],
    },
}


BUILTIN: list[dict] = [
    _TACKLE_PILOT, _LOGISTICS_PILOT, _MAINLINE_DD, _SCOUT, _FLEET_COMMANDER, _BLACK_OPS_PILOT,
    _MINER_FOUNDATION, _T2_INDUSTRIALIST, _PLANETARY_INDUSTRY, _HAULER, _EXPLORER,
    _WORMHOLE_EXPLORER, _MENTOR,
]

BUILTIN_BY_KEY = {t["key"]: t for t in BUILTIN}
BUILTIN_KEYS = [t["key"] for t in BUILTIN]


# --------------------------------------------------------------------------- #
#  Structure validation (doc 07 §7)
# --------------------------------------------------------------------------- #
def _validate_skill_list(entries, where: str) -> None:
    if not isinstance(entries, list):
        raise ValidationError(f"{where} must be a list.")
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - {"name", "level"}:
            raise ValidationError(f"{where} entries take only name and level.")
        if not isinstance(entry.get("name"), str) or not entry["name"].strip():
            raise ValidationError(f"{where} entries need a non-empty name string.")
        level = entry.get("level")
        if isinstance(level, bool) or not isinstance(level, int) or not (1 <= level <= 5):
            raise ValidationError(f"{where} entries need a level in 1-5.")


def _validate_template_milestone(ms: dict) -> None:
    if not isinstance(ms, dict):
        raise ValidationError("Each milestone must be an object.")
    kind = ms.get("kind")
    verification = ms.get("verification")
    if kind not in MilestoneKind.values:
        raise ValidationError(f"Unknown milestone kind: {kind!r}.")
    if not isinstance(ms.get("order"), int) or isinstance(ms.get("order"), bool):
        raise ValidationError("Each milestone needs an integer order.")
    if not isinstance(ms.get("title"), str) or not ms["title"].strip():
        raise ValidationError("Each milestone needs a title.")
    if "required" in ms and not isinstance(ms["required"], bool):
        raise ValidationError("Milestone required must be a boolean.")
    validate_verification(kind, verification)

    params = ms.get("params") or {}
    if not isinstance(params, dict):
        raise ValidationError("Milestone params must be an object.")

    if kind in _CONCRETE_PARAM_KINDS:
        # Already id-free — validate with the real params.py validators.
        validate_milestone_params(kind, params, verification)
    elif kind == MilestoneKind.SKILL_TARGET:
        if set(params) - {"skills"}:
            raise ValidationError("skill_target template params take only skills.")
        _validate_skill_list(params.get("skills"), "skill_target.skills")
        if not params.get("skills"):
            raise ValidationError("skill_target needs at least one skill.")
    elif kind == MilestoneKind.SHIP_OWNED:
        if set(params) - {"type_names", "require_fitted", "resolve"}:
            raise ValidationError("ship_owned template params take type_names/require_fitted/resolve.")
        names = params.get("type_names", [])
        resolve = params.get("resolve")
        if resolve is not None and resolve not in _SHIP_RESOLVERS:
            raise ValidationError("ship_owned.resolve must be 'doctrine_hull'.")
        if not isinstance(names, list) or not all(isinstance(n, str) and n for n in names):
            raise ValidationError("ship_owned.type_names must be a list of hull names.")
        if not names and resolve is None:
            raise ValidationError("ship_owned needs type_names or a resolve marker.")
        if "require_fitted" in params and not isinstance(params["require_fitted"], bool):
            raise ValidationError("ship_owned.require_fitted must be a boolean.")
    elif kind == MilestoneKind.DOCTRINE_READY:
        if set(params) - {"doctrine_id", "tier", "fit_id", "resolver", "unresolved"}:
            raise ValidationError("doctrine_ready template params carry unexpected keys.")
        doctrine_id = params.get("doctrine_id")
        if doctrine_id != _DOCTRINE_PLACEHOLDER and not (
            isinstance(doctrine_id, int) and not isinstance(doctrine_id, bool) and doctrine_id > 0
        ):
            raise ValidationError("doctrine_ready.doctrine_id must be $doctrine or a positive int.")
        if params.get("tier") is not None and params["tier"] not in {"viable", "optimal"}:
            raise ValidationError("doctrine_ready.tier must be 'viable' or 'optimal'.")


def validate_structure(structure: dict) -> None:
    """Validate a template ``structure`` document (doc 07 §7).

    Raises :class:`~django.core.exceptions.ValidationError` on any violation. Checks the version,
    the optional prose/skill/ship/knowledge/assumption sections, and every milestone (structurally
    for name/placeholder-bearing kinds, strictly via ``params.py`` for id-free kinds), with unique
    orders and at least one milestone.
    """
    if not isinstance(structure, dict):
        raise ValidationError("structure must be an object.")
    if structure.get("version") != STRUCTURE_VERSION:
        raise ValidationError(f"Unsupported structure version: {structure.get('version')!r}.")

    tiers = structure.get("skill_tiers", {})
    if tiers:
        if not isinstance(tiers, dict) or set(tiers) - {"minimum", "recommended", "mastery"}:
            raise ValidationError("skill_tiers takes only minimum/recommended/mastery.")
        for band, entries in tiers.items():
            _validate_skill_list(entries, f"skill_tiers.{band}")

    ships = structure.get("ship_targets", [])
    if not isinstance(ships, list):
        raise ValidationError("ship_targets must be a list.")
    for ship in ships:
        if not isinstance(ship, dict) or set(ship) - {"name", "role", "note"}:
            raise ValidationError("ship_targets entries take only name/role/note.")

    resolver = structure.get("doctrine_resolver")
    if resolver is not None:
        if not isinstance(resolver, dict) or set(resolver) - {"role_tokens", "name_tokens"}:
            raise ValidationError("doctrine_resolver takes only role_tokens/name_tokens.")
        for key in ("role_tokens", "name_tokens"):
            toks = resolver.get(key, [])
            if not isinstance(toks, list) or not all(isinstance(t, str) for t in toks):
                raise ValidationError(f"doctrine_resolver.{key} must be a list of strings.")

    milestones = structure.get("milestones")
    if not isinstance(milestones, list) or not milestones:
        raise ValidationError("structure needs a non-empty milestones list.")
    orders = [ms.get("order") for ms in milestones]
    if len(set(orders)) != len(orders):
        raise ValidationError("milestone orders must be unique within a template.")
    for ms in milestones:
        _validate_template_milestone(ms)

    for opt_list, name in (
        (structure.get("action_steps", []), "action_steps"),
        (structure.get("knowledge_links", []), "knowledge_links"),
        (structure.get("assumptions", []), "assumptions"),
    ):
        if not isinstance(opt_list, list):
            raise ValidationError(f"{name} must be a list.")


# --------------------------------------------------------------------------- #
#  Seeding (doc 07 §7, doc 15 migration plan)
# --------------------------------------------------------------------------- #
def _model_fields(template: dict) -> dict:
    """The CareerTemplate column values for one built-in (structure validated first)."""
    validate_structure(template["structure"])
    return {
        "name": template["name"],
        "category": template["category"],
        "description": template["structure"].get("summary", ""),
        "difficulty": template.get("difficulty", 1),
        "est_hours_note": template.get("est_hours_note", ""),
        "cost_note": template.get("cost_note", ""),
        "solo_group": template.get("solo_group", "mixed"),
        "risk_note": template.get("risk_note", ""),
        "income_note": template.get("income_note", ""),
        "newbro_friendly": template.get("newbro_friendly", False),
        "structure": template["structure"],
        "source": "builtin",
        "is_active": True,
    }


def sync_builtin_templates(model=None) -> int:
    """Upsert every built-in template by ``key`` (idempotent; re-runnable from the migration).

    Sets ``source=builtin`` and refreshes the built-in's content from code, leaving corp templates
    untouched. ``advanced_from`` self-links are wired in a second pass once every row exists.
    ``model`` lets the data migration pass its historical ``CareerTemplate`` so the seed is
    schema-stable. Returns the number of built-ins processed.
    """
    if model is None:
        from .models import CareerTemplate as model  # noqa: N813

    by_key = {}
    for template in BUILTIN:
        obj, _ = model.objects.update_or_create(
            key=template["key"], defaults=_model_fields(template)
        )
        by_key[template["key"]] = obj

    for template in BUILTIN:
        parent_key = template.get("advanced_from")
        obj = by_key[template["key"]]
        want = by_key.get(parent_key).pk if parent_key else None
        if obj.advanced_from_id != want:
            obj.advanced_from_id = want
            obj.save(update_fields=["advanced_from", "updated_at"])

    return len(BUILTIN)
