# [FORCA] Command Grid Documentation

Welcome to the documentation for **[FORCA] Command Grid**, a free, self-hostable
**operations hub for an EVE Online corporation**. This is the version 1.0 documentation
set and describes the current product.

## What it is

Command Grid connects a corporation's combat record, doctrines, member skills, industry,
market activity, logistics, and new-player onboarding into one application — and ends every
screen with a clear "what should I or we do next?" for a specific role. For a fuller
introduction, read the [Product Overview](./product-overview.md).

## Find your guide

Documentation is organised by audience. Start with the guide that matches your role.

| I am a… | Start here |
|---|---|
| **Corp member, pilot, or newbro** | [End-User Guide](./end-user-guide/README.md) |
| **CEO, director, officer, FC, recruiter, or mentor** | [Administrator Handbook](./administrator-handbook/README.md) |
| **Developer, architect, or reviewer** | [Contributor Handbook](./contributor-handbook/README.md) |
| **Sysadmin or DevOps engineer** | [Operator Handbook](./operator-handbook/README.md) |

## Reference and cross-cutting topics

| Topic | Page |
|---|---|
| Every implemented feature | [Feature Catalog](./feature-catalog.md) |
| Configuration and environment variables | [Configuration Reference](./configuration-reference.md) |
| Roles, permissions, and ESI scopes | [Permissions and Roles](./permissions-and-roles.md) |
| What data is stored and how it is protected | [Data and Privacy](./data-and-privacy.md) |
| External services and integrations | [Third-Party Services](./third-party-services.md) |
| Languages and localisation | [Configuration Reference](./configuration-reference.md#localisation) |
| Terminology | [Glossary](./glossary.md) |

### Detailed reference

- [Environment variables](./reference/environment-variables.md)
- [Database](./reference/database.md)
- [Background jobs](./reference/background-jobs.md)
- [API endpoints](./reference/api-endpoints.md)
- [CLI and scripts](./reference/cli-and-scripts.md)
- [Dependency inventory](./reference/dependency-inventory.md)
- [Licence review](./reference/licence-review.md)
- [Documentation maintenance](./reference/documentation-maintenance.md)

## Feature overview

Command Grid's implemented feature areas, grouped as in the application's console:

- **Community & intel** — Killboard, combat ranks, Hall of Fame, knowledge base,
  onboarding, mentorship, raffles.
- **Ships & doctrines** — Doctrine library, readiness, Shipyard, skill plans.
- **Fleet & combat** — Operations planner, intel/watchlists, standings, structures.
- **Navigation** — Route/jump/range planners and maps.
- **Industry & economy** — Industry Center, ERP build jobs, market, stockpile/assets,
  mining, planetary industry, corp finance, corp contracts.
- **Member services** — Freight, buyback, corp store (each audience-controlled).
- **Pilot tools** — Command Center dashboard, contribution ledger, SRP, tasks, daily
  briefing, pilot intelligence.
- **Command & readiness** — Readiness platform, Command Intelligence, recommendations,
  Pingboard.
- **Leadership & platform** — Recruitment, corporation data, EVE SSO/account, admin
  console/audit, localisation policy, comms-access sync, SDE reference data.

See the [Feature Catalog](./feature-catalog.md) for the complete, detailed list.

## Deployment and contribution

- **Deploy and operate:** [Operator Handbook](./operator-handbook/README.md).
- **Contribute code:** [Contributor Handbook](./contributor-handbook/README.md) and
  [`CONTRIBUTING.md`](../CONTRIBUTING.md).
- **Report a vulnerability:** [`SECURITY.md`](../SECURITY.md).

## Version 1.0 documentation scope

This documentation describes [FORCA] Command Grid as it exists today. Every feature page is
grounded in the current implementation. It does not describe development history or
unreleased work.

## Keeping documentation up to date

Documentation is part of the product. When behaviour changes, update the relevant page in
the same pull request. The maintenance conventions are described in
[reference/documentation-maintenance.md](./reference/documentation-maintenance.md), and the
contribution expectations are in [`CONTRIBUTING.md`](../CONTRIBUTING.md).

## EVE Online / CCP notice

EVE Online and the EVE logo are the registered trademarks of CCP hf. All rights reserved
worldwide. This is a non-commercial fan project and is not affiliated with or endorsed by
CCP hf. See [`NOTICE.md`](../NOTICE.md) and [`README.md`](../README.md).
