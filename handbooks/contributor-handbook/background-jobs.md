# Background Jobs

## Table of contents

- [The Celery architecture](#the-celery-architecture)
- [How the schedule is defined](#how-the-schedule-is-defined)
- [Adding a new scheduled task](#adding-a-new-scheduled-task)
- [Cadence discipline](#cadence-discipline)
- [Idempotency and inert-until-configured](#idempotency-and-inert-until-configured)
- [Full current schedule](#full-current-schedule)

## The Celery architecture

`config/celery.py` builds the Celery application (`app = Celery("forca")`), configured
from Django settings under the `CELERY_` namespace
(`config/settings/base.py`):

| Setting | Value | Why |
|---|---|---|
| Broker / result backend | `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND`, both defaulting to `REDIS_URL` | One Redis instance backs the cache, the broker, and the result backend in the default deployment. |
| `CELERY_TASK_IGNORE_RESULT` | `True` (global) | No caller ever reads a task's result (no `AsyncResult`/`.get()`/chords/groups/chains), so storing a `celery-task-meta-*` key per execution is pure write/memory churn on the shared request-path Redis. An individual task can opt back in if a caller is ever added that needs its result. |
| `CELERY_TASK_ACKS_LATE` | `True` | A task is acknowledged only after it completes, so a worker crash mid-task causes it to be redelivered rather than silently lost. |
| `CELERY_WORKER_PREFETCH_MULTIPLIER` | `1` | One task fetched at a time per worker process, so a slow task doesn't starve other queued work sitting behind it in that worker's prefetch buffer. |
| `CELERY_TASK_ALWAYS_EAGER` | `False` in dev/prod, `True` in tests | Tests run tasks synchronously in-process (see [testing.md](./testing.md#celery-in-tests)). |

`app.autodiscover_tasks()` finds every app's `tasks.py` automatically (no manual
registration of task modules needed); `app.conf.beat_schedule` is a single dict
defining every periodic job.

## How the schedule is defined

Every entry in `config/celery.py`'s `beat_schedule` dict follows this shape:

```python
"task-key-name": {
    "task": "app_label.task_function_name",
    "schedule": crontab(minute="*/15"),
},
```

The `beat` service (a dedicated container — see
[architecture.md](./architecture.md#web--worker--beat-topology)) reads this dict and
enqueues each task onto the Redis broker at its scheduled time; the `worker` service
consumes and executes it. Nothing here talks to ESI or an LLM directly — the task
function itself does, following the [ESI integration](./esi-integration.md) pattern.

## Adding a new scheduled task

1. **Define the task** in the owning app's `apps/<app>/tasks.py`, typically as a thin
   wrapper that calls into that app's service layer (see
   [architecture.md](./architecture.md#service-and-task-layering)).
2. **Register it** in `config/celery.py`'s `beat_schedule` dict with a **staggered**
   `crontab(...)` — pick a minute/hour offset that doesn't collide with jobs already
   running at the same tick (the existing schedule is full of comments explaining
   *why* a given offset was chosen relative to its neighbours; follow that convention
   for your entry too).
3. **Make it idempotent.** A missed beat tick, a duplicate delivery (possible with
   `acks_late`), or two overlapping runs must never double-apply an effect (double-pay,
   duplicate notification, duplicate ledger row). Prefer a due-table sweep
   (`WHERE due_at <= now()`) over `apply_async(eta=...)` per-item scheduling — it's the
   established idiom here (see e.g. `pingboard.dispatch_due`,
   `raffle.draw_due`) and self-heals after a missed tick rather than losing work.
4. **Ship it inert until configured**, if the feature it powers has an on/off switch
   or requires leadership-provided configuration (a webhook URL, an armed alert rule,
   an ESI scope grant). The task should no-op cheaply (one settings/config read) rather
   than erroring, so it's safe to ship enabled in the schedule from day one. See
   the pattern below.
5. **Document the cadence** in [../reference/background-jobs.md](../reference/background-jobs.md)
   so operators can see the full schedule in one place (`CONTRIBUTING.md` calls this
   out as a required doc update for new background jobs).

## Cadence discipline

Set every cadence **at or above the underlying data's ESI cache TTL** — polling faster
than ESI's own cache refresh wastes error budget for no new data. `core/freshness.py`'s
`THRESHOLDS` dict documents the staleness threshold this project treats as acceptable
per data class (e.g. 10 minutes for killmails, 1 hour for market prices, 24 hours for
market history); use these as a starting point when choosing a new task's schedule.
**Stagger** new jobs off round-number ticks (`:00`, `:15`, `:30`) that are already
crowded, using an offset minute so cache-warming jobs and heavier sync jobs don't
contend for CPU/Postgres/ESI budget at the same instant. The existing schedule is
dense with real examples of both disciplines — skim `config/celery.py` for a
similarly-shaped job before picking a new cadence.

## Idempotency and inert-until-configured

Two closely related conventions run through the entire schedule:

- **Idempotent by construction**: sync tasks use `update_or_create`/upsert patterns
  keyed on the natural id from ESI/zKill, ledger-writing tasks check for an existing
  record before creating a new one, and reconcile tasks compare against
  already-recorded state rather than blindly re-applying. This means a duplicate or
  overlapping run is a no-op, not a duplicate side effect.
- **Inert until configured**: a task that depends on a leadership-provided credential,
  ESI scope grant, or armed feature checks that precondition first and returns/logs
  a no-op if it isn't met, rather than assuming it is. This is what lets every beat
  entry ship active in the schedule for every deployment — including a fresh
  self-hosted install with nothing configured yet — without generating log noise or
  wasted work.

## Full current schedule

The complete, current beat schedule — every task, its cadence, and why that cadence
was chosen — is maintained separately as an operator-facing reference:
[../reference/background-jobs.md](../reference/background-jobs.md). Keep that page in
sync whenever you add, remove, or re-cadence a task.
