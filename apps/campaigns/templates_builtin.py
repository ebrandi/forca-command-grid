"""Built-in campaign templates — the reusable blueprints leaders start a campaign from.

A :class:`~apps.campaigns.models.CampaignTemplate` carries a ``blueprint`` JSON document that is
**structure only** (design doc 04 §13, brief §3): titles, descriptions, units, weights,
workstream keys, milestone titles, risk entries and relative day-offsets — never a user
reference, an absolute date, or a baked-in EVE id. Params that need an instance id (a doctrine,
a stockpile, a wallet division, an operation type) are left blank for the leader to fill after
instantiation; each such objective's description says so.

The blueprint schema (all keys optional unless noted):

``category``            campaign category (``Campaign.Category`` value).
``window_days``         suggested campaign length; materialises ``target_end_at`` when a start
                        date is given but no explicit end (doc 04 §13 "offsets, not absolute dates").
``summary`` / ``rationale`` / ``desired_outcome`` / ``success_criteria`` / ``failure_criteria``
                        suggested prose copied onto the draft (all editable afterwards).
``workstreams``         ``[{key, name, description, sort_order}]`` — ``key`` is the stable slug
                        objectives/milestones/risks reference to attach to a lane.
``objectives``          ``[{title, description, workstream, unit, direction, weight,
                        target_value, baseline_value, is_mandatory, requires_verification,
                        help_wanted, is_sensitive, metric_source, metric_params, due_offset_days,
                        sort_order}]``.
``milestones``          ``[{title, description, workstream, due_offset_days, sort_order}]``.
``risks``               ``[{description, probability, impact, workstream, mitigation,
                        contingency, trigger}]`` — the example blockers, pre-scored.

:func:`seed_builtin_templates` upserts every blueprint here by ``key`` (idempotent, re-runnable
from the data migration); :func:`apps.campaigns.services.instantiate_template` materialises one
into a draft campaign. This mirrors ``apps.raffle.contest_templates`` in shape.
"""
from __future__ import annotations

# Instance-bound metric-param keys — an EVE id or a per-campaign selection the leader must supply.
# Blueprints never carry these (they are stripped by save-as-template and absent from the builtins);
# generic tuning knobs like ``active_days`` are kept.
INSTANCE_PARAM_KEYS = frozenset({
    "doctrine_id", "stockpile_id", "type_id", "type_ids", "structure_ids", "division", "period",
    "op_type", "dimension",
})


