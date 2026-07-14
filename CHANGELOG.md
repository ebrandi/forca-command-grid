# Changelog

All notable changes to [FORCA] Command Grid are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
