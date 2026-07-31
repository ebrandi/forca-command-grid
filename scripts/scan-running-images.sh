#!/usr/bin/env bash
# scan-running-images.sh — scan the container images that are ACTUALLY RUNNING right now
# for OS-package vulnerabilities, and hand the result to a human.
#
# WHY "ACTUALLY RUNNING" IS THE WHOLE POINT:
#   CI scans an image it just built. The deploy gate scans an image it is about to start.
#   Neither says anything about the containers that have been serving traffic for six
#   weeks — and that is where the risk lives, because an image goes bad without anyone
#   touching this repository. nginx:1.27-alpine froze upstream: the same April-2025 build
#   forever, accumulating 37 HIGH/CRITICAL including a CRITICAL in OpenSSL, in the one
#   container that terminates TLS. Nothing changed here, so nothing rescanned, so nobody
#   knew. This script resolves its targets from the DAEMON — the image IDs the running
#   containers were actually started from — not from the compose file and not from a
#   fresh build, precisely so it audits reality rather than intent.
#
# WHY IT RUNS ON THE HOST:
#   Scanning images requires talking to the Docker daemon. Mounting /var/run/docker.sock
#   into the web or worker container would hand every code-execution foothold in the
#   application root on the host — a strictly worse vulnerability than any this script
#   could ever find. So the flow is one-directional: this runs on the host, and it hands
#   its findings DOWN to the app. The app never reaches up.
#
# WHY IT TALKS TO THE APP INSTEAD OF SENDING ITS OWN ALERT:
#   The app already has a notification fabric that reaches directors over Pingboard
#   (in-app, EVE-mail, Telegram/Discord DMs) with dedup, an on/off switch, and a director
#   finding that closes itself when a scan comes back clean. A second, script-local
#   notifier would double-report every finding and get the channel muted — which is the
#   failure this workstream exists to correct, not repeat. So the result is piped into a
#   management command and the app decides who hears about it.
#
#   The findings are ALSO printed here, always. If the app is down, or the ingest command
#   is missing, the operator's copy is the console output — captured by systemd's journal
#   and mailed by OnFailure. A finding that exists only inside a delivery mechanism is one
#   outage away from being a finding nobody has.
#
# EXIT CODES (systemd and the deploy both key off these):
#   0  scanned everything, no fixable HIGH/CRITICAL
#   1  scanned everything, fixable HIGH/CRITICAL found  → someone has work to do
#   2  could NOT complete (trivy missing, an image no longer scannable, delivery failed)
#      → the control itself is broken; "we do not know" outranks "we found something",
#        because it is the state that otherwise gets silently filed as clean.
#
# Usage:
#   scripts/scan-running-images.sh [--trigger scheduled|deploy|manual] [--exit-zero]
#                                  [--no-report] [--dry-run] [--json PATH]
#
#   make scan-images                 ad hoc
#   systemd timer                    daily — see scripts/systemd/ and
#                                    handbooks/operator-handbook/vulnerability-scanning.md
set -euo pipefail
# Resolve our own path BEFORE cd'ing, so --help still works when invoked by a relative
# path from another directory (systemd sets WorkingDirectory, an operator may not).
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
cd "$(dirname "$0")/.."
. scripts/lib.sh
. scripts/lib-trivy.sh

TRIGGER="manual"
EXIT_ZERO=0
REPORT=1
DRY_RUN=0
JSON_OUT=""

usage() {
  sed -n '/^# Usage:/,/^set -euo/p' "$SELF" | sed 's/^# \{0,1\}//; $d'
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --trigger)
      [ $# -ge 2 ] || die "--trigger needs a value (scheduled|deploy|manual)."
      case "$2" in
        scheduled|deploy|manual) TRIGGER="$2" ;;
        *) die "--trigger must be one of: scheduled, deploy, manual (got '$2')." ;;
      esac
      shift 2 ;;
    # Report only: used at the end of a deploy, where failing on an advisory disclosed
    # since the image was built helps nobody — the deploy has already happened. The
    # findings still print and still reach the app.
    --exit-zero)  EXIT_ZERO=1; shift ;;
    # Do not hand the result to the app (ad hoc use, or a host whose stack is down).
    --no-report)  REPORT=0; shift ;;
    # Resolve and print the targets without scanning — "what would you look at?".
    --dry-run)    DRY_RUN=1; shift ;;
    --json)
      [ $# -ge 2 ] || die "--json needs a path."
      JSON_OUT="$2"; shift 2 ;;
    -h|--help)    usage 0 ;;
    *) warn "Unknown argument: $1"; usage 1 ;;
  esac
