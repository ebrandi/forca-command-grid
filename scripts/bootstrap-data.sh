#!/usr/bin/env bash
# bootstrap-data.sh — load the EVE reference data a fresh instance needs.
#
# Idempotent: every importer is safe to re-run (they upsert and skip existing
# rows/files). Runs against the running stack via `docker compose exec web`.
#
# Stages (in order):
#   1. Static Data Export  — type/system/region/skill names (REQUIRED for a
#      working UI). Full SDE from Fuzzwork by default; --sample for the tiny
#      bundled fixture (dev/CI only).
#   2. Planetary Industry rulebook.
#   3. Type images mirror   — referenced-only by default (fast); --all-images
#      mirrors every published type (large + slow). Skip with --no-images
#      (nginx will proxy CCP on demand instead).
#   4. Jita prices          — optional first pass so ISK values show immediately
#      (the daily beat refreshes them thereafter).
#
# Usage:
#   scripts/bootstrap-data.sh [--sample] [--all-images] [--no-images] [--with-prices]
set -euo pipefail
cd "$(dirname "$0")/.."
. scripts/lib.sh

DC="$(compose_cmd)"
CF="$(prod_compose_file)"
require_cmd docker
[ -f "$CF" ] || die "Compose file not found: $CF"

SAMPLE=0; IMAGES=referenced; PRICES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --sample)       SAMPLE=1 ;;
    --all-images)   IMAGES=all ;;
    --no-images)    IMAGES=none ;;
    --with-prices)  PRICES=1 ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "Unknown argument: $1 (use --help)" ;;
  esac
  shift
done

web() { $DC -f "$CF" exec -T web python manage.py "$@"; }

# 1. SDE ---------------------------------------------------------------------
if [ "$SAMPLE" -eq 1 ]; then
  log "Loading the bundled SAMPLE SDE fixture (dev/CI only) ..."
  web load_sde
else
  log "Importing the full Static Data Export from Fuzzwork (large; can take minutes) ..."
  web import_sde_fuzzwork
fi
ok "SDE loaded."

# 1b. Dogma reference data ---------------------------------------------------
# Attributes/effects/ship-bonuses the Tocha's Lab fitting simulation evaluates.
# Idempotent and staged. The bundled sample covers the demo Rifter loadout; a full
# per-type dogma projection (from the SDE FSD) is a follow-on data-pipeline step —
# until then, ships outside the sample evaluate from their slot-count columns only.
log "Loading dogma reference data for the fitting simulation ..."
web load_dogma
ok "Dogma reference data loaded."

# 2. Planetary Industry ------------------------------------------------------
log "Loading Planetary Industry rulebook ..."
web load_pi_static
ok "PI rulebook loaded."

# 3. Type images -------------------------------------------------------------
case "$IMAGES" in
  referenced)
    log "Mirroring referenced type images (icons/renders seen on kills+doctrines) ..."
    web mirror_type_images --referenced-only
    ok "Referenced images mirrored (nginx proxies anything else on demand)." ;;
  all)
    log "Mirroring ALL published type images (large + slow; tens of thousands of files) ..."
    web mirror_type_images
    ok "All type images mirrored." ;;
  none)
    log "Skipping image mirror — nginx will proxy CCP's image server on demand." ;;
esac

# 4. Prices (optional) -------------------------------------------------------
if [ "$PRICES" -eq 1 ]; then
  log "Pricing referenced types from Jita (first pass; the daily beat refreshes) ..."
  web price_types || warn "price_types failed; the scheduled beat will populate prices later."
  ok "Initial prices loaded."
fi

ok "Data bootstrap complete. Run 'make health' to verify."
