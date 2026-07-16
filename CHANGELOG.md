# Changelog

All notable changes to [FORCA] Command Grid are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Shipyard availability, reservations and backorders** — the Shipyard no longer
  presents every doctrine ship as immediately available. Complete fitted ships are now
  tracked per fit and delivery location in a ledger-backed inventory (every movement is
  an immutable, attributed entry), and each card shows an honest, accessible state:
  ready for delivery (with the count), limited stock, available on backorder with an
  estimated delivery date, temporarily unavailable, or not offered. Ordering reserves
  real stock under database row locks — two pilots can never oversell the last ship —
  and anything beyond stock becomes a backorder the buyer explicitly acknowledges on a
  confirm page that disclosed the split and the estimate first. Order-time availability,
  split quantities, delivery location, fit revision and the promised date are frozen on
  the order for audit; cancellations release the hold, delivery consumes it exactly
  once, and a leadership-configurable sweep can expire holds nobody claims. Backorders
  consolidate into one supply need per fit and location, which officers turn into an
  Industry Project (finally wiring the store→industry link), an ERP build job, or a
  claimable task — production completion pings the officers to assemble and receipt the
  ships, which auto-allocates them to waiting orders oldest-first and can notify a
  waitlist. New officer surfaces: a **Shipyard inventory console**
  (`/store/inventory/`) with search, filters, bulk offer control, CSV export, audited
  stocktakes with mandatory reasons, an advisory ESI hull cross-check, and per-fit
  policy overrides with visible inheritance; and a **Shipyard fulfilment policy** page
  (`/store/inventory/policy/`) for backorders, lead times, reservation expiry, order
  caps and shop-front visibility. Stock recorded against an older fit revision stops
  counting until an officer revalidates it. New Shipyard filters (availability,
  delivery location) and sorts (availability, fastest delivery, price). Fully
  translated into all nine languages.