# --------------------------------------------------------------------------- #
#  The reference campaign (requirements §21) — "Establish Armour Battleship
#  Deployment Readiness": 30-day window, 12 objectives, 9 workstreams, the
#  blocker list pre-scored as risks. This is the acceptance-scenario blueprint.
# --------------------------------------------------------------------------- #
_ARMOUR_BS = {
    "key": "armour_bs_deployment",
    "name": "Establish Armour Battleship Deployment Readiness",
    "description": (
        "The reference deployment-readiness campaign: qualify pilots into the armour battleship "
        "doctrine, stage fitted hulls and consumables, secure an SRP reserve, prove the logistics "
        "route and finish with a live readiness exercise. Fill in the doctrine, stockpile, wallet "
        "and operation selections after creating — every id is left blank on purpose."
    ),
    "category": "deployment",
    "window_days": 30,
    "summary": "Stand up an armour battleship fleet in the new staging within 30 days.",
    "rationale": (
        "A credible armour battleship fleet in staging is the deterrent this deployment turns on — "
        "pilots trained, hulls fitted and staged, SRP funded, and the supply route proven before we "
        "commit."
    ),
    "desired_outcome": (
        "An armour battleship fleet that can undock at strength from the new staging, with trained "
        "pilots, staged replacements, a funded SRP reserve and a validated logistics route."
    ),
    "success_criteria": (
        "35 mainline + 12 logistics pilots qualified; 50 mainline and 15 logistics hulls fitted and "
        "staged; 30 replacement hulls and 5 fleets' consumables stocked; ≥3 FCs across TZs; route "
        "validated; SRP reserve funded; final readiness exercise passed."
    ),
    "failure_criteria": (
        "Deployment date reached without a fleet that can field mainline + logistics at strength, or "
        "with no funded SRP reserve / no proven supply route."
    ),
    "workstreams": [
        {"key": "doctrine", "name": "Doctrine & fitting",
         "description": "Agree, approve and publish the armour battleship doctrine and fits.",
         "sort_order": 0},
        {"key": "training", "name": "Training",
         "description": "Qualify pilots into the doctrine and run training fleets.",
         "sort_order": 1},
        {"key": "manufacturing", "name": "Manufacturing",
         "description": "Build and fit the mainline, logistics and replacement hulls.",
         "sort_order": 2},
        {"key": "procurement", "name": "Procurement",
         "description": "Source ammunition, consumables and modules for the fleets.",
         "sort_order": 3},
        {"key": "logistics", "name": "Logistics",
         "description": "Validate the route and move staged assets before the deadline.",
         "sort_order": 4},
        {"key": "finance", "name": "Finance & SRP",
         "description": "Fund and hold the agreed SRP reserve for the deployment.",
         "sort_order": 5},
        {"key": "fc_coverage", "name": "FC coverage",
         "description": "Ensure qualified fleet commanders across the covered timezones.",
         "sort_order": 6},
        {"key": "communications", "name": "Communications",
         "description": "Brief the corp, publish the schedule and keep pilots informed.",
         "sort_order": 7},
        {"key": "final_validation", "name": "Final validation",
         "description": "Run the final readiness exercise and sign the fleet off.",
         "sort_order": 8},
    ],
    "objectives": [
        {"title": "Qualify 35 mainline doctrine pilots", "workstream": "doctrine",
         "description": "Pilots flying the mainline armour battleship fit. Set the doctrine to "
                        "measure after creating the campaign.",
         "unit": "pilots", "direction": "gte", "weight": 3, "target_value": 35,
         "baseline_value": 0, "is_mandatory": True,
         "metric_source": "doctrine.qualified_pilots", "metric_params": {"active_days": 30},
         "due_offset_days": 24, "sort_order": 0},
        {"title": "Qualify 12 logistics pilots", "workstream": "training",
         "description": "Pilots flying the fleet logistics fit. Set the logistics doctrine to "
                        "measure after creating the campaign.",
         "unit": "pilots", "direction": "gte", "weight": 2, "target_value": 12,
         "baseline_value": 0, "is_mandatory": True,
         "metric_source": "doctrine.qualified_pilots", "metric_params": {"active_days": 30},
         "due_offset_days": 24, "sort_order": 1},
        {"title": "Stage 50 fitted mainline hulls", "workstream": "manufacturing",
         "description": "Fitted mainline hulls on hand in the staging structure. Point this at the "
                        "staging stockpile and hull type after creating.",
         "unit": "ships", "direction": "gte", "weight": 3, "target_value": 50,
         "baseline_value": 0, "is_mandatory": True,
         "metric_source": "stockpile.on_hand", "metric_params": {},
         "due_offset_days": 26, "sort_order": 2},
        {"title": "Stage 15 fitted logistics hulls", "workstream": "manufacturing",
         "description": "Fitted logistics hulls on hand in staging. Point this at the staging "
                        "stockpile and logistics hull type after creating.",
         "unit": "ships", "direction": "gte", "weight": 2, "target_value": 15,
         "baseline_value": 0, "is_mandatory": True,
         "metric_source": "stockpile.on_hand", "metric_params": {},
         "due_offset_days": 26, "sort_order": 3},
        {"title": "Build 30 replacement hulls", "workstream": "manufacturing",
         "description": "Spare hulls held against losses. Point this at the replacement stockpile "
                        "after creating.",
         "unit": "hulls", "direction": "gte", "weight": 2, "target_value": 30,
         "baseline_value": 0,
         "metric_source": "stockpile.on_hand", "metric_params": {},
         "due_offset_days": 28, "sort_order": 4},
        {"title": "Stock ammunition & consumables for 5 fleets", "workstream": "procurement",
         "description": "Ammo, charges and consumables sufficient for five fleet outings. Point "
                        "this at the consumables stockpile after creating.",
         "unit": "sets", "direction": "gte", "weight": 2, "target_value": 5,
         "baseline_value": 0,
         "metric_source": "stockpile.on_hand", "metric_params": {},
         "due_offset_days": 26, "sort_order": 5},
        {"title": "Confirm 3 qualified FCs across timezones", "workstream": "fc_coverage",
         "description": "Fleet commanders able to run the doctrine, spread across the covered "
                        "timezones. Tracked manually and verified.",
         "unit": "FCs", "direction": "gte", "weight": 2, "target_value": 3,
         "baseline_value": 0, "is_mandatory": True, "requires_verification": True,
         "metric_source": "", "metric_params": {},
         "due_offset_days": 20, "sort_order": 6},
        {"title": "Validate the logistics route", "workstream": "logistics",
         "description": "A confirmed, tested haul route into the staging system. Tracked manually "
                        "(0 = untested, 1 = validated) and verified.",
         "unit": "route", "direction": "gte", "weight": 1, "target_value": 1,
         "baseline_value": 0, "is_mandatory": True, "requires_verification": True,
         "metric_source": "", "metric_params": {},
         "due_offset_days": 12, "sort_order": 7},
        {"title": "Move staged assets before the deadline", "workstream": "logistics",
         "description": "Percentage of the staging move completed. Tracked manually.",
         "unit": "%", "direction": "gte", "weight": 2, "target_value": 100,
         "baseline_value": 0,
         "metric_source": "", "metric_params": {},
         "due_offset_days": 27, "sort_order": 8},
        {"title": "Secure the agreed SRP reserve", "workstream": "finance",
         "description": "SRP reserve (allocated − spent − exposure) held for the deployment. Set "
                        "the reserve target and period after creating; visible to finance only.",
         "unit": "ISK", "direction": "gte", "weight": 2, "target_value": 10000000000,
         "baseline_value": 0, "is_mandatory": True, "is_sensitive": True,
         "metric_source": "srp.reserve", "metric_params": {},
         "due_offset_days": 18, "sort_order": 9},
        {"title": "Run 3 training fleets", "workstream": "training",
         "description": "Completed training operations of the doctrine. Pick the operation type to "
                        "count after creating.",
         "unit": "fleets", "direction": "gte", "weight": 1, "target_value": 3,
         "baseline_value": 0,
         "metric_source": "operations.completed", "metric_params": {},
         "due_offset_days": 22, "sort_order": 10},
        {"title": "Complete 1 final readiness exercise", "workstream": "final_validation",
         "description": "A full dress-rehearsal fleet sign-off. Tracked manually and verified.",
         "unit": "exercise", "direction": "gte", "weight": 3, "target_value": 1,
         "baseline_value": 0, "is_mandatory": True, "requires_verification": True,
         "metric_source": "", "metric_params": {},
         "due_offset_days": 29, "sort_order": 11},
    ],
    "milestones": [
        {"title": "Doctrine approved & published", "workstream": "doctrine",
         "due_offset_days": 5, "sort_order": 0},
        {"title": "SRP reserve secured", "workstream": "finance",
         "due_offset_days": 18, "sort_order": 1},
        {"title": "First training fleet complete", "workstream": "training",
         "due_offset_days": 14, "sort_order": 2},
        {"title": "Manufacturing line running", "workstream": "manufacturing",
         "due_offset_days": 10, "sort_order": 3},
        {"title": "Assets staged in system", "workstream": "logistics",
         "due_offset_days": 27, "sort_order": 4},
        {"title": "Final readiness exercise", "workstream": "final_validation",
         "due_offset_days": 29, "sort_order": 5},
    ],
    "risks": [
        {"description": "Insufficient logistics pilots qualified in time", "workstream": "training",
         "probability": "high", "impact": "high",
         "mitigation": "Run dedicated logi training fleets; recruit from allied corps.",
         "contingency": "Deploy with a reduced logi wing and a fitted-ship reserve.",
         "trigger": "Fewer than 8 logi pilots two weeks out."},
        {"description": "Missing mainline hulls at staging", "workstream": "manufacturing",
         "probability": "medium", "impact": "high",
         "mitigation": "Front-load the build queue; buy hulls to cover the shortfall.",
         "contingency": "Charter freight of pre-built hulls from trade hubs.",
         "trigger": "Staged hulls below 60 percent of target at week three."},
        {"description": "Ammunition / consumable material shortage", "workstream": "procurement",
         "probability": "medium", "impact": "high",
         "mitigation": "Place standing buy orders early; diversify suppliers.",
         "contingency": "Ration consumables to priority fleets.",
         "trigger": "Any consumable line below one fleet's worth."},
        {"description": "Inadequate SRP reserve", "workstream": "finance",
         "probability": "medium", "impact": "high",
         "mitigation": "Ring-fence a wallet division; schedule donations drive.",
         "contingency": "Cap SRP payouts to hull-only until the reserve recovers.",
         "trigger": "Reserve below the agreed floor at any point."},
        {"description": "No FC available in a covered timezone", "workstream": "fc_coverage",
         "probability": "medium", "impact": "high",
         "mitigation": "Train back-up FCs; publish an FC roster.",
         "contingency": "Consolidate fleets into covered timezones only.",
         "trigger": "A timezone with zero qualified FCs one week out."},
        {"description": "Delayed freight into the staging system", "workstream": "logistics",
         "probability": "high", "impact": "medium",
         "mitigation": "Validate the route early; pre-position a hauler.",
         "contingency": "Fall back to the secondary route or jump freighters.",
         "trigger": "Any critical shipment stalled beyond 48h."},
        {"description": "Incomplete doctrine approval", "workstream": "doctrine",
         "probability": "medium", "impact": "high",
         "mitigation": "Lock the fit sign-off as the first milestone.",
         "contingency": "Publish a provisional fit and iterate.",
         "trigger": "Doctrine unapproved past the week-one milestone."},
    ],
}


