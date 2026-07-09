# Third-Party Services and Integrations

[FORCA] Command Grid integrates with EVE Online's official services and, optionally, with
several outbound messaging and AI providers. This page documents each integration that
exists in the current source, the credentials it needs, how to set it up, and how it fails.

Every optional integration ships **inert**: it does nothing until its credentials are
configured, and the application boots and runs without any of them (EVE SSO/ESI aside,
which are required for login and game data).

## Table of contents

- [EVE Online: SSO and ESI](#eve-online-sso-and-esi)
- [EVE image service](#eve-image-service)
- [EVE Static Data Export and community data](#eve-static-data-export-and-community-data)
- [Discord](#discord)
- [Slack, Telegram, WhatsApp](#slack-telegram-whatsapp)
- [Email](#email)
- [LLM provider (Command Intelligence)](#llm-provider-command-intelligence)
- [SSRF protection for outbound calls](#ssrf-protection-for-outbound-calls)

## EVE Online: SSO and ESI

**Required.** EVE Single Sign-On is the only login method, and the EVE Swagger Interface
(ESI) provides all game data.

- **What it does:** Authenticates pilots via OAuth2 (authorization-code + PKCE), and reads
  character/corporation data (skills, killmails, assets, wallet, structures, and more)
  according to granted scopes.
- **Credentials:** A registered EVE application at
  [developers.eveonline.com](https://developers.eveonline.com) supplies
  `EVE_SSO_CLIENT_ID`, `EVE_SSO_CLIENT_SECRET`, and a redirect URI matching
  `EVE_SSO_CALLBACK_URL`. Every scope the app uses must be enabled on that application.
- **Setup:**
  1. Create an EVE application; set its callback to `https://<your-domain>/auth/eve/callback/`.
  2. Enable the baseline and feature scopes (see [permissions-and-roles.md](./permissions-and-roles.md)).
  3. Set `EVE_SSO_CLIENT_ID`, `EVE_SSO_CLIENT_SECRET`, `EVE_SSO_CALLBACK_URL`.
  4. Set a real contact in `ESI_USER_AGENT` (CCP may throttle a generic agent).
  5. Have a director authorise the corp-data scopes from the ESI Scopes page.
- **A second, optional application** powers read-only recruitment vetting
  (`RECRUITMENT_SSO_*`); leave it blank to keep recruitment public-evidence-only.
- **Good-citizen behaviour:** The ESI client attaches a pinned `X-Compatibility-Date`, a
  descriptive `User-Agent`, honours ETags, and respects the ESI error budget (420) and
  rate limit (429) with backoff. All ESI calls run from Celery workers, never a web
  request.

**Failure modes and troubleshooting:**

| Symptom | Likely cause | Action |
|---|---|---|
| Login fails at the callback | Redirect URI mismatch | Ensure the EVE app callback exactly equals `EVE_SSO_CALLBACK_URL` |
| A feature scope cannot be granted | Scope not enabled on the EVE application | Enable it on developers.eveonline.com |
| Corp data empty | No director granted the corp scope | Grant the scope from a character with the in-game role |
| ESI calls throttled | Generic/blank User-Agent or budget exhaustion | Set a real `ESI_USER_AGENT`; the client backs off automatically |

## EVE image service

Ship renders, type icons, portraits, and corp/alliance logos come from CCP's image
service (`images.evetech.net`). In production, images are served **same-origin** through
an nginx proxy-cache at `/eveimg`, and a finite set of type icons/renders is mirrored to
local disk by the `mirror_type_images` command. Anything not mirrored falls back to the
cached proxy; types CCP has no art for get a neutral placeholder. This keeps pages
same-origin, survives upstream blips, and is derived into the Content-Security-Policy
image source.

## Google Fonts (web fonts)

Every page — including the anonymous landing page and the public features tour — loads
three typefaces from Google's font CDN (`fonts.googleapis.com` for the stylesheet,
`fonts.gstatic.com` for the font files). See `templates/base.html`.

This means **each visitor's IP address and User-Agent are sent to Google on every page
load**, before they log in and without their consent. It is also why the
Content-Security-Policy keeps `style-src 'unsafe-inline'` and allowlists two Google
origins (`core/middleware.py`), and it is the only remaining third-party origin the
browser contacts.

If you serve members in the EU, or simply want a zero-third-party deployment, self-host
the fonts. All three (Chakra Petch, Inter, JetBrains Mono) are under the SIL Open Font
License, which permits redistribution:

1. Download the `woff2` files for the weights used in `templates/base.html`.
2. Place them under `static/fonts/` with a copy of each `OFL.txt`.
3. Add `@font-face` rules to `static/css/` and drop the three `<link>` tags from
   `templates/base.html`.
4. Remove `https://fonts.googleapis.com` from `style-src` and
   `https://fonts.gstatic.com` from `font-src` in `_build_csp` (`core/middleware.py`).
5. `make collectstatic && make restart`.

The application does not otherwise contact Google.

## EVE Static Data Export and community data

The application needs EVE reference data (type/system/region/skill names) before the UI
renders meaningfully. Sources and import commands:

| Source | Purpose | Import |
|---|---|---|
| **Fuzzwork** (SDE conversion) | Full Static Data Export | `import_sde_fuzzwork` |
| Bundled sample fixture | Tiny SDE for dev/CI | `load_sde` |
| Planetary Industry rulebook | PI materials/schematics | `load_pi_static` |
| **Fuzzwork / Jita** | Live prices | `price_types`, and the daily price beat jobs |
| **EveRef** (optional) | Reference-data / killmail / market-history backfill | `import_everef_*` |
| **zKillboard** | Corp killmail feed | scheduled `killboard.import_home_corp_from_zkill` |

See the [operator data-bootstrap process](./operator-handbook/deployment.md) and
[cli-and-scripts.md](./reference/cli-and-scripts.md).

## Discord

**Optional.** Two distinct Discord integrations exist:

1. **Alerting (Pingboard):** posts alerts to Discord channels via per-channel,
   Fernet-encrypted webhook URLs configured in the Admin Console.
2. **Role sync (`comms_access`):** drives Discord roles from corp membership + RBAC, and
   supports OAuth account linking. Configured in the Admin Console
   (`/ops/admin/comms-access/`), where the bot token and OAuth client are stored
   **encrypted** and take precedence over the optional environment-variable fallback.

- **Credentials:** a Discord bot with **Manage Roles** on your guild (`DISCORD_BOT_TOKEN`
  or the console credential), and optionally a Discord OAuth application for account
  linking (`DISCORD_OAUTH_*`).
- **Kill switch:** `COMMS_ACCESS_ENABLED=0` hard-disables the role-sync subsystem
  regardless of console state.

**Failure modes:** a bot lacking Manage Roles, or positioned below the roles it manages in
the guild hierarchy, cannot assign them; webhook deletion on the Discord side makes a
channel delivery fail (Pingboard retries with backoff and records health).

## Slack, Telegram, WhatsApp

**Optional** Pingboard channels. Telegram and WhatsApp are fully console-configurable
(credentials stored encrypted per channel). Each provider stays inert until its secret is
set, and every provider's API host is allowlisted.

| Channel | Key credentials |
|---|---|
| Slack | `PINGBOARD_SLACK_BOT_TOKEN` (workspace posts/DMs) |
| Telegram | `PINGBOARD_TELEGRAM_BOT_TOKEN`, plus `PINGBOARD_TELEGRAM_WEBHOOK_SECRET` and `PINGBOARD_TELEGRAM_BOT_USERNAME` for pilot self-linking |
| WhatsApp | `PINGBOARD_WHATSAPP_BACKEND` = `meta` or `twilio`, with the matching Meta or Twilio credentials |

Pilots opt into direct messages by linking and verifying a handle on their Pingboard
channels page; EMERGENCY-priority alerts cannot be muted.

## Email

**Optional.** Used for scheduled leadership briefings and readiness alert mail. With no
`EMAIL_HOST`, Django uses the console backend (emails are logged, not sent), so nothing
leaves the server until SMTP is configured. In-game EVE-mail sending is a separate path
that uses a director's character token with a send-mail scope.

## LLM provider (Command Intelligence)

**Optional.** The Command Intelligence subsystem calls an external LLM **only from Celery
workers**. It is disabled cleanly when `LLM_API_KEY` is empty. The reference provider is
MiniMax via its OpenAI-compatible endpoint; an optional fallback provider can be
configured (set both its key and base URL or neither).

- **Credentials:** `LLM_API_KEY`, with `LLM_PROVIDER`, `LLM_MODEL`, `LLM_BASE_URL`, and
  `LLM_ALLOWED_HOSTS`.
- **Guards:** the base URL must be HTTPS and its host must be allowlisted (checked at
  startup); non-secret runtime knobs (model, budgets, thresholds) are tunable in the
  console without a redeploy; the secret is never logged.

## SSRF protection for outbound calls

Every outbound integration validates its target host against an explicit allowlist:

- **ESI** — `ESI_BASE_URL` host is checked against a fixed allowlist at startup.
- **LLM** — `LLM_BASE_URL` / `LLM_FALLBACK_BASE_URL` hosts checked against
  `LLM_ALLOWED_HOSTS` at startup.
- **Pingboard providers** — each provider's fixed API host is allowlisted in the adapter,
  so a bearer token can never be posted to an attacker-controlled host.

This ensures a poisoned or misconfigured URL cannot be used to exfiltrate tokens or corp
data to an arbitrary destination.
