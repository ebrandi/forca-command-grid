#!/usr/bin/env bash
# Shared container-image scanning helpers — ONE definition of what the image scan looks
# at and what counts as a finding.
#
# Two callers use it and they must not drift apart:
#   * scripts/audit-image-os.sh      — the deploy gate: the images about to be started
#   * scripts/scan-running-images.sh — the recurring scan: the images actually running
#
# If those two disagreed about severity floor or package scope, a deploy could pass a
# gate that the very next nightly scan fails on the same image, and nobody would trust
# either. So the knobs live here, in one place, and both scripts read them.
#
# WHY THIS LAYER EXISTS AT ALL, given pip-audit already runs:
#   pip-audit sees Python distributions and nothing else. The worst vulnerability this
#   install has ever carried was a CRITICAL in libcrypto3 (OpenSSL 3.3.3) inside
#   nginx:1.27-alpine — an OS package, in the one container that terminates TLS, in an
#   image the application code never even touches. No Python-package scanner can see it,
#   and no version bot could have proposed a fix either: that tag was frozen upstream, so
#   `docker pull` kept returning the same April-2025 build while it accumulated 37
#   HIGH/CRITICAL. Only something that reads the image's own package database can tell
#   you an unchanged pin has gone bad.
#
# Source it after scripts/lib.sh:
#   . scripts/lib.sh
#   . scripts/lib-trivy.sh

# --- scope of the gate -------------------------------------------------------------
# Every value is env-overridable so an operator can widen the scan ad hoc without editing
# a security control, but the defaults are the contract.

# HIGH and CRITICAL only. MEDIUM and below on a distro base image is a triage queue of
# dozens of entries that never reaches zero; putting it in a gate guarantees the gate is
# ignored, which costs more than the MEDIUMs do.
TRIVY_SEVERITY="${TRIVY_SEVERITY:-HIGH,CRITICAL}"

# OS packages only, and this is a deliberate division of labour rather than laziness.
# The Python layer of the application image is already audited by pip-audit *inside the
# running container* (scripts/audit-image.sh at deploy time, and the daily
# admin_audit.audit_dependencies task at rest), which owns its own director finding and
# its own notification lifecycle. Scanning Python here as well would raise every Python
# CVE twice, on two findings that clear at different times — and a surface that
# double-reports is how a channel gets muted. The image ships no node_modules
# (.dockerignore excludes it), so OS packages are exactly the uncovered layer.
TRIVY_PKG_TYPES="${TRIVY_PKG_TYPES:-os}"

# A cold vulnerability database can take minutes to pull on a small host. Better to wait
# than to fail with a timeout that reads like a finding.
TRIVY_TIMEOUT="${TRIVY_TIMEOUT:-15m}"

# Suppressions live with the CI scan so there is one list, not two. Every entry there is
# required to carry an expiry date, so a suppression rots loudly instead of becoming a
# permanent blind spot. Absent file = no suppressions, which is the correct default.
TRIVY_IGNOREFILE="${TRIVY_IGNOREFILE:-.github/trivyignore.yaml}"

# Deliberately NOT passing --ignore-unfixed. See scripts/image_scan_report.py: unfixable
# advisories are scanned and reported but never gate, so the gate stays actionable
# without the report going blind.

# --- trivy discovery ----------------------------------------------------------------

TRIVY_INSTALL_HINT='install it on the HOST (never inside a container — a scanner that
       needs the docker socket must not run in an app container):
         curl -sfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh \
           | sudo sh -s -- -b /usr/local/bin v0.70.0
       or see https://trivy.dev/latest/getting-started/installation/'

# trivy_available — 0 if trivy is on PATH, 1 otherwise. Never dies; for callers that
# must degrade rather than abort.
trivy_available() { command -v trivy >/dev/null 2>&1; }

# require_trivy — abort with an actionable message if trivy is missing.
#
# This refuses rather than warning-and-continuing on purpose. "Scanner absent" is the one
# failure mode that is indistinguishable from "nothing found" in every downstream
# surface, so a soft skip here would manufacture exactly the false sense of coverage this
# whole control exists to remove. It is also not a permanent red: unlike an upstream
# advisory with no fix, a missing binary is resolved once, in thirty seconds, by the
# command printed above.
require_trivy() {
  trivy_available || die "trivy is not installed, so the container images CANNOT be scanned.
       This is NOT a clean result — refusing to report one. $TRIVY_INSTALL_HINT"
}

# trivy_version — the scanner version, recorded on the scan document so a result can be
# judged against the tool that produced it.
trivy_version() {
  trivy --version 2>/dev/null | awk '/^Version:/ {print $2; exit}'
}

# --- scanning -----------------------------------------------------------------------

# trivy_scan_image <image-ref> <out.json> <err.log>
# Scan one image; 0 on success (report written), non-zero on failure (err.log holds why).
#
# Note the absence of --exit-code: trivy exits 0 whether or not it finds anything, so a
# non-zero return here always means the SCAN failed (image gone, DB unreachable, timeout)
# and never means "vulnerabilities found". Findings are decided from the JSON, one layer
# up, where fixable and unfixable can be told apart.
trivy_scan_image() {
  local ref="$1" out="$2" errlog="$3"
  local -a args=(
    image --quiet --format json
    --scanners vuln
    --pkg-types "$TRIVY_PKG_TYPES"
    --severity "$TRIVY_SEVERITY"
    --timeout "$TRIVY_TIMEOUT"
  )
  if [ -f "$TRIVY_IGNOREFILE" ]; then args+=(--ignorefile "$TRIVY_IGNOREFILE"); fi
  trivy "${args[@]}" "$ref" >"$out" 2>"$errlog"
}

# scan_targets_row <tsv-file> <key> <image> <image_id> <services> <containers> <report> <error>
# Append one target to the TSV the reporter reads.
#
# Every field is stripped of tabs and newlines first. That is not cosmetic: the error
# field carries trivy's own stderr, which is multi-line and quote-laden, and a delimiter
# smuggled in from scanner output would corrupt the row — most likely into something that
# parses as a target with no findings, i.e. a silent false all-clear.
scan_targets_row() {
  local tsv="$1"; shift
  local out="" field
  for field in "$@"; do
    out+="$(printf '%s' "$field" | tr '\t\n\r' '   ')"$'\t'
  done
  printf '%s\n' "${out%$'\t'}" >>"$tsv"
}

# scan_targets_report <tsv> <workdir> <source> <trigger> [extra args...]
# Run the reporter over a target list. Writes <workdir>/scan.json, prints the human
# rendering, and RETURNS the reporter's exit code (0 clean / 1 fixable findings /
# 2 incomplete) without tripping `set -e` in the caller.
scan_targets_report() {
  local tsv="$1" workdir="$2" source="$3" trigger="$4"; shift 4
  local rc=0
  python3 scripts/image_scan_report.py "$tsv" \
    --source "$source" --trigger "$trigger" \
    --host "$(hostname 2>/dev/null || echo unknown)" \
    --as-of "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --scanner-version "$(trivy_version)" \
    --severity-floor "$TRIVY_SEVERITY" \
    --pkg-types "$TRIVY_PKG_TYPES" \
    --json "$workdir/scan.json" --text - "$@" || rc=$?
  return $rc
}
