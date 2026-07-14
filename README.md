# [FORCA] Command Grid

> A free, self-hostable **operations hub for an EVE Online corporation** — where combat
> losses, doctrine needs, member skills, market gaps, and industry capacity become clear
> actions.

[FORCA] Command Grid connects a corporation's killboard, doctrines, skills, industry,
market stocking, logistics, and new-player onboarding into one application, and ends every
screen with a clear answer to *"what should I — or we — do next?"* for a specific role. It
is **not** just another killboard; the killboard is one module among many, and the value
comes from the way the modules feed each other.

## Table of contents

- [Who it is for](#who-it-is-for)
- [What it does](#what-it-does)
- [See it live](#see-it-live)
- [Quick start (local development)](#quick-start-local-development)
- [Production deployment](#production-deployment)
- [What you must provide](#what-you-must-provide)
- [Configuration](#configuration)
- [Documentation](#documentation)
- [Contributing](#contributing)
- [Security](#security)
- [Licence](#licence)
- [Third-party acknowledgements](#third-party-acknowledgements)
- [EVE Online / CCP trademark notice](#eve-online--ccp-trademark-notice)
- [AI-assisted development](#ai-assisted-development)

## Who it is for

- **Corporation members and newbros** — a single home dashboard, doctrine readiness, skill
  plans, personal combat analytics, onboarding, mentorship, and member services.
- **Corporation leadership** — fleet operations, a readiness platform, corp finance and
  structures, explainable recommendations, strategic Command Intelligence, and a role-gated
  admin console with an audit log.
- **Contributors** — a well-structured Django codebase with one app per bounded context.
- **IT operators** — a containerised stack deployable on a single server with Docker
  Compose.

## What it does

Major implemented feature areas:

- **Community & intel** — Killboard, combat ranks, Hall of Fame, knowledge base,
  onboarding, mentorship, raffles.
- **Ships & doctrines** — Doctrine library, per-pilot readiness, Shipyard, skill plans.
- **Fleet & combat** — Operations planner (RSVP, sign-ups, attendance), intel/watchlists,
  standings, structures.
- **Navigation** — Route/jump/range planners and region maps.
- **Industry & economy** — Industry Center (BOM, invention, chains, jobs), market
  intelligence, stockpile and asset mirrors, mining, planetary industry, corp finance.
- **Member services** — Freight, buyback, and corp store, each with configurable audiences.
- **Pilot tools** — The Command Center dashboard, contribution ledger, SRP, tasks, daily
  briefing.
- **Command & readiness** — The readiness platform, LLM-backed Command Intelligence,
  recommendations, and the Pingboard alerting/calendar system.

For the complete, implementation-grounded list, see the
[Feature Catalog](./handbooks/feature-catalog.md).

The interface is localised into nine languages: English (canonical, and never disabled),
Portuguese (Brazil), Spanish, French, Russian, German, Simplified Chinese, Korean, and
Japanese. A fresh install ships with English only enabled; leadership turns individual
locales on from the admin console at `/ops/admin/i18n/`, which also shows per-locale
translation coverage. The non-English catalogues are machine drafts with an LLM
native-review pass, not professionally human-reviewed translations.

## See it live

A live instance is running at **[forca.club](https://forca.club/)**. Sign in with EVE
Single Sign-On to explore the app in action.

## Quick start (local development)

The project runs entirely in Docker, keeping your host clean. You need Docker Engine with
the Compose plugin.

```bash
make dev                             # web + worker + beat + postgres + redis (autoreload)
make bootstrap-sample                # tiny bundled sample SDE fixture (dev/CI only)
docker compose exec web python manage.py seed_demo       # roles, home corp, a demo doctrine
docker compose exec web python manage.py createsuperuser
# open http://127.0.0.1:8000/   (role-gated console at /ops/)
```

Run the test suite and linter in Docker:

```bash
docker compose run --rm -e DJANGO_SETTINGS_MODULE=config.settings.test web pytest
docker compose run --rm web ruff check .
```

`make help` lists every target. See the
[Contributor Handbook](./handbooks/contributor-handbook/local-development.md) for details,
including running against real EVE SSO/ESI.

## Production deployment

The production stack (nginx + gunicorn + Celery worker/beat + PostgreSQL + Redis) runs via
Docker Compose. A one-shot, idempotent provisioning script is provided for a fresh Ubuntu
server, and a `Makefile` plus `scripts/` give a discoverable operator command surface.

Start with the **[Operator Handbook](./handbooks/operator-handbook/README.md)**, in
particular:

- [Requirements](./handbooks/operator-handbook/requirements.md)
- [Deployment](./handbooks/operator-handbook/deployment.md)
- [Backup and restore](./handbooks/operator-handbook/backup-and-restore.md)
- [Upgrades](./handbooks/operator-handbook/upgrades.md)

## What you must provide

This project ships **no credentials of any kind**. Nothing here is tied to the author's
corporation or infrastructure — you register your own applications and hold your own keys.

**Required** to run the application at all:

| What | Where you get it |
|---|---|
| An **EVE SSO application** (client id + secret) | [developers.eveonline.com](https://developers.eveonline.com) — set its `redirect_uri` to `https://<your-domain>/auth/eve/callback/` |
| A **real contact email** for the ESI `User-Agent` | Yours. CCP's ESI policy requires a contactable address, and the app refuses to boot in production with a placeholder. |
| Your **home corporation id** | Any EVE tool, e.g. zKillboard |
| A domain with **TLS** | Your registrar; the deploy script obtains a Let's Encrypt certificate |

The deploy script generates `DJANGO_SECRET_KEY`, `TOKEN_ENCRYPTION_KEY`,
`POSTGRES_PASSWORD` and `REDIS_PASSWORD` for you.

**Optional** — each unlocks one subsystem and is inert if left unset:

| Integration | What you supply |
|---|---|
| Recruitment vetting | A **second** EVE SSO application with its own non-login callback |
| Discord role sync | A bot token + a Discord OAuth application |
| Pingboard alerts | Slack bot token, Telegram bot token, or WhatsApp (Meta or Twilio) credentials |
| Command Intelligence (AI) | An LLM API key and base URL (any OpenAI-compatible provider) |
| Email briefings | SMTP host and credentials |

## Configuration

Configuration is by environment variables (see [`.env.example`](./.env.example) for the
fully commented template) plus leadership-tunable settings edited in the admin console
without a redeploy. Production refuses to boot unless `DJANGO_SECRET_KEY`,
`TOKEN_ENCRYPTION_KEY`, `DJANGO_ALLOWED_HOSTS` and `DATABASE_URL` are all set — it reports
every missing one at once. `I18N_ENABLED` is the env-level kill switch for localisation and
defaults on; setting it to `0` forces the whole interface back to English and hides the
language selector.

Full details: [Configuration Reference](./handbooks/configuration-reference.md) and
[Environment Variables](./handbooks/reference/environment-variables.md).

## Documentation

All documentation lives under [`handbooks/`](./handbooks/README.md). Start at the
[documentation landing page](./handbooks/README.md), which routes you by audience:

- [End-User Guide](./handbooks/end-user-guide/README.md) — pilots and newbros
- [Administrator Handbook](./handbooks/administrator-handbook/README.md) — corp leadership
- [Contributor Handbook](./handbooks/contributor-handbook/README.md) — developers
- [Operator Handbook](./handbooks/operator-handbook/README.md) — IT operators

## Contributing

Contributions are welcome. See [`CONTRIBUTING.md`](./CONTRIBUTING.md) and the
[Contributor Handbook](./handbooks/contributor-handbook/README.md), and please follow the
[`CODE_OF_CONDUCT.md`](./CODE_OF_CONDUCT.md).

## Security

Please report vulnerabilities privately as described in [`SECURITY.md`](./SECURITY.md). Do
not open public issues for security problems, and never include secrets in a report.

## Licence

This project is released under the **MIT License**. See [`LICENSE`](./LICENSE) for the full
text.

## Changelog

Release history is recorded in [`CHANGELOG.md`](./CHANGELOG.md). This is the version 1.0
release line.

## Third-party acknowledgements

Command Grid builds on open-source software and integrates with EVE Online's official
services and community data sources. See [`ACKNOWLEDGEMENTS.md`](./ACKNOWLEDGEMENTS.md),
[`NOTICE.md`](./NOTICE.md), and
[Third-Party Services](./handbooks/third-party-services.md).

## EVE Online / CCP trademark notice

EVE Online and the EVE logo are the registered trademarks of CCP hf. All rights reserved
worldwide. All artwork, screenshots, characters, world facts, and other recognizable
features of the intellectual property relating to EVE Online are the property of CCP hf.

[FORCA] Command Grid is a non-commercial EVE Online fan project and third-party application.
It is **not** affiliated with, sponsored by, or endorsed by CCP hf. Game data and images
are provided by CCP hf. via ESI, EVE SSO, the SDE, and the official image service, and
remain the property of CCP hf.

## AI-assisted development

[FORCA] Command Grid development was assisted by AI coding tools, including Claude Code
using Anthropic models such as Opus and Sonnet, and OpenCode using models including GLM,
MiniMax, Qwen, and Kimi. All code, documentation, architecture, security, and release
decisions remain the responsibility of the project maintainers.
