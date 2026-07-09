# Contributing to [FORCA] Command Grid

Thank you for your interest in improving [FORCA] Command Grid — a free, open-source
EVE Online corporation operations hub. This document is the entry point for
contributors. For architecture, testing, and deep implementation guidance, see the
[Contributor Handbook](./handbooks/contributor-handbook/README.md).

## Table of contents

- [Ways to contribute](#ways-to-contribute)
- [Local development setup](#local-development-setup)
- [Branching](#branching)
- [Making a change](#making-a-change)
- [Code quality expectations](#code-quality-expectations)
- [Tests](#tests)
- [Documentation](#documentation)
- [Security](#security)
- [Never commit secrets](#never-commit-secrets)
- [Reporting bugs](#reporting-bugs)
- [Proposing larger changes](#proposing-larger-changes)
- [Pull request checklist](#pull-request-checklist)

## Ways to contribute

- Fix bugs or improve existing features.
- Improve documentation in `handbooks/`.
- Improve test coverage.
- Review open pull requests.
- Report reproducible bugs.

## Local development setup

The project runs entirely in Docker, keeping your host clean. You need Docker Engine
with the Compose plugin.

```bash
make dev                 # build + start web, worker, beat, postgres, redis (autoreload)
make bootstrap-sample    # load the tiny bundled sample SDE fixture (dev/CI only)
docker compose exec web python manage.py seed_demo         # roles, home corp, demo doctrine
docker compose exec web python manage.py createsuperuser
# App: http://127.0.0.1:8000/   (role-gated console at /ops/)
```

Full details, including running against real EVE SSO/ESI, are in
[local-development.md](./handbooks/contributor-handbook/local-development.md).

## Branching

- `main` is the release branch. Keep it deployable.
- Create a topic branch off `main` for every change, using a short descriptive name
  (for example `fix/killboard-valuation` or `feat/operations-rsvp-reminder`).
- Keep branches focused; one logical change per branch makes review and revert easy.

## Making a change

1. Open (or comment on) an issue describing the problem or proposal first for anything
   non-trivial, so design can be agreed before code is written.
2. Branch from `main`.
3. Make the change with tests.
4. Run the linter and the test suite locally (see below).
5. Update any affected documentation in `handbooks/`.
6. Open a pull request.

## Code quality expectations

- Python code targets **Python 3.12** and follows the existing structure: one Django app
  per bounded context under `apps/`, shared primitives in `core/`.
- Lint and format with **ruff** (configuration in [`pyproject.toml`](./pyproject.toml)):

  ```bash
  docker compose run --rm web ruff check .
  docker compose run --rm web ruff format --check .
  ```

- Match the conventions of the surrounding code: naming, docstrings, and the
  service/task separation used across the codebase.
- Web requests must never call ESI or an LLM directly — those calls belong in Celery
  tasks. See the [architecture guide](./handbooks/contributor-handbook/architecture.md).

## Tests

The suite runs against PostgreSQL in Docker:

```bash
docker compose run --rm -e DJANGO_SETTINGS_MODULE=config.settings.test web pytest
```

- Add or update tests for every behavioural change.
- Do not commit skipped, `.only`, or placeholder tests as if they were coverage.
- See [testing.md](./handbooks/contributor-handbook/testing.md) for patterns and
  gotchas (including how ESI is mocked).

## Documentation

Documentation lives in `handbooks/` and the root community files. **When you change
behaviour, update the docs in the same pull request.** In particular:

- New or changed environment variables → update
  [configuration-reference.md](./handbooks/configuration-reference.md) and
  [reference/environment-variables.md](./handbooks/reference/environment-variables.md).
- New features → update [feature-catalog.md](./handbooks/feature-catalog.md) and the
  relevant handbook.
- New background jobs → update
  [reference/background-jobs.md](./handbooks/reference/background-jobs.md).
- New ESI scopes → update [permissions-and-roles.md](./handbooks/permissions-and-roles.md).

## Security

- Follow the [contributor security guidelines](./handbooks/contributor-handbook/security-guidelines.md).
- Report vulnerabilities privately per [`SECURITY.md`](./SECURITY.md) — never in a public
  issue or pull request.
- New outbound integrations must validate their target host against an allowlist.

## Never commit secrets

- `.env` files are git-ignored and must stay that way.
- Use only dummy placeholder values in examples and tests.
- Never paste real tokens, client secrets, or another person's EVE character data into
  code, tests, issues, or pull requests.

## Reporting bugs

Open an issue with:

- What you expected to happen and what actually happened.
- Steps to reproduce.
- The version/commit and, for operators, the relevant (secret-free) configuration.
- Relevant log excerpts with any sensitive values redacted.

## Proposing larger changes

For a substantial feature or an architectural change, open an issue describing the
problem, the proposed approach, and the affected apps/models before writing code. This
saves rework and lets maintainers weigh the change against the project's direction.

## Pull request checklist

- [ ] Branched from `main`, focused on one logical change.
- [ ] `ruff check` and `ruff format --check` pass.
- [ ] `pytest` passes, with tests added/updated for the change.
- [ ] Documentation in `handbooks/` updated where behaviour changed.
- [ ] No secrets, tokens, or personal data added.
- [ ] Commit messages are clear and describe the change.

See the full [pull request guide](./handbooks/contributor-handbook/pull-request-guide.md)
for review expectations and the release process.
