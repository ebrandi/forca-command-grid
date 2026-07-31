# Security Hardening

A checklist of the security controls implemented in [FORCA] Command Grid's deployment
tooling and application configuration. This describes what the software and the provided
deployment scripts actually do; items marked **recommended** are operator best practices
that the software does not itself enforce.

## Table of contents

- [Container hardening](#container-hardening)
- [Application surface](#application-surface)
- [Network and edge](#network-and-edge)
- [Host hardening](#host-hardening)
- [Secrets](#secrets)
- [Dependency scanning](#dependency-scanning)
- [Container-image scanning (the OS-package layer)](#container-image-scanning-the-os-package-layer)
- [Further reading](#further-reading)

## Container hardening

| Control | Detail |
|---|---|
| Non-root process | Every application container (`web`, `worker`, `beat`) runs as the `appuser` created in the [`Dockerfile`](../../Dockerfile), not root. |
| Dropped capabilities | Every service in `docker-compose.prod.yml` sets `cap_drop: ALL` — no Linux capability is retained beyond what an unprivileged process needs. |
| No privilege escalation | Every service sets `security_opt: no-new-privileges:true`. |
| Memory backstops | Every service has an explicit `mem_limit`, sized to its observed working set, so a leak or spike in one container cannot OOM-cascade the whole host (which ships with no swap in the reference tuning). |
| No unnecessary tooling in the image | The application image installs only `libpq5` for the Postgres client library — no `curl` or similar network tooling that could aid a post-exploitation attacker, since healthchecks use Python's `urllib` instead. |
| Postgres/Redis network isolation | Neither `postgres` nor `redis` has a host port mapping — both are reachable only from other containers on the compose network. |

## Application surface

| Control | Detail |
|---|---|
| Django admin disabled by default | `ENABLE_DJANGO_ADMIN` defaults to `False` in production (`config/settings/prod.py`) — the stock `/admin/` is not mounted; the application uses its own role-gated console at `/ops/` instead. Set `DJANGO_ENABLE_ADMIN=1` only as a deliberate break-glass measure. |
| Secure cookies | `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE` default to `True`; sessions are `HttpOnly` with `SameSite=Lax`. The language cookie (`forca_language`, not Django's stock `django_language`) lasts a year and is `HttpOnly` with `SameSite=Lax` — Django leaves it script-readable by default, but nothing in the front end reads it, so `LANGUAGE_COOKIE_HTTPONLY = True` closes that XSS read/write path. Its Secure flag comes from `DJANGO_LANGUAGE_COOKIE_SECURE`, defaulting to whatever `SESSION_COOKIE_SECURE` is, so it is Secure in production too. |
| Session lifetime bounds | A 12-hour sliding idle timeout (`DJANGO_SESSION_COOKIE_AGE`) with `SESSION_SAVE_EVERY_REQUEST = True`, bounding the replay window of a stolen session cookie. |
| HTTPS / HSTS | `SECURE_SSL_REDIRECT` defaults `True`; `SECURE_HSTS_SECONDS` defaults to one year with `includeSubDomains` and `preload`. |
| Clickjacking protection | `X_FRAME_OPTIONS = "DENY"`, reinforced at the nginx edge (see below). |
| Content-type sniffing protection | `SECURE_CONTENT_TYPE_NOSNIFF = True`, reinforced at the nginx edge. |
| CSRF trusted origins | Derived automatically from `DJANGO_ALLOWED_HOSTS` as `https://<host>` unless explicitly overridden. |
| Role-based access control | Ordered role tiers with least-privilege lateral capabilities; granting the Director role requires a second director's approval (dual control) — see [Permissions and Roles](../permissions-and-roles.md). |
| Encrypted tokens at rest | OAuth refresh tokens and integration credentials are encrypted with Fernet, keyed by `TOKEN_ENCRYPTION_KEY` — see [Data and Privacy](../data-and-privacy.md#how-authentication-tokens-are-handled). |
| SSRF guards | Outbound ESI, LLM, and messaging-provider hosts are validated against explicit allowlists at startup — see [Third-Party Services § SSRF protection](../third-party-services.md#ssrf-protection-for-outbound-calls). |
| Hardened XML parsing | EVE-client fitting XML imports use `defusedxml`, not the standard library's XML parser. |

## Network and edge

| Control | Detail |
|---|---|
| Firewall scope | `deploy/deploy-ubuntu-26.04.sh` configures `ufw` to deny all inbound traffic by default and explicitly allow only SSH, 80, and 443. |
| Bare-IP requests dropped | `deploy/nginx/forca.prod.conf` returns `444` (connection closed, no response) for any request whose `Host` header is a raw IP address, on both the HTTP and HTTPS server blocks — this prevents scanner/uptime-probe traffic from generating `DisallowedHost` noise and saves a round trip to Django. |
| Rate limiting | Separate `limit_req_zone` budgets for the login surface (5 r/s), the general app surface (30 r/s), and the EVE-image proxy (300 r/s, sized for legitimate image-dense page loads) — the login surface is throttled hardest since it's the brute-force-sensitive path. |
| Edge security headers | `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, and HSTS are set at the nginx layer as well as in Django, covering responses nginx serves directly (e.g. error pages, cached images). |
| TLS configuration | `ssl_protocols TLSv1.2 TLSv1.3` only; `ssl_prefer_server_ciphers on`. |
| Upstream TLS verification | The EVE-image proxy verifies CCP's upstream TLS certificate (`proxy_ssl_verify on` with the image-server CA bundle) rather than trusting it blindly, preventing an on-path attacker from injecting cached image content. |
| nginx version hidden | `server_tokens off` — the exact nginx version is not advertised in headers or error pages. |
| AI/SEO crawler containment | A `User-Agent` map plus a faceted-URL match blocks known AI-training/SEO crawlers from the killboard's effectively-infinite filter-query URL space (`429` with `Retry-After`), preventing them from pinning application/database resources; `robots.txt` asks compliant crawlers to avoid the same space. |

## Host hardening

| Control | Detail |
|---|---|
| `fail2ban` | Installed and enabled by the deploy script — mitigates brute-force attempts against exposed services (notably SSH). |
| `unattended-upgrades` | Installed and configured by the deploy script — the host applies security patches automatically. |
| Non-root service user | The application runs under a dedicated system user (`forca`) with no login shell (`/usr/sbin/nologin`), not as root, and is added only to the `docker` group. |
| Systemd-managed lifecycle | The `forca.service` unit starts the stack on boot via `docker compose up -d`, so the stack comes back after a host reboot without manual intervention. |

## Secrets

| Control | Detail |
|---|---|
| `.env` file permissions | The deploy script writes `.env` at mode `600`, owned by the `forca` user; never commit a filled-in `.env` (it is git-ignored). |
| Generated secrets | `DJANGO_SECRET_KEY`, `POSTGRES_PASSWORD`, `TOKEN_ENCRYPTION_KEY`, and `REDIS_PASSWORD` are generated with `openssl rand` at first install and never overwritten by a re-run. |
| No secrets in output | Every provided script (`backup.sh`, `restore.sh`, `create-admin.sh`, `cert-init.sh`, and the deploy script itself) is written to never print secret values. |
| `TOKEN_ENCRYPTION_KEY` handling | Encrypts OAuth refresh tokens and integration credentials at rest; losing it is a service disruption (forces re-authorization), not a silent compromise. **Recommended:** back it up separately from database backups — see [Backup and Restore](./backup-and-restore.md#token_encryption_key-backup). |
| Boot-time secret validation | Production settings refuse to start with the insecure development `DJANGO_SECRET_KEY` default, or with `TOKEN_ENCRYPTION_KEY`/`DJANGO_ALLOWED_HOSTS` unset — misconfiguration fails loudly at boot rather than running insecurely. |

## Dependency scanning

| Control | Detail |
|---|---|
| Daily automated scan (in-application) | `admin_audit.audit_dependencies` runs at 06:43 UTC via Celery Beat, running `pip-audit` against the packages **installed in the running container**, raising one idempotent Director-visible finding and relaying a *change* to leadership over Pingboard. This is the **authoritative** recurring control for a self-hosted instance, since production deploys don't go through GitHub. It was weekly until July 2026; a week is a long time both to carry a fresh CVE and — worse — to keep claiming one that has already been fixed. |
| Weekly automated scan (CI) | [`.github/workflows/security.yml`](../../.github/workflows/security.yml) runs the same `pip-audit` check on push to `main`, on pull requests touching requirements files, and on the same weekly schedule — a second look for code that is pushed to GitHub. |
| **Deploy-time gate on the built image** | All three controls above audit `requirements.txt` — they audit *intent*. Because that file carries **lower** bounds, it stays green against an image built weeks ago that never picked the fixed version up; a container here once served 24 Pillow CVEs while every requirements scan passed. `make deploy` and `scripts/update.sh` therefore run [`scripts/audit-image.sh`](../../scripts/audit-image.sh) immediately after the build and **before** any migration or container swap. It runs `pip-audit` with no `-r` flag *inside the image*, so it sees the versions actually installed, transitive packages the requirements file never names, and `pip` itself. A finding **aborts the deploy** while the old stack is still serving. |
| Overriding the deploy gate | `SKIP_DEPENDENCY_AUDIT=1 make deploy` skips it — intended only for an air-gapped host that cannot reach the advisory feed, or an emergency roll-forward where the outage outweighs the CVE. It logs a warning; re-run `make audit-image` and rebuild once the reason is gone. |
| Installed-package scan (CI) | The `pip-audit-image` job in the security workflow builds the image and runs the same no-`-r` audit, so a pull request that would produce a vulnerable image fails before it is merged. |
| Least-privilege CI | The security workflow requests only `contents: read` permission and disables credential persistence on checkout. |

## Container-image scanning (the OS-package layer)

Everything in the table above sees **Python distributions**. None of it can see an
operating-system package, and the worst vulnerability this deployment has ever carried was
exactly that: a CRITICAL in OpenSSL inside a frozen `nginx:1.27-alpine`, in the one
internet-facing, TLS-terminating container in the stack. Full detail — cadence, findings,
who acts — is in [Vulnerability Scanning](./vulnerability-scanning.md).

| Control | Detail |
|---|---|
| **Deploy-time gate on OS packages** | [`scripts/audit-image-os.sh`](../../scripts/audit-image-os.sh) runs `trivy` over every image `docker-compose.prod.yml` is about to start — the application image's Debian layer plus `nginx`, `postgres` and `redis` — immediately after the build and **before** any migration or container swap. A **fixable** HIGH/CRITICAL aborts the deploy while the old stack is still serving. Wired into `make deploy` and `scripts/update.sh`. |
| **Daily scan of what is actually running** | [`scripts/scan-running-images.sh`](../../scripts/scan-running-images.sh), on a systemd timer at 04:20 local, resolves the image IDs the **running containers** were started from (via the Docker daemon, not the compose file) and scans those. This is the only control that catches an image that was clean when deployed and has since gone bad — which is what happens when an upstream tag freezes and `docker pull` keeps returning the same stale build. Install with `make install-scan-timer`. |
| Findings reach a human | The scan hands its result to the application (`manage.py ingest_image_scan`), which raises one Director finding and relays a *change* over the existing Pingboard channels. It deliberately does not add a second notifier: a parallel broadcaster would double-report every finding, and a channel that double-reports gets muted. |
| Host-only, never socket-in-container | The scan runs on the **host**. `/var/run/docker.sock` is never mounted into `web`, `worker` or `beat` — socket access is root-equivalent on the host, so it would be a strictly worse vulnerability than any the scan could find. Findings flow host → app; the app never reaches up. |
| Gates cannot go permanently red | Only findings with a **released fix version** fail a build. The Debian base routinely carries a couple of dozen unfixable HIGH/CRITICAL advisories; failing on those daily would get the gate switched off, and a switched-off gate that still appears in the deploy log is worse than none. Unfixable findings are still scanned, reported and tracked. |
| Overriding the deploy gate | `SKIP_IMAGE_SCAN=1 make deploy`, with the same affirmative-value-only parsing as `SKIP_DEPENDENCY_AUDIT` — `0`, `no`, `false`, `off` all mean *do not skip*, and an unrecognised value aborts rather than guessing. |
| Suppressions must expire | Individual advisories can be suppressed in [`.github/trivyignore.yaml`](../../.github/trivyignore.yaml), where every entry is required to carry an `expired_at` date so a suppression rots loudly instead of becoming a permanent blind spot. |
| A fix clears its own finding | The last step of `make deploy` / `make update` re-runs both audits against what is now running, report-only. Without it, a release that fixes a CVE leaves the old finding open until the next scheduled scan — and an alarm still lit after the fix is what trains people to ignore the next real one. |
| Missing scanner is never "clean" | If `trivy` is absent the scripts refuse and exit non-zero rather than reporting a clean result: "the scanner is missing" and "nothing was found" are indistinguishable downstream, and a soft skip would manufacture the appearance of coverage. |

## Further reading

- [SECURITY.md](../../SECURITY.md) — the project's overall security posture and how to
  report a vulnerability.
- [Contributor security guidelines](../contributor-handbook/security-guidelines.md) —
  secure coding practices for anyone modifying the application itself.
- [Data and Privacy](../data-and-privacy.md) — what data is stored and how it is
  protected.
- [Permissions and Roles](../permissions-and-roles.md) — the full RBAC model and ESI
  scope catalogue.