_DOCTRINE_ROLLOUT = {
    "key": "doctrine_rollout",
    "name": "Doctrine Rollout",
    "description": "Train the corp into a new doctrine: publish the fit, qualify pilots and prove "
                   "it in training fleets.",
    "category": "doctrine_rollout",
    "window_days": 28,
    "summary": "Get the corp flying a new doctrine within a month.",
    "desired_outcome": "A published doctrine with enough qualified, practised pilots to field it.",
    "success_criteria": "Doctrine approved; target pilots qualified; training fleets run.",
    "workstreams": [
        {"key": "doctrine", "name": "Doctrine & fitting", "sort_order": 0},
        {"key": "training", "name": "Training", "sort_order": 1},
        {"key": "communications", "name": "Communications", "sort_order": 2},
    ],
    "objectives": [
        {"title": "Approve & publish the doctrine", "workstream": "doctrine",
         "description": "Agree the fit and publish it. Tracked manually (0/1) and verified.",
         "unit": "doctrine", "direction": "gte", "weight": 2, "target_value": 1,
         "baseline_value": 0, "is_mandatory": True, "requires_verification": True,
         "due_offset_days": 5, "sort_order": 0},
        {"title": "Qualify 20 pilots", "workstream": "training",
         "description": "Pilots flying the doctrine fit. Set the doctrine to measure after creating.",
         "unit": "pilots", "direction": "gte", "weight": 3, "target_value": 20,
         "baseline_value": 0, "is_mandatory": True,
         "metric_source": "doctrine.qualified_pilots", "metric_params": {"active_days": 30},
         "due_offset_days": 24, "sort_order": 1},
        {"title": "Run 3 training fleets", "workstream": "training",
         "description": "Completed training operations. Pick the operation type after creating.",
         "unit": "fleets", "direction": "gte", "weight": 1, "target_value": 3,
         "baseline_value": 0,
         "metric_source": "operations.completed", "metric_params": {},
         "due_offset_days": 26, "sort_order": 2},
        {"title": "Publish the training schedule", "workstream": "communications",
         "description": "Schedule and briefing published to the corp. Tracked manually (0/1).",
         "unit": "post", "direction": "gte", "weight": 1, "target_value": 1,
         "baseline_value": 0, "due_offset_days": 3, "sort_order": 3, "help_wanted": True},
    ],
    "milestones": [
        {"title": "Doctrine published", "workstream": "doctrine", "due_offset_days": 5,
         "sort_order": 0},
        {"title": "Halfway on qualifications", "workstream": "training", "due_offset_days": 14,
         "sort_order": 1},
    ],
    "risks": [
        {"description": "Low pilot uptake of the doctrine", "workstream": "training",
         "probability": "medium", "impact": "high",
         "mitigation": "Incentivise with SRP and training fleets.",
         "trigger": "Under half the target qualified at the halfway milestone."},
        {"description": "Fit approval drags", "workstream": "doctrine",
         "probability": "medium", "impact": "medium",
         "mitigation": "Time-box the sign-off to week one."},
    ],
}


