#!/usr/bin/env bash
# install-image-scan-timer.sh — install (or remove) the daily running-image scan timer.
#
# WHY AN INSTALLER RATHER THAN "DROP THESE TWO FILES IN /etc/systemd/system":
#   The units need three things this repository cannot know: where the checkout lives,
#   which user may talk to the Docker daemon, and that user's group. Hand-editing those
#   into a unit file is the step people skip, and a scan that was never installed looks
#   exactly like a scan that finds nothing. So the substitution and the verification are
#   done here, once, with the failure modes checked up front.
#
# It also refuses to install a timer that cannot work: if the target user cannot reach
# the Docker socket, the scan would fail silently every night at 04:20 and the only
# evidence would be a red unit nobody looks at.
#
# Usage:
#   sudo bash scripts/install-image-scan-timer.sh [--user NAME] [--dir PATH]
#   sudo bash scripts/install-image-scan-timer.sh --uninstall
#
# Defaults: --user is the invoking (pre-sudo) user, --dir is this checkout.
set -euo pipefail
cd "$(dirname "$0")/.."
. scripts/lib.sh

UNIT_DIR="/etc/systemd/system"
SERVICE="forca-image-scan.service"
TIMER="forca-image-scan.timer"
TARGET_USER="${SUDO_USER:-$(id -un)}"
TARGET_DIR="$(pwd)"
UNINSTALL=0

while [ $# -gt 0 ]; do
  case "$1" in
    --user)      [ $# -ge 2 ] || die "--user needs a value."; TARGET_USER="$2"; shift 2 ;;
    --dir)       [ $# -ge 2 ] || die "--dir needs a value.";  TARGET_DIR="$2";  shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help)   sed -n '/^# Usage:/,/^# Defaults/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "Unknown argument: $1 (try --help)" ;;
  esac
done

require_cmd systemctl "this host does not use systemd — see the cron alternative in
       handbooks/operator-handbook/vulnerability-scanning.md"
[ "$(id -u)" -eq 0 ] || die "Installing a system unit needs root: re-run with sudo."

if [ "$UNINSTALL" -eq 1 ]; then
  systemctl disable --now "$TIMER" 2>/dev/null || true
  rm -f "${UNIT_DIR}/${TIMER}" "${UNIT_DIR}/${SERVICE}"
  systemctl daemon-reload
  ok "Removed ${TIMER} and ${SERVICE}."
  warn "The running containers are no longer scanned on a schedule. Nothing else covers
       the OS-package layer of what is actually running."
  exit 0
fi

id -u "$TARGET_USER" >/dev/null 2>&1 || die "No such user: ${TARGET_USER}"
TARGET_GROUP="$(id -gn "$TARGET_USER")"
[ -f "${TARGET_DIR}/scripts/scan-running-images.sh" ] \
  || die "No FORCA checkout at ${TARGET_DIR} (scripts/scan-running-images.sh is missing)."

# A timer that cannot reach the daemon fails every night into a log nobody reads.
# Establish that now, while a human is watching.
if ! runuser -u "$TARGET_USER" -- docker ps >/dev/null 2>&1; then
  die "User '${TARGET_USER}' cannot talk to the Docker daemon, so the scan would fail every
       night. Add them to the docker group ('sudo usermod -aG docker ${TARGET_USER}', then
       log out and back in) and re-run, or pass --user with an account that can."
fi

if ! runuser -u "$TARGET_USER" -- command -v trivy >/dev/null 2>&1; then
  # Not fatal — the scanner can be installed after the timer — but say it plainly, because
  # "installed the timer" must not be mistaken for "the images are being scanned".
  warn "trivy is not on ${TARGET_USER}'s PATH. The timer is being installed, but every run
       will fail (exit 2, 'not a clean result') until trivy is installed on this host."
fi

for unit in "$SERVICE" "$TIMER"; do
  sed -e "s|__FORCA_DIR__|${TARGET_DIR}|g" \
      -e "s|__FORCA_USER__|${TARGET_USER}|g" \
      -e "s|__FORCA_GROUP__|${TARGET_GROUP}|g" \
      "scripts/systemd/${unit}" >"${UNIT_DIR}/${unit}"
  chmod 0644 "${UNIT_DIR}/${unit}"
done

systemctl daemon-reload
systemctl enable --now "$TIMER"

ok "Installed ${SERVICE} + ${TIMER} (user ${TARGET_USER}, dir ${TARGET_DIR})."
log "Next run:      systemctl list-timers ${TIMER}"
log "Run it now:    sudo systemctl start ${SERVICE} && journalctl -u ${SERVICE} -n 60"
log "What it does:  handbooks/operator-handbook/vulnerability-scanning.md"
