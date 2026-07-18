# Licence Review

This page is a **maintainer aid**, not a legal opinion. It summarises the licences of the
project's dependencies as collected from public package metadata, and flags items that a
maintainer should confirm before a public or commercial release. For the acknowledgements
file see [`NOTICE.md`](../../NOTICE.md); for the full list see
[dependency-inventory.md](./dependency-inventory.md).

> **Disclaimer.** Licence identifiers here are best-effort and may be incomplete or out of
> date. This is not legal advice. Verify each dependency against its authoritative source
> before relying on it in a regulated or commercial context.

## Table of contents

- [Project licence](#project-licence)
- [Licence summary](#licence-summary)
- [Notes on specific dependencies](#notes-on-specific-dependencies)
- [Items requiring maintainer review](#items-requiring-maintainer-review)
- [EVE Online / CCP content](#eve-online--ccp-content)

## Project licence

[FORCA] Command Grid is released under the **MIT License** ([`LICENSE`](../../LICENSE)).

## Licence summary

The Python and frontend dependencies are predominantly under permissive licences
(MIT, BSD, Apache-2.0), which are generally compatible with an MIT-licensed project. A few
carry weak-copyleft or specific terms that are worth noting:

| Licence | Type | Dependencies (examples) | Note |
|---|---|---|---|
| MIT | Permissive | redis, django-environ, gunicorn, whitenoise, PyJWT, urllib3, pytest, ruff, Alpine.js, Chart.js, Tailwind | Compatible |
| BSD-2/3-Clause | Permissive | Django, DRF, drf-spectacular, celery, pytest-django, svg-pan-zoom | Compatible |
| Apache-2.0 | Permissive (patent grant) | requests, responses, pip-audit, cryptography (dual) | Compatible |
| PSF-2.0 | Permissive | defusedxml, CPython | Compatible |
| MPL-2.0 | Weak copyleft (file-level) | certifi | Used unmodified as a dependency |
| LGPL-3.0-or-later | Weak copyleft | psycopg | Used unmodified as a shared library dependency |
| PostgreSQL License | Permissive | PostgreSQL image | Compatible |

## Notes on specific dependencies

- **psycopg (LGPL-3.0-or-later):** used as an unmodified database driver dependency, not
  statically linked into or modified within the project. This is the normal usage pattern
  for LGPL libraries. Confirm your distribution model if you redistribute a modified build.
- **certifi (MPL-2.0):** MPL is file-level copyleft; certifi is consumed unmodified as a CA
  bundle. No project source is derived from it.
- **cryptography:** dual-licensed Apache-2.0 OR BSD-3-Clause — either applies.
- **EVEShipFit (evaluated, not adopted):** for the Tocha's Lab fitting engine the EVEShipFit
  projects (`dogma-engine`, `data`, `react`, `sde`, `eveship.fit`, `preview-renderer`,
  `static-data-viewer`, `demonstrator`) were evaluated. All are **MIT** (the EVE *data* they
  process is CCP-owned under the CCP Developer Licence). None is a build or runtime dependency
  and no code, tests, fixtures or data were copied — an independent server-side engine was
  built instead. This imposes no MIT notice obligation on the shipped product; the attribution
  is recorded transparently in [`THIRD_PARTY_NOTICES.md`](../../THIRD_PARTY_NOTICES.md) and the
  decision in `docs/architecture/decisions/tochas-lab-fitting-engine.md`.

## Items requiring maintainer review

*Maintainer review required* for the following before public release:

- **htmx licence identifier** — confirm the exact licence of the pinned htmx version
  against its distribution (metadata has historically shown both BSD-2-Clause and MIT for
  different releases).
- **`polib` licence identifier** — dev/test dependency (`requirements-dev.txt`), used by the
  catalogue freshness and terminology checks. Confirm its licence against the package
  metadata and add it to the summary table above.
- **Container base image bundled packages** — the `-alpine` and `-slim` images bundle many
  OS packages under their own licences. If you redistribute the built images, review the
  aggregate licences of the bundled OS packages.
- **`gettext` in the application image** — the `Dockerfile` apt-installs `gettext` so
  `compilemessages` can compile the message catalogues at build time. The image is a single
  stage, so those binaries ship inside it rather than staying behind in a builder stage. It
  is used unmodified and is not linked into the application, but confirm the licence of the
  Debian package before you redistribute the built image.
- **Exact pinned versions** — regenerate the inventory against the lock files
  (`frontend/package-lock.json` and the resolved Python versions) at release time, since
  transitive dependencies and their licences can change.
- **Community data sources** (Fuzzwork, EveRef, zKillboard) — review each service's current
  terms of use for your deployment; they are community services, not licensed libraries.

## EVE Online / CCP content

EVE Online game data, imagery, names, and trademarks are the property of CCP hf. and are
used under CCP's third-party developer framework. They are **not** licensed to this project
under the MIT License and are not covered by the dependency licences above. See
[`NOTICE.md`](../../NOTICE.md) and the trademark notice in [`README.md`](../../README.md).
Operators of a public deployment are responsible for displaying the required fan-site
disclaimer and complying with CCP's current policies.