done

require_cmd docker
require_cmd python3 "the scan reporter is a stdlib-only Python 3 script"
DC="$(compose_cmd)"
CF="$(prod_compose_file)"

# The Compose PROJECT name is the only thing read from the compose file, and only to know
# which containers on this host are ours. Everything about *what is in* those containers
# comes from the daemon. FORCA_PROJECT overrides for a non-standard install.
PROJECT="${FORCA_PROJECT:-${COMPOSE_PROJECT_NAME:-}}"
if [ -z "$PROJECT" ] && [ -f "$CF" ]; then
  PROJECT="$(awk '/^name:[[:space:]]*[^[:space:]]/ {print $2; exit}' "$CF")"
fi
PROJECT="${PROJECT:-$(basename "$PWD")}"

# The management command that ingests the result. Overridable because this script and the
# app-side command ship from different lanes; if the command is ever renamed, an operator
# can point at the new name without editing a security control.
INGEST_COMMAND="${FORCA_IMAGE_SCAN_COMMAND:-ingest_image_scan}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
TSV="$WORK/targets.tsv"
: >"$TSV"

log "Resolving the images of the RUNNING containers in project '${PROJECT}' ..."
CONTAINERS="$(docker ps --filter "label=com.docker.compose.project=${PROJECT}" --format '{{.ID}}' || true)"
if [ -z "$CONTAINERS" ]; then
  die "No running containers found for compose project '${PROJECT}'.
       This is NOT a clean scan — nothing was examined. Either the stack is down
       ('make ps'), or the project name differs on this host (set FORCA_PROJECT)."
fi

# Group containers by the image ID they are actually running. web/worker/beat share one
# image; scanning it once and listing three services is both faster and honest — three
# copies of the same finding would read as three problems.
declare -A IMG_NAME=() IMG_SERVICES=() IMG_CONTAINERS=()
ORDER=()
for cid in $CONTAINERS; do
  # .Image is the image ID the container was created from — the bytes it is running.
  # .Config.Image is the reference it was started from, kept only for human readability:
  # a tag can be moved to a completely different image after the container starts, so it
  # is a label, never the scan target.
  read -r name image_id config_image service <<<"$(docker inspect --format \
    '{{.Name}} {{.Image}} {{.Config.Image}} {{index .Config.Labels "com.docker.compose.service"}}' \
    "$cid" 2>/dev/null || true)"
  [ -n "${image_id:-}" ] || { warn "Could not inspect container ${cid} — skipping."; continue; }
  name="${name#/}"
  case " ${ORDER[*]-} " in
    *" $image_id "*) ;;
    *) ORDER+=("$image_id"); IMG_NAME["$image_id"]="${config_image:-$image_id}" ;;
  esac
  IMG_SERVICES["$image_id"]="${IMG_SERVICES[$image_id]:+${IMG_SERVICES[$image_id]},}${service:-?}"
  IMG_CONTAINERS["$image_id"]="${IMG_CONTAINERS[$image_id]:+${IMG_CONTAINERS[$image_id]},}${name}"
done

[ "${#ORDER[@]}" -gt 0 ] || die "Resolved no images from the running containers — nothing was scanned."

if [ "$DRY_RUN" -eq 1 ]; then
  for id in "${ORDER[@]}"; do
    printf '  %-28s %s\n     containers: %s\n     image id:   %s\n' \
      "${IMG_SERVICES[$id]}" "${IMG_NAME[$id]}" "${IMG_CONTAINERS[$id]}" "$id"
  done
  ok "${#ORDER[@]} distinct image(s) would be scanned (severity ${TRIVY_SEVERITY}, packages ${TRIVY_PKG_TYPES})."
  exit 0
