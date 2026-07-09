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
