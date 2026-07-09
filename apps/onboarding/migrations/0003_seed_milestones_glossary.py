"""Default onboarding milestones + a newbro glossary.

Create-only seeds: get_or_create by natural key, so anything leaders have
already added or edited (Admin Console -> New Player content) is never
overwritten, and re-running is a no-op. Reverse is a no-op for the same
reason — we can't tell seeded rows from leader-authored ones.
"""
from __future__ import annotations

from django.db import migrations

MILESTONES = [
    ('link-character', 'Link your EVE character', 'Log in with EVE SSO so the Grid knows who you are — and link every alt you fly, they all count.', 'account', {'type': 'linked'}, '', 10),
    ('join-comms', 'Register on alliance voice comms', "Comms are mandatory for fleets — you don't need to talk, just listen. Grab the invite from your recruiter and set your comms name to your main character's.", 'account', {}, '', 20),
    ('grant-esi', 'Grant the corp tools your ESI scopes', 'The Grid can only help with what it can see. Grant the optional scopes (assets, market, planets) on the ESI Scopes page — one click per character.', 'account', {'type': 'scopes', 'scopes': ['esi-assets.read_assets.v1']}, '/auth/eve/scopes/', 30),
    ('import-skills', 'Import your skills', "Share your skill sheet so every 'can I fly this?' answer on this site is about YOU. One click — it keeps itself fresh afterwards.", 'skills', {'type': 'skills_imported'}, '/skills/', 40),
    ('train-doctrine', 'Train into your first doctrine ship', 'Being able to fly what the FC calls for is your single biggest contribution. Pick the starter doctrine and follow its training plan.', 'doctrine', {'type': 'doctrine_any'}, '/doctrines/', 50),
    ('move-to-staging', 'Move your gear to staging', 'Set your home station to staging, move your doctrine ships there, and keep a cheap travel ship at each end. Use the freight service instead of hauling it yourself.', 'activity', {}, '', 60),
    ('first-fleet', 'Fly your first fleet', 'Watch the ops board and x up. Nobody expects anything from you on fleet one — undocking with us IS the milestone.', 'activity', {}, '/operations/', 70),
    ('first-isk', 'Start your first income stream', 'Ratting, corp mining fleets, or PI — pick one and get ISK flowing. Sell loot and ore to the corp buyback: zero hauling risk, paid today.', 'activity', {}, '', 80),
]

