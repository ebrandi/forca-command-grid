#!/usr/bin/env bash
# check-frontend-build.sh — prove the committed front-end assets are what the current
# sources actually compile to, and fail if they are not.
#
# WHY THIS EXISTS:
#   static/css/app.css is a PREBUILT artifact. Production has no Node step — the deploy
#   pipeline builds a Python image, runs collectstatic, and ships whatever bytes are
#   committed under static/. So a template that starts using a Tailwind class the
#   committed stylesheet never compiled gets NO styling in production, silently: no
#   error, no warning, no failing test. That has already happened here — four days of
#   dead styles shipped before anyone noticed the wheel was unstyled.
#
#   Nothing else in the tree catches it. The Dockerfile does not run Tailwind, CI did
#   not, the Makefile did not, and `collectstatic` is perfectly happy to collect a
#   stale stylesheet. This script is that missing gate.
#
# WHAT IT CHECKS:
#   `npm run build` (vendor + Tailwind) is re-run from the current sources, and the
#   result is compared byte-for-byte against the assets already in the tree:
#     * static/css/app.css        — recompiled whenever any template's class list changes
#     * static/js/vendor/*.js     — recopied whenever package.json bumps a pinned library
#   Both are committed build outputs with the same failure mode, so both are checked.
#
# REPRODUCIBILITY (why this does not flap):
#   package.json pins tailwindcss and all four runtime libraries to EXACT versions, and
#   Tailwind 3.x vendors its own PostCSS/cssnano toolchain under tailwindcss/peers, so
#   the emitted CSS is a function of the pinned Tailwind version alone — not of whatever
#   transitive tree npm happened to resolve. A fresh `npm install` and the developer's
#   months-old node_modules produce identical bytes (verified both ways).
#
#   frontend/package-lock.json is COMMITTED, so the install below is `npm ci`: the exact
#   tree, transitive packages included, pinned by digest. That is what makes a failure
#   here unambiguous — it can only mean someone changed a template or package.json and
#   did not rebuild. Without the lockfile a failure had two possible meanings ("forgot to
#   rebuild" or "npm resolved a different transitive tree today"), and an ambiguous gate
#   is one people mute. Verified before committing it: a clean `npm ci` reproduces all
#   five artifacts byte-for-byte.
#
#   The `npm install` fallback below survives only for the case where the lockfile is
#   somehow missing; it should never be taken in CI.
#
# ON FAILURE the rebuilt assets are LEFT IN PLACE: they are the correct content, so the
# fix is simply to review and commit them. This is deliberate — a check that reverted
# its own answer would make you run the build a second time by hand.
#
# Usage: scripts/check-frontend-build.sh    (called by `make frontend-check` and CI)
set -euo pipefail
cd "$(dirname "$0")/.."
. scripts/lib.sh

require_cmd node "install Node.js 20+ (the build tooling lives in frontend/)"
require_cmd npm  "install Node.js 20+ (the build tooling lives in frontend/)"
require_cmd cmp

# Every artifact `npm run build` writes, relative to the repo root.
ASSETS=(
  static/css/app.css
  static/js/vendor/alpine.min.js
  static/js/vendor/htmx.min.js
  static/js/vendor/chart.umd.js
  static/js/vendor/svg-pan-zoom.min.js
)

SNAPSHOT="$(mktemp -d)"
trap 'rm -rf "$SNAPSHOT"' EXIT

for asset in "${ASSETS[@]}"; do
  [ -f "$asset" ] || die "Missing committed build output: $asset"
  mkdir -p "$SNAPSHOT/$(dirname "$asset")"
  cp "$asset" "$SNAPSHOT/$asset"
done

log "Rebuilding the front-end assets from source (frontend/) ..."
# The lockfile is committed, so this is always the `npm ci` branch. The fallback exists
# only so a tree missing the lockfile degrades to a working (if less strict) check rather
# than a hard failure; taking it in CI means the lockfile went missing and the gate has
# quietly lost its reproducibility guarantee, so it warns loudly.
if [ -f frontend/package-lock.json ]; then
  ( cd frontend && npm ci --no-audit --no-fund )
else
  warn "frontend/package-lock.json is MISSING — it is committed and should always be here."
  warn "Falling back to 'npm install'; this check is no longer strictly reproducible."
  ( cd frontend && npm install --no-audit --no-fund )
fi
( cd frontend && npm run build )

STALE=()
for asset in "${ASSETS[@]}"; do
  cmp -s "$SNAPSHOT/$asset" "$asset" || STALE+=("$asset")
done

if [ ${#STALE[@]} -ne 0 ]; then
  for asset in "${STALE[@]}"; do
    warn "STALE: $asset does not match what the current sources compile to."
  done
  die "Committed front-end assets are stale — production would ship dead styles.
       The rebuilt files are already in your working tree: review and commit them
       (cd frontend && npm run build, then git add the files listed above)."
fi

ok "Front-end assets are in sync with their sources (${#ASSETS[@]} files checked)."
