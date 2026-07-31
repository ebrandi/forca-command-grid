#!/usr/bin/env bash
# audit-image.sh — scan the packages ACTUALLY INSTALLED in the built production image
# for known vulnerabilities, and fail if any are found.
#
# WHY THIS EXISTS, when `make audit` already runs pip-audit:
#   `pip-audit -r requirements.txt` audits the requirements FILE. It answers "would a
#   fresh install of these constraints be clean?" — it audits INTENT. It says nothing
#   about the image you are about to start, because requirements.txt carries lower
#   bounds: an image built weeks ago satisfies `Pillow>=12.3.0` with whatever it
#   resolved back then and keeps running it forever.
#
#   That gap has already bitten this install. A green `pip-audit -r` sat next to a
#   running container carrying 24 Pillow CVEs, because the requirements file had been
#   corrected and the image had never been rebuilt. Nothing in the deploy path noticed.
#
#   `pip-audit` with NO `-r` audits the environment it runs in — every distribution
#   importlib.metadata can see, at the exact version installed, including transitive
#   packages that requirements.txt never names and pip itself. Run inside the image
#   that is about to serve traffic, it audits REALITY.
#
# This is a GATE, not a report: a non-zero pip-audit exit aborts the caller. It runs
# after the image is built and before any container is swapped, so a vulnerable build
# is caught while the old stack is still happily serving.
#
# Escape hatch: SKIP_DEPENDENCY_AUDIT=1 skips the scan (and says so, loudly). It exists
# for the two cases where failing is wrong rather than right — an air-gapped host that
# cannot reach the OSV/PyPI advisory feeds, and an emergency roll-forward where the
# outage is worse than the CVE. Using it is a decision someone has to type out; the
# default is to refuse the deploy.
#
# Usage: scripts/audit-image.sh          (called by `make audit-image`, `make deploy`,
#                                         and scripts/update.sh)
set -euo pipefail
cd "$(dirname "$0")/.."
. scripts/lib.sh

DC="$(compose_cmd)"
CF="$(prod_compose_file)"
require_cmd docker
[ -f "$CF" ] || die "Compose file not found: $CF"

# Only an explicitly affirmative value skips. A bare -n test would treat
# SKIP_DEPENDENCY_AUDIT=0 — which an operator would reasonably type meaning "do NOT
# skip" — as a request to disable the gate, silently. A security control must not turn
# itself off because someone spelled "no" in a way the shell calls non-empty, so anything
# unrecognised aborts and asks rather than guessing either way. The parsing lives in
# lib.sh's skip_gate() so every gate in scripts/ shares it and none can regress alone.
if skip_gate SKIP_DEPENDENCY_AUDIT; then
  warn "SKIP_DEPENDENCY_AUDIT is set — deploying WITHOUT auditing the built image."
  warn "The image may carry known-vulnerable packages. Run 'make audit-image' once the"
  warn "reason for skipping is gone, and rebuild if it reports anything."
  exit 0
fi

log "Auditing the packages installed in the built image (not just requirements.txt) ..."
# --no-deps: postgres/redis are irrelevant here and must not be restarted mid-deploy.
# No `-r` flag: this audits the container's own site-packages.
$DC -f "$CF" run --rm --no-deps -T web pip-audit --progress-spinner off \
  || die "The built image contains known-vulnerable packages (see above).
       Fix the floor in requirements.txt, rebuild ('make build'), and re-run.
       If this is an emergency roll-forward, SKIP_DEPENDENCY_AUDIT=1 overrides — deliberately."

ok "No known vulnerabilities in the installed packages."
