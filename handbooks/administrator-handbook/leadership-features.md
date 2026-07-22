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
- [Combat Signatures](#combat-signatures)
- [Operations management](#operations-management)
- [Readiness platform](#readiness-platform)
- [Command Intelligence](#command-intelligence)
- [Recommendations and alerts](#recommendations-and-alerts)
- [Pingboard](#pingboard)
- [SRP program](#srp-program)
- [Mining payouts](#mining-payouts)
- [Industry settings](#industry-settings)
- [Shipyard inventory and fulfilment policy](#shipyard-inventory-and-fulfilment-policy)
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

## Combat Signatures

Let home-corp pilots build personalised, publicly embeddable banner images from their own
killboard data. The feature ships **dark**: it needs both the `killboard` feature enabled on
**Admin → Features** and its own **master switch** turned on in the console. Manage it from
the admin console's Combat Signatures screens (`/ops/`).

**Enabling and defaults (Director).** The settings screen is the master switch plus the
corp-wide defaults: the **active-signature quota** per pilot (default 5), the **refresh
interval** for live banners in hours (default 6), whether **snapshots** are allowed (on by
default), the **featured-trophy cap** (default 4), the default background/layout/period a
new signature starts from, and which **size presets** pilots may choose. Saving stamps who
changed it and audits `signatures.settings_update` with the changed field names.

**Membership-loss policy (Director).** By default a pilot who leaves the corp has their
signatures **frozen** — the images stay up, automatic refresh stops, and editing is blocked
(a rejoin unfreezes them on the next sweep). Turning on **revoke on leave** instead **deletes**
a leaver's images and disables the signatures. Choose per your corp's disclosure posture.

**Background curation (Director).** The background library is a fixed set of original
procedural designs seeded from a committed manifest. You can **enable/disable** and
**reorder** designs, and pick the default — but there is **no upload path anywhere by
design**; leadership cannot add a raster image, only curate the built-ins. Toggles and
reorders audit `signatures.background_toggle` / `signatures.background_reorder`. See the
[background library reference](../reference/signature-backgrounds.md) for the catalogue and
its provenance.

**Render health and moderation (Officer).** The dashboard shows status/render counts, the
oldest pending render, parked failures **with the technical render error** a pilot never
sees, storage usage and an orphan estimate, and a **provenance check** that verifies the
shipped background art still matches its recorded checksums. The per-pilot search screen lets
an officer find a member's signatures and **disable** one (its public image goes offline
immediately), **re-enable** it, or **force a re-render**. Admin disable/enable skip the owner
ceiling (moderation, not an owner edit) and audit `signatures.admin_disable` /
`signatures.admin_enable`; a forced re-render audits `signatures.admin_regenerate`.

**Maintenance (Director).** Two buttons on the console call the maintenance-task launcher:
**re-render all** (flags every active signature for a fresh render — the refresh tick then
drains them at its per-tick cap, so it never storms the worker) and **clean up orphaned
images** (deletes banner files with no live signature). Re-render all is the job to run after
restoring a database onto an empty media volume.

**Audit trail.** Every signature action writes an immutable audit row. Owner actions:
`signatures.create`, `signatures.edit`, `signatures.rename`, `signatures.duplicate`,
`signatures.rotate_token`, `signatures.snapshot`, `signatures.disable`, `signatures.enable`,
`signatures.delete`, and the manual `signatures.regenerate`. System/lifecycle:
`signatures.freeze` and `signatures.unfreeze` (membership sweep). Leadership:
`signatures.settings_update`, `signatures.background_toggle`, `signatures.background_reorder`,
`signatures.admin_disable`, `signatures.admin_enable`, and `signatures.admin_regenerate`.

The operator-facing side — the media volume, nginx serving, the two beat jobs, backups, and
the env knobs — is in the
[Operations Runbook](../operator-handbook/operations-runbook.md#combat-signatures).

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

A ping is not rendered once and copied everywhere. Legs addressed to individual pilots
(in-app, EVE-mail, DM handles) are grouped by each pilot's own language preference and
sent once per language, so one ping can leave in several languages at the same time. A
pilot who has never picked a language gets the broadcast language instead: dispatch runs
in a background worker, which has no browser to detect one from. A shared leg has no
single recipient — a Discord webhook, a configured group channel — so it is rendered once
in the corp's **broadcast language**, set alongside the enabled languages at
`/ops/admin/i18n/`; see
[Features and audiences: Languages](./features-and-audiences.md#languages).

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

## Shipyard inventory and fulfilment policy

Control what the doctrine Shipyard actually promises. The **fulfilment policy**
(`/store/inventory/policy/`) sets the corp-wide defaults: whether backorders are
accepted, the default lead time, whether one order may mix reserved stock with a
backordered remainder, the default delivery location, per-order caps, whether
out-of-stock ships stay visible, the optional waitlist, and an optional expiry window
that releases the holds of orders nobody claims (off by default). The **inventory
console** (`/store/inventory/`) lists every offered fit with its on-hand complete
ships, active reservations, available-to-promise, incoming production, open backorders,
safety/reorder/target levels, estimated days of cover and alerts; per fit you can
override any policy default (blank fields visibly inherit), receive newly assembled
ships (which reserves them for waiting backorders oldest-first), run stocktakes
(mandatory reason, immutable ledger, advisory ESI hull cross-check that never
overwrites your count), revalidate stock stranded by a fit edit, and turn the
consolidated supply need into an Industry Project, an ERP build job, or a claimable
task without ever creating duplicates. Record only complete, fitted ships here — hulls
and loose modules belong in the stockpile.

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