- **Per-class Corp Store pricing for capital hulls** — capital and supercapital
  made-to-order hulls are no longer priced off Jita sell × markup (a basis that barely
  exists for hulls that never trade in Jita). They are now priced off their **estimated
  build cost** (EVE Ref's full job cost, falling back to the local SDE material estimate)
  times a per-class profit multiplier leaders configure in **Store settings**
  (`/store/settings/`): separate markups for sub-capital, capital and supercapital hulls.
  Each order freezes its price basis and the build-cost estimate it was quoted from; when
  no build-cost source can answer, a capital order is refused rather than silently quoted
  off a misleading market reference. Sub-capital hulls and doctrine fits keep the classic
  live-Jita-sell markup. Fully translated into all nine languages.
- **Linked Pilots and pilot switching** — a pilot can link several EVE characters to one
  account and switch between them without logging out. New **Pilot → Linked Pilots** page
  (`/pilot/linked-pilots/`), a persistent pilot selector in the sidebar and the mobile drawer,
  and a selector on the Command Center. Every pilot is authorised individually through EVE SSO
  (CCP exposes no way to discover which characters share an EVE account, so nothing is
  inferred). Fully translated into all nine languages.
- **Pilot ESI health** — each linked pilot shows its own authorisation status, missing scopes
  and last synchronisation, with a per-pilot **Reauthorise** action. A dead token on one pilot
  never blocks switching to another.
- **Unlinking** — releases a pilot and destroys its ESI authorisation, keeping its historical
  records. The last remaining pilot cannot be unlinked (an EVE pilot is how you sign in).

### Changed

- **Corporation authority now follows the active pilot, not the account.** Previously a role
  was granted to the *account* if **any** of its characters qualified — so linking one Director
  alt made every pilot on that account a Director, including pilots in unrelated corporations.
  Authority is now the lesser of what the human was granted and what the pilot they are flying
  can substantiate: an out-of-corp pilot carries no corp standing at all, and Director authority
  requires the pilot to actually hold the in-game Director role. Officer still follows the
  person across their corp pilots; `admin` and superuser are unaffected. **Directors keep their
  access across the upgrade** — a data migration attaches the in-game Director seat to each
  current director's corp pilots, and the six-hourly ESI reconcile then narrows it to the pilots
  that genuinely hold the role.
- **Pilot quest logs are now per-pilot.** `PilotDirective` and `PilotRecommendation` were keyed
  on the account while being computed from one character, so regenerating one pilot's quest log
  overwrote another's. Both are now scoped to the pilot they describe; existing rows are
  attached to each account's main.
- The Command Center, "my assets", skills, readiness, operations, doctrines, killboard, buyback,
  store, freight, mentorship and navigation surfaces all resolve *the pilot you are flying*
  rather than the account's main.

### Fixed

- The pilot digest cache (`briefing:pilot:*`) was keyed by account while holding one pilot's
  prose, and was not language-keyed — so it served the Celery warmer's English to every
  non-English reader for the life of the entry.
- The htmx history cache kept rendered, pilot-specific pages in `localStorage`, where no
  `Cache-Control` header governs them; a Back press could repaint a previous pilot's page.

- **Localisation** — the interface is available in nine languages: English, Portuguese
  (Brazil), Spanish, French, Russian, German, Simplified Chinese, Korean, and Japanese.
  English is the canonical source language and cannot be turned off. The translations are
  machine drafts with an LLM native-review pass, not professional human translation.
- **Language selector and account preference** — a selector at the foot of the sidebar lets
  a pilot pick their interface language. The choice is written to the `forca_language`
  cookie, so an anonymous visitor's pick survives; a signed-in pilot's is also stored on
  their account (`identity.User.language`). For a signed-in pilot the active language is
  resolved from the account preference, then the cookie, then `Accept-Language`, then the
  configured default.
- **Localisation policy console** — a Director-only page at `/ops/admin/i18n/` controls
  which locales the selector offers, the default locale, whether the browser's
  `Accept-Language` header is honoured, and whether anonymous visitors may choose a
  language. It ships with English only enabled, so nothing user-visible changes until a
  Director turns a locale on. Browser detection is on by default, so enabling a locale
  immediately serves it to every pilot whose browser asks for it.
- **Per-reader notification text** — notifications and other database-backed prose are no
  longer stored as translated text. They are persisted as a message key plus its parameters
  and rendered in the reader's own language at display time; a group broadcast with no
  single recipient uses the configured broadcast locale.
- **`I18N_ENABLED`** — an environment kill switch, default on. Turning it off
  short-circuits locale resolution to English and hides the selector.

## [1.0.0]

The first public release of [FORCA] Command Grid — a free, self-hostable operations hub for
an EVE Online corporation.

### Included

- **Authentication & identity** — EVE Single Sign-On (OAuth2 authorization-code + PKCE),
  character linking with ownership-change protection, encrypted token storage, and a
  role-based access control system with lateral capabilities and dual-control Director
  grants.
- **Community & intel** — killboard with valuation and rankings, combat ranks, Hall of
  Fame, knowledge base, new-player onboarding, mentorship programme, and raffle contests.
- **Ships & doctrines** — doctrine library, per-pilot readiness engine, Shipyard, and skill
  plans.
- **Fleet & combat** — operations planner (RSVP, sign-ups, attendance), intel/watchlists,
  standings board, and structure monitoring.
- **Navigation** — route, jump, and range planners with region maps.
- **Industry & economy** — the Industry Center (BOM, invention, chains, jobs), ERP build
  jobs, market intelligence, stockpile and asset mirrors, mining ledger and payouts,
  planetary industry planner, corp finance, and corp contracts.
- **Member services** — freight, buyback, and corp store, each with configurable audiences.
- **Pilot tools** — the Command Center dashboard, contribution ledger, SRP, tasks, and a
  daily briefing.
- **Command & readiness** — the readiness platform, LLM-backed Command Intelligence,
  explainable recommendations, and the Pingboard alerting and calendar system.
- **Operations** — a containerised Docker Compose stack (nginx, gunicorn, Celery worker and
  beat, PostgreSQL, Redis), an idempotent provisioning script for Ubuntu, and a `Makefile`
  plus `scripts/` operator command surface.
- **Documentation** — a complete handbook set for end users, administrators, contributors,
  and operators, plus reference material.

[1.0.0]: https://github.com/ebrandi/forca-command-grid/releases/tag/v1.0.0
