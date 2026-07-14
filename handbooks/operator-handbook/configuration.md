# Configuration

An operator-focused walkthrough of configuring [FORCA] Command Grid. For the exhaustive,
canonical list of every environment variable — defaults, requirements, and security
notes — see the **[Configuration Reference](../configuration-reference.md)**; this page
does not repeat that content.

## Table of contents

- [Where configuration lives](#where-configuration-lives)
- [Required-to-boot variables](#required-to-boot-variables)
- [Setting up EVE SSO](#setting-up-eve-sso)
- [Console-managed configuration (no `.env` required)](#console-managed-configuration-no-env-required)
  - [Language and locale policy](#language-and-locale-policy)
- [Changing configuration after deployment](#changing-configuration-after-deployment)

## Where configuration lives

Configuration comes from environment variables, loaded from a `.env` file at the
repository root (`docker-compose.prod.yml` sets `env_file: .env` on every service). The
canonical, fully commented template is [`.env.example`](../../.env.example) — copy it to
`.env` and fill it in (`make setup` does the copy for you), or let
`deploy/deploy-ubuntu-26.04.sh` generate the required secrets automatically.

Beyond environment variables, most day-to-day, leadership-tunable behavior — feature
audiences, SRP payout modes, readiness rules, Pingboard channels, retention windows, and
more — lives in the database and is edited through the role-gated Admin Console at
`/ops/`, with no redeploy required. See
[Configuration Reference § Database-stored settings](../configuration-reference.md#database-stored-settings-leadership-tunable).

## Required-to-boot variables

Production (`config.settings.prod`) refuses to start unless these three are set:

| Variable | Purpose |
|---|---|
| `DJANGO_SECRET_KEY` | Django's cryptographic secret. Boot fails if left at the insecure development default. |
| `TOKEN_ENCRYPTION_KEY` | Fernet key encrypting stored OAuth refresh tokens and integration credentials at rest. |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated Host header allowlist. |

The one-shot deploy script generates the first two for you and derives
`DJANGO_ALLOWED_HOSTS` from `--domain`. On the manual path, generate them yourself:

```bash
openssl rand -base64 50                    # DJANGO_SECRET_KEY
openssl rand -base64 32 | tr '+/' '-_'     # TOKEN_ENCRYPTION_KEY (url-safe, keep the '=' padding)
openssl rand -base64 24 | tr -d '/+='      # POSTGRES_PASSWORD / REDIS_PASSWORD
```

Full detail, defaults, and every other variable (database, Redis/Celery, sessions,
HTTPS/HSTS knobs, and so on) are in the
[Configuration Reference](../configuration-reference.md#environment-variables).

## Setting up EVE SSO

1. Register an application at [developers.eveonline.com](https://developers.eveonline.com)
   with a callback URL of `https://<your-domain>/auth/eve/callback/`.
2. Enable the scopes you intend to use on that application (baseline login scopes at
   minimum; opt-in feature scopes as needed) — the full catalogue is in
   [Permissions and Roles § ESI scopes](../permissions-and-roles.md#esi-scopes).
3. Set `EVE_SSO_CLIENT_ID`, `EVE_SSO_CLIENT_SECRET`, and `EVE_SSO_CALLBACK_URL` in
   `.env` — the callback URL **must exactly match** the one registered on the CCP
   application.
4. Set a real address in `ESI_USER_AGENT` — CCP may throttle a generic or blank
   User-Agent.
5. Set `FORCA_HOME_CORP_ID` to your corporation's numeric EVE id.
6. After deploying, log in and have a **Director** character authorize the corp-level
   scopes from the ESI Scopes page (`/auth/eve/scopes/`).

See [Requirements § EVE SSO / ESI application registration](./requirements.md#eve-sso--esi-application-registration)
and [Third-Party Services](../third-party-services.md#eve-online-sso-and-esi) for setup
detail and failure modes.

## Console-managed configuration (no `.env` required)

Several optional integrations are configured **entirely through the Admin Console**,
with credentials stored **encrypted** in the database rather than in `.env`. A console
value always takes precedence over an environment-variable fallback, so you never need
to redeploy to add, rotate, or remove these credentials:

| Integration | Console location | Notes |
|---|---|---|
| **Discord** (role sync + alerting) | `/ops/admin/comms-access/` | Bot token and OAuth client secret stored encrypted; `COMMS_ACCESS_ENABLED` is an operator-level hard kill switch (default on) |
| **Telegram** | Pingboard console section | Bot token and webhook secret stored encrypted per channel |
| **WhatsApp** | Pingboard console section | Meta or Twilio credentials stored encrypted per channel |
| **LLM / Command Intelligence** | Command Intelligence console section | Non-secret runtime knobs (model, budgets, thresholds) are console-tunable; the API key itself is set via `LLM_API_KEY` and stays inert until set |

Each of these integrations ships **inert** — it does nothing until configured — and the
application boots and runs normally without any of them. See
[Third-Party Services](../third-party-services.md) for what each one does and how it
fails, and [Configuration Reference](../configuration-reference.md) for the fallback
environment variables.

### Language and locale policy

The application ships in nine languages: English, Portuguese (Brazil), Spanish, French,
Russian, German, Simplified Chinese, Korean, and Japanese. English is the canonical
source language and can never be disabled. Which of the others the language selector
offers is a leadership policy decision rather than deployment configuration: it is set at
`/ops/admin/i18n/` (**Director** role), stored as the `i18n.config` app setting, and takes
effect immediately with no redeploy, like the rest of this section. The shipped defaults
enable **English only**, so a fresh install stays English-only until someone turns a
locale on. The console page shows per-locale translation coverage; the catalogues are
machine drafts with an LLM native-review pass, not professionally human-reviewed
translations, so read the coverage figure before you commit to a language.

**Enabling a locale is a corp-wide flip, not a preview.** Browser detection defaults to
**on**, so the moment you tick a locale, every pilot whose browser prefers that language
gets it on their next page load, unless they have already picked a language of their own
(an explicit choice outranks the browser). To look at a locale before committing, untick
browser detection first — the locale is then reachable only by picking it explicitly in
the language selector.

Two knobs stay at the environment level (defaults and detail in the
[Configuration Reference](../configuration-reference.md#environment-variables)):
`I18N_ENABLED` (default on) is the hard kill switch — set `I18N_ENABLED=0` in `.env` to
short-circuit locale resolution to English and hide the selector, in the same spirit as
the `COMMS_ACCESS_ENABLED` switch above. `DJANGO_LANGUAGE_COOKIE_SECURE` sets the
`Secure` flag on the `forca_language` cookie and defaults to whatever
`DJANGO_SESSION_COOKIE_SECURE` is.

## Changing configuration after deployment

- **Environment variables:** edit `.env`, then apply with
  `docker compose -f docker-compose.prod.yml up -d` (recreates containers that read the
  changed variables). `.env` is never overwritten by re-running the deploy script or
  `make deploy`.
- **Console-managed settings:** edit them directly in `/ops/` — changes take effect
  immediately, no redeploy needed.
- Always keep `.env` at mode `600` and never commit it — see
  [Security Hardening](./security-hardening.md).
