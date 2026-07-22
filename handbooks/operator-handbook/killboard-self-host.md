# Run your corporation's killboard

A focused guide to standing up [FORCA] Command Grid **as a self-hosted corp killboard** —
the smallest useful deployment. You get a private, corp-scoped killboard (kills, losses,
rankings, analytics, intel tools) welded to your own ESI feed, without adopting the full
ERP/industry/command suite on day one. You can turn the heavier modules on later.

This page is the killboard-first quick start. It links to the canonical references rather
than repeating them:

- Host provisioning and the full install: [Deployment](./deployment.md).
- Every environment variable in detail: [Configuration Reference](../configuration-reference.md).
- What each background job does: [Background Jobs Reference](../reference/background-jobs.md).

## Table of contents

- [What you get](#what-you-get)
- [Prerequisites](#prerequisites)
- [1. Register an EVE application](#1-register-an-eve-application)
- [2. Configure the environment](#2-configure-the-environment)
- [3. Bring the stack up](#3-bring-the-stack-up)
- [4. Grant the Director killmail token](#4-grant-the-director-killmail-token)
- [5. Import your history](#5-import-your-history)
- [6. Brand the board](#6-brand-the-board)
- [The setup wizard](#the-setup-wizard)
- [Upgrades](#upgrades)

## What you get

The **killboard-first profile** keeps these member-facing modules on and turns the rest off:

| On | Off (until you enable them) |
|---|---|
| Killboard (kills, losses, rankings, analytics, gamification) | Industry, mining, planetary, stockpile |
| Killboard intel (watchlists, scan analyzer) | Operations, structures, finance, contracts |
| Hall of Fame | SRP, doctrines/shipyard, mentorship, recruitment |
| Market pricing (ISK valuation) | Command intelligence, readiness, campaigns |

Nothing is deleted — every module is one toggle away on **Admin → Features**, and
`apply_profile full` re-enables everything.

## Prerequisites

- A Docker host that meets [Requirements](./requirements.md) (the killboard profile is
  lighter than the full suite, but the same stack).
- Your corporation's **id** (from zKillboard or [EveWho](https://evewho.com/)).
- A character with the in-game **Director** role in that corporation (or the CEO), to grant
  the corporation killmail feed.

## 1. Register an EVE application

Create an application at
[developers.eveonline.com/applications](https://developers.eveonline.com/applications):

- **Connection type:** Authentication & API Access.
- **Callback URL:** exactly `https://<your-domain>/auth/eve/callback/`. The setup wizard
  prints the exact URL for your host — copy it from there so it matches byte for byte.
- **Scopes:** at minimum `esi-killmails.read_corporation_killmails.v1` and
  `esi-killmails.read_killmails.v1`. Granting the default scope set is fine.

Note the **Client ID** and **Secret Key**.

## 2. Configure the environment

In your `.env` (see [Configuration](./configuration.md) for the full list), set at least:

```dotenv
FORCA_HOME_CORP_ID=98000001            # your corporation id — the board keys on it
FORCA_PROFILE=killboard                # declares intent (the wizard reads this)
EVE_SSO_CLIENT_ID=<client id>
EVE_SSO_CLIENT_SECRET=<secret key>
EVE_SSO_CALLBACK_URL=https://<your-domain>/auth/eve/callback/
FORCA_SITE_URL=https://<your-domain>
```

`FORCA_PROFILE=killboard` is an **intent marker only** — the actual feature flags live in the
database, so you apply the preset with a management command in the next step. Setting it does
not silently change the full-suite default for existing installs.

## 3. Bring the stack up

Follow [Deployment](./deployment.md) to build and start the stack, run migrations, load the
SDE and warm prices. Then apply the killboard-first feature preset:

```bash
docker compose run --rm web python manage.py apply_profile killboard
```

This turns off the heavy modules and leaves the killboard, its intel tools, Hall of Fame and
market pricing on. Re-run `apply_profile full` at any time to restore the whole suite; add
`--dry-run` to preview the change first.

## 4. Grant the Director killmail token

The single biggest onboarding step: the corporation killmail feed needs a token from a
character with the in-game **Director** role (the CEO qualifies automatically).

1. Have the director log in through EVE SSO on your instance.
2. On **ESI Scopes** (`/auth/eve/scopes/`), confirm the corporation-killmails scope is
   granted for that character.
3. Within about 15 minutes the background scheduler polls the corporation feed. The setup
   wizard's **Director killmail token** step turns green once the first poll succeeds.

If the step stays amber ("a token exists but the feed hasn't polled"), the usual causes are:
the character does not actually hold the Director role, or the Celery **beat** scheduler is
not running (see [Monitoring and Health](./monitoring-and-health.md)).

## 5. Import your history

A new board is empty until you backfill it. From the wizard's **Killmail history** step, use
the one-click launcher, or run the commands directly:

```bash
# EVE Ref archives — fastest, no ESI rate limit. Blank dates = full history.
docker compose run --rm web python manage.py import_everef_killmails --from 2015-01-01

# or zKillboard — walks the corp's whole history, paced against ESI.
docker compose run --rm web python manage.py import_zkill_history
```

The wizard runs the **same** commands in the background and shows live progress. Only one
import runs at a time; a cancel takes effect after the current batch.

## 6. Brand the board

On the wizard's **Branding** step (director-only) you can set a display name, a logo URL (an
`https://` address or a `/static/` path — no file upload in v1), an accent colour (a hex
value like `#c8a24b`), and a footer tagline. Every field is optional; unset fields fall back
to the corporation name and the default theme.

## The setup wizard

Everything above is checked live on the **setup wizard** at `/killboard/setup/`
(director-only, linked from the Killboard section of the sidebar). Each step recomputes its
status on every load — there is no state to get out of sync — and shows the exact remedial
action for anything not yet done.

## Upgrades

Upgrades are identical to the full suite — see [Upgrades](./upgrades.md). Your profile choice
and branding are stored in the database, so they survive upgrades; re-running
`apply_profile killboard` after an upgrade is safe and idempotent if you want to be sure.
