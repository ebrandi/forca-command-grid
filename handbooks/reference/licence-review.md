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
- [EVE image server usage review (2026-07-22)](#eve-image-server-usage-review-2026-07-22)
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

## EVE image server usage review (2026-07-22)

The **Combat Signatures** feature composites EVE character portraits and corporation/alliance
logos into pilot-authored banner images. This is a review of that usage against CCP's current
terms, recorded here as a maintainer aid (not a legal opinion). It is **not** a paraphrase of
the licence — only a record of what was checked and the good-faith conclusion.

- **Reviewed:** 2026-07-22. Both source pages were **reachable** at review time.
- **Sources consulted:**
  - EVE Developer License Agreement — <https://developers.eveonline.com/license-agreement>
  - EVE image server documentation — <https://docs.esi.evetech.net/docs/image_server.html>
- **How the feature uses the imagery.** Portraits and logos are fetched **server-side** from
  the official image server (`images.evetech.net`) by the Celery worker only — never during a
  public request — cached on the local media mirror (`characters/<id>/portrait-256.jpg`,
  `corporations|alliances/<id>/logo-128.png`, refetched weekly), and composited into
  banners the requesting pilot assembled from their own data. The banner backgrounds are
  wholly original, project-owned procedural art containing no CCP artwork (see
  [signature-backgrounds.md](./signature-backgrounds.md)). No banner claims CCP endorsement,
  and no CCP mark is combined with a third-party mark.
- **What the sources say (as read on the review date).** The image-server documentation
  states the image service is intended to be used directly as a CDN and that self-caching is
  **optional, not prohibited** ("You do not need to cache the images and serve them
  yourself"). The Developer License Agreement permits use and display of Game Data within a
  **non-commercial** Application for its stated purpose, requires displaying CCP's proprietary
  notice, and prohibits representing the application as CCP or otherwise implying endorsement.
- **Good-faith conclusion.** The Combat Signatures usage is **consistent with these terms**
  and with how the application already uses the official image server elsewhere (the eveimg
  type-image mirror and killboard portraits). Fetching official image-server portraits/logos,
  caching them on disk, and compositing them into pilot-requested banners over
  project-owned backgrounds, with no endorsement claim, falls within the permitted use.
- **Caveats / re-verify before a public or commercial release:**
  - The deployment must remain **non-commercial** — the licence forbids charging for or
    monetising the application. This is an operator obligation already noted project-wide.
  - The deployment must display the required **fan-site disclaimer / CCP proprietary notice**
    (already a project requirement — see [`NOTICE.md`](../../NOTICE.md) and the trademark
    notice in [`README.md`](../../README.md)).
  - The licence-agreement page showed **no effective/last-updated date** at review time, and
    the image-server documentation page notes the ESI docs have relocated to a new developer
    portal (the legacy page remains accessible). Re-verify both against their authoritative
    current locations before relying on this review in a regulated or commercial context.

## EVE Online / CCP content

EVE Online game data, imagery, names, and trademarks are the property of CCP hf. and are
used under CCP's third-party developer framework. They are **not** licensed to this project
under the MIT License and are not covered by the dependency licences above. See
[`NOTICE.md`](../../NOTICE.md) and the trademark notice in [`README.md`](../../README.md).
Operators of a public deployment are responsible for displaying the required fan-site
disclaimer and complying with CCP's current policies.