_DEPLOYMENT_PREP = {
    "key": "deployment_prep",
    "name": "Deployment Prep",
    "description": "Prepare a timed deployment: stage assets, validate the route and brief the "
                   "corp before the move.",
    "category": "deployment",
    "window_days": 21,
    "summary": "Ready the corp for a deployment in three weeks.",
    "desired_outcome": "Assets staged, route proven and the corp briefed for the deployment.",
    "success_criteria": "Route validated; assets staged; briefing published.",
    "workstreams": [
        {"key": "logistics", "name": "Logistics", "sort_order": 0},
        {"key": "manufacturing", "name": "Manufacturing", "sort_order": 1},
        {"key": "communications", "name": "Communications", "sort_order": 2},
    ],
    "objectives": [
        {"title": "Validate the staging route", "workstream": "logistics",
         "description": "A tested haul route into staging. Tracked manually (0/1) and verified.",
         "unit": "route", "direction": "gte", "weight": 2, "target_value": 1,
         "baseline_value": 0, "is_mandatory": True, "requires_verification": True,
         "due_offset_days": 7, "sort_order": 0},
        {"title": "Stage 40 fitted hulls", "workstream": "manufacturing",
         "description": "Fitted hulls on hand in staging. Point this at the staging stockpile.",
         "unit": "ships", "direction": "gte", "weight": 3, "target_value": 40,
         "baseline_value": 0, "is_mandatory": True,
         "metric_source": "stockpile.on_hand", "metric_params": {},
         "due_offset_days": 18, "sort_order": 1},
        {"title": "Move staged assets", "workstream": "logistics",
         "description": "Percentage of the move completed. Tracked manually.",
         "unit": "%", "direction": "gte", "weight": 2, "target_value": 100,
         "baseline_value": 0, "due_offset_days": 20, "sort_order": 2},
        {"title": "Publish the deployment briefing", "workstream": "communications",
         "description": "Deployment plan and schedule published. Tracked manually (0/1).",
         "unit": "post", "direction": "gte", "weight": 1, "target_value": 1,
         "baseline_value": 0, "due_offset_days": 4, "sort_order": 3},
    ],
    "milestones": [
        {"title": "Route validated", "workstream": "logistics", "due_offset_days": 7,
         "sort_order": 0},
        {"title": "Assets staged", "workstream": "logistics", "due_offset_days": 20,
         "sort_order": 1},
    ],
    "risks": [
        {"description": "Delayed freight into staging", "workstream": "logistics",
         "probability": "high", "impact": "medium",
         "mitigation": "Pre-position a hauler; validate the route early."},
        {"description": "Hulls not ready in time", "workstream": "manufacturing",
         "probability": "medium", "impact": "high",
         "mitigation": "Front-load the build queue; buy to cover the gap."},
    ],
}


