"""Render-time i18n seam for the seeded mentorship catalogue (Seam A) — "translate until edited".

The programme singleton's ``intro_text``, and the learning-track / field-exercise / badge /
reward-rule catalogues, are shipped English **content**: migration ``0002_seed_mentorship`` writes
them into the database (and :func:`apps.mentorship.services.active_program` seeds the programme row
with the ``MentorshipProgram.intro_text`` default). Leaders then CRUD those rows.

Wrapping the seed in ``gettext_lazy`` cannot work: Django coerces a lazy proxy to ``str`` on
``.save()``, so whichever locale happened to be active when the row was created would be frozen into
the column forever. So the English stays in the database — the fallback *and* the audit record —
marked for extraction here with ``gettext_noop`` (Django's ``makemessages`` passes
``--keyword=gettext_noop``, so ``xgettext`` sees it exactly as it sees ``_()``), and it is
translated at *render* time keyed on the row's stable key.

:func:`_translate_until_edited` is the seam:

    stored text == the shipped English  →  still the seed's words  →  return the TRANSLATION
    stored text != the shipped English  →  a leader edited it  →  return it VERBATIM, untranslated

That comparison **is** the "has this been edited?" test, so there is no ``edited`` flag column to
maintain. A missing ``msgstr`` resolves back to the English msgid, so the seam can never blank a
populated field. Keys (``track.key``, ``task.key``, ``badge.key``, ``rule_key``) are identifiers —
persisted, looked up, compared — and are never translated.

The English below is copied VERBATIM from the seed source (migration ``0002_seed_mentorship`` and
the ``MentorshipProgram.intro_text`` default). If a msgid drifts from the seed by one character the
equality check fails and the row renders English, so keep them in lockstep.
"""
from __future__ import annotations

from django.utils.translation import gettext, gettext_noop

# The programme is a singleton (one row, no natural key), so its intro_text is addressed by a
# module constant rather than a key. Copied VERBATIM from the ``MentorshipProgram.intro_text``
# default (models.py / migration 0001), which ``active_program()`` stores on first use.
PROGRAM_INTRO = gettext_noop(
    "New to the corp or to EVE? Our veterans will fly with you and show you "
    "the ropes — from your first overview setup to your first fleet kill. "
    "Sign up as a cadet, get paired with a mentor, and work through hands-on "
    "field exercises at your own pace."
)

# {track.key -> shipped English title} — VERBATIM from migration 0002 TRACKS.
TRACK_TITLES: dict[str, str] = {
    "welcome": gettext_noop("Welcome to the Corporation"),
    "eve-client": gettext_noop("EVE Client & Overview Setup"),
    "travel-safety": gettext_noop("Travel, Safety & Survival"),
    "fitting-doctrine": gettext_noop("Ship Fitting & Doctrine Basics"),
    "ratting": gettext_noop("Ratting"),
    "mining": gettext_noop("Mining"),
    "exploration": gettext_noop("Exploration"),
    "pvp-basics": gettext_noop("PvP Basics"),
    "fleet-ops": gettext_noop("Fleet Operations"),
    "logistics-buyback": gettext_noop("Logistics & Buyback Services"),
    "industry": gettext_noop("Manufacturing & Industry"),
    "skill-planning": gettext_noop("Skill Planning"),
}

# {track.key -> shipped English summary} — VERBATIM from migration 0002 TRACKS.
TRACK_SUMMARIES: dict[str, str] = {
    "welcome": gettext_noop("Get plugged into comms, people and the way we do things."),
    "eve-client": gettext_noop("Make the client actually readable — overview, brackets, windows."),
    "travel-safety": gettext_noop("Get where you're going and come back alive."),
    "fitting-doctrine": gettext_noop("Understand slots, tank, and why a doctrine fit is a doctrine fit."),
    "ratting": gettext_noop("Make ISK from rats without feeding your ship to a roam."),
    "mining": gettext_noop("Pull ore, compress it, and feed the buyback."),
    "exploration": gettext_noop("Scan it down, hack it, and get out clean."),
    "pvp-basics": gettext_noop("Tackle, hold point, follow the FC, and learn from the mail."),
    "fleet-ops": gettext_noop("Anchor, align, broadcast — be an asset in a fleet."),
    "logistics-buyback": gettext_noop("Move things and turn loot into ISK, the corp way."),
    "industry": gettext_noop("Blueprints, ME/TE, and installing your first job."),
    "skill-planning": gettext_noop("Train the right skills in the right order."),
}

