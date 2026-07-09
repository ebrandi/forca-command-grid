"""Create-only seed: the programme singleton, a default cohort, 12 learning tracks
with field exercises, recognition badges, and starter reward rules.

Create-only (``get_or_create`` by natural key, ``reverse=noop``) so leader edits
are never clobbered and re-running is idempotent — test DBs always get the seed.
Validation methods/criteria use only id-less auto-checks (doctrine_any, killmails,
fleet/courier/buyback/mining/industry, sessions) that work without per-corp type
ids; leaders can add id-specific checks (skill_min, doctrine_ready) later.
"""
from __future__ import annotations

from django.db import migrations

# Track category constants (mirror MentorshipTrack.Category).
WELCOME, CLIENT, TRAVEL, FITTING = "welcome", "client", "travel", "fitting"
RATTING, MINING, EXPLORATION, PVP = "ratting", "mining", "exploration", "pvp"
FLEET, LOGISTICS, INDUSTRY, SKILLS = "fleet", "logistics", "industry", "skills"

# Validation method constants (mirror MentorshipTask.Validation).
MANUAL, MENTEE, DUAL, LEADER = "manual_mentor", "mentee_confirm", "dual_confirm", "leadership"
API_ASSIST, API_REQ, EVIDENCE, AUTO, HYBRID = (
    "api_assisted", "api_required", "evidence", "auto_internal", "hybrid")

BADGES = [
    ("cadet-first-steps", "First Steps", "Completed corp onboarding.", "i-rookie", "bronze", "mentee"),
    ("cadet-navigator", "Navigator", "Learned to travel and survive.", "i-route", "bronze", "mentee"),
    ("cadet-first-blood", "First Blood", "Scored a first PvP kill with the corp.", "i-cross", "silver", "mentee"),
    ("cadet-fleet-ready", "Fleet Ready", "Flew a real corp fleet op.", "i-target", "silver", "mentee"),
    ("cadet-graduate", "Graduate", "Completed the Mentorship Program.", "i-trophy", "gold", "mentee"),
    ("veteran-mentor", "Veteran Mentor", "Guided a cadet to graduation.", "i-shield", "gold", "mentor"),
]

