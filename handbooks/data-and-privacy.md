# Data and Privacy

This page describes what data [FORCA] Command Grid stores, why it needs each category,
how EVE authentication tokens are handled, and how operators should protect a deployment.
It reflects the current implementation; it is **not** a legal privacy policy and makes no
compliance claims. Operators of a public deployment are responsible for their own privacy
policy and legal obligations.

## Table of contents

- [Data categories](#data-categories)
- [EVE ESI data collected](#eve-esi-data-collected)
- [How authentication tokens are handled](#how-authentication-tokens-are-handled)
- [How data is refreshed](#how-data-is-refreshed)
- [How data is cached](#how-data-is-cached)
- [Logs and data](#logs-and-data)
- [Data sent to third parties by the visitor's browser](#data-sent-to-third-parties-by-the-visitors-browser)
- [Cookies](#cookies)
- [Retention and member departure](#retention-and-member-departure)
- [Member data rights](#member-data-rights)
- [Protecting the deployment](#protecting-the-deployment)
- [Backups](#backups)
- [Privacy considerations for public deployments](#privacy-considerations-for-public-deployments)

## Data categories

| Category | Examples | Why it is needed |
|---|---|---|
| **Account / identity** | Application user, linked EVE characters, roles, chosen UI language | Login, membership gating, access control, showing the interface in the pilot's language |
| **OAuth tokens** | Encrypted EVE refresh tokens and granted scopes | Background syncs of the pilot's authorised data |
| **Character game data** | Skills, skill queue, attributes, implants, killmails, assets, fittings, industry jobs, PI colonies | Readiness, doctrines, industry, SRP, personal tools |
| **Corporation game data** | Membership/roster, wallet journal, contacts/standings, structures, moon extractions, mining ledger, corp blueprints/jobs, corp contracts | Leadership dashboards and corp operations |
| **Reference data (SDE)** | Type/system/region/skill names, PI rulebook | Rendering names and computing requirements |
| **Member-generated content** | Doctrine fits, knowledge-base pages, notes, plans, contest and SRP records | The features members use |
| **Operational records** | Audit log, recommendations, alerts, calendar events, integration credentials (encrypted) | Governance, alerting, and integrations |

The application stores only what the corporation's authorised scopes provide, plus the
content members and leadership create in the app.

## EVE ESI data collected

ESI data is read only for the scopes a pilot or director has explicitly granted. The
full scope catalogue and what each unlocks is documented in
[permissions-and-roles.md](./permissions-and-roles.md). In summary:

- **Baseline login scopes** read the pilot's own skills, skill queue, implants, personal
  killmails, corp membership, and in-game corp roles.
- **Opt-in pilot scopes** read the pilot's own assets, industry jobs, blueprints,
  contracts, fittings, PI colonies, or (for a booked mentoring session only) real-time
  online/location.
- **Director scopes** read corporation-level data — roster, assets, wallet, contacts,
  structures, moon extractions, corp blueprints/jobs, notifications, and mail — using a
  character that holds the required in-game corporation role.

Some ESI data is real-time only and is treated accordingly: for example, mentorship
session presence (`read_online`/`read_location`) is polled **only** during a session the
pilot booked and is **never stored** beyond the check.

## How authentication tokens are handled

- The application uses EVE SSO OAuth2 with the **authorization-code + PKCE (S256)** flow.
  Access and refresh tokens are minted by CCP after the pilot consents to scopes.
- **Refresh tokens are encrypted at rest** using Fernet symmetric encryption
  (`cryptography`), keyed by the operator's `TOKEN_ENCRYPTION_KEY`. Plaintext refresh
  tokens are not stored.
- JWTs are validated server-side against CCP's published signing keys before a token is
  trusted.
- The **recruitment** (second, optional) SSO application reads a consenting candidate's
  skills and corp roles **once** and does **not** store the token.
- Losing `TOKEN_ENCRYPTION_KEY` makes stored refresh tokens unrecoverable — members simply
  re-authorise. Back the key up securely and separately from the database.

## How data is refreshed

Game data is kept current by scheduled Celery jobs (see
[reference/background-jobs.md](./reference/background-jobs.md)). Cadences sit at or above
ESI cache TTLs. Directors can also trigger an immediate "sync now" from the relevant page.
Corporation syncs are cheap no-ops until the matching scope is granted.

## How data is cached

Redis backs both the Django cache and the Celery broker. Cached values include warmed
dashboards, ranking aggregates, resolved names, and computed signals; every cache entry
carries a bounded TTL, and Redis is configured to evict TTL-bearing cache keys under
memory pressure while preserving the task queue. Caching is a performance optimisation
over the authoritative database rows.

## Logs and data

- Application logs go to stdout (captured by Docker) at the configured `DJANGO_LOG_LEVEL`.
- Logs may contain character or corporation identifiers, request paths, and error context.
  Secrets are not intentionally logged; the ESI and LLM clients redact credentials.
- The application maintains an **audit log** of sensitive administrative actions
  (configuration changes, syncs, financial views) for governance.
- Alert recipient handling deliberately keeps personal contact details out of the
  broadcast audit trail.

## Data sent to third parties by the visitor's browser

One third-party origin is contacted directly by the browser: **Google Fonts**
(`fonts.googleapis.com`, `fonts.gstatic.com`), loaded from `templates/base.html` on every
page, including pages served to anonymous visitors. Google therefore receives each
visitor's IP address and User-Agent. Nothing else is loaded off-origin — EVE imagery is
proxied same-origin through `/eveimg`.

If that is not acceptable for your members (for example under the GDPR, where embedding
Google Fonts without consent has been found unlawful), self-host the fonts. The steps are
in [third-party-services.md](third-party-services.md#google-fonts-web-fonts).

All *server-side* outbound calls (ESI, EveRef, Discord, Slack, Telegram, WhatsApp, LLM
providers) are made by the application, not the browser, and are host-allowlisted.

## Cookies

Every cookie the application sets is first-party. There are no analytics or advertising
cookies.

- **Session** and **CSRF** — Django's own, needed for login and form safety. Production
  marks both `Secure` (`DJANGO_SESSION_COOKIE_SECURE`, `DJANGO_CSRF_COOKIE_SECURE`) and
  `SameSite=Lax`. The session cookie is `HttpOnly`; the CSRF cookie is not, because
  template forms have to read it.
- **`messages`** — Django's transient carrier for one-off notices ("Fit saved"). It is
  written only when a notice is pending and is cleared once the notice has been shown.
- **`forca_language`** — the language the visitor picked from the selector. It is written
  only on an explicit choice, including a choice made by an anonymous visitor when
  leadership has enabled anonymous selection. It holds a locale code and nothing else, is
  `HttpOnly` and `SameSite=Lax` with a one-year age, and is `Secure` in production
  (`DJANGO_LANGUAGE_COOKIE_SECURE`, defaulting to whatever `DJANGO_SESSION_COOKIE_SECURE`
  is set to).

A signed-in pilot's choice is also stored on the account (`identity.User.language`), which
is blank until they pick a language, and that account preference is what the interface
follows. The cookie is written either way; for a visitor who is not signed in it is the
only record of the choice.

## Retention and member departure

- A nightly **retention** job enforces configured retention windows on historical and
  operational data.
- A separate nightly **member-leave** job applies the on-departure retention policy to a
  former member's data. It ships **disarmed** (report-only — it writes a report and
  deletes nothing) until a director explicitly arms it on the retention page.
- When a pilot leaves the home corporation, they lose member access on the next sync
  (enforced by the membership gate).

Retention windows and the member-leave policy are configured in the Admin Console.

## Member data rights

Each pilot has access to their own data-rights pages (under `/privacy`), which are
reachable even for a logged-in non-member. These let a pilot view and request deletion of
their own data. Directors administer retention policy centrally.

## Protecting the deployment

Operators should:

- Keep `.env` at mode `600`, owned by the application user, and never commit it.
- Back up `TOKEN_ENCRYPTION_KEY` securely and separately.
- Terminate TLS and keep HSTS enabled (the default production configuration does this).
- Expose only ports 80/443; keep PostgreSQL and Redis off any host port mapping (the
  provided compose file does this).
- Restrict who holds Officer/Director roles and superuser accounts.
- Follow the [security hardening checklist](./operator-handbook/security-hardening.md).

## Backups

The provided backup script produces gzipped PostgreSQL dumps with basic integrity checks
and retention pruning; restore is a guarded, confirmation-gated operation. Store backups
securely — a database dump contains encrypted tokens and corp data. See
[operator backup-and-restore](./operator-handbook/backup-and-restore.md).

## Privacy considerations for public deployments

- The public surface is intentionally limited: the killboard, rankings, audience-`public`
  features, and the recruitment/onboarding surface. Everything internal is behind the
  membership gate.
- Public deployments should publish a privacy policy and the required EVE Online / CCP
  fan-site disclaimer (see [`NOTICE.md`](../NOTICE.md) and [`README.md`](../README.md)).
- Consider which features you expose publicly (killboard drill-downs are crawler-throttled
  by the provided nginx configuration) and set feature audiences deliberately.