# {task.key -> shipped English title} — VERBATIM from migration 0002 TRACKS tasks.
TASK_TITLES: dict[str, str] = {
    "welcome-rules": gettext_noop("Read the corp rules & culture"),
    "welcome-comms": gettext_noop("Set up Discord / Mumble comms"),
    "welcome-who": gettext_noop("Learn who the officers & FCs are"),
    "welcome-services": gettext_noop("Understand corp services (SRP, buyback, logistics, doctrines)"),
    "welcome-scopes": gettext_noop("Link your character & grant baseline ESI"),
    "welcome-chat": gettext_noop("Have your first onboarding chat with your mentor"),
    "client-overview": gettext_noop("Configure your overview"),
    "client-brackets": gettext_noop("Configure brackets & the tactical display"),
    "client-windows": gettext_noop("Lay out local, d-scan and the fleet window"),
    "client-review": gettext_noop("Mentor reviews your UI setup"),
    "travel-timers": gettext_noop("Learn session-change timers & gate cloak"),
    "travel-dscan": gettext_noop("Learn to use directional scan"),
    "travel-bookmarks": gettext_noop("Create safe spots, tacticals & bookmarks"),
    "travel-exercise": gettext_noop("Complete a travel exercise with your mentor"),
    "fit-slots": gettext_noop("Learn high/mid/low/rig/drone/cargo slots"),
    "fit-stats": gettext_noop("Learn CPU/PG, cap, resists, tank, range & application"),
    "fit-review": gettext_noop("Review a corp doctrine fit with your mentor"),
    "fit-flyable": gettext_noop("Be able to fly a corp doctrine ship"),
    "fit-skillplan": gettext_noop("Create a skill plan toward a doctrine"),
    "rat-safe": gettext_noop("Learn safe ratting practice & aligning out"),
    "rat-sites": gettext_noop("Learn site selection & when to dock up"),
    "rat-session": gettext_noop("Run a ratting session with your mentor nearby"),
    "rat-review": gettext_noop("Review a near-miss, loss or good tick"),
    "mine-roles": gettext_noop("Learn mining ship roles & compression"),
    "mine-buyback": gettext_noop("Learn the corp buyback flow"),
    "mine-session": gettext_noop("Join a mining session"),
    "mine-buyback-do": gettext_noop("Submit a buyback lot"),
    "explo-probe": gettext_noop("Learn probe scanning"),
    "explo-hacking": gettext_noop("Learn the hacking minigame"),
    "explo-wh": gettext_noop("Learn wormhole safety basics"),
    "explo-session": gettext_noop("Complete an exploration practice run"),
    "pvp-tackle": gettext_noop("Learn tackle: scram, web, point, prop & transversal"),
    "pvp-fc": gettext_noop("Learn to follow FC commands & broadcasts"),
    "pvp-roam": gettext_noop("Join a training roam or home-defence fleet"),
    "pvp-debrief": gettext_noop("Review a killmail or lossmail with your mentor"),
    "fleet-roles": gettext_noop("Learn fleet roles, anchoring & the watchlist"),
    "fleet-broadcast": gettext_noop("Demonstrate correct broadcast usage"),
    "fleet-join": gettext_noop("Fly a real corp fleet op"),
    "fleet-debrief": gettext_noop("Complete a mentor debrief after the op"),
    "logi-request": gettext_noop("Learn how to request freight & how courier contracts work"),
    "logi-buyback-rules": gettext_noop("Learn the corp buyback rules"),
    "logi-courier": gettext_noop("Complete a courier contract (or example)"),
    "logi-confirm": gettext_noop("Mentor confirms you understand the flow"),
    "ind-bp": gettext_noop("Learn blueprint basics (BPO vs BPC)"),
    "ind-me-te": gettext_noop("Learn material & time efficiency"),
    "ind-install": gettext_noop("Install a small industry job"),
    "ind-review": gettext_noop("Review cost, materials & output with your mentor"),
    "skill-review": gettext_noop("Review your current skills with your mentor"),
    "skill-goal": gettext_noop("Choose a short-term training goal"),
    "skill-plan": gettext_noop("Create a doctrine-relevant skill plan"),
    "skill-recheck": gettext_noop("Re-check progress after a couple of weeks"),
}

