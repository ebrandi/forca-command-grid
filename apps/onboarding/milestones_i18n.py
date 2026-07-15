"""Render-time i18n seam for the seeded onboarding milestones and glossary (Seam A).

``OnboardingMilestone.title`` / ``.description`` and ``GlossaryTerm.definition`` /
``.term`` are **seeded into the database** by migration
``0003_seed_milestones_glossary`` and then editable by leaders (Admin Console ->
New Player content). A ``gettext_lazy`` proxy cannot help: Django coerces it to
``str`` on ``.save()``, freezing whatever locale was active when the row was
created. So the English stays a plain ``str`` in the column -- the canonical
value, the fallback and the audit record -- marked for extraction with
``gettext_noop`` (Django's ``makemessages`` passes ``--keyword=gettext_noop``, so
xgettext sees it exactly as it sees ``_()``), and it is translated at *render*
time by the ``*_for`` helpers, keyed on the row's stable natural key
(``OnboardingMilestone.key`` / ``GlossaryTerm.term``).

"Translate until edited": while the stored value still equals the shipped English
for a known key, the translation is returned; a leader-edited value, or an unknown
key, is returned verbatim in every locale. The stored value is always the floor,
so this can never blank a milestone or a term.

Two things to keep in mind when editing this catalogue:

* Pure acronyms / EVE proper-noun headwords (ISK, FC, Jita, Cyno, ...) are not in
  :data:`GLOSSARY_TERMS` -- only the descriptive multiword phrases are prose. They
  fall back verbatim, which is by policy for game jargon.
* Keep every definition free of a bare ``%``: a literal percent (e.g. "10% speed")
  makes xgettext flag the msgid python-format, and msgfmt then demands that directive
  in every locale -- a fatal build error. ``Tidi`` is deliberately worded "a tenth of
  normal speed" for exactly this reason.
"""
from __future__ import annotations

from django.utils.translation import gettext, gettext_noop

# {OnboardingMilestone.key -> shipped English title}
MILESTONE_TITLES: dict[str, str] = {
    'link-character': gettext_noop('Link your EVE character'),
    'join-comms': gettext_noop('Register on alliance voice comms'),
    'grant-esi': gettext_noop('Grant the corp tools your ESI scopes'),
    'import-skills': gettext_noop('Import your skills'),
    'train-doctrine': gettext_noop('Train into your first doctrine ship'),
    'move-to-staging': gettext_noop('Move your gear to staging'),
    'first-fleet': gettext_noop('Fly your first fleet'),
    'first-isk': gettext_noop('Start your first income stream'),
}

# {OnboardingMilestone.key -> shipped English description}
MILESTONE_DESCRIPTIONS: dict[str, str] = {
    'link-character': gettext_noop('Log in with EVE SSO so the Grid knows who you are — and link every alt you fly, they all count.'),
    'join-comms': gettext_noop("Comms are mandatory for fleets — you don't need to talk, just listen. Grab the invite from your recruiter and set your comms name to your main character's."),
    'grant-esi': gettext_noop('The Grid can only help with what it can see. Grant the optional scopes (assets, market, planets) on the ESI Scopes page — one click per character.'),
    'import-skills': gettext_noop("Share your skill sheet so every 'can I fly this?' answer on this site is about YOU. One click — it keeps itself fresh afterwards."),
    'train-doctrine': gettext_noop('Being able to fly what the FC calls for is your single biggest contribution. Pick the starter doctrine and follow its training plan.'),
    'move-to-staging': gettext_noop('Set your home station to staging, move your doctrine ships there, and keep a cheap travel ship at each end. Use the freight service instead of hauling it yourself.'),
    'first-fleet': gettext_noop('Watch the ops board and x up. Nobody expects anything from you on fleet one — undocking with us IS the milestone.'),
    'first-isk': gettext_noop('Ratting, corp mining fleets, or PI — pick one and get ISK flowing. Sell loot and ore to the corp buyback: zero hauling risk, paid today.'),
}

