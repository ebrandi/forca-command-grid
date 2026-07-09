# Security Policy

[FORCA] Command Grid stores EVE Online character and corporation data, encrypted OAuth
refresh tokens, and — for corporations that enable them — outbound integration
credentials. We take the security of the application and its deployments seriously.

## Supported versions

| Version | Supported |
|---|---|
| 1.x (current) | ✅ Security fixes provided |
| < 1.0 | ❌ Not supported |

Version 1.0 is the first public release line. Security fixes are delivered on the `main`
branch; operators should track it and apply updates promptly (see
[operator upgrade guide](./handbooks/operator-handbook/upgrades.md)).

## Reporting a vulnerability

**Please do not open a public issue for security vulnerabilities.** Public disclosure
before a fix is available puts every deployment at risk.

Instead, use GitHub's private vulnerability reporting: the **"Report a vulnerability"**
button under this repository's *Security* tab. It opens a private advisory visible only to
you and the maintainers.

If that is unavailable to you, email the maintainer at **ebrandi@FreeBSD.org** with
`[FORCA SECURITY]` in the subject line.

Please include, where possible:

- A description of the vulnerability and its impact.
- The affected component (app, endpoint, background job, deployment file, or dependency).
- Steps to reproduce, or a proof-of-concept, kept private to the report.
- The version, commit, or deployment configuration affected.
- Any suggested remediation.

**Do not include live secrets, production tokens, or another party's personal data in
your report.** Redact them; describe the class of value instead.

## Responsible disclosure

- We ask that you give us a reasonable opportunity to investigate and release a fix
  before any public disclosure.
- We will acknowledge your report, keep you informed of progress, and credit you in the
  release notes if you wish.
- Please act in good faith: do not access, modify, or exfiltrate data that is not yours,
  and do not degrade the availability of any live deployment while testing.

## Security posture (high level)

The following controls are implemented in the current source. They describe the
application's design; they are **not** a warranty. Each operator remains responsible for
the security of their own deployment.

- **Authentication** is via EVE Single Sign-On using OAuth2 authorization-code flow with
  PKCE (S256) and server-side JWT validation against CCP's published keys.
- **OAuth refresh tokens and integration credentials are encrypted at rest** with Fernet
  (`cryptography`), keyed by `TOKEN_ENCRYPTION_KEY`.
- **Role-based access control** with ordered role tiers plus least-privilege lateral
  capabilities; granting the Director role requires a second director's approval
  (dual control).
- **Session hardening**: sliding idle timeout plus an absolute session-lifetime ceiling,
  `Secure`/`HttpOnly`/`SameSite` cookies, and CSRF protection.
- **Transport security** in production: HTTPS redirect, HSTS (with preload and
  subdomains), and secure proxy header handling.
- **Response hardening**: a per-request nonce-based Content-Security-Policy,
  `X-Frame-Options: DENY`, `nosniff`, and a strict referrer policy.
- **SSRF guards**: outbound ESI, LLM, and messaging hosts are validated against explicit
  allowlists at startup and in adapters.
- **Hardened XML parsing** (`defusedxml`) for EVE-client fitting imports.
- **Least-privilege containers**: the application runs as a non-root user with
  `cap_drop: ALL` and `no-new-privileges`, and the stock Django admin is disabled by
  default in production.
- **Dependency scanning**: `pip-audit` runs both in CI and as a scheduled in-application
  job that surfaces newly disclosed CVEs to leadership.

For deeper detail, see:

- [Operator security hardening checklist](./handbooks/operator-handbook/security-hardening.md)
- [Contributor security guidelines](./handbooks/contributor-handbook/security-guidelines.md)
- [Data and privacy](./handbooks/data-and-privacy.md)
- [Permissions and roles](./handbooks/permissions-and-roles.md)

## Handling secrets

Never commit real secrets. `.env` files are git-ignored; configuration templates use
dummy placeholder values only. If you believe a secret has been committed or exposed,
treat it as compromised: rotate it immediately and notify the maintainers privately.