# {task.key -> shipped English mentee_instructions} — VERBATIM from migration 0002 TRACKS tasks.
TASK_MENTEE_INSTRUCTIONS: dict[str, str] = {
    "welcome-rules": gettext_noop("Read the corp rules and code of conduct end to end."),
    "welcome-comms": gettext_noop("Install and log into the corp's comms tool and join the right channels."),
    "welcome-who": gettext_noop("Learn how to recognise officers, directors and fleet commanders."),
    "welcome-services": gettext_noop("Learn what SRP, buyback, freight and doctrines are and when to use them."),
    "welcome-scopes": gettext_noop("Link your main character so the tools can help you."),
    "welcome-chat": gettext_noop("Sit down (on comms) with your mentor for a first proper chat."),
    "client-overview": gettext_noop("Set up a clean overview with the right tabs and columns."),
    "client-brackets": gettext_noop("Tune brackets so grid is readable at a glance."),
    "client-windows": gettext_noop("Dock the local, directional-scan and fleet windows where you can see them."),
    "client-review": gettext_noop("Share your screen (or a screenshot) so your mentor can review your UI."),
    "travel-timers": gettext_noop("Understand session timers, gate cloak and the 'don't decloak into a camp' rule."),
    "travel-dscan": gettext_noop("Learn to read d-scan — angle, range, and what a probe on scan means."),
    "travel-bookmarks": gettext_noop("Make a few safes and tactical bookmarks in your home system."),
    "travel-exercise": gettext_noop("Fly a short route with your mentor, using safes and d-scan the whole way."),
    "fit-slots": gettext_noop("Learn what each slot type is for."),
    "fit-stats": gettext_noop("Understand the core fitting stats and the trade-offs between them."),
    "fit-review": gettext_noop("Open a corp doctrine fit and go through every module with your mentor."),
    "fit-flyable": gettext_noop("Train into (or confirm you can already fly) at least one active doctrine ship."),
    "fit-skillplan": gettext_noop("Use the skills tool to build a plan toward a doctrine hull."),
    "rat-safe": gettext_noop("Learn to rat aligned, watch local/intel, and dock when it's spicy."),
    "rat-sites": gettext_noop("Learn which sites to run and the triggers to stop."),
    "rat-session": gettext_noop("Run a ratting session while your mentor is on comms / nearby."),
    "rat-review": gettext_noop("Talk through what happened on a run — good or bad."),
    "mine-roles": gettext_noop("Learn the mining hull roles and why compression matters."),
    "mine-buyback": gettext_noop("Learn how to turn ore into ISK via corp buyback."),
    "mine-session": gettext_noop("Join a fleet mine (or mine near your mentor) and pull some ore."),
    "mine-buyback-do": gettext_noop("Submit a real buyback lot for your ore."),
    "explo-probe": gettext_noop("Learn to scan down cosmic signatures with probes."),
    "explo-hacking": gettext_noop("Practice the data/relic hacking minigame."),
    "explo-wh": gettext_noop("Learn the rules for entering and leaving wormholes safely."),
    "explo-session": gettext_noop("Do a supervised exploration run and talk through each site."),
    "pvp-tackle": gettext_noop("Learn what scram/web/point do and how transversal keeps you alive."),
    "pvp-fc": gettext_noop("Learn the standard FC calls and how to use broadcasts."),
    "pvp-roam": gettext_noop("Get on a killmail with the corp — a roam, gank or home defence."),
    "pvp-debrief": gettext_noop("Pull up a recent kill or loss and talk through what happened."),
    "fleet-roles": gettext_noop("Learn wings/squads, anchoring, and what the watchlist is for."),
    "fleet-broadcast": gettext_noop("Show you can broadcast for reps/target/align correctly."),
    "fleet-join": gettext_noop("Sign up for and fly a corp operation."),
    "fleet-debrief": gettext_noop("Debrief the op with your mentor afterwards."),
    "logi-request": gettext_noop("Learn to book a courier and what collateral/reward mean."),
    "logi-buyback-rules": gettext_noop("Learn what buyback pays and where."),
    "logi-courier": gettext_noop("Run a real courier contract through the freight service."),
    "logi-confirm": gettext_noop("Talk your mentor through the whole logistics flow."),
    "ind-bp": gettext_noop("Learn the difference between BPOs and BPCs."),
    "ind-me-te": gettext_noop("Learn what ME/TE do and why they matter."),
    "ind-install": gettext_noop("Install a small manufacturing or research job."),
    "ind-review": gettext_noop("Go over the BOM and margin of your job with your mentor."),
    "skill-review": gettext_noop("Go through your current skills and gaps with your mentor."),
    "skill-goal": gettext_noop("Pick a concrete short-term skill goal."),
    "skill-plan": gettext_noop("Build a skill plan toward a corp doctrine."),
    "skill-recheck": gettext_noop("Come back to your mentor after ~2 weeks to review progress."),
}