# {GlossaryTerm.term -> shipped English definition}
GLOSSARY_DEFINITIONS: dict[str, str] = {
    'Highsec / Lowsec / Nullsec': gettext_noop('EVE space comes in security bands. Highsec: NPC police (CONCORD) punish attackers. Lowsec: no police, but no warp-stopping bubbles either. Nullsec — where we live: no police at all, player alliances own the space, and anything goes. More risk, better rewards, real friends.'),
    'Doctrine': gettext_noop("A standard ship fitting published by leadership — the exact hull, modules, and ammo the corp wants you to fly so the whole fleet works together. Fly the doctrine fit as written; it's also what makes your loss SRP-eligible."),
    'Logi': gettext_noop("Logistics ships — the fleet's healers. Logi pilots repair the shields or armor of fleetmates who broadcast for help. Flying logi is one of the most valued roles a newer pilot can learn."),
    'ISK': gettext_noop("EVE's currency (InterStellar Kredits). Everything — ships, ammo, services — is priced in ISK, usually quoted in millions ('m') or billions ('b'). Losing a ship really means losing its ISK cost, which is why cheap, replaceable ships are king."),
    'FC': gettext_noop("Fleet Commander — the pilot leading the fleet and calling every move on voice comms. Their word is law mid-fight: when they say 'check check' or 'clear comms', stop chatting so orders can be heard, and put questions in fleet text chat instead of on comms."),
    'X up': gettext_noop("Typing 'x' in fleet or corp chat to volunteer — for a fleet spot, a specific ship, or a job ('x up if you can fly logi'). It simply means 'count me in'."),
    'Anchor': gettext_noop('The designated pilot the whole fleet physically follows in combat — you set your ship to orbit or keep-at-range on them and let them do the flying. This keeps the fleet in one tight, controlled ball while you focus on targets and broadcasts.'),
    'Align': gettext_noop("To point your ship at a destination and build up speed without actually warping — an aligned ship enters warp almost instantly when the order comes. 'Align to the sun' means turn toward it and wait; do not warp until the FC says so."),
    'Warp to zero': gettext_noop("Warping to exactly 0 km from your destination instead of landing at a distance, so you can jump a gate or dock the moment you arrive. 'Gate, gate, warp to zero' means warp straight onto the gate and jump as soon as you land."),
    'Broadcast': gettext_noop('One-click alerts sent through the fleet window that every fleet member sees. The one that saves your life: broadcast for shield or armor the instant you start taking damage, so the logi pilots know exactly who to heal.'),
    'Free': gettext_noop("Intel-channel shorthand for 'no hostiles here'. Alliance intel channels run on quick calls like 'X-7 free' or 'status?', answered with 'free' (clear) or the names of hostile pilots in the system — check them before you travel. Other groups say 'clr' or 'clear' — same meaning."),
    'Batphone': gettext_noop("Calling a bigger allied group for backup when a fight is too large to handle alone — named after Batman's emergency hotline. 'They batphoned' means the enemy rang up heavy friends, and the fight is about to get much bigger."),
    'D-scan': gettext_noop("The directional scanner — a free, instant scan that lists ships and structures within about 14 AU of your position. Spamming d-scan while you're in space is one of the core survival habits in EVE: it shows most threats before they land on you — but not cloaked ships or Combat Recon cruisers, which are invisible to d-scan, so a clear scan is never a promise."),
    'Safe spot': gettext_noop("A bookmark out in empty space, away from planets, gates, and anything warpable, where you can sit while hostiles sweep the system. A related trick is the insta-undock: a bookmark lined up with a station's exit so you can warp away the second you undock, before campers can lock you. A safe spot hides you — it doesn't make you invincible: hostiles with combat scanner probes can find you in a minute or two, so keep hitting d-scan (probes show up on it) and keep moving between safes."),
    'Podded': gettext_noop("If your ship is destroyed you're left floating in your pod (escape capsule); lose that too and you've been podded — you wake up in a fresh clone at your home station. Podding destroys any implants (skill- and stat-boosting plugs in your head), so keep your home station set somewhere useful and don't take an expensive head into big fights."),
    'Gate camp': gettext_noop("A group of hostiles parked at a stargate, waiting to kill whatever jumps through — in nullsec usually with bubbles so you can't just warp off. Check the intel channels before traveling alone; most newbro losses happen at camps."),
    'Bubble': gettext_noop("A warp disruption bubble — a large sphere that stops everyone inside it from warping and drags passing warps to its edge. Bubbles don't work in high- or low-security space — you'll meet them in nullsec, wormholes, and Pochven. They come from interdictor ships ('dictors') or anchorable deployables; they're the teeth of every nullsec gate camp."),
    'Tidi': gettext_noop("Time dilation — when a massive battle overloads the server, EVE deliberately slows time in that system, sometimes to a tenth of normal speed, so it can process everything. Everything moves in slow motion for everyone equally; it's normal, so stay calm and follow orders."),
    'Tackle': gettext_noop("Catching hostile ships so they can't warp away — a tackler pins a target in place with a warp scrambler or disruptor ('scram' or 'point') while the fleet kills it. It's one of the highest-impact jobs a newer pilot can fly: nothing dies if nothing is tackled."),
    'Cyno': gettext_noop('A cynosural field — a bright beacon a ship lights in space that lets capital ships (and some others) jump directly to it from light-years away, skipping gates entirely. Cynos power both our logistics, like jump freighters, and ambushes (see Hotdrop).'),
    'Hotdrop': gettext_noop("When hostiles light a cyno on your position and heavy ships pour in on top of you with no warning. It's why you never ignore a lone unknown pilot while ratting — they may be the beacon for an entire fleet."),
    'Gank': gettext_noop('Killing a target with overwhelming, unfair force — most famously suicide ganking in high-security space, where attackers happily let the NPC police (CONCORD) destroy their cheap ships because your cargo is worth more. Never haul more than you can afford to lose.'),
    'Awox': gettext_noop("When someone turns their guns on their own corpmates, usually an infiltrator who joined just to betray — named after an infamous player who did exactly that. It's why corps vet applicants, and why you should report suspicious behavior to leadership."),
    'Ratting (krabbing)': gettext_noop("Killing NPC pirates ('rats') in our space for bounty ISK — nullsec's bread-and-butter income, also called krabbing. Bounties arrive in 20-minute payouts, so pilots measure income in 'ticks' ('40m ticks' means 40 million ISK per payout)."),
    'PI': gettext_noop('Planetary Industry — setting up automated extractors and factories on planets for semi-passive income. It takes a few clicks a day and can be managed from anywhere, making it a popular first income stream for new nullsec pilots.'),
    'PLEX': gettext_noop('An item CCP sells for real money that players can also buy and sell for ISK on the in-game market; redeeming 500 of them grants a month of Omega (full subscription) time. In other words, a healthy in-game income can pay for your game time.'),
    'Jita': gettext_noop("EVE's biggest trade hub — a high-security system (specifically the famous Jita 4-4 station) where nearly everything in the game is bought and sold. 'Jita price' is the standard reference price, and our hauling services exist to move goods between Jita and home."),
    'Contract': gettext_noop("EVE's built-in system for player-to-player deals: handing over items, hauling jobs, or ship sales. Corp doctrine ships and buyback payouts usually arrive as contracts — and since scamming is a legal part of EVE, always read a contract carefully before accepting."),
    'JF': gettext_noop('Jump Freighter — a huge cargo ship that jumps across light-years to a cyno beacon instead of taking gates. The alliance JF service hauls your stuff between Jita and our staging system for a small fee, so you never have to fly a defenseless hauler through hostile space.'),
    'SRP': gettext_noop("Ship Replacement Program — lose a doctrine ship on an official fleet and the corp or alliance pays you back. File your loss (usually a killmail link — EVE's automatic record of a ship loss, copyable from the in-game combat log or our killboard) after the op; SRP is why you can commit to fights without fearing the ISK loss."),
    'Buyback': gettext_noop('A corp service that buys your ore, loot, and salvage at a posted percentage of market value, paid on the spot. You skip the hauling and selling; the corp handles the logistics and keeps a small cut for the trouble.'),
    'Blues / Reds': gettext_noop("Friend-or-foe labels set by standings your leaders configure: blues are allies — including coalition partners, other alliances we're teamed up with — and reds are hostiles. The colors show in local chat and on your overview; never shoot a blue. Anyone with no standings (a 'neutral') is NOT a friend — most nullsec groups, ours included, fly NBSI: Not Blue? Shoot It. Report neutrals in intel."),
    'Sov': gettext_noop("Short for sovereignty — an alliance's official ownership of nullsec systems, which unlocks upgrades like better ratting and mining. Our sov is the space we live in and what enemies attack, so defending it is the point of many fleets."),
    'CTA': gettext_noop("Call To Arms — the highest-priority fleet ping (an alert on Discord/comms telling everyone to log in); if you can log in, you're expected to show up. You'll also hear 'strat op' (a planned strategic operation) and 'home defense' (drop everything, our space is under attack) — these fleets are almost always SRP-covered."),
    'Staging': gettext_noop("The system and structure the alliance designates as its military home — keep your doctrine ships and a clone there, because that's where fleets form. When the alliance temporarily relocates staging into a war zone, that's called a deployment."),
    'Opsec / Spai': gettext_noop("Opsec (operational security) means keeping fleet times, pings, and plans inside the alliance — assume enemy spies ('spais') are reading anything public, because in EVE they really are. Never repost pings, fleet locations, or intel outside official channels."),
    'o7': gettext_noop("A little text salute — the 'o' is a head, the '7' a saluting arm. Pilots use it as hello, goodbye, and a sign of respect; you'll see a wall of 'o7' in fleet chat at the end of every op."),
}

# {GlossaryTerm.term -> shipped English headword (descriptive phrases only)}
GLOSSARY_TERMS: dict[str, str] = {
    'Safe spot': gettext_noop('Safe spot'),
    'Gate camp': gettext_noop('Gate camp'),
}


def _field_for(key: str, stored: str, seed: dict[str, str]) -> str:
    """Translate ``stored`` only while it still holds the shipped English for a
    known ``key``; return it verbatim once edited (or for an unknown key)."""
    if stored and stored == seed.get(key or ""):
        return gettext(stored)
    return stored


def milestone_title_for(key: str, stored: str) -> str:
    return _field_for(key, stored, MILESTONE_TITLES)


def milestone_description_for(key: str, stored: str) -> str:
    return _field_for(key, stored, MILESTONE_DESCRIPTIONS)


def glossary_definition_for(term: str, stored: str) -> str:
    return _field_for(term, stored, GLOSSARY_DEFINITIONS)


def glossary_term_for(term: str, stored: str) -> str:
    # The term column is its own natural key, so it is both the lookup and the value.
    return _field_for(term, stored, GLOSSARY_TERMS)