# Each track: (key, title, summary, category, icon, is_core, sort_order, tasks[])
# Each task: (key, title, difficulty, participants, method, criteria, reward_eligible,
#             mentee_instructions, mentor_instructions, evidence_requirement)
TRACKS = [
    ("welcome", "Welcome to the Corporation",
     "Get plugged into comms, people and the way we do things.", WELCOME, "i-rookie", True, 10, [
        ("welcome-rules", "Read the corp rules & culture", "intro", "mentee", MENTEE, {}, False,
         "Read the corp rules and code of conduct end to end.",
         "Point your cadet at the rules doc; answer any questions.", "none"),
        ("welcome-comms", "Set up Discord / Mumble comms", "intro", "both", DUAL,
         {}, False, "Install and log into the corp's comms tool and join the right channels.",
         "Confirm your cadet can hear and be heard on comms.", "none"),
        ("welcome-who", "Learn who the officers & FCs are", "intro", "both", MANUAL, {}, False,
         "Learn how to recognise officers, directors and fleet commanders.",
         "Walk through the org chart and who to ask for what.", "none"),
        ("welcome-services", "Understand corp services (SRP, buyback, logistics, doctrines)",
         "basic", "both", MANUAL, {}, False,
         "Learn what SRP, buyback, freight and doctrines are and when to use them.",
         "Give the tour of the corp's services and expectations.", "none"),
        ("welcome-scopes", "Link your character & grant baseline ESI", "intro", "mentee", API_ASSIST,
         {"type": "scopes_granted", "scopes": ["esi-skills.read_skills.v1"]}, False,
         "Link your main character so the tools can help you.",
         "Confirm the cadet's character is linked and skills import.", "none"),
        ("welcome-chat", "Have your first onboarding chat with your mentor", "intro", "both", DUAL,
         {}, True, "Sit down (on comms) with your mentor for a first proper chat.",
         "Have a relaxed first session — goals, timezones, what they want from EVE.", "none"),
    ]),
    ("eve-client", "EVE Client & Overview Setup",
     "Make the client actually readable — overview, brackets, windows.", CLIENT, "i-grid", True, 20, [
        ("client-overview", "Configure your overview", "basic", "mentee", MANUAL, {}, False,
         "Set up a clean overview with the right tabs and columns.",
         "Review the cadet's overview live; fix obvious gaps.", "optional"),
        ("client-brackets", "Configure brackets & the tactical display", "basic", "mentee", MENTEE,
         {}, False, "Tune brackets so grid is readable at a glance.",
         "Sanity-check bracket settings.", "none"),
        ("client-windows", "Lay out local, d-scan and the fleet window", "basic", "mentee", MENTEE,
         {}, False, "Dock the local, directional-scan and fleet windows where you can see them.",
         "Confirm the window layout works.", "none"),
        ("client-review", "Mentor reviews your UI setup", "basic", "both", MANUAL, {}, True,
         "Share your screen (or a screenshot) so your mentor can review your UI.",
         "Do a full pass over the cadet's UI and overview.", "optional"),
    ]),
    ("travel-safety", "Travel, Safety & Survival",
     "Get where you're going and come back alive.", TRAVEL, "i-route", True, 30, [
        ("travel-timers", "Learn session-change timers & gate cloak", "basic", "both", MANUAL, {}, False,
         "Understand session timers, gate cloak and the 'don't decloak into a camp' rule.",
         "Explain session timers and cloak with real examples.", "none"),
        ("travel-dscan", "Learn to use directional scan", "basic", "both", DUAL, {}, False,
         "Learn to read d-scan — angle, range, and what a probe on scan means.",
         "Run a live d-scan drill with your cadet.", "none"),
        ("travel-bookmarks", "Create safe spots, tacticals & bookmarks", "basic", "mentee", MANUAL,
         {}, False, "Make a few safes and tactical bookmarks in your home system.",
         "Verify the cadet's safes are actually safe (off-grid).", "none"),
        ("travel-exercise", "Complete a travel exercise with your mentor", "basic", "both", DUAL,
         {}, True, "Fly a short route with your mentor, using safes and d-scan the whole way.",
         "Take the cadet on a supervised travel run through a chokepoint.", "none"),
    ]),
    ("fitting-doctrine", "Ship Fitting & Doctrine Basics",
     "Understand slots, tank, and why a doctrine fit is a doctrine fit.", FITTING, "i-ship", True, 40, [
        ("fit-slots", "Learn high/mid/low/rig/drone/cargo slots", "basic", "both", MANUAL, {}, False,
         "Learn what each slot type is for.",
         "Walk through slot layout on a real hull.", "none"),
        ("fit-stats", "Learn CPU/PG, cap, resists, tank, range & application", "basic", "both", MANUAL,
         {}, False, "Understand the core fitting stats and the trade-offs between them.",
         "Explain the fitting stats with a doctrine fit open.", "none"),
        ("fit-review", "Review a corp doctrine fit with your mentor", "basic", "both", MANUAL, {}, False,
         "Open a corp doctrine fit and go through every module with your mentor.",
         "Explain why each module is on the doctrine fit.", "none"),
        ("fit-flyable", "Be able to fly a corp doctrine ship", "intermediate", "mentee", AUTO,
         {"type": "doctrine_any"}, True,
         "Train into (or confirm you can already fly) at least one active doctrine ship.",
         "Help the cadet pick the fastest doctrine to get into.", "none"),
        ("fit-skillplan", "Create a skill plan toward a doctrine", "basic", "mentee", AUTO,
         {"type": "skill_plan_exists"}, False,
         "Use the skills tool to build a plan toward a doctrine hull.",
         "Help pick a sensible first doctrine skill goal.", "none"),
    ]),
    ("ratting", "Ratting",
     "Make ISK from rats without feeding your ship to a roam.", RATTING, "i-coin", False, 50, [
        ("rat-safe", "Learn safe ratting practice & aligning out", "basic", "both", MANUAL, {}, False,
         "Learn to rat aligned, watch local/intel, and dock when it's spicy.",
         "Explain ratting safety and the 'align out' habit.", "none"),
        ("rat-sites", "Learn site selection & when to dock up", "basic", "both", MANUAL, {}, False,
         "Learn which sites to run and the triggers to stop.",
         "Cover site choice and bail triggers.", "none"),
        ("rat-session", "Run a ratting session with your mentor nearby", "basic", "both", DUAL,
         {"type": "session_confirmed", "min_participants": 2}, True,
         "Run a ratting session while your mentor is on comms / nearby.",
         "Watch intel and coach while your cadet rats.", "none"),
        ("rat-review", "Review a near-miss, loss or good tick", "basic", "both", MANUAL, {}, False,
         "Talk through what happened on a run — good or bad.",
         "Debrief a real ratting moment with your cadet.", "none"),
    ]),
    ("mining", "Mining",
     "Pull ore, compress it, and feed the buyback.", MINING, "i-cube", False, 60, [
        ("mine-roles", "Learn mining ship roles & compression", "basic", "both", MANUAL, {}, False,
         "Learn the mining hull roles and why compression matters.",
         "Explain mining ships, boosts and compression.", "none"),
        ("mine-buyback", "Learn the corp buyback flow", "basic", "both", MANUAL, {}, False,
         "Learn how to turn ore into ISK via corp buyback.",
         "Walk through submitting a buyback lot.", "none"),
        ("mine-session", "Join a mining session", "basic", "both", API_ASSIST,
         {"type": "mining_ledger", "min_units": 1000}, True,
         "Join a fleet mine (or mine near your mentor) and pull some ore.",
         "Get the cadet into a mining op and confirm they mined.", "none"),
        ("mine-buyback-do", "Submit a buyback lot", "basic", "mentee", AUTO,
         {"type": "buyback_offer"}, True,
         "Submit a real buyback lot for your ore.",
         "Confirm the cadet submitted a buyback lot.", "none"),
    ]),
    ("exploration", "Exploration",
     "Scan it down, hack it, and get out clean.", EXPLORATION, "i-map", False, 70, [
        ("explo-probe", "Learn probe scanning", "basic", "both", MANUAL, {}, False,
         "Learn to scan down cosmic signatures with probes.",
         "Run a scanning drill with your cadet.", "none"),
        ("explo-hacking", "Learn the hacking minigame", "basic", "mentee", MENTEE, {}, False,
         "Practice the data/relic hacking minigame.",
         "Share hacking tips (node types, virus strength).", "none"),
        ("explo-wh", "Learn wormhole safety basics", "intermediate", "both", MANUAL, {}, False,
         "Learn the rules for entering and leaving wormholes safely.",
         "Cover WH mass/time and the 'bookmark both sides' rule.", "none"),
        ("explo-session", "Complete an exploration practice run", "basic", "both", DUAL,
         {"type": "session_confirmed", "min_participants": 2}, True,
         "Do a supervised exploration run and talk through each site.",
         "Take the cadet on a low-risk exploration run.", "none"),
    ]),
    ("pvp-basics", "PvP Basics",
     "Tackle, hold point, follow the FC, and learn from the mail.", PVP, "i-cross", True, 80, [
        ("pvp-tackle", "Learn tackle: scram, web, point, prop & transversal", "basic", "both", MANUAL,
         {}, False, "Learn what scram/web/point do and how transversal keeps you alive.",
         "Explain tackle and range control with examples.", "none"),
        ("pvp-fc", "Learn to follow FC commands & broadcasts", "basic", "both", MANUAL, {}, False,
         "Learn the standard FC calls and how to use broadcasts.",
         "Run a quick comms/broadcast drill.", "none"),
        ("pvp-roam", "Join a training roam or home-defence fleet", "intermediate", "both", API_ASSIST,
         {"type": "killmail_recent", "days": 30, "min_count": 1}, True,
         "Get on a killmail with the corp — a roam, gank or home defence.",
         "Bring your cadet on a low-stakes fleet and get them on a mail.", "none"),
        ("pvp-debrief", "Review a killmail or lossmail with your mentor", "basic", "both", MANUAL,
         {}, True, "Pull up a recent kill or loss and talk through what happened.",
         "Debrief a real mail — what went right, what to change.", "optional"),
    ]),
    ("fleet-ops", "Fleet Operations",
     "Anchor, align, broadcast — be an asset in a fleet.", FLEET, "i-target", True, 90, [
        ("fleet-roles", "Learn fleet roles, anchoring & the watchlist", "basic", "both", MANUAL, {}, False,
         "Learn wings/squads, anchoring, and what the watchlist is for.",
         "Explain fleet structure and anchoring.", "none"),
        ("fleet-broadcast", "Demonstrate correct broadcast usage", "basic", "both", DUAL, {}, False,
         "Show you can broadcast for reps/target/align correctly.",
         "Check the cadet broadcasts the right things at the right time.", "none"),
        ("fleet-join", "Fly a real corp fleet op", "intermediate", "both", AUTO,
         {"type": "fleet_attended", "min_count": 1}, True,
         "Sign up for and fly a corp operation.",
         "Get your cadet on a scheduled op and keep an eye on them.", "none"),
        ("fleet-debrief", "Complete a mentor debrief after the op", "basic", "both", MANUAL, {}, True,
         "Debrief the op with your mentor afterwards.",
         "Run a short after-action with your cadet.", "none"),
    ]),
    ("logistics-buyback", "Logistics & Buyback Services",
     "Move things and turn loot into ISK, the corp way.", LOGISTICS, "i-truck", False, 100, [
        ("logi-request", "Learn how to request freight & how courier contracts work", "basic", "both",
         MANUAL, {}, False, "Learn to book a courier and what collateral/reward mean.",
         "Walk through booking a freight contract.", "none"),
        ("logi-buyback-rules", "Learn the corp buyback rules", "basic", "both", MANUAL, {}, False,
         "Learn what buyback pays and where.",
         "Explain the buyback rates and locations.", "none"),
        ("logi-courier", "Complete a courier contract (or example)", "basic", "mentee", AUTO,
         {"type": "courier_contract", "verified_only": True}, True,
         "Run a real courier contract through the freight service.",
         "Confirm the cadet completed (and ideally ESI-verified) a haul.", "none"),
        ("logi-confirm", "Mentor confirms you understand the flow", "basic", "both", MANUAL, {}, False,
         "Talk your mentor through the whole logistics flow.",
         "Confirm the cadet understands collateral, reward and risk.", "none"),
    ]),
    ("industry", "Manufacturing & Industry",
     "Blueprints, ME/TE, and installing your first job.", INDUSTRY, "i-box", False, 110, [
        ("ind-bp", "Learn blueprint basics (BPO vs BPC)", "basic", "both", MANUAL, {}, False,
         "Learn the difference between BPOs and BPCs.",
         "Explain blueprints and runs.", "none"),
        ("ind-me-te", "Learn material & time efficiency", "basic", "both", MANUAL, {}, False,
         "Learn what ME/TE do and why they matter.",
         "Show ME/TE impact on a real build.", "none"),
        ("ind-install", "Install a small industry job", "basic", "mentee", API_ASSIST,
         {"type": "industry_job"}, True,
         "Install a small manufacturing or research job.",
         "Help the cadet install their first job.", "none"),
        ("ind-review", "Review cost, materials & output with your mentor", "basic", "both", MANUAL,
         {}, False, "Go over the BOM and margin of your job with your mentor.",
         "Review the economics of the cadet's job.", "none"),
    ]),
    ("skill-planning", "Skill Planning",
     "Train the right skills in the right order.", SKILLS, "i-bolt", True, 120, [
        ("skill-review", "Review your current skills with your mentor", "intro", "both", MANUAL, {}, False,
         "Go through your current skills and gaps with your mentor.",
         "Review the cadet's skills and obvious priorities.", "none"),
        ("skill-goal", "Choose a short-term training goal", "basic", "mentee", MENTEE, {}, False,
         "Pick a concrete short-term skill goal.",
         "Help pick a motivating first goal.", "none"),
        ("skill-plan", "Create a doctrine-relevant skill plan", "basic", "mentee", AUTO,
         {"type": "skill_plan_exists"}, True,
         "Build a skill plan toward a corp doctrine.",
         "Confirm the plan targets a doctrine the corp needs.", "none"),
        ("skill-recheck", "Re-check progress after a couple of weeks", "basic", "both", MANUAL, {}, True,
         "Come back to your mentor after ~2 weeks to review progress.",
         "Follow up on the cadet's training progress.", "none"),
    ]),
]

