"""Render-time i18n seam for the built-in career templates — "translate until edited".

The 13 paths in :mod:`apps.capsuleer.templates_builtin` are shipped English **content**: the seed
migration writes them into ``CareerTemplate`` rows (columns *and* the ``structure`` JSON), and
:func:`apps.capsuleer.plan.instantiate_template` copies the milestone titles again onto the
``CareerMilestone`` rows a pilot then edits — with the goal's own title copied from the path's name.

Wrapping the source dicts in ``gettext_lazy`` cannot work: Django coerces a lazy proxy to ``str``
on ``.save()`` (and the ``structure`` JSON would not even serialise), so whichever locale happened
to be active at seed time would be frozen into the row forever. So the English stays in the
database (it is the fallback *and* the audit record) and the translations live here, in a code-side
catalogue keyed by the stable built-in key:

* :data:`BUILTIN_MSGIDS` — ``{source_key: {field: gettext_lazy msgid}}``. Real literal ``_("…")``
  calls, so ``xgettext`` sees them (a ``_(f"…")`` or ``_(variable)`` would be a silent no-op).
  They are resolved with ``str(...)`` at *render* time, under the active locale — never at write
  time.
* :data:`BUILTIN_ENGLISH` — the same table derived from ``templates_builtin`` at import, holding
  the *shipped English* (never a msgid). It is what a stored value is compared against.

:func:`render` is the seam:

    stored text == the shipped English  →  still the built-in's words  →  return the TRANSLATION
    stored text != the shipped English  →  the pilot edited it  →  return it VERBATIM, untranslated

That comparison **is** the "has this been edited?" test, which is why there is no ``edited``
boolean column: a flag would have to be maintained by every write path (views, services, admin,
shell, future data migrations) and would silently rot the first time one forgot. The stored text
cannot rot — it either is the shipped English or it is not.

Never translated, because they are identifiers or EVE game data rather than prose: the template
``key``, ``category``, ``difficulty``, ``solo_group``, milestone ``kind`` / ``verification`` /
``params`` (skill and hull *names*, doctrine ids, kb slugs), and the ship-target ``name`` /
``role`` tokens. A sentence that merely *contains* an EVE name is still translatable prose and is
in the catalogue.
"""
from __future__ import annotations

from django.core.exceptions import FieldDoesNotExist
from django.utils.translation import gettext_lazy as _

from . import templates_builtin


# --------------------------------------------------------------------------- #
#  Stable keys — the single definition of the ``source_key`` format.
#  ``instantiate_template`` stamps the milestone key onto every ``CareerMilestone`` it copies a
#  title into; the structure-prose keys address the parts of ``CareerTemplate.structure`` a page
#  renders (assumptions, ship notes, knowledge-link labels). Milestones key off their ``order``,
#  which the template validator already forces to be unique within a template.
# --------------------------------------------------------------------------- #
def template_key(key: str) -> str:
    return key


def milestone_key(key: str, order: int) -> str:
    return f"{key}.ms.{order}"


def ship_key(key: str, index: int) -> str:
    return f"{key}.ship.{index}"


def knowledge_key(key: str, kb_slug: str) -> str:
    return f"{key}.kb.{kb_slug}"


def assumption_key(key: str, index: int) -> str:
    return f"{key}.assumption.{index}"


# ``CareerTemplate.description`` is seeded from ``structure["summary"]`` — one string, one msgid,
# addressable under either name so a page can render the column or the JSON with the same seam.
FIELD_ALIASES = {"summary": "description"}


