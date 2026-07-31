# Upgrades

How to safely upgrade a running [FORCA] Command Grid installation to a newer version.

## Table of contents

- [The safe upgrade path](#the-safe-upgrade-path)
- [Re-running the deploy script](#re-running-the-deploy-script)
- [Migration procedure](#migration-procedure)
- [Rollback considerations](#rollback-considerations)

## The safe upgrade path

```bash
make update
```

This runs `scripts/update.sh`, which performs the following steps **in order**, aborting
on the first failure so a partial upgrade never leaves the stack in an unknown state:

| Step | Action |
|---|---|
| 1/10 | **Back up the database** (`scripts/backup.sh ./backups`) — aborts the entire upgrade if the backup fails |
| 2/10 | `git fetch --prune origin`, `git checkout <branch>`, then `git pull --ff-only origin <branch>` — **fast-forward only**; refuses to proceed if your checkout has diverged (e.g. local changes) rather than silently discarding anything |
| 3/10 | Stamp the new build revision (`deploy/stamp-version.sh`) so the footer reflects what's actually deployed |
| 4/10 | **Build** the new image (`docker compose -f docker-compose.prod.yml build`) — the running stack keeps serving throughout |
| 5/10 | **Audit the Python packages actually installed in the new image** (`scripts/audit-image.sh`) — `pip-audit` with no `-r` flag, run inside the image, so it sees the versions really installed rather than the lower bounds in `requirements.txt`. A finding **aborts the upgrade** here, before the first irreversible step; `SKIP_DEPENDENCY_AUDIT=1` overrides it (see below) |
| 6/10 | **Scan the OS packages of every image the upgrade will start** (`scripts/audit-image-os.sh`) — `trivy` over the application image's Debian layer *and* the pinned `nginx`, `postgres` and `redis` images, which step 5 is structurally blind to. A **fixable** HIGH/CRITICAL aborts the upgrade; `SKIP_IMAGE_SCAN=1` overrides it. See [Vulnerability Scanning](./vulnerability-scanning.md) |
| 7/10 | **Migrate** on the new image, in a one-off container (`run --rm --no-deps -T web python manage.py migrate --noinput`), while the old stack is still live |
| 8/10 | **`collectstatic`** on the new image, also in a one-off container — the static manifest must be in the shared volume *before* the new web container boots, or gunicorn starts without one and 500s |
| 9/10 | **Swap** the containers (`up -d`), wait for services (`scripts/wait-for-services.sh`), then **restart nginx last** — nginx caches the web container's IP and hands out 502s until it is restarted (skipped if this deployment has no `nginx` service) |
| 10/10 | **Re-audit what is now running**, report-only (`manage.py audit_dependencies --exit-zero --trigger deploy` and `scripts/scan-running-images.sh --trigger deploy --exit-zero`) — so an upgrade that fixes a CVE retires its own Director finding within minutes instead of leaving a stale alarm standing until the next scheduled scan |

The health check (`scripts/healthcheck.sh`) runs between steps 9 and 10 — a failure there
is reported as a warning, not aborted, since the upgrade steps themselves already
completed; investigate immediately if it fails.

The order matters, and it is not the obvious one. Building and swapping *before* migrating
would start the new code against the old schema, and every session-bearing request 500s
(`column ... does not exist`) for the whole migrate window — the site is down while the
upgrade "succeeds". Migrating on the new image while the old containers still serve shrinks
the window in which code and schema disagree to the container swap itself.

The rebuild in step 4/10 is also what ships a translation change: the message catalogues are
compiled into the image (`compilemessages` runs in the `Dockerfile`), so a catalogue edit
needs a rebuild rather than a container restart, and a malformed `.po` fails the build here
instead of silently falling back to English.

The audit in step 5/10 is deliberately placed between the build and the migration. Every
step before it is reversible by doing nothing at all; the migration is the first that is
not. It closes a real gap: `make audit`, the CI workflow and the weekly in-app scan all
audit `requirements.txt`, which carries **lower** bounds, so they stay green against an
image that was built before the fix and never rebuilt — that is how a container here came
to serve 24 Pillow CVEs under a passing scan. Auditing inside the built image instead
reports what will actually run, including transitive packages the requirements file never
names and `pip` itself. Run it on its own at any time with `make audit-image`.

If the host cannot reach the advisory feed (air-gapped), or you are rolling forward in an
emergency where the outage outweighs the finding, `SKIP_DEPENDENCY_AUDIT=1 make update`
skips the step and says so in the log. Re-run `make audit-image` and rebuild once the
reason has passed — the override is a deliberate decision, not a default. `SKIP_IMAGE_SCAN=1`
does the same for step 6/10. Both flags accept only an affirmative value: `SKIP_IMAGE_SCAN=0`
means *do not skip*, and an unrecognised value aborts rather than guessing — an earlier
version of this gate treated `=0` as "skip" and silently disabled itself.

Step 10/10 is the counterpart to those gates, and it exists because of a specific, real
failure. A Director finding on this deployment went on reporting 25 open Pillow
vulnerabilities for two days after the fix had shipped, because nothing rescanned until
the next weekly run. A control that keeps crying wolf after the wolf is dead is how people
are trained to ignore the next real alert, so an upgrade now refreshes both surfaces
against what it just started. Both are report-only: the upgrade has already happened, and
failing it at that point would help nobody.

The same rebuild ships **`Dockerfile` OS-package changes** — for example the `fonts-noto-cjk`
package the Combat Signatures renderer uses for Chinese/Japanese/Korean glyphs. A stale image
missing it renders CJK names and labels as tofu boxes (the renderer otherwise degrades
gracefully to DejaVu), so an upgrade that touches the `Dockerfile` must go through the rebuild,
not a bare container restart.

By default it upgrades the **currently checked-out branch**; pass a branch explicitly if
needed:

```bash
scripts/update.sh main
```

Because the backup happens first and unconditionally, `make update` is the recommended
way to upgrade in all cases — it is strictly safer than manually rebuilding and
migrating.

## Re-running the deploy script

`deploy/deploy-ubuntu-26.04.sh` is **idempotent**: re-running the exact command you used
for the initial install (see [Deployment](./deployment.md#path-a-the-one-shot-script))
performs an in-place upgrade — it fetches/checks out/fast-forwards the repository,
rebuilds, and re-runs migrations, **without** overwriting your existing `.env` or
regenerating secrets. This is a reasonable alternative to `make update` if you also want
to re-apply host-level provisioning (firewall rules, `fail2ban`, `unattended-upgrades`
configuration) at the same time, or if the systemd unit / cron job needs to be
re-installed.

Note that the script does **not** take an explicit pre-upgrade database backup the way
`scripts/update.sh` does — if you use this path for a routine upgrade, take a manual
`make backup` first, or rely on the nightly backup cron already in place from the
initial install.

## Migration procedure

Database schema migrations are applied automatically by both upgrade paths (`make update`
and the deploy script). `scripts/update.sh` runs them in a one-off container on the newly
built image, before the containers are swapped (step 5/7 above).

To apply migrations on their own at any time (e.g. after a manual `git pull` and
rebuild), against the *running* stack:

```bash
make migrate
```

`scripts/healthcheck.sh` (and therefore `make health`) checks for **unapplied
migrations** as one of its health checks — if an upgrade step is ever skipped, the next
health check will flag it rather than let the discrepancy go unnoticed.

## Rollback considerations

Use `scripts/rollback.sh`. Because `scripts/update.sh` always dumps the database as step
1/7 — before any code or schema change — the ingredients for a rollback exist for
**every** upgrade performed through `make update`.

```bash
# See what you can roll back to
git log --oneline -10
ls -1t ./backups/*.sql.gz

# Code only: fast, keeps data written since the upgrade
make rollback REF=v1.0.0

# Code + database: the only fully consistent option
make rollback REF=v1.0.0 DUMP=./backups/forca-20260709-031500.sql.gz
```

The script refuses to run with uncommitted changes, takes a fresh safety backup of the
**current** database first (so the rollback is itself reversible), records the revision
you came from in `.rollback-from`, rebuilds, and runs a health check. It leaves you on a
detached HEAD; `git checkout main` returns to the branch tip.

### Choosing whether to restore the database

Django does **not** un-apply migrations here. If the upgrade you are undoing added
migrations, the schema is newer than the code you are rolling back to.

| Situation | What to do |
| --- | --- |
| The upgrade added no migrations | `make rollback REF=...` — code only |
| It added only *additive* migrations (new table, new column) | **Not automatically safe.** Code only works if every added column is nullable or carries a *database* default — see below |
| It *altered* or *dropped* columns, or changed constraints | You must pass `DUMP=` — old code against the new schema will fail in ways that are hard to diagnose |

**"Additive" does not mean "safe".** Django enforces field defaults in Python, not in
PostgreSQL: `AddField(default=...)` emits `ADD COLUMN ... DEFAULT x NOT NULL` and then
immediately `ALTER COLUMN ... DROP DEFAULT`, so the column is left **NOT NULL with no
database default**. Roll the code back and every read still works and `/healthz` still
passes — while every INSERT from the older code, which does not know the column exists and
so never supplies it, dies on a not-null violation. The breakage is silent and delayed: it
lands on whatever writes first. Only `db_default=` (Django 5.0+) leaves a real default
behind in the database.

This is not hypothetical. `apps/identity/migrations/0004_user_language.py` adds
`User.language` as a `CharField(max_length=16, blank=True, default="")` — additive, no
`db_default`. Roll the code back past it without restoring the database and the site looks
entirely healthy, while a new pilot's first login cannot create a user row.

`scripts/rollback.sh` no longer asks you to make this judgement. Before a code-only
rollback it works out which migrations exist in the current tree but not in the revision
you are going back to, and runs `manage.py rollback_safety` on them. That command asks the
live database what those columns actually look like, and refuses the rollback if any added
column is NOT NULL with no database default, or if one of the migrations did something
outright destructive (`RemoveField`, `AlterField`, `DeleteModel`, and similar). It prints
the offending tables and your options. `make rollback REF=... DRIFT=1` overrides the
refusal, once you have read the list and accept that those tables become unwritable to the
rolled-back code. The check reads the schema through the running `web` container, so if the
stack is down the script stops and tells you it could not verify, rather than passing you
silently.

Restoring the database **discards every row written since that dump was taken** (new
killmails, SRP claims, ledger entries). That is the price of a self-consistent state.
When in doubt, restore: an inconsistent schema is worse than a few hours of lost sync
data, and the syncs will re-fetch from ESI.

### Doing it by hand

If you would rather not use the script, the same four steps are:

1. `scripts/backup.sh ./backups` — safety net for the rollback itself.
2. `git checkout <previous-ref>` and `deploy/stamp-version.sh .`
3. `docker compose -f docker-compose.prod.yml up -d --build`
4. `make restore FILE=./backups/<pre-upgrade-dump>.sql.gz`, then `make health`.

Skip the restore in step 4 only if you have positively established that the schema left
behind is writable by the old code: no column added since the target revision may be NOT
NULL without a database default. An additive `AddField` counts as the schema moving. By
hand you do not get the `rollback_safety` gate that `scripts/rollback.sh` runs for you, so
this is the step where the judgement is yours.