REWARD_RULES = [
    # (key, label, audience, trigger, trigger_ref, reward_type, amount, points, badge_key,
    #  requires_leadership_approval, requires_verification)
    ("welcome-done", "Cadet: completed onboarding", "mentee", "track_complete", "welcome",
     "points", 0, 20, None, False, False),
    ("first-fleet", "Cadet: first corp fleet", "mentee", "task", "fleet-join",
     "points", 0, 15, "cadet-fleet-ready", False, True),
    ("first-blood", "Cadet: first PvP kill", "mentee", "task", "pvp-roam",
     "points", 0, 15, "cadet-first-blood", False, True),
    ("first-courier", "Cadet: first haul", "mentee", "task", "logi-courier",
     "points", 0, 10, None, False, True),
    ("first-industry", "Cadet: first industry job", "mentee", "task", "ind-install",
     "points", 0, 10, None, False, True),
    ("graduate-isk", "Cadet: programme graduation", "mentee", "program_complete", "program",
     "isk", 50000000, 0, "cadet-graduate", True, False),
    ("mentor-graduate", "Mentor: guided a cadet to graduation", "mentor", "program_complete", "program",
     "isk", 25000000, 30, "veteran-mentor", True, False),
    ("mentor-active-30", "Mentor: 30 days active mentoring", "mentor", "pairing_active_days", "30",
     "points", 0, 15, None, False, False),
]


