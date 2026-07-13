"""Curated content for the public features showcase (/features/).

One entry per featured screen, in tour order. Screenshots live in static/showcase/.
Kept as plain data so the copy is editable in one place; the view (config.views.showcase)
decorates it with running index, alternating layout and per-category counts.

All visitor-facing copy is wrapped in ``gettext_lazy``. It MUST stay lazy: this module is
evaluated once at import, so eager ``gettext`` would freeze whichever locale was active at
process start into every response. The lazy proxies resolve per request, at render time.
Category *keys* ("pilot", "combat", ...), sprite ids and screenshot paths are identifiers —
never translate those. EVE game nouns inside the copy (Jita, Typhoon, ISK, EFT, ...) are
protected terms and must survive translation unchanged; see core/i18n/data/protected-terms.yml.
"""
from __future__ import annotations

from django.utils.translation import gettext_lazy as _

# Filter categories, in feed + chip order. key -> label shown to visitors.
CATEGORIES = [
    ("pilot", _("Pilot")),
    ("combat", _("Combat & Intel")),
    ("industry", _("Industry & Market")),
    ("services", _("Member services")),
    ("community", _("Community")),
    ("navigation", _("Navigation")),
    ("leadership", _("Leadership")),
]
CATEGORY_LABELS = dict(CATEGORIES)

_S = "showcase/"  # static path prefix


def _t(fn, alt):
    return {"src": _S + fn, "alt": alt}


# The "command deck": high-value leadership/intelligence tools that are private to the corp,
# so no screenshots are shown. Promoted with a redacted-HUD treatment instead. Each: icon
# (sprite id), tag, title, lede, benefits[].
PRIVATE_FEATURES = [
    {
        "icon": "i-command", "tag": _("Director access"),
        "title": _("Command Intelligence"),
        "lede": _("An AI staff officer for strategic decisions — grounded in your own killboard, "
                  "doctrines and readiness, never generic advice."),
        "benefits": [
            _("After-action reviews of every battle: what happened, what went wrong, what to fix"),
            _("Courses of action weighed against your real constraints and doctrine"),
            _("Strategic campaigns, a what-if simulator, and plain-language answers from your intel archive"),
        ],
    },
    {
        "icon": "i-shield", "tag": _("Officer tools"),
        "title": _("Readiness platform"),
        "lede": _("One honest score for how ready your corp actually is to fight — across a dozen "
                  "dimensions, refreshed continuously."),
        "benefits": [
            _("Skills, doctrines, assets, staging, finance, SRP and fleet-shape scored together"),
            _("A risk register that routes each gap to the officer who owns it"),
            _("Weekly readiness report, trend timeline and a fleet-composition simulator"),
        ],
    },
    {
        "icon": "i-coin", "tag": _("Director access"),
        "title": _("Corp finance & SRP"),
        "lede": _("The corp's money, visible and under control — from wallet balances to ship "
                  "replacement, with the guard rails leadership needs."),
        "benefits": [
            _("Wallet balances, income/expense, forecast and top earners at a glance"),
            _("Ship-replacement program with budgets, payout modes and separation of duties"),
            _("Loss-impact analysis so replacement spend tracks real fleet value"),
        ],
    },
    {
        "icon": "i-box", "tag": _("Officer tools"),
        "title": _("Structures & sovereignty"),
        "lede": _("Never lose a structure to a missed timer — every asset in space, ranked by "
                  "urgency, with alerts before it matters."),
        "benefits": [
            _("Fuel level, state and reinforcement timers for every corp structure"),
            _("Sovereignty ADM and vulnerability windows where your alliance holds space"),
            _("Deduped officer alerts on fuel and timers, days ahead"),
        ],
    },
    {
        "icon": "i-rookie", "tag": _("Officer tools"),
        "title": _("Recruitment desk"),
        "lede": _("Vet applicants on evidence, not vibes — candidates consent to a one-time ESI "
                  "read that becomes clear vetting evidence."),
        "benefits": [
            _("Consent-first, one-time skill and corp-role read (no tokens stored)"),
            _("Derived evidence: can they fly the doctrines, are the roles what they claim"),
            _("A pipeline from applicant to member, with officer sign-off"),
        ],
    },
    {
        "icon": "i-lock", "tag": _("Director access"),
        "title": _("Governance & audit"),
        "lede": _("Least-privilege by construction, and a full record of every sensitive action — "
                  "so leadership can hold access to account."),
        "benefits": [
            _("Role-based access with dual-control on director grants"),
            _("Per-service audience control — corp, alliance, public or off"),
            _("An immutable audit log and configurable data-retention policy"),
        ],
    },
]