# --------------------------------------------------------------------------- #
#  The catalogue: one gettext msgid per translatable built-in field.
#  Keep it in lockstep with ``templates_builtin`` — ``tests/test_builtin_template_i18n.py`` fails
#  if any msgid drifts from the shipped English, or if a translatable field has no msgid.
# --------------------------------------------------------------------------- #
BUILTIN_MSGIDS: dict[str, dict[str, str]] = {
    "tackle_pilot": {
        "name": _("Tackle Pilot"),
        "description": _("Learn to hold tackle and fly fast frigates in corp fleets."),
        "est_hours_note": _("Days of training; mostly learn-by-flying. Typical, not personalised."),
        "cost_note": _("Very low — T1 frigates are cheap and expected to die."),
        "risk_note": _("High loss rate, low stakes."),
        "income_note": _("None direct; fleet loot and SRP."),
    },
    "tackle_pilot.ms.1": {
        "title": _("Train Propulsion Jamming III"),
    },
    "tackle_pilot.ms.2": {
        "title": _("Own a T1 tackle frigate"),
    },
    "tackle_pilot.ms.3": {
        "title": _("Overview setup lesson"),
    },
    "tackle_pilot.ms.4": {
        "title": _("Safe travel and d-scan basics"),
    },
    "tackle_pilot.ms.5": {
        "title": _("Fly in 3 corp fleets"),
    },
    "tackle_pilot.ms.6": {
        "title": _("First corp killmail"),
    },
    "tackle_pilot.ms.7": {
        "title": _("Hold tackle under FC direction"),
    },
    "tackle_pilot.ms.8": {
        "title": _("After-action reflection"),
    },
    "tackle_pilot.ship.0": {
        "note": _("or another T1 fast frigate for your race"),
    },
    "tackle_pilot.ship.1": {
        "note": _("mastery hull (race-appropriate)"),
    },
    "tackle_pilot.kb.overview-setup": {
        "label": _("Overview setup"),
    },
    "tackle_pilot.kb.tackle-101": {
        "label": _("Tackle 101"),
    },
    "tackle_pilot.kb.fleet-comms-basics": {
        "label": _("Fleet comms basics"),
    },
    "tackle_pilot.assumption.0": {
        "text": _("Training estimates assume remap-free attributes."),
    },
    "tackle_pilot.assumption.1": {
        "text": _("Costs exclude implants."),
    },
    "logistics_pilot": {
        "name": _("Logistics Pilot"),
        "description": _("Fly logistics cruisers and keep corp fleets alive."),
        "est_hours_note": _("Weeks to T1 logi comfort. Typical, not personalised."),
        "cost_note": _("Low-moderate — a T1 logi cruiser and fittings."),
        "risk_note": _("Primary-target risk in fleets."),
        "income_note": _("None direct; high fleet value and SRP priority."),
    },
    "logistics_pilot.ms.1": {
        "title": _("Train your remote repair system to IV"),
    },
    "logistics_pilot.ms.2": {
        "title": _("Qualify for the corp logistics fit (viable)"),
    },
    "logistics_pilot.ms.3": {
        "title": _("Own a logistics hull"),
    },
    "logistics_pilot.ms.4": {
        "title": _("Overview and watchlist setup for logi"),
    },
    "logistics_pilot.ms.5": {
        "title": _("Cap-chain exercise"),
    },
    "logistics_pilot.ms.6": {
        "title": _("Fly in 3 corp fleets"),
    },
    "logistics_pilot.ms.7": {
        "title": _("Fly logistics in a fleet under an FC"),
    },
    "logistics_pilot.ms.8": {
        "title": _("Qualify at the optimal fit level"),
    },
    "logistics_pilot.ms.9": {
        "title": _("Review session with your mentor"),
    },
    "logistics_pilot.ship.0": {
        "note": _("affordable training hull; or the resolved corp logi doctrine hull"),
    },
    "logistics_pilot.ship.1": {
        "note": _("T2 mastery hull (race-appropriate)"),
    },
    "logistics_pilot.kb.logi-overview-watchlist": {
        "label": _("Logi overview & watchlist"),
    },
    "logistics_pilot.kb.cap-chain-basics": {
        "label": _("Cap-chain basics"),
    },
    "logistics_pilot.kb.broadcast-basics": {
        "label": _("Broadcast basics"),
    },
    "logistics_pilot.assumption.0": {
        "text": _("The resolved doctrine's requirements take precedence over the fallback skill list."),
    },
    "logistics_pilot.assumption.1": {
        "text": _("Training estimates assume remap-free attributes."),
    },
    "mainline_dd": {
        "name": _("Mainline Damage Dealer"),
        "description": _("Fly the corp doctrine's mainline DPS hull in fleets."),
        "est_hours_note": _("Low-moderate. Typical, not personalised."),
        "cost_note": _("Low — doctrine mainline hulls are corp-standard, often store-supplied."),
        "risk_note": _("Normal fleet losses."),
        "income_note": _("None direct; fleet loot and SRP."),
    },
    "mainline_dd.ms.1": {
        "title": _("Qualify for the mainline fit (viable)"),
    },
    "mainline_dd.ms.2": {
        "title": _("Own the doctrine hull"),
    },
    "mainline_dd.ms.3": {
        "title": _("Anchoring and broadcast basics"),
    },
    "mainline_dd.ms.4": {
        "title": _("Fly in 3 corp fleets"),
    },
    "mainline_dd.ms.5": {
        "title": _("First corp killmail"),
    },
    "mainline_dd.ms.6": {
        "title": _("Hold anchor and switch targets on call"),
    },
    "mainline_dd.ms.7": {
        "title": _("Qualify at the optimal fit level"),
    },
    "mainline_dd.ship.0": {
        "note": _("your doctrine's mainline DPS ship (resolved at instantiation)"),
    },
    "mainline_dd.kb.broadcast-basics": {
        "label": _("Broadcast basics"),
    },
    "mainline_dd.kb.fleet-comms-basics": {
        "label": _("Fleet comms basics"),
    },
    "mainline_dd.kb.fitting-basics": {
        "label": _("Fitting basics"),
    },
    "mainline_dd.assumption.0": {
        "text": _("The resolved doctrine's hull and requirements take precedence over the fallbacks."),
    },
    "scout": {
        "name": _("Scout"),
        "description": _("Be the fleet's eyes: d-scan, intel and route scouting."),
        "est_hours_note": _("Moderate; the skill is mostly human. Typical, not personalised."),
        "cost_note": _("Very low (T1 frigates) to moderate (covert ops)."),
        "risk_note": _("Low ship value, high situational demand."),
        "income_note": _("None direct."),
    },
    "scout.ms.1": {
        "title": _("Train Astrometrics III"),
    },
    "scout.ms.2": {
        "title": _("D-scan drill: find and report"),
    },
    "scout.ms.3": {
        "title": _("Make safes and insta-undocks in home space"),
    },
    "scout.ms.4": {
        "title": _("Intel reporting format lesson"),
    },
    "scout.ms.5": {
        "title": _("Scout a route for a fleet or hauler"),
    },
    "scout.ms.6": {
        "title": _("Fly in 3 corp fleets"),
    },
    "scout.ms.7": {
        "title": _("Train Cloaking IV"),
    },
    "scout.ms.8": {
        "title": _("Own a Covert Ops frigate"),
    },
    "scout.ship.0": {
        "note": _("or another T1 exploration/fast frigate"),
    },
    "scout.ship.1": {
        "note": _("mastery hull (race-appropriate)"),
    },
    "scout.kb.intel-reporting": {
        "label": _("Intel reporting"),
    },
    "scout.kb.dscan-mastery": {
        "label": _("D-scan mastery"),
    },
    "scout.kb.safe-spots": {
        "label": _("Safe spots"),
    },
    "scout.assumption.0": {
        "text": _("Scouting for a fleet carries no role signal, so those milestones are mentor-verified."),
    },
    "fleet_commander": {
        "name": _("Fleet Commander"),
        "description": _("Grow from fleet member to a signed-off corp fleet commander."),
        "est_hours_note": _("High, mostly practice and review. Typical, not personalised."),
        "cost_note": _("Low — skills plus doctrine hulls you already fly."),
        "risk_note": _("Reputational fear is the real barrier; the path normalises supervised failure."),
        "income_note": _("None."),
    },
    "fleet_commander.ms.1": {
        "title": _("Comms and tooling ready (mumble/discord, broadcasts, fleet finder)"),
    },
    "fleet_commander.ms.2": {
        "title": _("Doctrine knowledge review with a mentor"),
    },
    "fleet_commander.ms.3": {
        "title": _("Train Leadership V"),
    },
    "fleet_commander.ms.4": {
        "title": _("Fly in 10 corp fleets"),
    },
    "fleet_commander.ms.5": {
        "title": _("Shadow an FC on 3 fleets (backseat/second)"),
    },
    "fleet_commander.ms.6": {
        "title": _("Target-calling and broadcast exercise"),
    },
    "fleet_commander.ms.7": {
        "title": _("Lead a training fleet"),
    },
    "fleet_commander.ms.8": {
        "title": _("After-action review of your fleet"),
    },
    "fleet_commander.ms.9": {
        "title": _("Officer sign-off for the corp FC list"),
    },
    "fleet_commander.ms.10": {
        "title": _("Plan a progression to larger fleets"),
    },
    "fleet_commander.ship.0": {
        "note": _("whatever the corp mainline doctrine flies; Command Ships at mastery"),
    },
    "fleet_commander.kb.fc-101": {
        "label": _("FC 101"),
    },
    "fleet_commander.kb.doctrine-theory": {
        "label": _("Doctrine theory"),
    },
    "fleet_commander.kb.after-action-reviews": {
        "label": _("After-action reviews"),
    },
    "fleet_commander.assumption.0": {
        "text": _("Officer-verified milestones require the goal shared at officers visibility."),
    },
    "black_ops_pilot": {
        "name": _("Black Ops Pilot"),
        "description": _("Fly stealth bombers and covert operations toward Black Ops."),
        "est_hours_note": _("High (long skill tails). Typical, not personalised."),
        "cost_note": _("Moderate (bombers) to very high (Black Ops battleships)."),
        "risk_note": _("Expensive losses are part of the trade."),
        "income_note": _("None direct."),
    },
    "black_ops_pilot.ms.1": {
        "title": _("Train Cloaking IV"),
    },
    "black_ops_pilot.ms.2": {
        "title": _("Own a Stealth Bomber"),
    },
    "black_ops_pilot.ms.3": {
        "title": _("Cloaky movement and bridge etiquette"),
    },
    "black_ops_pilot.ms.4": {
        "title": _("Qualify for the Black Ops doctrine (viable)"),
    },
    "black_ops_pilot.ms.5": {
        "title": _("Covert cyno exercise"),
    },
    "black_ops_pilot.ms.6": {
        "title": _("Participate in a Black Ops operation"),
    },
    "black_ops_pilot.ms.7": {
        "title": _("Train Cynosural Field Theory V"),
    },
    "black_ops_pilot.ms.8": {
        "title": _("Fly in 5 corp fleets"),
    },
    "black_ops_pilot.ms.9": {
        "title": _("Mastery review: hunter or bridge-capable"),
    },
    "black_ops_pilot.ship.0": {
        "note": _("stealth bomber (race-appropriate)"),
    },
    "black_ops_pilot.ship.1": {
        "note": _("mastery hull (race-appropriate)"),
    },
    "black_ops_pilot.kb.blops-etiquette": {
        "label": _("Black Ops etiquette"),
    },
    "black_ops_pilot.kb.covert-cyno": {
        "label": _("Covert cyno"),
    },
    "black_ops_pilot.kb.hunting-basics": {
        "label": _("Hunting basics"),
    },
    "black_ops_pilot.assumption.0": {
        "text": _("Degrades to a doctrine-free plan when no Black Ops doctrine exists (brief §12)."),
    },
    "black_ops_pilot.assumption.1": {
        "text": _("The covert cyno alt is a separate goal on that character."),
    },
    "miner_foundation": {
        "name": _("Mining Foundation"),
        "description": _("Start mining safely and grow into a barge with corp buyback."),
        "est_hours_note": _("Low to start. Typical, not personalised."),
        "cost_note": _("Near zero (Venture) to modest (Procurer)."),
        "risk_note": _("Gank exposure in a barge — the tanky-barge lesson exists for a reason."),
        "income_note": _("Steady, low risk."),
    },
    "miner_foundation.ms.1": {
        "title": _("Own a Venture"),
    },
    "miner_foundation.ms.2": {
        "title": _("Train Mining III"),
    },
    "miner_foundation.ms.3": {
        "title": _("Mine with the corp (first recorded corp mining)"),
    },
    "miner_foundation.ms.4": {
        "title": _("Train Industry V"),
    },
    "miner_foundation.ms.5": {
        "title": _("Own a mining barge"),
    },
    "miner_foundation.ms.6": {
        "title": _("Sell ore through the corp buyback"),
    },
    "miner_foundation.ms.7": {
        "title": _("Join a corp mining fleet"),
    },
    "miner_foundation.ms.8": {
        "title": _("Ore choices and compression basics"),
    },
    "miner_foundation.ship.0": {
        "note": _("trivially cheap starter"),
    },
    "miner_foundation.ship.1": {
        "note": _("the tanky choice"),
    },
    "miner_foundation.kb.buyback-howto": {
        "label": _("Buyback how-to"),
    },
    "miner_foundation.kb.ore-guide": {
        "label": _("Ore guide"),
    },
    "miner_foundation.kb.mining-fleet-etiquette": {
        "label": _("Mining fleet etiquette"),
    },
    "miner_foundation.assumption.0": {
        "text": _(
            "Only corp-observer (moon) mining is recorded; belt/solo mining needs a personal ESI scope that "
            "is not integrated, so those tonnes do not count automatically."
        ),
    },
    "t2_industrialist": {
        "name": _("Tech II Industrialist"),
        "description": _("Move from T1 production into invention and Tech II manufacturing."),
        "est_hours_note": _("High (science skill tails). Typical, not personalised."),
        "cost_note": _("Significant setup — blueprints, datacores, invention materials, lab access."),
        "risk_note": _("Market risk, not ship risk."),
        "income_note": _("The strongest sustained ISK path in the catalogue."),
    },
    "t2_industrialist.ms.1": {
        "title": _("Train Industry V"),
    },
    "t2_industrialist.ms.2": {
        "title": _("Deliver a T1 production batch to the corp"),
    },
    "t2_industrialist.ms.3": {
        "title": _("Set up research/invention access (lab, BPOs, datacores)"),
    },
    "t2_industrialist.ms.4": {
        "title": _("Train your invention prerequisites"),
    },
    "t2_industrialist.ms.5": {
        "title": _("Complete your first invention job"),
    },
    "t2_industrialist.ms.6": {
        "title": _("Complete your first T2 manufacturing run"),
    },
    "t2_industrialist.ms.7": {
        "title": _("Price and margin review with the market tools"),
    },
    "t2_industrialist.ms.8": {
        "title": _("Deliver 3 corp production jobs"),
    },
    "t2_industrialist.ms.9": {
        "title": _("Production line review with a mentor"),
    },
    "t2_industrialist.kb.invention-setup": {
        "label": _("Invention setup"),
    },
    "t2_industrialist.kb.industry-margins": {
        "label": _("Industry margins"),
    },
    "t2_industrialist.kb.t2-production-chains": {
        "label": _("T2 production chains"),
    },
    "t2_industrialist.assumption.0": {
        "text": _(
            "The invention prerequisite defaults to a Gallente line; the plan expander personalises the "
            "encryption and engineering-science skills to the pilot's chosen invention line."
        ),
    },
    "t2_industrialist.assumption.1": {
        "text": _(
            "Corp-delivered batches verify automatically; personal invention/manufacturing jobs are "
            "self-certified (jobs are snapshot-replaced upstream)."
        ),
    },
    "planetary_industry": {
        "name": _("Planetary Industrialist"),
        "description": _("Build passive income from planetary production chains."),
        "est_hours_note": _("Very low ongoing after setup. Typical, not personalised."),
        "cost_note": _("Low (command centers plus customs fees)."),
        "risk_note": _("Minimal in high-sec; customs-office exposure elsewhere."),
        "income_note": _("Modest, passive."),
    },
    "planetary_industry.ms.1": {
        "title": _("Train Command Center Upgrades III"),
    },
    "planetary_industry.ms.2": {
        "title": _("Establish your first colony"),
    },
    "planetary_industry.ms.3": {
        "title": _("Run a P2 production chain"),
    },
    "planetary_industry.ms.4": {
        "title": _("Train Interplanetary Consolidation IV"),
    },
    "planetary_industry.ms.5": {
        "title": _("Run a multi-planet P3 chain"),
    },
    "planetary_industry.ms.6": {
        "title": _("Exports, imports and customs fees lesson"),
    },
    "planetary_industry.ms.7": {
        "title": _("Monthly profit review"),
    },
    "planetary_industry.ship.0": {
        "note": _("the PI-dedicated pickup hull"),
    },
    "planetary_industry.kb.pi-chains": {
        "label": _("PI chains"),
    },
    "planetary_industry.kb.pi-customs": {
        "label": _("PI customs"),
    },
    "planetary_industry.kb.pi-planning": {
        "label": _("PI planning"),
    },
    "planetary_industry.assumption.0": {
        "text": _("Colony milestones are self-certified; only existence/count/level is ever observed."),
    },
    "hauler": {
        "name": _("Hauler"),
        "description": _("Move goods safely and run corp courier contracts."),
        "est_hours_note": _("Low-moderate. Typical, not personalised."),
        "cost_note": _("Low (T1 industrial) rising with hull class."),
        "risk_note": _("Gank corridors — the safety lessons are load-bearing."),
        "income_note": _("Freight fees via the corp logistics programme."),
    },
    "hauler.ms.1": {
        "title": _("Own a T1 industrial"),
    },
    "hauler.ms.2": {
        "title": _("Tank-versus-cargo fitting lesson"),
    },
    "hauler.ms.3": {
        "title": _("Safe travel drill: instadocks, alignment, no autopilot with cargo"),
    },
    "hauler.ms.4": {
        "title": _("Complete your first verified corp courier contract"),
    },
    "hauler.ms.5": {
        "title": _("Complete 5 verified corp deliveries"),
    },
    "hauler.ms.6": {
        "title": _("Train Racial Industrial V"),
    },
    "hauler.ms.7": {
        "title": _("Own a DST or Blockade Runner"),
    },
    "hauler.ms.8": {
        "title": _("Freight pricing and collateral review"),
    },
    "hauler.ship.0": {
        "note": _("or another racial T1 industrial"),
    },
    "hauler.ship.1": {
        "note": _("mastery hull"),
    },
    "hauler.kb.hauler-fitting": {
        "label": _("Hauler fitting"),
    },
    "hauler.kb.freight-pricing": {
        "label": _("Freight pricing"),
    },
    "hauler.kb.gank-avoidance": {
        "label": _("Gank avoidance"),
    },
    "hauler.assumption.0": {
        "text": _(
            "Courier milestones verify via corp courier contracts reaching VERIFIED; where contract "
            "verification is unavailable the state is honest unknown, not assumed complete."
        ),
    },
    "explorer": {
        "name": _("Explorer"),
        "description": _("Scan down relic and data sites and bring the loot home."),
        "est_hours_note": _("Low to start. Typical, not personalised."),
        "cost_note": _("Near zero — a fitted T1 exploration frigate."),
        "risk_note": _("You will lose ships; carry nothing you can't replace."),
        "income_note": _("Lumpy but real (relic loot)."),
    },
    "explorer.ms.1": {
        "title": _("Own an exploration frigate"),
    },
    "explorer.ms.2": {
        "title": _("Train Astrometrics III"),
    },
    "explorer.ms.3": {
        "title": _("Scan down and complete your first relic or data site"),
    },
    "explorer.ms.4": {
        "title": _("Travel a low-sec loop and come home"),
    },
    "explorer.ms.5": {
        "title": _("Bookmark and escape drills (safes, cloak-warp)"),
    },
    "explorer.ms.6": {
        "title": _("Train Cloaking IV"),
    },
    "explorer.ms.7": {
        "title": _("Bank 50M ISK from exploration loot"),
    },
    "explorer.ms.8": {
        "title": _("Share a find: loot review or map note"),
    },
    "explorer.ship.0": {
        "note": _("or another T1 exploration frigate"),
    },
    "explorer.ship.1": {
        "note": _("mastery hull; Astero is premium"),
    },
    "explorer.kb.scanning-101": {
        "label": _("Scanning 101"),
    },
    "explorer.kb.hacking-minigame": {
        "label": _("Hacking minigame"),
    },
    "explorer.kb.exploration-fits": {
        "label": _("Exploration fits"),
    },
    "explorer.assumption.0": {
        "text": _("Site completions leave no ESI trace, so those milestones are self-certified by design."),
    },
    "wormhole_explorer": {
        "name": _("Wormhole Explorer"),
        "description": _("Take exploration into wormhole space with the judgement it demands."),
        "est_hours_note": _("Moderate on skills, high on judgement. Typical, not personalised."),
        "cost_note": _("Low-moderate (covert ops recommended)."),
        "risk_note": _("Everyone in J-space is hunting you; local doesn't exist."),
        "income_note": _("Strong (J-space relic sites)."),
    },
    "wormhole_explorer.ms.1": {
        "title": _("Train Astrometrics IV"),
    },
    "wormhole_explorer.ms.2": {
        "title": _("Wormhole identification: class, mass, lifetime reading"),
    },
    "wormhole_explorer.ms.3": {
        "title": _("Map a chain with bookmarks and naming convention"),
    },
    "wormhole_explorer.ms.4": {
        "title": _("D-scan discipline drill (constant scan while working)"),
    },
    "wormhole_explorer.ms.5": {
        "title": _("Run sites in J-space and bring the loot home"),
    },
    "wormhole_explorer.ms.6": {
        "title": _("Own a Covert Ops frigate"),
    },
    "wormhole_explorer.ms.7": {
        "title": _("Home-hole operations / rolling basics (if the corp runs a WH group)"),
    },
    "wormhole_explorer.ms.8": {
        "title": _("Review with a wormhole mentor"),
    },
    "wormhole_explorer.ship.0": {
        "note": _("or Astero; a T1 exploration frigate is acceptable with the risk stated"),
    },
    "wormhole_explorer.kb.wormhole-ids": {
        "label": _("Wormhole IDs"),
    },
    "wormhole_explorer.kb.chain-mapping": {
        "label": _("Chain mapping"),
    },
    "wormhole_explorer.kb.jspace-survival": {
        "label": _("J-space survival"),
    },
    "wormhole_explorer.assumption.0": {
        "text": _("Site and chain milestones leave no ESI trace, so they are human-verified."),
    },
    "mentor": {
        "name": _("Mentor"),
        "description": _("Give back: register as a mentor and guide new pilots."),
        "est_hours_note": _("Whatever you choose to give. Typical, not personalised."),
        "cost_note": _("None."),
        "risk_note": _("None."),
        "income_note": _("None (mentorship reward caps exist in the programme config)."),
    },
    "mentor.ms.1": {
        "title": _("Meet the mentor eligibility bar (tenure/character age per programme config)"),
    },
    "mentor.ms.2": {
        "title": _("Register a mentor profile with your focus areas"),
    },
    "mentor.ms.3": {
        "title": _("Take your first mentee pairing"),
    },
    "mentor.ms.4": {
        "title": _("Guide a mentee through a learning track"),
    },
    "mentor.ms.5": {
        "title": _("Endorse a mentee's practical milestone in Capsuleer Path"),
    },
    "mentor.ms.6": {
        "title": _("Contribute or improve a KB lesson page"),
    },
    "mentor.ms.7": {
        "title": _("Officer recognition of sustained mentoring"),
    },
    "mentor.kb.mentoring-guide": {
        "label": _("Mentoring guide"),
    },
    "mentor.kb.writing-lessons": {
        "label": _("Writing lessons"),
    },
    "mentor.assumption.0": {
        "text": _("This path is about people, not SP — no skill plan is generated for it."),
    },
    "mentor.assumption.1": {
        "text": _("Milestones 1 and 7 require the goal shared at officers visibility."),
    },
}