def seed(apps, schema_editor):
    Program = apps.get_model("mentorship", "MentorshipProgram")
    Cohort = apps.get_model("mentorship", "MentorshipCohort")
    Track = apps.get_model("mentorship", "MentorshipTrack")
    Task = apps.get_model("mentorship", "MentorshipTask")
    Badge = apps.get_model("mentorship", "MentorshipBadge")
    Rule = apps.get_model("mentorship", "MentorshipRewardRule")

    Program.objects.get_or_create(
        name="Mentorship Program", defaults={"is_active": True, "enabled": True}
    )
    cohort, _ = Cohort.objects.get_or_create(
        key="general-intake",
        defaults={"name": "General Intake", "is_active": True,
                  "description": "The always-on default cohort for new cadets."},
    )

    badges = {}
    for i, (key, label, desc, icon, tier, audience) in enumerate(BADGES):
        badge, _ = Badge.objects.get_or_create(
            key=key,
            defaults={"label": label, "description": desc, "icon": icon, "tier": tier,
                      "audience": audience, "sort_order": i * 10},
        )
        badges[key] = badge

    for tkey, title, summary, category, icon, is_core, order, tasks in TRACKS:
        track, _ = Track.objects.get_or_create(
            key=tkey,
            defaults={"title": title, "summary": summary, "category": category, "icon": icon,
                      "is_core": is_core, "sort_order": order, "active": True},
        )
        for i, (task_key, ttitle, difficulty, participants, method, criteria, reward_eligible,
                mentee_i, mentor_i, evidence) in enumerate(tasks):
            Task.objects.get_or_create(
                key=task_key,
                defaults={
                    "track": track, "title": ttitle, "difficulty": difficulty,
                    "participants": participants, "validation_method": method, "criteria": criteria,
                    "reward_eligible": reward_eligible, "mentee_instructions": mentee_i,
                    "mentor_instructions": mentor_i, "evidence_requirement": evidence,
                    "sort_order": i * 10, "active": True,
                },
            )

    for i, (key, label, audience, trigger, trigger_ref, rtype, amount, points, badge_key,
            approval, verify) in enumerate(REWARD_RULES):
        Rule.objects.get_or_create(
            key=key,
            defaults={
                "label": label, "audience": audience, "trigger": trigger, "trigger_ref": trigger_ref,
                "reward_type": rtype, "amount": amount, "points": points,
                "badge": badges.get(badge_key), "requires_leadership_approval": approval,
                "requires_verification": verify, "active": True, "sort_order": i * 10,
            },
        )


def unseed(apps, schema_editor):
    # Create-only seed: nothing to reverse (leader edits must survive).
    pass


class Migration(migrations.Migration):
    dependencies = [("mentorship", "0001_initial")]
    operations = [migrations.RunPython(seed, unseed)]