# {task.key -> shipped English mentor_instructions} — VERBATIM from migration 0002 TRACKS tasks.
TASK_MENTOR_INSTRUCTIONS: dict[str, str] = {
    "welcome-rules": gettext_noop("Point your cadet at the rules doc; answer any questions."),
    "welcome-comms": gettext_noop("Confirm your cadet can hear and be heard on comms."),
    "welcome-who": gettext_noop("Walk through the org chart and who to ask for what."),
    "welcome-services": gettext_noop("Give the tour of the corp's services and expectations."),
    "welcome-scopes": gettext_noop("Confirm the cadet's character is linked and skills import."),
    "welcome-chat": gettext_noop("Have a relaxed first session — goals, timezones, what they want from EVE."),
    "client-overview": gettext_noop("Review the cadet's overview live; fix obvious gaps."),
    "client-brackets": gettext_noop("Sanity-check bracket settings."),
    "client-windows": gettext_noop("Confirm the window layout works."),
    "client-review": gettext_noop("Do a full pass over the cadet's UI and overview."),
    "travel-timers": gettext_noop("Explain session timers and cloak with real examples."),
    "travel-dscan": gettext_noop("Run a live d-scan drill with your cadet."),
    "travel-bookmarks": gettext_noop("Verify the cadet's safes are actually safe (off-grid)."),
    "travel-exercise": gettext_noop("Take the cadet on a supervised travel run through a chokepoint."),
    "fit-slots": gettext_noop("Walk through slot layout on a real hull."),
    "fit-stats": gettext_noop("Explain the fitting stats with a doctrine fit open."),
    "fit-review": gettext_noop("Explain why each module is on the doctrine fit."),
    "fit-flyable": gettext_noop("Help the cadet pick the fastest doctrine to get into."),
    "fit-skillplan": gettext_noop("Help pick a sensible first doctrine skill goal."),
    "rat-safe": gettext_noop("Explain ratting safety and the 'align out' habit."),
    "rat-sites": gettext_noop("Cover site choice and bail triggers."),
    "rat-session": gettext_noop("Watch intel and coach while your cadet rats."),
    "rat-review": gettext_noop("Debrief a real ratting moment with your cadet."),
    "mine-roles": gettext_noop("Explain mining ships, boosts and compression."),
    "mine-buyback": gettext_noop("Walk through submitting a buyback lot."),
    "mine-session": gettext_noop("Get the cadet into a mining op and confirm they mined."),
    "mine-buyback-do": gettext_noop("Confirm the cadet submitted a buyback lot."),
    "explo-probe": gettext_noop("Run a scanning drill with your cadet."),
    "explo-hacking": gettext_noop("Share hacking tips (node types, virus strength)."),
    "explo-wh": gettext_noop("Cover WH mass/time and the 'bookmark both sides' rule."),
    "explo-session": gettext_noop("Take the cadet on a low-risk exploration run."),
    "pvp-tackle": gettext_noop("Explain tackle and range control with examples."),
    "pvp-fc": gettext_noop("Run a quick comms/broadcast drill."),
    "pvp-roam": gettext_noop("Bring your cadet on a low-stakes fleet and get them on a mail."),
    "pvp-debrief": gettext_noop("Debrief a real mail — what went right, what to change."),
    "fleet-roles": gettext_noop("Explain fleet structure and anchoring."),
    "fleet-broadcast": gettext_noop("Check the cadet broadcasts the right things at the right time."),
    "fleet-join": gettext_noop("Get your cadet on a scheduled op and keep an eye on them."),
    "fleet-debrief": gettext_noop("Run a short after-action with your cadet."),
    "logi-request": gettext_noop("Walk through booking a freight contract."),
    "logi-buyback-rules": gettext_noop("Explain the buyback rates and locations."),
    "logi-courier": gettext_noop("Confirm the cadet completed (and ideally ESI-verified) a haul."),
    "logi-confirm": gettext_noop("Confirm the cadet understands collateral, reward and risk."),
    "ind-bp": gettext_noop("Explain blueprints and runs."),
    "ind-me-te": gettext_noop("Show ME/TE impact on a real build."),
    "ind-install": gettext_noop("Help the cadet install their first job."),
    "ind-review": gettext_noop("Review the economics of the cadet's job."),
    "skill-review": gettext_noop("Review the cadet's skills and obvious priorities."),
    "skill-goal": gettext_noop("Help pick a motivating first goal."),
    "skill-plan": gettext_noop("Confirm the plan targets a doctrine the corp needs."),
    "skill-recheck": gettext_noop("Follow up on the cadet's training progress."),
}