GLOSSARY = [
    ('Highsec / Lowsec / Nullsec', 'EVE space comes in security bands. Highsec: NPC police (CONCORD) punish attackers. Lowsec: no police, but no warp-stopping bubbles either. Nullsec — where we live: no police at all, player alliances own the space, and anything goes. More risk, better rewards, real friends.'),
    ('Doctrine', "A standard ship fitting published by leadership — the exact hull, modules, and ammo the corp wants you to fly so the whole fleet works together. Fly the doctrine fit as written; it's also what makes your loss SRP-eligible."),
    ('Logi', "Logistics ships — the fleet's healers. Logi pilots repair the shields or armor of fleetmates who broadcast for help. Flying logi is one of the most valued roles a newer pilot can learn."),
    ('ISK', "EVE's currency (InterStellar Kredits). Everything — ships, ammo, services — is priced in ISK, usually quoted in millions ('m') or billions ('b'). Losing a ship really means losing its ISK cost, which is why cheap, replaceable ships are king."),
    ('FC', "Fleet Commander — the pilot leading the fleet and calling every move on voice comms. Their word is law mid-fight: when they say 'check check' or 'clear comms', stop chatting so orders can be heard, and put questions in fleet text chat instead of on comms."),
    ('X up', "Typing 'x' in fleet or corp chat to volunteer — for a fleet spot, a specific ship, or a job ('x up if you can fly logi'). It simply means 'count me in'."),
    ('Anchor', 'The designated pilot the whole fleet physically follows in combat — you set your ship to orbit or keep-at-range on them and let them do the flying. This keeps the fleet in one tight, controlled ball while you focus on targets and broadcasts.'),
    ('Align', "To point your ship at a destination and build up speed without actually warping — an aligned ship enters warp almost instantly when the order comes. 'Align to the sun' means turn toward it and wait; do not warp until the FC says so."),
    ('Warp to zero', "Warping to exactly 0 km from your destination instead of landing at a distance, so you can jump a gate or dock the moment you arrive. 'Gate, gate, warp to zero' means warp straight onto the gate and jump as soon as you land."),
    ('Broadcast', 'One-click alerts sent through the fleet window that every fleet member sees. The one that saves your life: broadcast for shield or armor the instant you start taking damage, so the logi pilots know exactly who to heal.'),
    ('Free', "Intel-channel shorthand for 'no hostiles here'. Alliance intel channels run on quick calls like 'X-7 free' or 'status?', answered with 'free' (clear) or the names of hostile pilots in the system — check them before you travel. Other groups say 'clr' or 'clear' — same meaning."),
    ('Batphone', "Calling a bigger allied group for backup when a fight is too large to handle alone — named after Batman's emergency hotline. 'They batphoned' means the enemy rang up heavy friends, and the fight is about to get much bigger."),
    ('D-scan', "The directional scanner — a free, instant scan that lists ships and structures within about 14 AU of your position. Spamming d-scan while you're in space is one of the core survival habits in EVE: it shows most threats before they land on you — but not cloaked ships or Combat Recon cruisers, which are invisible to d-scan, so a clear scan is never a promise."),
    ('Safe spot', "A bookmark out in empty space, away from planets, gates, and anything warpable, where you can sit while hostiles sweep the system. A related trick is the insta-undock: a bookmark lined up with a station's exit so you can warp away the second you undock, before campers can lock you. A safe spot hides you — it doesn't make you invincible: hostiles with combat scanner probes can find you in a minute or two, so keep hitting d-scan (probes show up on it) and keep moving between safes."),
    ('Podded', "If your ship is destroyed you're left floating in your pod (escape capsule); lose that too and you've been podded — you wake up in a fresh clone at your home station. Podding destroys any implants (skill- and stat-boosting plugs in your head), so keep your home station set somewhere useful and don't take an expensive head into big fights."),
    ('Gate camp', "A group of hostiles parked at a stargate, waiting to kill whatever jumps through — in nullsec usually with bubbles so you can't just warp off. Check the intel channels before traveling alone; most newbro losses happen at camps."),
    ('Bubble', "A warp disruption bubble — a large sphere that stops everyone inside it from warping and drags passing warps to its edge. Bubbles don't work in high- or low-security space — you'll meet them in nullsec, wormholes, and Pochven. They come from interdictor ships ('dictors') or anchorable deployables; they're the teeth of every nullsec gate camp."),
    ('Tidi', "Time dilation — when a massive battle overloads the server, EVE deliberately slows time in that system (down to 10% speed) so it can process everything. Everything moves in slow motion for everyone equally; it's normal, so stay calm and follow orders."),
    ('Cyno', 'A cynosural field — a bright beacon a ship lights in space that lets capital ships (and some others) jump directly to it from light-years away, skipping gates entirely. Cynos power both our logistics, like jump freighters, and ambushes (see Hotdrop).'),
    ('Hotdrop', "When hostiles light a cyno on your position and heavy ships pour in on top of you with no warning. It's why you never ignore a lone unknown pilot while ratting — they may be the beacon for an entire fleet."),
    ('Gank', 'Killing a target with overwhelming, unfair force — most famously suicide ganking in high-security space, where attackers happily let the NPC police (CONCORD) destroy their cheap ships because your cargo is worth more. Never haul more than you can afford to lose.'),
    ('Awox', "When someone turns their guns on their own corpmates, usually an infiltrator who joined just to betray — named after an infamous player who did exactly that. It's why corps vet applicants, and why you should report suspicious behavior to leadership."),
    ('Ratting (krabbing)', "Killing NPC pirates ('rats') in our space for bounty ISK — nullsec's bread-and-butter income, also called krabbing. Bounties arrive in 20-minute payouts, so pilots measure income in 'ticks' ('40m ticks' means 40 million ISK per payout)."),
    ('PI', 'Planetary Industry — setting up automated extractors and factories on planets for semi-passive income. It takes a few clicks a day and can be managed from anywhere, making it a popular first income stream for new nullsec pilots.'),
    ('PLEX', 'An item CCP sells for real money that players can also buy and sell for ISK on the in-game market; redeeming 500 of them grants a month of Omega (full subscription) time. In other words, a healthy in-game income can pay for your game time.'),
    ('Jita', "EVE's biggest trade hub — a high-security system (specifically the famous Jita 4-4 station) where nearly everything in the game is bought and sold. 'Jita price' is the standard reference price, and our hauling services exist to move goods between Jita and home."),
    ('Contract', "EVE's built-in system for player-to-player deals: handing over items, hauling jobs, or ship sales. Corp doctrine ships and buyback payouts usually arrive as contracts — and since scamming is a legal part of EVE, always read a contract carefully before accepting."),
    ('JF', 'Jump Freighter — a huge cargo ship that jumps across light-years to a cyno beacon instead of taking gates. The alliance JF service hauls your stuff between Jita and our staging system for a small fee, so you never have to fly a defenseless hauler through hostile space.'),
    ('SRP', "Ship Replacement Program — lose a doctrine ship on an official fleet and the corp or alliance pays you back. File your loss (usually a killmail link — EVE's automatic record of a ship loss, copyable from the in-game combat log or our killboard) after the op; SRP is why you can commit to fights without fearing the ISK loss."),
    ('Buyback', 'A corp service that buys your ore, loot, and salvage at a posted percentage of market value, paid on the spot. You skip the hauling and selling; the corp handles the logistics and keeps a small cut for the trouble.'),
    ('Blues / Reds', "Friend-or-foe labels set by standings your leaders configure: blues are allies — including coalition partners, other alliances we're teamed up with — and reds are hostiles. The colors show in local chat and on your overview; never shoot a blue. Anyone with no standings (a 'neutral') is NOT a friend — most nullsec groups, ours included, fly NBSI: Not Blue? Shoot It. Report neutrals in intel."),
    ('Sov', "Short for sovereignty — an alliance's official ownership of nullsec systems, which unlocks upgrades like better ratting and mining. Our sov is the space we live in and what enemies attack, so defending it is the point of many fleets."),
    ('CTA', "Call To Arms — the highest-priority fleet ping (an alert on Discord/comms telling everyone to log in); if you can log in, you're expected to show up. You'll also hear 'strat op' (a planned strategic operation) and 'home defense' (drop everything, our space is under attack) — these fleets are almost always SRP-covered."),
    ('Staging', "The system and structure the alliance designates as its military home — keep your doctrine ships and a clone there, because that's where fleets form. When the alliance temporarily relocates staging into a war zone, that's called a deployment."),
    ('Opsec / Spai', "Opsec (operational security) means keeping fleet times, pings, and plans inside the alliance — assume enemy spies ('spais') are reading anything public, because in EVE they really are. Never repost pings, fleet locations, or intel outside official channels."),
    ('o7', "A little text salute — the 'o' is a head, the '7' a saluting arm. Pilots use it as hello, goodbye, and a sign of respect; you'll see a wall of 'o7' in fleet chat at the end of every op."),
]


def seed(apps, schema_editor):
    Milestone = apps.get_model("onboarding", "OnboardingMilestone")
    Term = apps.get_model("onboarding", "GlossaryTerm")
    for key, title, description, category, criteria, url, sort_order in MILESTONES:
        Milestone.objects.get_or_create(
            key=key,
            defaults={
                "title": title,
                "description": description,
                "category": category,
                "criteria": criteria,
                "url": url,
                "sort_order": sort_order,
                "active": True,
            },
        )
    for term, definition in GLOSSARY:
        Term.objects.get_or_create(term=term, defaults={"definition": definition})


class Migration(migrations.Migration):
    dependencies = [
        ("onboarding", "0002_onboardingmilestone_url"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
