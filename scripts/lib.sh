#!/usr/bin/env bash
# Shared helpers for FORCA Command Grid deployment scripts.
# Source this from other scripts: `. "$(dirname "$0")/lib.sh"`
#
# Provides: strict-mode-friendly logging, command/env validation, and a
# resolver for the docker compose invocation + production compose file.
# Never prints secret values.

# Colours only when stdout is a TTY (keeps CI/pipe logs clean).
if [ -t 1 ]; then
  _C_INFO=$'\033[1;36m'; _C_OK=$'\033[1;32m'; _C_WARN=$'\033[1;33m'; _C_ERR=$'\033[1;31m'; _C_OFF=$'\033[0m'
else
  _C_INFO=''; _C_OK=''; _C_WARN=''; _C_ERR=''; _C_OFF=''
fi

log()  { printf '%s[forca]%s %s\n' "$_C_INFO" "$_C_OFF" "$*"; }
ok()   { printf '%s[ ok ]%s %s\n'  "$_C_OK"   "$_C_OFF" "$*"; }
warn() { printf '%s[warn]%s %s\n'  "$_C_WARN" "$_C_OFF" "$*" >&2; }
die()  { printf '%s[fail]%s %s\n'  "$_C_ERR"  "$_C_OFF" "$*" >&2; exit 1; }

# require_cmd <name> [hint] — fail clearly if a command is missing.
require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: '$1'${2:+ — $2}"
}

# require_env <VAR> — fail if an env var is unset/empty (value never printed).
require_env() {
  local name="$1"
  local val="${!name:-}"
  [ -n "$val" ] || die "Required environment variable is unset: $name"
}

# skip_gate <VAR_NAME> — did an operator explicitly ask to skip a security gate?
# Returns 0 (skip), 1 (do not skip), and dies on anything it cannot read as either.
#
# ONE authority for this decision, because getting it wrong is silent and has already
# happened here: the image audit's escape hatch was once `[ -n "$SKIP_DEPENDENCY_AUDIT" ]`,
# so an operator who typed SKIP_DEPENDENCY_AUDIT=0 meaning "no, do not skip" turned the
# gate OFF — the shell calls "0" a non-empty string. A control that disables itself when
# you tell it not to is worse than no control, because the deploy log still says the gate
# ran. Every skip flag in scripts/ goes through here so that bug has one place to not be.
#
# Refusing to guess on an unrecognised value is the same principle: "maybe" must not
# resolve to "off" for a security gate.
skip_gate() {
  local name="$1"
  case "$(printf '%s' "${!name:-}" | tr '[:upper:]' '[:lower:]')" in
    ""|0|no|false|off) return 1 ;;
    1|yes|true|on)     return 0 ;;
    *) die "$name is set to '${!name}', which is neither on nor off. Refusing to guess
     whether you meant to disable a security gate — set it to 1 to skip, or unset it." ;;
  esac
}

# compose_cmd — echo the docker compose invocation ("docker compose" or the
# legacy "docker-compose"), or die if neither is present.
compose_cmd() {
  if docker compose version >/dev/null 2>&1; then
    printf 'docker compose'
  elif command -v docker-compose >/dev/null 2>&1; then
    printf 'docker-compose'
  else
    die "Docker Compose not found (need Docker Engine + the compose plugin)."
  fi
}

# prod_compose_file — resolve the production compose file relative to the repo
# root (parent of scripts/). Honour COMPOSE_FILE if the caller set it.
prod_compose_file() {
  if [ -n "${COMPOSE_FILE:-}" ]; then printf '%s' "$COMPOSE_FILE"; return; fi
  local root; root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  printf '%s/docker-compose.prod.yml' "$root"
}
