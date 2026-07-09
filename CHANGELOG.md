# Changelog

All notable changes to [FORCA] Command Grid are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