def _english_catalogue() -> dict[str, dict[str, str]]:
    """The shipped English behind every catalogue entry, walked out of ``templates_builtin``.

    Derived, never hand-written, so it is *exactly* the text the seed wrote into the row and
    ``instantiate_template`` copied onto the goal — which is what makes the edit test in
    :func:`render` trustworthy.
    """
    out: dict[str, dict[str, str]] = {}

    def put(key: str, **fields: str | None) -> None:
        values = {name: value for name, value in fields.items() if value}
        if values:
            out.setdefault(key, {}).update(values)

    for t in templates_builtin.BUILTIN:
        k = t["key"]
        structure = t["structure"]
        put(
            template_key(k), name=t.get("name"),
            # The ``description`` column is seeded from the structure summary (``_model_fields``).
            description=structure.get("summary"),
            est_hours_note=t.get("est_hours_note"), cost_note=t.get("cost_note"),
            risk_note=t.get("risk_note"), income_note=t.get("income_note"),
        )
        for ms in structure.get("milestones", []):
            put(milestone_key(k, ms["order"]), title=ms.get("title"))
        for i, ship in enumerate(structure.get("ship_targets", [])):
            put(ship_key(k, i), note=ship.get("note"))
        for kb in structure.get("knowledge_links", []):
            put(knowledge_key(k, kb["kb_slug"]), label=kb.get("label"))
        for i, assumption in enumerate(structure.get("assumptions", [])):
            put(assumption_key(k, i), text=assumption)
    return out