_STOCKPILE_DRIVE = {
    "key": "stockpile_drive",
    "name": "Stockpile Drive",
    "description": "Rebuild replacement stock: manufacture hulls, deliver builds and top up "
                   "consumables to target levels.",
    "category": "stockpile",
    "window_days": 30,
    "summary": "Restore doctrine stock to target levels.",
    "desired_outcome": "Replacement hulls and consumables back at their target on-hand levels.",
    "success_criteria": "Hull stock and consumable stock at or above target.",
    "workstreams": [
        {"key": "manufacturing", "name": "Manufacturing", "sort_order": 0},
        {"key": "procurement", "name": "Procurement", "sort_order": 1},
    ],
    "objectives": [
        {"title": "Stock 50 replacement hulls", "workstream": "manufacturing",
         "description": "Hulls on hand against the replacement target. Point this at the stockpile.",
         "unit": "hulls", "direction": "gte", "weight": 3, "target_value": 50,
         "baseline_value": 0, "is_mandatory": True,
         "metric_source": "stockpile.on_hand", "metric_params": {},
         "due_offset_days": 28, "sort_order": 0},
        {"title": "Deliver 50 built hulls in the window", "workstream": "manufacturing",
         "description": "Hulls delivered from industry in this window. Set the output type ids "
                        "after creating.",
         "unit": "hulls", "direction": "gte", "weight": 2, "target_value": 50,
         "baseline_value": 0,
         "metric_source": "industry.deliveries", "metric_params": {},
         "due_offset_days": 28, "sort_order": 1},
        {"title": "Top up consumables for 10 fleets", "workstream": "procurement",
         "description": "Consumable sets on hand. Point this at the consumables stockpile.",
         "unit": "sets", "direction": "gte", "weight": 2, "target_value": 10,
         "baseline_value": 0,
         "metric_source": "stockpile.on_hand", "metric_params": {},
         "due_offset_days": 24, "sort_order": 2},
    ],
    "milestones": [
        {"title": "Build queue running", "workstream": "manufacturing", "due_offset_days": 5,
         "sort_order": 0},
        {"title": "Halfway to target", "workstream": "manufacturing", "due_offset_days": 15,
         "sort_order": 1},
    ],
    "risks": [
        {"description": "Build material shortage", "workstream": "procurement",
         "probability": "medium", "impact": "high",
         "mitigation": "Secure minerals ahead of the queue."},
    ],
}


