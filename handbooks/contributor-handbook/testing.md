# Testing

## Table of contents

- [Running the suite](#running-the-suite)
- [Test layout](#test-layout)
- [Fixtures and test data](#fixtures-and-test-data)
- [Mocking ESI](#mocking-esi)
- [Celery in tests](#celery-in-tests)
- [Coverage](#coverage)
- [Linting](#linting)
- [Gotchas](#gotchas)

## Running the suite

The suite runs against a real PostgreSQL instance in Docker (there is no SQLite
fallback):

```bash
docker compose run --rm -e DJANGO_SETTINGS_MODULE=config.settings.test web pytest
```

Django settings resolve to `config.settings.test` for this run (also pinned as the
default in `pyproject.toml`'s `[tool.pytest.ini_options]`, so plain `pytest` inside the
container works too). Common variations:

```bash
# A single module
docker compose run --rm -e DJANGO_SETTINGS_MODULE=config.settings.test web pytest tests/test_killboard.py

# A single test
docker compose run --rm -e DJANGO_SETTINGS_MODULE=config.settings.test web pytest tests/test_rbac.py::test_officer_role_required -q

# Keep the LLM subsystem disabled even if your local .env sets a real key (see Gotchas below)
docker compose run --rm -e DJANGO_SETTINGS_MODULE=config.settings.test -e LLM_API_KEY= web pytest
```

`pytest-django` drives Django test-database setup/teardown; `addopts = "-ra -q"` in
`pyproject.toml` keeps output terse while still summarising failures/skips.

## Test layout

```
tests/                # the primary suite: ~270 test_*.py modules, one conftest.py
apps/*/tests.py       # per-app tests, where present (testpaths includes "apps")
core/                 # core/ is also a configured test path
```

`pyproject.toml`'s `[tool.pytest.ini_options]` sets:

```toml
DJANGO_SETTINGS_MODULE = "config.settings.test"
python_files = ["test_*.py", "tests.py"]
testpaths = ["tests", "apps", "core"]
```

so `pytest` alone (no path argument) discovers everything across `tests/`, every app,
and `core/`. Most of the suite lives in the top-level `tests/` directory, organised
one module per feature area (for example `test_killboard.py`,
`test_readiness_engine.py`, `test_esi_client.py`, `test_rbac.py`) rather than mirrored
1:1 against `apps/`.

## Fixtures and test data

Shared fixtures live in `tests/conftest.py`:

- **`_clear_cache`** (autouse) — clears the Django cache and several process-local
  memoisation caches (SDE name lookups, price snapshots, recipe/BOM cache) before and
  after every test, so a cached result from one test never leaks into another.
- **`sde`** — loads the bundled sample SDE fixture (`call_command("load_sde",
  sde_version="test")`) into the test database.
- **`priced_sde`** — builds on `sde` and seeds a `MarketPrice` (Jita sell) for every
  SDE type so cost/build-vs-buy/valuation tests have a real market signal to price
  against (killmail/industry valuation deliberately never falls back to the SDE's
  `base_price` — see [../killmail-valuation notes referenced from feature-catalog.md](../feature-catalog.md)).
- **`user`** / **`character`** — a bare `User` (unusable password — there is no
  Django-password login path) and a linked `EveCharacter` marked as the main, corp
  member character.

Beyond these, individual test modules construct their own model instances directly
via the Django ORM (`Model.objects.create(...)`), rather than through factory classes.
`factory-boy` is listed in `requirements-dev.txt` but is not currently used by any test
module — treat `tests/conftest.py` fixtures and plain ORM object creation as the
established pattern when writing new tests.

## Mocking ESI

The `responses` library (`responses>=0.25,<0.26`) mocks outbound HTTP to CCP's ESI and
EVE SSO endpoints; roughly two dozen test modules use it (grep for `@responses.activate`
or `import responses`). Because `core/esi/client.py` and `core/esi/oauth.py` are the
only code paths that make these HTTP calls, mocking happens at that boundary rather
than by stubbing higher-level service functions — write new ESI-touching tests the same
way, registering the exact ESI path(s) your code under test will hit.

## Celery in tests

`config.settings.test` sets `CELERY_TASK_ALWAYS_EAGER = True` and
`CELERY_TASK_EAGER_PROPAGATES = True`, so calling a Celery task's `.delay()` (or
calling it directly) executes synchronously in-process during a test, and an exception
inside the task propagates to the test instead of being swallowed by the broker
machinery. This is why task logic can be exercised with an ordinary pytest test rather
than a running worker.

## Coverage

`pytest-cov` is a dev dependency; `[tool.coverage.run]` in `pyproject.toml` omits
migrations, `apps/*/tests.py`/`tests/`, and `config/wsgi.py`/`config/asgi.py` from
coverage accounting. Run with:

```bash
docker compose run --rm -e DJANGO_SETTINGS_MODULE=config.settings.test web \
  pytest --cov=apps --cov=core --cov-report=term-missing
```

## Linting

```bash
docker compose run --rm web ruff check .
docker compose run --rm web ruff format --check .
```

Ruff's configuration (`pyproject.toml`) targets `py312`, a 120-character line length,
and the rule sets `E` (pycodestyle errors), `F` (pyflakes), `I` (import sorting), `UP`
(pyupgrade), `B` (bugbear), `DJ` (Django-specific), and `S` (bandit security rules).
`migrations/` is excluded from linting entirely; `S101` (assert usage) is ignored
project-wide since tests use plain `assert`, and `DJ008` (missing `__str__`) is ignored
since not every model — particularly join/intermediate tables — needs one. Test files
additionally ignore `S105`/`S106` (hardcoded password/secret string checks), since test
fixtures legitimately hardcode dummy credentials.

## Gotchas

- **Rely on the pytest exit code, not the summary line, when running under a wrapper
  script or CI step that captures output** — some invocation paths (piping through
  another tool, background runs) can truncate the final summary line while the process
  exit code remains authoritative.
- **`LLM_API_KEY` leaking from a local `.env` into test runs.** If your development
  `.env` has a real `LLM_API_KEY` set (for exercising `apps.command_intel` manually),
  it will also be picked up by `config.settings.base` during a test run and flip
  `COMMAND_INTEL_ENABLED` to `True`, changing behaviour the Command Intelligence tests
  assume is disabled. Pass `-e LLM_API_KEY=` explicitly to the `docker compose run`
  invocation to force it back off for a deterministic test run.
- **Do not run `pytest` in two places against the same database at once** — for
  example, running the suite locally while a background agent/CI job is also running
  it. Both share the same Docker Postgres test database name and will corrupt each
  other's fixtures/transactions.
- Tests that need the SDE must request the `sde` (or `priced_sde`) fixture explicitly;
  it is not loaded automatically for every test, since most tests don't need it and
  loading it has a real cost.
