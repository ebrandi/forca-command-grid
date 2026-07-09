# Leadership Features

A concise, per-feature guide to running each leadership subsystem. Every subsystem below
is grounded in the [Feature catalog](../feature-catalog.md) — see that page for full
detail on data, background jobs, and integrations.

**A recurring design point:** wherever a subsystem includes a reward or payout engine
(combat rank rewards, mentorship rewards, SRP, guaranteed buyback), it **never auto-moves
ISK**. Each generates a reviewable record that an officer or director explicitly approves
and marks paid, with separation of duties preventing self-approval. The app keeps the
books; a human always pulls the trigger in-game.

## Table of contents

- [Killboard administration](#killboard-administration)
- [Operations management](#operations-management)
- [Readiness platform](#readiness-platform)
- [Command Intelligence](#command-intelligence)
- [Recommendations and alerts](#recommendations-and-alerts)
- [Pingboard](#pingboard)
- [SRP program](#srp-program)
- [Mining payouts](#mining-payouts)
- [Industry settings](#industry-settings)
- [Mentorship program](#mentorship-program)
- [Raffle contests](#raffle-contests)
- [Recruitment desk](#recruitment-desk)
- [Corp finance, structures, and standings](#corp-finance-structures-and-standings)

## Killboard administration

Manage kill-feed thresholds and newbro framing at `/killboard/killfeed/settings/`, and the
combat rank ladder (a configurable 17-rung structure) plus its optional reward engine from
the console. The reward engine ships off by default; when you enable it, baselining
ensures existing pilots aren't retroactively rewarded for past activity. Battle reports
and watchlists (`/killboard/battles/`, `/killboard/intel/`) are officer-managed intel
tools built on the same killmail feed.

## Operations management

Create and run fleet operations at `/operations/create/` and `/operations/`: set an
objective, target date, doctrines, and ship composition, and the system scores readiness
and turns gaps into prep tasks automatically. As an officer or `fc`-capability holder you
can edit, override, or cancel an op, announce it, pull the live in-game fleet for
attendance, and manage the structure-timer board (`/operations/timers/`) and recurring op
templates (`/operations/templates/`).

## Readiness platform

A configurable readiness intelligence platform, entirely officer-facing for
configuration. Enable and weight each of the readiness dimensions, tune scoring method and
thresholds, and set alert rules, mandatory ships, and strategic role targets from the
console. The platform then runs on its own: it computes a composite readiness index,
records durable findings, generates prep tasks, fires alerts, and produces a weekly
executive report — with a risk register, timeline, and a what-if fleet simulator at
`/readiness/`.

## Command Intelligence

An LLM-backed strategic subsystem at `/command/` for officers and directors. It builds an
intelligence snapshot from every other data source, computes capability ceilings, and
generates classification-tiered staff briefings with prioritised Courses of Action —
visibility further gated by classification clearance, up to director-eyes-only. It also
runs campaigns, a what-if simulator, conversational Q&A over the archive, and battle
after-action reviews. All LLM calls run in background workers only; leave `LLM_API_KEY`
unset to keep the subsystem disabled cleanly. Model, budgets, thresholds, classification
floors, and the autonomous proposer's kill switch are all console-configurable.

## Recommendations and alerts

Explainable, ranked officer suggestions — doctrine readiness, skill training gaps, stock
shortages, build-vs-buy calls, hauling needs, newbro next-steps, and combat-loss patterns —
with an action queue at `/recommendations/`. This subsystem also relays interesting
in-game notifications and corp mailing-list mail to the site and Discord; tune relay
behaviour and recommendation weighting from the console's relay and tuning settings pages.

## Pingboard

The unified alerting and calendar system at `/pingboard/`. Configure channels (Discord,
Slack, Telegram, WhatsApp, EVE-mail), automation rules, and message templates from
`/ops/admin/pingboard/`. Dispatch floors for urgent and emergency priority are
configurable (director by default) — adjust them there if an officer needs to send
time-critical pings. The events calendar syncs from source services automatically and
materialises reminder alerts on its own.

## SRP program

Tune a single ship-replacement program — payout mode (replacement, full ISK, or
top-up), valuation, and eligibility rules — at `/srp/settings/`. Review the claim queue at
`/srp/queue/`, batch-approve or batch-pay eligible claims, and set the reimbursement
budget at `/srp/budget/`. As noted above, the app tracks and records SRP; it never moves
ISK, and an SRP manager can never approve their own claim.

## Mining payouts

Review the corp mining ledger (pulled from ESI refinery observer records), set the corp
mining tax, and split operation proceeds among participants from `/mining/`. Finalised
payouts are frozen and their individual lines are protected against duplicate or
overlapping claims.

## Industry settings

Configure market, tax, fee, and facility defaults, plus governance toggles, for the
Industry Center at `/ops/admin/industry/settings/`. This single settings page underpins
the calculator, invention planner, chain explorer, and job tracker every member uses.

## Mentorship program

Configure the cadet↔veteran mentorship programme, approve pairings, and pay
programme rewards from the console. Field-exercise validation runs against already-synced
data (never live ESI in the request path), so approvals are based on evidence the app can
show you, not a pilot's self-report alone.

## Raffle contests

Run fair, reproducible engagement raffles from `/raffle/`: configure ticket sources (PvP,
mining, fleet attendance, manual awards), and let the commit-reveal draw mechanism run the
actual selection with a full, publicly auditable trail. Raffles carry their own audience
(default `corp`) — see [Features and audiences](./features-and-audiences.md).

## Recruitment desk

The recruitment candidate pipeline at `/recruitment/` requires the `recruitment.manage`
capability (the `recruiter` role or officer and above). It works from public data by
default — corp history, character age, killboard record — presented as evidence with
confidence levels, never an automated accept/reject decision. An optional, time-boxed
candidate ESI consent flow (via a second, separate EVE application) can add richer claims
when a candidate agrees to it; the raw token is used once and discarded, never stored.

## Corp finance, structures, and standings

- **Corp finance** (`/roster/finance/`) — wallet balances, income/expense analytics,
  forecast, and journal. Director-only.
- **Structures** (`/operations/`, structure board) — fuel, online/low-power state, and
  reinforcement timers for corp Upwell structures, with a deduped officer alert on any
  breach-set change. Officer.
- **Standings board** (`/roster/standings/`) — the blue/red standings board driven by corp
  contacts. Officer to sync, member to view.
- **Roster and access governance** (`/roster/`, `/ops/admin/access/`) — corp membership
  and registration compliance (officer), and the partner-alliance / friendly-corporation
  records that widen alliance-service access (director).

---

Next: see [Workflows](./workflows.md) for recommended day-to-day, weekly, and monthly
routines that tie these subsystems together.
