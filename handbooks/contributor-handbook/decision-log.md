# Design decision log

Source comments across the codebase cite short markers — `ADR-0001`, `design doc 06 §4`,
`P7`, `research/03`. Those referred to a private design corpus that predates the public
release and was never published. This page is the public replacement: it records what each
marker means so a reference in a comment resolves to something you can read.

Nothing here is new policy. Each entry is reconstructed from the decision as it is actually
implemented, with the code that enforces it. Where a comment cites a marker not listed
below (`design doc 07`, `doc 14 §3`, `P7`, `research/03`), read it as "a design note that
was not published"; the behaviour it describes is in the cited module.

## Architecture decisions

### ADR-0001 — Apps own their own configuration; no cross-app coupling

Each Django app under `apps/` keeps its own configuration namespace and its own sender
config rather than reaching into another app's settings. Integrations that are not core to
the product are *optional enrichment* and must degrade cleanly when absent.

- `apps/pingboard/compat.py` — pingboard keeps its own sender config; only the send
  mechanism is shared.
- `apps/command_intel/notify.py` — Command Intelligence reads its own
  `evemail_sender_character_id`, not readiness's.
- `core/esi/adapters/zkill.py` — zKillboard is optional enrichment; the app works without it.
- `core/audit.py` — the audit log records actions, never the private data they touched.

### ADR-0003 — LLM providers sit behind an adapter

A provider is added by writing an adapter, without touching the client or the prompts.
See `apps/command_intel/llm/adapters/`.

### ADR-0006 — Soft links across app boundaries, never cross-app foreign keys

Apps reference each other's rows by id, not by `ForeignKey`. This keeps app deletion and
migration independent. See `apps/command_intel/models.py` (soft-link to
`killboard.BattleReport`) and the shared programmatic task factory in
`apps/tasks/services.py`.

### ADR-0007 — Report classification drives access control

Command Intelligence reports carry a classification, and access is decided from it rather
than from the requesting view. See `apps/command_intel/access.py`.

### ADR-0008 — No LLM call in a web request

Every LLM call runs in a Celery worker. A web request creates a `pending` row and returns;
the UI polls until the worker fills it in. This bounds request latency and keeps a slow or
hanging provider from consuming gunicorn threads.

- `apps/command_intel/tasks.py`, `apps/command_intel/ask.py`,
  `apps/command_intel/battle_analysis.py`, `apps/mentorship/tasks.py`.
- The pattern is visible in `templates/command_intel/ask.html`.

### ADR-0009 — LLM output is schema-validated and entity-grounded

Model output is parsed against a schema and its entity references are checked against real
rows before anything is stored or displayed. See `apps/command_intel/llm/schema.py`.

## Localisation decisions

The localisation code cites a *second* marker series — `D5`, `D13`, `D14`, `D17`, and
`docs/i18n/adr/ADR-0003`. It is numbered inside the i18n design corpus and is unrelated to
the `ADR-000n` series above: the i18n corpus has its own `ADR-0003`, which has nothing to do
with LLM adapters. The `D<n>` markers are numbered per design document, so a `D5` in a module
that has nothing to do with language (`apps/killboard/views.py`, htmx fragments) belongs to a
different document again. Read a `D<n>` in the context of the module that cites it.

### i18n `ADR-0003` — A custom locale middleware, and no `i18n_patterns`

The active language is set by `core.i18n.LocaleMiddleware`, not Django's stock
`LocaleMiddleware`. The stock one runs before `request.user` exists, so it cannot honour a
pilot's stored preference or the impersonation swap. Ours is placed immediately after
`apps.impersonation.middleware.ImpersonationMiddleware` and before the membership and feature
gates. URLs are not language-prefixed — `i18n_patterns` is deliberately unused. See the
`MIDDLEWARE` comment in `config/settings/base.py` and `core/i18n/middleware.py`.

### D5–D7 — Locale resolution precedence, and "view-as" renders in the director's language

Authenticated: account preference (`identity.User.language`) → language cookie →
`Accept-Language` → the configured default. Anonymous: the same list without the first step.
Every candidate is validated against the enabled allow-list before it is activated. Under a
director's "view-as" session the page renders in the *director's* language, because resolution
reads `request.real_user` — the human at the browser — not the impersonated pilot (D6). See
`core/i18n/resolver.py`.

### D11 — The JS message catalogue is an external response, never inlined

The JavaScript catalogue is served by Django's `JavaScriptCatalog` view at `/i18n/jsi18n/`
(`core/i18n/urls.py`), not dumped into a `<script>` block in a template. The script CSP is
nonce-based with no `'unsafe-inline'` (`core/middleware.py`), and an external catalogue keeps
it that way.

### D13 — EVE game-data proper nouns are never translated in a `.po`