# {badge.key -> shipped English label} — VERBATIM from migration 0002 BADGES.
BADGE_LABELS: dict[str, str] = {
    "cadet-first-steps": gettext_noop("First Steps"),
    "cadet-navigator": gettext_noop("Navigator"),
    "cadet-first-blood": gettext_noop("First Blood"),
    "cadet-fleet-ready": gettext_noop("Fleet Ready"),
    "cadet-graduate": gettext_noop("Graduate"),
    "veteran-mentor": gettext_noop("Veteran Mentor"),
}

# {rule.key -> shipped English label} — VERBATIM from migration 0002 REWARD_RULES.
REWARD_RULE_LABELS: dict[str, str] = {
    "welcome-done": gettext_noop("Cadet: completed onboarding"),
    "first-fleet": gettext_noop("Cadet: first corp fleet"),
    "first-blood": gettext_noop("Cadet: first PvP kill"),
    "first-courier": gettext_noop("Cadet: first haul"),
    "first-industry": gettext_noop("Cadet: first industry job"),
    "graduate-isk": gettext_noop("Cadet: programme graduation"),
    "mentor-graduate": gettext_noop("Mentor: guided a cadet to graduation"),
    "mentor-active-30": gettext_noop("Mentor: 30 days active mentoring"),
}

# A ``MentorshipBadgeAward.reason`` carries no key column, but its reason is copied from a reward
# rule's label (``rewards._settle``). So the shipped English reward-rule labels double as the seed
# set for the badge-award tooltip: a stored reason that still matches one is translated; a leader's
# custom award reason (anything not in this set) renders verbatim.
_REWARD_LABEL_ENGLISH = frozenset(REWARD_RULE_LABELS.values())


def _translate_until_edited(stored: str, seed: str | None) -> str:
    """Translate ``stored`` only while it still equals the shipped English ``seed``.

    Once a leader edits the row (``stored != seed``) or the key is unknown (``seed is None``), the
    stored text is returned verbatim in every locale. A missing ``msgstr`` resolves back to the
    English msgid, so this never blanks a populated field and never raises.
    """
    if stored and seed and stored == seed:
        return gettext(stored)
    return stored


def track_title(key: str, stored: str) -> str:
    return _translate_until_edited(stored, TRACK_TITLES.get(key or ""))


def track_summary(key: str, stored: str) -> str:
    return _translate_until_edited(stored, TRACK_SUMMARIES.get(key or ""))


def task_title(key: str, stored: str) -> str:
    return _translate_until_edited(stored, TASK_TITLES.get(key or ""))


def task_mentee_instructions(key: str, stored: str) -> str:
    return _translate_until_edited(stored, TASK_MENTEE_INSTRUCTIONS.get(key or ""))


def task_mentor_instructions(key: str, stored: str) -> str:
    return _translate_until_edited(stored, TASK_MENTOR_INSTRUCTIONS.get(key or ""))


def badge_label(key: str, stored: str) -> str:
    return _translate_until_edited(stored, BADGE_LABELS.get(key or ""))


def reward_label(rule_key: str, stored: str) -> str:
    return _translate_until_edited(stored, REWARD_RULE_LABELS.get(rule_key or ""))


def program_intro(stored: str) -> str:
    return _translate_until_edited(stored, PROGRAM_INTRO)


def reward_reason(stored: str) -> str:
    """The reader-locale form of a badge-award ``reason`` (which has no key column).

    Translated while it still matches a shipped reward-rule label; a leader's custom reason is
    returned verbatim.
    """
    if stored and stored in _REWARD_LABEL_ENGLISH:
        return gettext(stored)
    return stored