BUILTIN_ENGLISH: dict[str, dict[str, str]] = _english_catalogue()


def msgid(source_key: str, field: str) -> str | None:
    """The ``gettext_lazy`` msgid for a built-in field — ``None`` for corp content/unknown keys."""
    return BUILTIN_MSGIDS.get(source_key or "", {}).get(FIELD_ALIASES.get(field, field))


def english(source_key: str, field: str) -> str:
    """The English this built-in field ships (and was seeded/copied) with, or ``""``."""
    return BUILTIN_ENGLISH.get(source_key or "", {}).get(FIELD_ALIASES.get(field, field), "")


def render(source_key: str, field: str, stored: str, *, max_length: int | None = None) -> str:
    """The seam. Returns the translation while the row still holds the shipped English, and the
    stored text verbatim once a pilot has edited it. Never blanks, never raises.

    ``max_length`` is the *stored* column's limit: ``instantiate_template`` truncates as it copies
    (``title[:140]``), so the shipped English has to be truncated the same way before the
    comparison — otherwise a long built-in title would always look "edited" and would never
    translate.
    """
    stored = stored or ""
    proxy = msgid(source_key, field)
    if proxy is None:
        return stored  # corp template or pilot-authored row — gettext never touches user content
    shipped = english(source_key, field)
    if max_length:
        shipped = shipped[:max_length]
    if stored != shipped:
        return stored  # edited: the pilot's own words, verbatim, in every locale
    # str() resolves the lazy proxy NOW, under the active locale; a missing msgstr falls back to
    # the English msgid, so this branch can never blank the row.
    return str(proxy) or stored