Ship, module, skill, system and region names come from the SDE, keyed by EVE id. Where CCP
publishes an official localisation it is applied at the SDE display seam
(`apps/sde/templatetags/eve.py`); it is never authored into a catalogue, and there is no
per-locale exception path for it. The rule and its leak-detector term list live in
`core/i18n/data/protected-terms.yml`.

### D14 — Persisted notification prose is re-rendered per recipient

A translated string must never be written to the database: `.save()` coerces a lazy
translation proxy to `str` and freezes the writer's locale (usually a Celery worker, which has
none). So an alert is stored as a message key plus its context and resolved at delivery time,
in each recipient's own language (`apps/pingboard/dispatch.py` buckets recipients by
language). A group message has no one recipient, so it renders in the single configured
`broadcast_locale` (`core/i18n/config.py`). The translatable scaffolds are the
`apps/*/messages.py` modules — see `apps/pingboard/messages.py` for the contract. Corp-authored
template bodies and an officer's free text are delivered verbatim in every locale.

### D16 — Protected terms are linted, not trusted

Agreed keep-English jargon (`FC`, `cyno`, `logi`, `doctrine`, `killmail`, `SRP`, …) and
game-data names must survive translation. `core/i18n/terminology.py` loads the rule data and
`tests/test_i18n_terminology.py` scans every shipped `locale/*/LC_MESSAGES/django.po`, so a
translation that renders a protected term into the target language fails the build. A jargon
term can be released for a single locale by recording an approved exception against it in
`core/i18n/data/protected-terms.yml`; none is recorded today. Game-data names have no
exception path at all (D13).

### D17 — Language-scoped cache keys

Any cache whose payload embeds translated prose folds the active language into its key, via
`i18n_cache_key` in `core/i18n/cache.py`, so a value rendered in one language is never served
to a reader in another. `apps/doctrines/services.py` is an example. Language-neutral caches
(ids, role booleans) do not do this.

### D18 — A locale that arrives on the wire is untrusted input

`set_language` re-derives the code from the enabled allow-list rather than echoing the posted
string back into a `Set-Cookie`, and redirects only to a same-origin `next` through the shared
`core.redirects.safe_next` guard (`core/i18n/views.py`). The same rule holds off-request: a
stored `User.language` is validated against the allow-list before it is activated, so a raw
locale value never reaches the filesystem (`apps/pingboard/dispatch.py`).

## Tocha's Lab (ship fitting)

### TL1 — An independent server-side fitting engine, not an upstream WASM dependency

The Tocha's Lab calculation engine (`apps/fitting/engine`) is an original Python
implementation derived from publicly documented EVE mechanics, sourcing dogma data from the
CCP SDE we already import. The EVEShipFit organisation was evaluated in full (all repos MIT,
but Rust→WASM/browser-first, token-gated data, no in-repo tests; EVE data is CCP-owned, not
MIT) and **not adopted** — it is retained only as an optional black-box validation oracle.
Provenance and attribution are in [`THIRD_PARTY_NOTICES.md`](../../THIRD_PARTY_NOTICES.md);
the full analysis is in `docs/architecture/decisions/tochas-lab-fitting-engine.md`.

### TL2 — Derived telemetry is never authoritative state

A saved fit stores only its immutable `FitRevision` content plus the engine + data versions.
Every number is recomputed from (revision + skill profile + operating/damage profile +
engine/data version), so a historical fit never silently changes and a data refresh is safe.
The engine is reached only through the `FittingEngine` adapter boundary; nothing else calls
the evaluator, so the implementation can be replaced without touching the feature.

## Where the old documents went

| Comment reference | Read instead |
| --- | --- |
| `SYSTEM_DESIGN.md` | [architecture.md](architecture.md) |
| `DATA_MODEL.md` | [domain-model.md](domain-model.md) |
| `ESI_INTEGRATION.md` | [esi-integration.md](esi-integration.md) |
| `SECURITY_AND_PRIVACY.md` | [security-guidelines.md](security-guidelines.md), [data-and-privacy.md](../data-and-privacy.md) |
| `docs/SECURITY_RESIDUAL_RISKS.md` | [security-guidelines.md](security-guidelines.md) |
| `docs/performance/DATABASE_INDEX_REVIEW.md` | [database.md](../reference/database.md) |
| `docs/pingboard/*`, `docs/notifications/*` | [background-jobs.md](../reference/background-jobs.md), [console-overview.md](../administrator-handbook/console-overview.md) |
| `docs/i18n/03-decisions.md`, `docs/i18n/adr/*` | [Localisation decisions](#localisation-decisions) above |
| `docs/i18n/design/*` | [Localisation decisions](#localisation-decisions) above, [architecture.md](architecture.md), [testing.md](testing.md) |
| `docs/i18n/glossary/README.md` | `core/i18n/data/protected-terms.yml` (the committed rule data), [testing.md](testing.md) |
