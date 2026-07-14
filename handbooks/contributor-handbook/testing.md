# Testing

## Table of contents

- [Running the suite](#running-the-suite)
- [Test layout](#test-layout)
- [Fixtures and test data](#fixtures-and-test-data)
- [Mocking ESI](#mocking-esi)
- [Celery in tests](#celery-in-tests)
- [Coverage](#coverage)
- [Linting](#linting)
- [Localisation gates](#localisation-gates)
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

## Localisation gates

The app is localised into nine languages: English, plus eight translated catalogues
committed at `locale/<code>/LC_MESSAGES/django.po`. Three gates guard them, and any PR
that adds a user-visible string will meet at least the first two.

**Catalogue compilation.** `python manage.py compilemessages` runs in CI
(`.github/workflows/ci.yml`, step "Compile message catalogues") and again during the
Docker image build (`Dockerfile`: `RUN DJANGO_SETTINGS_MODULE=config.settings.base python
manage.py compilemessages`). A malformed `.po` is a red build, not a silent fallback to
English.

**Catalogue freshness.** `tests/test_i18n_catalogue_freshness.py` re-runs `django-admin
makemessages` against a scratch copy of the tree and compares the set of
`(msgctxt, msgid)` pairs it extracts against the committed
`locale/de/LC_MESSAGES/django.po` (any locale would do — the msgid set is identical in
all of them). Comparing msgid identities rather than file bytes is what keeps the gate
immune to gettext formatting drift. A string marked in the code but never extracted fails
the suite; the fix is to re-run `makemessages` and commit the updated
`locale/*/LC_MESSAGES/django.po` in the same PR.

**Terminology.** `tests/test_i18n_terminology.py` lints every committed catalogue against
`core/i18n/data/protected-terms.yml`. That file holds two lists: a tripwire sample of EVE
game-data names (`Rifter`, `Jita`, `Damage Control II`), which must appear verbatim in the
translation, and 41 pieces of community jargon (`FC`, `cyno`, `logi`, `killmail`, `SRP`,
`doctrine` and so on) that stay English unless a per-locale exception has been approved in
that same file — none has been so far. A catalogue that renders one of them into the
target language fails here.

There are 21 `tests/test_*i18n*` modules in all. Beyond the two gates above they cover the
per-app seams — for example `test_i18n_resolution.py` (which language a request resolves
to), `test_i18n_render.py`, and `test_readiness_i18n_seam.py`.

## Gotchas

- **The catalogue-freshness test skips itself when `xgettext` or `polib` is missing.** It
  is guarded by `shutil.which("xgettext")` and `pytest.importorskip("polib")`. Both are
  present in the image `docker compose` builds (`gettext` is installed by the `Dockerfile`,
  `polib` comes from `requirements-dev.txt`), so run the suite in the container. On a bare
  host without them the gate silently no-ops, and a missing msgid is only caught later, by
  CI (which installs `gettext` before running the suite).
- **`.mo` files are build output, never input.** They are gitignored (`*.mo` in
  `.gitignore`) and `.dockerignore` excludes `locale/**/*.mo`: if a stale `.mo` rides along
  in the build context, `compilemessages` reports it as already up to date and skips, so
  the build-time gate never runs. Never commit one — regenerate with `compilemessages`.
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