fi

require_trivy

i=0
for id in "${ORDER[@]}"; do
  i=$((i + 1))
  ref="${IMG_NAME[$id]}"
  log "Scanning ${IMG_SERVICES[$id]} — ${ref} ..."

  # A running container can outlive its own image: a rebuild moves the tag, then
  # `docker image prune` reaps the now-untagged original while the container keeps
  # serving happily from its already-unpacked filesystem. The honest answer then is "we
  # cannot tell", recorded per-container. Falling back to scanning the TAG would scan a
  # DIFFERENT image and present the answer as if it described this container — the exact
  # intent-versus-reality lie this control exists to catch.
  if ! docker image inspect "$id" >/dev/null 2>&1; then
    scan_targets_row "$TSV" "$id" "$ref" "$id" "${IMG_SERVICES[$id]}" "${IMG_CONTAINERS[$id]}" "" \
      "the running image is no longer in the local image store (rebuilt + pruned?), so what this container is running cannot be scanned; redeploy to replace it with a scannable image"
    warn "UNSCANNABLE: ${IMG_SERVICES[$id]} runs image ${id} which no longer exists locally."
    continue
  fi

  if trivy_scan_image "$id" "$WORK/${i}.json" "$WORK/${i}.err"; then
    scan_targets_row "$TSV" "$id" "$ref" "$id" "${IMG_SERVICES[$id]}" "${IMG_CONTAINERS[$id]}" "${i}.json" ""
  else
    scan_targets_row "$TSV" "$id" "$ref" "$id" "${IMG_SERVICES[$id]}" "${IMG_CONTAINERS[$id]}" "" \
      "trivy failed: $(tail -n 3 "$WORK/${i}.err" 2>/dev/null || echo 'no error output')"
    warn "Scan FAILED for ${IMG_SERVICES[$id]} (${ref}) — see the message above; not treating it as clean."
  fi
done

RC=0
scan_targets_report "$TSV" "$WORK" running-containers "$TRIGGER" || RC=$?

if [ -n "$JSON_OUT" ]; then
  cp "$WORK/scan.json" "$JSON_OUT"
  log "Scan document written to ${JSON_OUT}"
fi

# --- hand the result to the app ------------------------------------------------------
# Best-effort in the sense that a delivery fault never destroys the scan (the findings are
# already on the console), but NOT silent: a scan nobody receives is the defect we are
# fixing, so a failed hand-off is a non-zero exit in its own right.
if [ "$REPORT" -eq 1 ]; then
  if ! docker ps --filter "label=com.docker.compose.project=${PROJECT}" \
        --filter "label=com.docker.compose.service=web" --format '{{.ID}}' | grep -q .; then
    warn "The 'web' container is not running — cannot deliver the scan result to the app."
    warn "The findings above are the only record of this run."
    RC=2
  # --exit-zero because this branch is asking one question only: did the hand-off work?
  # The command's own non-zero exit means "there are fixable findings", which this script
  # has already decided for itself and encoded in RC. Without the flag a perfectly
  # successful delivery of a findings document would be read as a delivery FAILURE —
  # printing a warning about a problem that did not happen, and overwriting RC=1 (real
  # findings, act on them) with RC=2 (the control is broken, trust nothing). That inverts
  # the two states an operator most needs to tell apart.
  elif $DC -f "$CF" exec -T web python manage.py "$INGEST_COMMAND" --trigger "$TRIGGER" \
         --exit-zero <"$WORK/scan.json"; then
    ok "Scan result delivered to the app (manage.py ${INGEST_COMMAND})."
  else
    warn "Could not deliver the scan result to the app via 'manage.py ${INGEST_COMMAND}'."
    warn "If that command does not exist yet, set FORCA_IMAGE_SCAN_COMMAND to its real name."
    warn "The findings above stand — they simply did not reach the on-site surface."
    RC=2
  fi
fi

if [ "$EXIT_ZERO" -eq 1 ]; then
  [ "$RC" -eq 0 ] || warn "Exiting 0 as requested (--exit-zero); the result above is unchanged."
  exit 0
fi
exit "$RC"