# Each: cat, title, lede, benefits[], shot, alt, thumbs[]
FEATURES = [
    # ---- Pilot ----
    {
        "cat": "pilot",
        "title": _("Your Command Center"),
        "lede": _("Every pilot lands on one page that already knows what matters — the single most useful "
                  "thing to do this week, your combat-rank climb, your readiness, and the live corp feed."),
        "benefits": [
            _("A prioritised quest log tells you what to fly, train or build next — and why the corp needs it"),
            _("Combat rank, 7-day kills/losses and ISK destroyed, at a glance"),
            _("Doctrine coverage and a readiness score, with one-click build plans"),
        ],
        "shot": _S + "Screenshot_8-7-2026_21447_forca.club.jpeg",
        "alt": _("FORCA pilot Command Center dashboard with quest log, combat rank and readiness"),
    },
    {
        "cat": "pilot",
        "title": _("Skills & training, planned around real demand"),
        "lede": _("Your full skill sheet and live training queue, synced straight from EVE — plus training "
                  "plans that target the doctrines and roles the corp actually needs."),
        "benefits": [
            _("Total SP, every skill and your live queue with time-to-finish"),
            _("Build a training plan from a doctrine or role and follow it in order"),
            _("Refresh straight from EVE — no manual entry, ever"),
        ],
        "shot": _S + "Screenshot_8-7-2026_211221_forca.club.jpeg",
        "alt": _("My Skills & Training page with SP totals and a live training queue"),
    },
    {
        "cat": "pilot",
        "title": _("Doctrines you can actually fly"),
        "lede": _("Every corp doctrine as a fit card tagged with whether you can fly it right now, backed by "
                  "charts that show fleet readiness and which skills unlock the most ships."),
        "benefits": [
            _("Filter instantly to fits you're flyable in — or one skill away from"),
            _("“Train these next” ranks the skills that unlock the most doctrines"),
            _("Fleet-shape and readiness charts show leadership where the gaps are"),
        ],
        "shot": _S + "Screenshot_8-7-2026_211950_forca.club.jpeg",
        "alt": _("Ships & Doctrines dashboard with readiness donuts and fit cards"),
        "thumbs": [_t("screenshot-1783542019644.png", _("Best next doctrine to unlock"))],
    },
    # ---- Combat & Intel ----
    {
        "cat": "combat",
        "title": _("A killboard built for your corp"),
        "lede": _("Your whole combat record in one feed — efficiency, threat level, biggest kills and the "
                  "pilots and systems carrying the fight — with none of the third-party clutter."),
        "benefits": [
            _("Kills tracked with efficiency, threat and a 14-day trend"),
            _("Biggest-kills carousel plus top-killer and active-system rails"),
            _("Every killmail doctrine-tagged and filterable by time window"),
        ],
        "shot": _S + "Screenshot_8-7-2026_211520_forca.club.jpeg",
        "alt": _("Killboard kill feed with efficiency, threat and biggest kills"),
        "thumbs": [_t("Screenshot_8-7-2026_211551_forca.club.jpeg", _("Killmail detail"))],
    },
    {
        "cat": "combat",
        "title": _("Combat analytics that find the pattern"),
        "lede": _("Twelve months of kills and losses, what you destroy versus what you lose, doctrine "
                  "compliance, and exactly when your corp fights — the intel to fly smarter."),
        "benefits": [
            _("Monthly kills/losses and ISK trends in one chart"),
            _("Ships-destroyed vs ships-lost donuts with top-10 tables"),
            _("A day-by-hour heatmap of when your fleet is strongest"),
        ],
        "shot": _S + "Screenshot_8-7-2026_211620_forca.club.jpeg",
        "alt": _("Combat statistics dashboard with monthly charts and an activity heatmap"),
        "thumbs": [_t("screenshot-1783544085673.png", _("Per-pilot combat analytics"))],
    },
    {
        "cat": "combat",
        "title": _("Leaderboards that reward the fight"),
        "lede": _("Nine fair, time-boxed leaderboards — most valuable kills, top killers, solo, efficiency "
                  "and more — plus a 17-rung combat-rank ladder, ready to drive prize challenges."),
        "benefits": [
            _("Period filters (7d / 30d / season / all-time) keep contests fair"),
            _("See your own standing and climb the combat-rank ladder"),
            _("Capital-kill highlights and per-category top pilots"),
        ],
        "shot": _S + "Screenshot_8-7-2026_21171_forca.club.jpeg",
        "alt": _("Combat rankings page with nine leaderboards and a rank ladder"),
    },
    {
        "cat": "combat",
        "title": _("Find the fight before it finds you"),
        "lede": _("Rank null-sec systems by live ratting and mining activity — weighted against recent PvP — "
                  "so your roam heads where the targets are, not where they were."),
        "benefits": [
            _("Systems scored by NPC kills, traffic and contested activity"),
            _("Filter by region and security band in one click"),
            _("Jump straight from a hot system into a route"),
        ],
        "shot": _S + "Screenshot_8-7-2026_212225_forca.club.jpeg",
        "alt": _("Roaming targets intel table scoring null-sec systems by activity"),
        "thumbs": [_t("Screenshot_8-7-2026_212254_forca.club.jpeg", _("Gate-camp watch"))],
    },
    # ---- Industry & Market ----
    {
        "cat": "industry",
        "title": _("Cost any build at live Jita prices"),
        "lede": _("Pick an item, runs, ME and structure — and get the full material bill, job fees, build "
                  "time and profit margin, priced against the live market."),
        "benefits": [
            _("Total cost, sell value, net profit and per-unit margin up front"),
            _("A ready shopping list with need / to-buy / cost per material"),
            _("Structure, rig and “build when cheaper” strategy built in"),
        ],
        "shot": _S + "Screenshot_8-7-2026_212933_forca.club.jpeg",
        "alt": _("Manufacturing calculator costing a build with a material shopping list"),
        "thumbs": [_t("Screenshot_8-7-2026_212843_forca.club.jpeg", _("Industry Center hub"))],
    },
    {
        "cat": "industry",
        "title": _("Know if inventing beats buying"),
        "lede": _("Enter your science skills and a decryptor and the planner returns real success chance, "
                  "attempts per success, true cost per BPC — and a clear invent-vs-buy verdict."),
        "benefits": [
            _("Success chance and attempts-per-success from your actual skills"),
            _("Real cost per BPC including datacores and decryptor"),
            _("A plain verdict: invent it, or just buy it"),
        ],
        "shot": _S + "Screenshot_8-7-2026_213037_forca.club.jpeg",
        "alt": _("Tech II invention planner with success chance and invent-vs-buy verdict"),
    },
    {
        "cat": "industry",
        "title": _("Turn a shopping list into a build plan"),
        "lede": _("Plan a batch — say ten Typhoons — and it resolves the entire bill of materials down to "
                  "minerals, shows in-stock versus to-acquire, and flags the cost bottlenecks."),
        "benefits": [
            _("Full bill of materials resolved to raw minerals"),
            _("In-stock vs to-acquire, with a bottleneck breakdown"),
            _("Push straight to the corp job board and reserve stock"),
        ],
        "shot": _S + "Screenshot_8-7-2026_213438_forca.club.jpeg",
        "alt": _("Industry project detail resolving a Typhoon batch to minerals"),
        "thumbs": [_t("Screenshot_8-7-2026_213525_forca.club.jpeg", _("Industry projects board"))],
    },
    {
        "cat": "industry",
        "title": _("Planetary Industry, guided"),
        "lede": _("A guided PI assistant that explains the P0→P4 tiers, recommends what to make, explores "
                  "production chains, and imports your live colonies to spot problems."),
        "benefits": [
            _("“What should I make?” recommendations for your planets"),
            _("Explore any production chain end to end"),
            _("Import live colonies to check layouts and output"),
        ],
        "shot": _S + "Screenshot_8-7-2026_213544_forca.club.jpeg",
        "alt": _("Planetary Industry hub with chain explorer and tier primer"),
        "thumbs": [
            _t("Screenshot_8-7-2026_21368_forca.club.jpeg", _("PI production-chain explorer")),
            _t("Screenshot_8-7-2026_213657_forca.club.jpeg", _("PI: what should I make?")),
        ],
    },
    {
        "cat": "industry",
        "title": _("Build what the fleet is short on"),
        "lede": _("One ranked list of what your doctrines are missing across the corp, costed by ISK-to-close "
                  "— turn any shortfall into a tracked production plan in a click."),
        "benefits": [
            _("Shortfalls ranked by cost to keep N doctrine sets stocked"),
            _("Per-hull breakdown of what's missing right now"),
            _("One click turns a gap into a costed build plan"),
        ],
        "shot": _S + "screenshot-1783542789260.png",
        "alt": _("Corp demand screen ranking doctrine shortfalls by cost to close"),
    },
    {
        "cat": "industry",
        "title": _("See the spreads worth building"),
        "lede": _("What the corp needs and where the profit is — a colour-coded build-cost-versus-Jita table "
                  "that surfaces the hulls and rigs actually worth manufacturing."),
        "benefits": [
            _("“Profitable to build” ranked by margin against live Jita"),
            _("Corp-needs panel flags anything below target stock"),
            _("Capital hulls and structure rigs with real margins"),
        ],
        "shot": _S + "screenshot-1783543375763.png",
        "alt": _("Market page with a profitable-to-build table and margins"),
    },
    # ---- Member services ----
    {
        "cat": "services",
        "title": _("Order a doctrine ship, built to spec"),
        "lede": _("Every sanctioned fit as an orderable card with a live built price — filter to your hull "
                  "and role, confirm you can fly it, and order or copy the EFT in a click."),
        "benefits": [
            _("Transparent pricing: Jita plus a flat corp markup"),
            _("Filter by hull, role and whether you can fly it"),
            _("Order the hull or copy the EFT straight into EVE"),
        ],
        "shot": _S + "Screenshot_8-7-2026_212119_forca.club.jpeg",
        "alt": _("Shipyard grid of orderable doctrine fits with built prices"),
        "thumbs": [
            _t("Screenshot_8-7-2026_214752_forca.club.jpeg", _("Corp Store build board")),
            _t("screenshot-1783543711830.png", _("Corp Store supply forecast")),
        ],
    },
    {
        "cat": "services",
        "title": _("Get a fair offer in ten seconds"),
        "lede": _("Paste your hangar and get an instant, fair offer based on live Jita-sell prices — sell "
                  "from anywhere, skip the market grind, get paid."),
        "benefits": [
            _("Instant appraisal from live Jita-sell pricing"),
            _("Transparent location margin — no mystery haircuts"),
            _("Sell from anywhere; leadership sets who can use it"),
        ],
        "shot": _S + "Screenshot_8-7-2026_214619_forca.club.jpeg",
        "alt": _("Buyback paste-to-appraise screen with an instant ISK offer"),
    },
    {
        "cat": "services",
        "title": _("Instant courier quotes"),
        "lede": _("A Red-Frog-style hauling service: pick ship class, route, volume and collateral and get an "
                  "instant reward quote — then post it straight to the corp freight board."),
        "benefits": [
            _("Instant reward and jumps from route, volume and collateral"),
            _("Rates set by your officers, posted to the freight board"),
            _("Blockade-runner or DST classes with clear limits"),
        ],
        "shot": _S + "Screenshot_8-7-2026_214452_forca.club.jpeg",
        "alt": _("Freight rate calculator producing a courier quote"),
    },
    # ---- Community ----
    {
        "cat": "community",
        "title": _("Every capsuleer flies better with a wingman"),
        "lede": _("A full mentorship program pairing new Cadets with Veteran Mentors across twelve learning "
                  "tracks — with eligibility, matching, rewards and a clear four-step flow."),
        "benefits": [
            _("Twelve learning tracks from Ratting to Fleet Ops"),
            _("Eligibility, matching and rewards handled end to end"),
            _("Cadets earn ISK, points and badges as they progress"),
        ],
        "shot": _S + "Screenshot_8-7-2026_21545_forca.club.jpeg",
        "alt": _("Mentorship program landing page with learning tracks"),
        "thumbs": [
            _t("Screenshot_8-7-2026_21614_forca.club.jpeg", _("Learning tracks grid")),
            _t("Screenshot_8-7-2026_21728_forca.club.jpeg", _("Track: fitting & doctrine")),
            _t("Screenshot_8-7-2026_21852_forca.club.jpeg", _("Track: fleet operations")),
        ],
    },
    {
        "cat": "community",
        "title": _("Recognise who carried the corp"),
        "lede": _("A monthly leaderboard that puts every contribution — built, hauled, mined, flown, killed — "
                  "on one honest points scale, so effort of every kind gets seen."),
        "benefits": [
            _("Top-10 contributors on one unified monthly scale"),
            _("“What counts” makes the scoring fully transparent"),
            _("Per-category top-fives for builders, haulers, miners and more"),
        ],
        "shot": _S + "Screenshot_8-7-2026_211811_forca.club.jpeg",
        "alt": _("Hall of Fame monthly contribution leaderboard"),
    },
    {
        "cat": "community",
        "title": _("Raffles that reward showing up"),
        "lede": _("Provably-fair prize raffles that drive real engagement — earn tickets by flying, unlock "
                  "bigger pools as the corp hits goals, and draw winners nobody can rig."),
        "benefits": [
            _("Earn tickets for PvP, mining or fleet activity"),
            _("Unlock bars and ticket boosters drive turnout"),
            _("A commit-reveal draw that's provably fair"),
        ],
        "shot": _S + "Screenshot_8-7-2026_211829_forca.club.jpeg",
        "alt": _("Raffle contest page with prize tiers and a ticket leaderboard"),
    },
    {
        "cat": "community",
        "title": _("New players, up to speed fast"),
        "lede": _("A “Welcome to nullsec” path with a personal progress tracker, a veteran survival guide, "
                  "and a searchable glossary of EVE slang — so newbros stop drowning."),
        "benefits": [
            _("A guided checklist from “get connected” to “live here”"),
            _("A veteran survival guide covering the real basics"),
            _("A searchable glossary so the jargon stops being a wall"),
        ],
        "shot": _S + "Screenshot_8-7-2026_2154_forca.club.jpeg",
        "alt": _("New Player onboarding page with progress tracker and glossary"),
    },
    # ---- Navigation ----
    {
        "cat": "navigation",
        "title": _("Plan a capital jump, fuel and all"),
        "lede": _("Full jump-drive routing for capitals: enter the hull and your JDC/JFC/JF skills and get a "
                  "fuelled plan with low-sec staging, per-leg cooldowns and a copyable cyno chain."),
        "benefits": [
            _("Cyno and gate legs, distance, fuel and ISK cost computed"),
            _("Low-sec exit staging options ranked for you"),
            _("Copy the cyno waypoints or gate route straight to EVE"),
        ],
        "shot": _S + "Screenshot_8-7-2026_21257_forca.club.jpeg",
        "alt": _("Jump Planner with fuel, staging and per-leg routing"),
        "thumbs": [
            _t("Screenshot_8-7-2026_212352_forca.club.jpeg", _("Route map")),
            _t("Screenshot_8-7-2026_212633_forca.club.jpeg", _("Jump-range reach map")),
        ],
    },
    {
        "cat": "navigation",
        "title": _("Maps with the data layers that matter"),
        "lede": _("Interactive region and system maps with toggleable overlays — security, your activity, "
                  "traffic, ship and NPC kills, sovereignty — so you read space at a glance."),
        "benefits": [
            _("Overlay security, traffic, kills and sov on any region"),
            _("Drill from region to constellation to a single system"),
            _("Jump from any map straight into route planning"),
        ],
        "shot": _S + "Screenshot_8-7-2026_212658_forca.club.jpeg",
        "alt": _("Region map with data-overlay layer toggles"),
        "thumbs": [
            _t("Screenshot_8-7-2026_212815_forca.club.jpeg", _("Solar-system detail")),
            _t("Screenshot_8-7-2026_212645_forca.club.jpeg", _("All region maps")),
        ],
    },
    # ---- Leadership ----
    # NOTE: Command Intelligence deliberately has NO screenshot here. It is listed in
    # PRIVATE_FEATURES above with the redacted-HUD treatment, because a real after-action
    # review names real pilots and grades their performance. The screenshot that used to
    # sit here showed exactly that: a named member criticised for losing two ships on an
    # off-doctrine fit, plus a recommendation to restrict their ship assignments. That is
    # corp-internal commentary about an identifiable person and does not belong on a
    # public, indexable marketing page. Do not re-add a real AAR screenshot; if you want
    # to show this feature, render one from synthetic data.
    {
        "cat": "leadership",
        "title": _("Run the whole corp from one console"),
        "lede": _("Every leadership control in one place — roles, doctrines, readiness, rewards, services, "
                  "structures, alerts and intelligence — with a full audit trail and no database to touch."),
        "benefits": [
            _("Dozens of self-service config surfaces, grouped and clear"),
            _("Every sensitive action role-gated and audit-logged"),
            _("Runs the corp without ever opening a database"),
        ],
        "shot": _S + "Screenshot_8-7-2026_215355_forca.club.jpeg",
        "alt": _("Leadership admin console with grouped configuration cards"),
    },
    {
        "cat": "leadership",
        "title": _("One alerting hub for the whole corp"),
        "lede": _("Pingboard unifies alerts and a shared calendar — timers, pings and urgent activity in one "
                  "place, delivered to Discord, EVE-mail and wherever your pilots actually look."),
        "benefits": [
            _("Recent, scheduled and failed alerts at a glance"),
            _("A synced corp calendar of ops and timers"),
            _("Fans out to Discord, EVE-mail, Slack, Telegram and WhatsApp"),
        ],
        "shot": _S + "Screenshot_8-7-2026_21144_forca.club.jpeg",
        "alt": _("Pingboard alerts dashboard with calendar and urgent activity"),
        "thumbs": [_t("Screenshot_8-7-2026_211429_forca.club.jpeg", _("Corp calendar"))],
    },
]
