# Pull Request Guide

## Table of contents

- [Branching](#branching)
- [Before opening a pull request](#before-opening-a-pull-request)
- [Review process](#review-process)
- [Pull request checklist](#pull-request-checklist)
- [Coding conventions](#coding-conventions)
- [Updating documentation when behaviour changes](#updating-documentation-when-behaviour-changes)
- [Release process](#release-process)

## Branching

- `main` is the release branch and is expected to stay deployable at every commit.
- Create a topic branch off `main` for every change, named descriptively (e.g.
  `fix/killboard-valuation`, `feat/operations-rsvp-reminder`).
- Keep a branch focused on one logical change — it makes review and, if necessary,
  revert straightforward.
- For a substantial feature or architectural change, open an issue describing the
  problem, the proposed approach, and the affected apps/models *before* writing code,
  so design can be agreed first (see [CONTRIBUTING.md](../../CONTRIBUTING.md)).

## Before opening a pull request

Run, from the repository root:

```bash
docker compose run --rm web ruff check .
docker compose run --rm web ruff format --check .
docker compose run --rm -e DJANGO_SETTINGS_MODULE=config.settings.test web pytest
```

All three must pass locally before requesting review — see
[testing.md](./testing.md) for options (running a subset, coverage, the `LLM_API_KEY`
gotcha).

## Review process

Author and reviewer are deliberately separate passes:

- **The author** writes the change, its tests, and updates any documentation the
  change affects, then opens the pull request with a description of *why* the change
  is needed, not just what it does.
- **The reviewer** evaluates the diff independently — correctness, adherence to the
  architecture (service/task layering, the ESI-only-from-workers rule), security
  implications (RBAC scoping, new outbound calls, secret handling — see
  [security-guidelines.md](./security-guidelines.md)), and test adequacy. A reviewer
  should not rubber-stamp their own change; every pull request needs a second set of
  eyes before merge.
- Address review feedback with new commits (or a clean rebase) rather than force-
  pushing over history mid-review where avoidable, so the reviewer can see what
  changed since their last pass.

## Pull request checklist

- [ ] Branched from `main`, focused on one logical change.
- [ ] `ruff check` and `ruff format --check` pass.
- [ ] `pytest` passes, with tests added/updated for the change.
- [ ] Documentation in `handbooks/` updated where behaviour changed (see below).
- [ ] New user-visible strings are marked for translation, `make messages` has been
      re-run, and `locale/*/LC_MESSAGES/django.po` is committed with the change (see
      [testing.md](./testing.md#localisation-gates)).
- [ ] No translated string is written to the database, and no protected EVE term
      (`core/i18n/data/protected-terms.yml`) is translated.
- [ ] No secrets, tokens, or personal data added.
- [ ] New outbound integrations validate their target host against an allowlist (see
      [security-guidelines.md](./security-guidelines.md#ssrf-allowlists-on-every-outbound-integration)).
- [ ] Commit messages are clear and describe the change.

## Coding conventions

- **Python 3.12**, one Django app per bounded context under `apps/`, shared primitives
  in `core/` — match the structure described in
  [README.md](./README.md#repository-structure) and [architecture.md](./architecture.md).
- **ruff** is the source of truth for style (`pyproject.toml`):
  - Line length **120**, target version **`py312`**.
  - Rule sets: **`E`** (pycodestyle errors), **`F`** (pyflakes), **`I`** (import
    sorting), **`UP`** (pyupgrade — prefer modern syntax), **`B`** (bugbear — common
    bug patterns), **`DJ`** (Django-specific checks), **`S`** (bandit security checks).
  - **`migrations/`** is excluded from linting entirely — never hand-edit a generated
    migration to satisfy ruff.
  - Project-wide ignores: **`S101`** (assert usage — tests use plain `assert`) and
    **`DJ008`** (missing `__str__` — not required on join/intermediate tables).
    Test-path-specific ignores: **`S105`**/**`S106`** (hardcoded password-like
    strings) under `tests/*`, `**/tests/*`, and `*/settings/*`.
- **Match the surrounding code's conventions**: naming, docstring style (a short
  module-level docstring stating the bounded context, per `apps/*/models.py`),
  and the view → service → model layering already used in that app.
- **Web requests must never call ESI or an LLM directly** — see
  [architecture.md](./architecture.md#the-golden-rule-esi-and-llm-calls-only-from-celery-workers).
  This is checked in review, not by a lint rule, so call it out explicitly if you see
  it in a diff.
- **Never persist a translated string.** Django coerces a lazy translation proxy to
  `str` on `.save()`, freezing the row in the *writer's* locale (usually a Celery
  worker, which has no locale) for every reader. Persist a key plus params
  (`<field>_key` / `<field>_params`, or `source_key`) and resolve it at read time from
  the app's `messages.py` scaffolds — see
  [architecture.md](./architecture.md#the-second-rule-never-persist-a-translated-string)
  and `apps/recommendations/messages.py` for the reference implementation.

## Updating documentation when behaviour changes

Update documentation **in the same pull request** as the behavioural change, not as a
follow-up:

| Change | Update |
|---|---|
| New or changed environment variable | [configuration-reference.md](../configuration-reference.md) |
| New member-facing feature | [feature-catalog.md](../feature-catalog.md) and the relevant handbook page |
| New or re-cadenced background job | [reference/background-jobs.md](../reference/background-jobs.md) and, if it changes the architecture-level pattern, [background-jobs.md](./background-jobs.md) |
| New ESI scope or role/permission | [permissions-and-roles.md](../permissions-and-roles.md) |
| New app, or a change to an app's responsibility | [domain-model.md](./domain-model.md)'s bounded-context table |
| New outbound integration or security-relevant change | [security-guidelines.md](./security-guidelines.md) and, if it changes the posture summary, [SECURITY.md](../../SECURITY.md) |
| New or changed user-visible string | the message catalogues (`locale/*/LC_MESSAGES/django.po`), via `make messages` — see [testing.md](./testing.md#localisation-gates) |

## Release process

Version `1.x` is the current, supported release line (see [SECURITY.md](../../SECURITY.md)):
`main` is expected to remain deployable at all times, and security fixes land on
`main` rather than a separate maintenance branch. There is no separate long-lived
release branch for `1.x` — operators track `main` and apply updates via
`make update` (see the [operator handbook](../operator-handbook/README.md)). Versioning
follows a semantic-versioning-style discipline (`pyproject.toml`'s `version` field):
increment the patch/minor component for backward-compatible fixes and additions, and
call out any breaking change (a migration that isn't purely additive, a removed
setting, a changed default) prominently in the pull request description so operators
can plan for it before updating.