_SRP_RECOVERY = {
    "key": "srp_recovery",
    "name": "SRP Reserve Recovery",
    "description": "Restore a depleted SRP reserve: fund the reserve, run a donations drive and "
                   "hold it above the agreed floor.",
    "category": "srp_reserve",
    "window_days": 30,
    "summary": "Rebuild the SRP reserve to its agreed floor.",
    "desired_outcome": "SRP reserve funded above the agreed floor and holding.",
    "success_criteria": "Reserve at or above the target for the review period.",
    "workstreams": [
        {"key": "finance", "name": "Finance & SRP", "sort_order": 0},
        {"key": "communications", "name": "Communications", "sort_order": 1},
    ],
    "objectives": [
        {"title": "Fund the SRP reserve to target", "workstream": "finance",
         "description": "SRP reserve (allocated − spent − exposure). Set the target and period "
                        "after creating; visible to finance only.",
         "unit": "ISK", "direction": "gte", "weight": 3, "target_value": 10000000000,
         "baseline_value": 0, "is_mandatory": True, "is_sensitive": True,
         "metric_source": "srp.reserve", "metric_params": {},
         "due_offset_days": 28, "sort_order": 0},
        {"title": "Run a donations drive", "workstream": "communications",
         "description": "Donations campaign published and running. Tracked manually (0/1).",
         "unit": "drive", "direction": "gte", "weight": 1, "target_value": 1,
         "baseline_value": 0, "help_wanted": True, "due_offset_days": 5, "sort_order": 1},
    ],
    "milestones": [
        {"title": "Reserve halfway to floor", "workstream": "finance", "due_offset_days": 15,
         "sort_order": 0},
    ],
    "risks": [
        {"description": "Continued SRP losses outpace funding", "workstream": "finance",
         "probability": "medium", "impact": "high",
         "mitigation": "Cap payouts to hull-only until the reserve recovers.",
         "trigger": "Reserve falling for two consecutive weeks."},
    ],
}