# The provenance attribute to read, in order: a copied milestone carries ``source_key``; a goal
# carries the ``template_key`` it was instantiated from; a ``CareerTemplate`` row *is* the built-in
# and carries ``key``.
_KEY_ATTRS = ("source_key", "template_key", "key")


def provenance_key(obj) -> str:
    """The catalogue key for a row. The *first attribute that exists* wins, even when empty — a
    custom milestone has an empty ``source_key`` and must stop there rather than fall through to
    some other identifier on the row."""
    for attr in _KEY_ATTRS:
        if hasattr(obj, attr):
            return getattr(obj, attr) or ""
    return ""


def text(obj, field: str, *, source_key: str | None = None, msgid_field: str | None = None) -> str:
    """:func:`render` for a model row — reads the stored value, its provenance key and its
    ``max_length`` off the field itself.

    ``msgid_field`` overrides the catalogue field name when the column is named differently from
    the blueprint field it was copied from (``CareerGoal.title`` holds the template's ``name``).
    """
    if source_key is None:
        source_key = provenance_key(obj)
    try:
        max_length = obj._meta.get_field(field).max_length
    except FieldDoesNotExist:
        max_length = None
    return render(
        source_key, msgid_field or field, getattr(obj, field, "") or "", max_length=max_length
    )
