#!/usr/bin/env bash
# audit-image-os.sh — scan the OS packages of every image this deploy is about to start,
# and refuse the deploy if any carry a FIXABLE HIGH/CRITICAL vulnerability.
#
# THE SIBLING, AND WHY BOTH EXIST:
#   scripts/audit-image.sh audits the PYTHON packages installed in the freshly built
#   application image. This audits the OPERATING SYSTEM packages of every image in the
#   stack — the application image's Debian layer plus the nginx, Postgres and Redis
#   images, which contain no Python of ours at all and which audit-image.sh is
#   structurally incapable of seeing.
#
#   That blind spot is not theoretical. The worst thing this install has ever shipped was
#   a CRITICAL in OpenSSL inside nginx:1.27-alpine: an OS package, in the container that
#   terminates TLS, reachable from the internet, invisible to every Python-package
#   scanner and to Dependabot alike (the tag was frozen upstream, so there was no newer
#   tag to propose — the same pin simply went bad).
#
# WHAT MAKES IT A GATE RATHER THAN A REPORT:
#   It runs after `build` and before anything is started or migrated, so a bad image is
#   caught while the previous stack is still serving and the cost of refusing is a deploy
#   that did not happen — not one that has to be rolled back.
#
# WHY IT DOES NOT GO PERMANENTLY RED (read before "tightening" this):
#   Only FIXABLE findings fail the build. The Debian base routinely carries a couple of
#   dozen HIGH/CRITICAL advisories with no released fix (perl, util-linux, code paths the
#   app never invokes). There is no action a human could take on those today, so failing
#   on them every day would get this gate deleted — and a deleted gate still looks like
#   coverage on the deploy log, which is worse than never having had one. Unfixable
#   findings are still scanned, still printed, and still reach the on-site surface via the
#   running-image scan; they simply are not a build failure. The scoping lives in
#   scripts/image_scan_report.py, and .github/trivyignore.yaml (expiry-dated entries only)
#   is the pressure valve for a fixable finding that genuinely cannot be actioned today.
#
# Escape hatch: SKIP_IMAGE_SCAN=1 — deliberate, documented, and affirmative-value-only
# (see skip_gate in scripts/lib.sh; SKIP_IMAGE_SCAN=0 does NOT disable the gate). It
# exists for an air-gapped host that cannot reach the vulnerability database, and for an
# emergency roll-forward where the outage is worse than the CVE.
#
# Usage: scripts/audit-image-os.sh    (called by `make audit-image-os`, `make deploy`
#                                      and scripts/update.sh)
set -euo pipefail
cd "$(dirname "$0")/.."
. scripts/lib.sh
. scripts/lib-trivy.sh

# The escape hatch is checked FIRST, before any prerequisite. An operator invoking the
# documented override on a host that lacks trivy (or docker, or a readable compose file)
# means "proceed without this gate" — making them satisfy the scanner's prerequisites in
# order to be allowed to skip the scanner would just teach them to comment the call out.
if skip_gate SKIP_IMAGE_SCAN; then
  warn "SKIP_IMAGE_SCAN is set — deploying WITHOUT scanning the images' OS packages."
  warn "The stack may start containers carrying known-vulnerable system libraries."
  warn "Run 'make audit-image-os' once the reason for skipping is gone."
  exit 0
fi

DC="$(compose_cmd)"
CF="$(prod_compose_file)"
require_cmd docker
require_cmd python3 "the scan reporter is a stdlib-only Python 3 script"
[ -f "$CF" ] || die "Compose file not found: $CF"

# Unlike the running-image scan, this one legitimately reads the compose file: the
# question here is "what is this deploy about to start?", and the compose file IS that
# answer. Reality is audited afterwards, by scan-running-images.sh.
mapfile -t IMAGES < <($DC -f "$CF" config --images 2>/dev/null | awk 'NF' | sort -u)
[ "${#IMAGES[@]}" -gt 0 ] || die "Could not resolve any images from ${CF}.
       ('$DC -f $CF config' failing usually means .env is missing or unreadable.)"

require_trivy

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
TSV="$WORK/targets.tsv"
: >"$TSV"

log "Scanning the OS packages of ${#IMAGES[@]} image(s) this deploy will start ..."
i=0
for ref in "${IMAGES[@]}"; do
  i=$((i + 1))
  log "  ${ref}"
  if trivy_scan_image "$ref" "$WORK/${i}.json" "$WORK/${i}.err"; then
    scan_targets_row "$TSV" "$ref" "$ref" "" "" "" "${i}.json" ""
  else
    # A failed scan is NOT a pass. The most likely causes are an unbuilt image or an
    # unreachable advisory database, and both mean we do not know what we are about to
    # start — which is exactly the state a gate must not wave through.
    scan_targets_row "$TSV" "$ref" "$ref" "" "" "" "" \
      "trivy failed: $(tail -n 3 "$WORK/${i}.err" 2>/dev/null || echo 'no error output')"
  fi
done

RC=0
scan_targets_report "$TSV" "$WORK" pending-images "${IMAGE_SCAN_TRIGGER:-deploy}" || RC=$?

case "$RC" in
  0) ok "No fixable HIGH/CRITICAL OS-package vulnerabilities in the images to be started." ;;
  1) die "One or more images carry FIXABLE HIGH/CRITICAL OS-package vulnerabilities (listed above).
       Fix by rebuilding on a refreshed base ('docker compose -f ${CF} build --pull') or by
       bumping the pinned image tag in ${CF} — a frozen upstream tag never gets rebuilt, so
       'no newer tag' is not the same as 'nothing to fix'.
       If this genuinely cannot be actioned today, add an EXPIRY-DATED entry to
       .github/trivyignore.yaml. If this is an emergency roll-forward, SKIP_IMAGE_SCAN=1
       overrides — deliberately." ;;
  *) die "The image scan could not complete (details above), so we do NOT know whether the
       images about to start are safe. That is not a pass. Re-run once the cause is
       resolved, or SKIP_IMAGE_SCAN=1 to proceed deliberately without the answer." ;;
esac
