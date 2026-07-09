# Acknowledgements

[FORCA] Command Grid is built on the work of many open-source projects and community
resources. This file offers thanks and points to the detailed attribution. For the full
dependency-by-dependency licence listing, see [`NOTICE.md`](./NOTICE.md) and
[handbooks/reference/dependency-inventory.md](./handbooks/reference/dependency-inventory.md).

## EVE Online and CCP Games

This project is an EVE Online fan project built on CCP's official third-party developer
framework. EVE Online, the EVE logo, the EVE Swagger Interface (ESI), EVE Single Sign-On,
the Static Data Export, and the EVE image service are the property of CCP hf. We are
grateful for the open ecosystem CCP maintains for community developers. This project is not
affiliated with, sponsored by, or endorsed by CCP hf.

## Community data and services

- **Fuzzwork** — Static Data Export conversions and market price data.
- **EveRef** — reference-data and historical archive datasets.
- **zKillboard** — the community killmail feed.

## Open-source software

The application stands on the shoulders of, among others:

- **Django**, **Django REST Framework**, and **Celery** for the backend.
- **PostgreSQL**, **Redis**, **gunicorn**, and **nginx** for the runtime.
- **Alpine.js**, **htmx**, **Chart.js**, **svg-pan-zoom**, and **Tailwind CSS** for the
  front end.
- **cryptography**, **PyJWT**, **requests**, **defusedxml**, **whitenoise**, and the wider
  Python ecosystem.
- **pytest**, **factory-boy**, **responses**, and **ruff** for development and testing.

A complete list, with licences and purposes, is in [`NOTICE.md`](./NOTICE.md). Dependency
licence identifiers there are collected on a best-effort basis; see
[handbooks/reference/licence-review.md](./handbooks/reference/licence-review.md) for the
maintainer review notes.

## AI-assisted development

[FORCA] Command Grid development was assisted by AI coding tools, including Claude Code using
Anthropic models such as Opus and Sonnet, and OpenCode using models including GLM, MiniMax,
Qwen, and Kimi. All code, documentation, architecture, security, and release decisions
remain the responsibility of the project maintainers.