_MEMBER_INTEGRATION = {
    "key": "member_integration",
    "name": "New Member Integration",
    "description": "Bring a new-member intake up to speed: assign mentors, complete onboarding and "
                   "get pilots into their first fleets.",
    "category": "membership",
    "window_days": 45,
    "summary": "Integrate the new-member intake over six weeks.",
    "desired_outcome": "New members mentored, onboarded and flying in fleets.",
    "success_criteria": "Mentors assigned; onboarding complete; first fleets flown.",
    "workstreams": [
        {"key": "recruitment", "name": "Recruitment & mentorship", "sort_order": 0},
        {"key": "training", "name": "Training", "sort_order": 1},
    ],
    "objectives": [
        {"title": "Assign mentors to every new member", "workstream": "recruitment",
         "description": "New members with an assigned mentor. Tracked manually.",
         "unit": "pilots", "direction": "gte", "weight": 2, "target_value": 10,
         "baseline_value": 0, "is_mandatory": True, "due_offset_days": 7, "sort_order": 0},
        {"title": "Complete onboarding checklist", "workstream": "recruitment",
         "description": "New members through the onboarding checklist. Tracked manually.",
         "unit": "pilots", "direction": "gte", "weight": 2, "target_value": 10,
         "baseline_value": 0, "due_offset_days": 21, "sort_order": 1},
        {"title": "Fly first fleets", "workstream": "training",
         "description": "New members who have flown at least one fleet. Tracked manually.",
         "unit": "pilots", "direction": "gte", "weight": 2, "target_value": 10,
         "baseline_value": 0, "help_wanted": True, "due_offset_days": 40, "sort_order": 2},
    ],
    "milestones": [
        {"title": "Mentors assigned", "workstream": "recruitment", "due_offset_days": 7,
         "sort_order": 0},
        {"title": "Onboarding complete", "workstream": "recruitment", "due_offset_days": 21,
         "sort_order": 1},
    ],
    "risks": [
        {"description": "New members go inactive before integrating", "workstream": "recruitment",
         "probability": "high", "impact": "medium",
         "mitigation": "Early mentor contact; welcome fleets in the first week.",
         "trigger": "A new member with no activity for ten days."},
    ],
}


BUILTIN: list[dict] = [
    _ARMOUR_BS,
    _DOCTRINE_ROLLOUT,
    _DEPLOYMENT_PREP,
    _STOCKPILE_DRIVE,
    _SRP_RECOVERY,
    _MEMBER_INTEGRATION,
]

BUILTIN_BY_KEY = {t["key"]: t for t in BUILTIN}
BUILTIN_KEYS = [t["key"] for t in BUILTIN]


def seed_builtin_templates(model=None) -> int:
    """Seed every built-in blueprint by ``key`` with ``get_or_create`` — idempotent and, crucially,
    a re-run **never overwrites operator edits** (doc 06 §9, #43): a deactivated or edited builtin
    stays as the operator left it. ``model`` lets the migration pass its historical
    ``CampaignTemplate`` so the seed is schema-stable."""
    if model is None:
        from .models import CampaignTemplate as model  # noqa: N813

    n = 0
    for t in BUILTIN:
        blueprint = {k: v for k, v in t.items() if k not in ("key", "name", "description", "category")}
        model.objects.get_or_create(
            key=t["key"],
            defaults={
                "name": t["name"],
                "description": t["description"],
                "category": t["category"],
                "blueprint": blueprint,
                "is_builtin": True,
                "active": True,
            },
        )
        n += 1
    return n
