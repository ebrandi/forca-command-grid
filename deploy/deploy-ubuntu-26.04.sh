#!/usr/bin/env bash
#
# deploy-ubuntu-26.04.sh — Provision and deploy [FORCA] Command Grid on a fresh
# Ubuntu server (24.04 LTS or newer).
#
# Architecture (matches docker-compose.prod.yml): the ENTIRE stack runs in
# Docker Compose — nginx (TLS terminator on 80/443), web (gunicorn + Django),
# worker (Celery), beat (Celery Beat), postgres, redis. There is NO host nginx;
# nginx runs as a container and reads its cert from <app>/certs. TLS is issued by
# certbot in standalone mode with renewal hooks that manage the nginx container
# (see scripts/cert-init.sh).
#
# The script is idempotent: re-running it upgrades an existing install (pull,
# rebuild, migrate). It never prints secrets and exits non-zero on the first
# failed step.
#
# Minimum host: 2 vCPU, 4 GB RAM, 25 GB free disk (40 GB+ once the full SDE and the
# mirrored EVE images land). Checked by --preflight; override with --force.
#
# Usage:
#   # Secrets via the environment, so they never appear in `ps` or shell history:
#   export EVE_SSO_CLIENT_SECRET=...
#   sudo -E ./deploy-ubuntu-26.04.sh --domain grid.example.com \
#       --repo https://github.com/ebrandi/forca-command-grid.git \
#       --admin-email you@example.com --sso-client-id <id> \
#       --contact-email you@example.com --home-corp-id <corp_id>
#
# Flags (all have FORCA_*/EVE_* env equivalents; flags win):
#   --domain            Public FQDN that resolves to this server (required for TLS)
#   --repo              Git URL of the application repository (required)
#   --branch            Git branch/tag to deploy            (default: main)
#   --app-dir           Install directory                   (default: /opt/forca)
#   --admin-email       Email for Let's Encrypt registration
#   --sso-client-id     EVE SSO application client id
#   --sso-client-secret EVE SSO application client secret
#                       (prefer the EVE_SSO_CLIENT_SECRET env var — a value passed
#                        here is visible to every user on the box via `ps`)
#   --contact-email     Contact email embedded in the ESI User-Agent
#   --home-corp-id      Home corporation id (numeric)
#   --ssh-port          SSH port to keep open in the firewall (default: autodetect)
#   --no-tls            Skip certbot; serve a self-signed cert (staging only)
#   --skip-bootstrap    Do not import SDE/images on this run (do it later)
#   --skip-app          Provision the host only; do not build/run the app
#   --reset-firewall    Wipe existing ufw rules before applying ours (destructive)
#   --dry-run           Print what would be done, change nothing, exit 0
#   --force             Continue even if the preflight resource checks fail
#   -h, --help          Show this help
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults & argument parsing
# ---------------------------------------------------------------------------
APP_USER="forca"
APP_DIR="${FORCA_APP_DIR:-/opt/forca}"
BRANCH="${FORCA_BRANCH:-main}"
DOMAIN="${FORCA_DOMAIN:-}"
REPO_URL="${FORCA_REPO_URL:-}"
ADMIN_EMAIL="${FORCA_ADMIN_EMAIL:-}"
SSO_CLIENT_ID="${EVE_SSO_CLIENT_ID:-}"
SSO_CLIENT_SECRET="${EVE_SSO_CLIENT_SECRET:-}"
CONTACT_EMAIL="${FORCA_CONTACT_EMAIL:-}"
HOME_CORP_ID="${FORCA_HOME_CORP_ID:-}"
USE_TLS=1
SKIP_APP=0
SKIP_BOOTSTRAP=0
DRY_RUN=0
FORCE=0
RESET_FIREWALL=0
SSH_PORT="${FORCA_SSH_PORT:-}"
COMPOSE_FILE="docker-compose.prod.yml"

# Minimum host resources. The full SDE import plus the mirrored EVE image set is the
# dominant consumer of disk; RAM is dominated by Postgres + the Celery worker.
# MIN_DISK_GB is adjusted down after argument parsing when --skip-bootstrap is set.
MIN_CPU=2
MIN_RAM_MB=3500          # a "4 GB" VM reports ~3.8 GB usable
MIN_DISK_GB=25
MIN_DISK_GB_NO_SDE=12    # images + Docker layers only, no SDE/asset import

log()  { printf '\033[1;36m[deploy]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }
# In --dry-run, describe the action instead of performing it.
run()  { if [[ "${DRY_RUN}" -eq 1 ]]; then printf '\033[1;35m[dry-run]\033[0m %s\n' "$*"; else "$@"; fi; }

# Print the header comment block (everything between the shebang and `set -euo pipefail`)
# rather than a hardcoded line range, which silently rots when the header changes.
usage() { sed -n '2,/^set -euo pipefail$/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0; }

secret_from_flag=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain)            DOMAIN="$2"; shift 2 ;;
    --repo)              REPO_URL="$2"; shift 2 ;;
    --branch)            BRANCH="$2"; shift 2 ;;
    --app-dir)           APP_DIR="$2"; shift 2 ;;
    --admin-email)       ADMIN_EMAIL="$2"; shift 2 ;;
    --sso-client-id)     SSO_CLIENT_ID="$2"; shift 2 ;;
    --sso-client-secret) SSO_CLIENT_SECRET="$2"; secret_from_flag=1; shift 2 ;;
    --contact-email)     CONTACT_EMAIL="$2"; shift 2 ;;
    --home-corp-id)      HOME_CORP_ID="$2"; shift 2 ;;
    --ssh-port)          SSH_PORT="$2"; shift 2 ;;
    --no-tls)            USE_TLS=0; shift ;;
    --skip-bootstrap)    SKIP_BOOTSTRAP=1; shift ;;
    --skip-app)          SKIP_APP=1; shift ;;
    --reset-firewall)    RESET_FIREWALL=1; shift ;;
    --dry-run)           DRY_RUN=1; shift ;;
    --force)             FORCE=1; shift ;;
    -h|--help)           usage ;;
    *) die "Unknown argument: $1 (use --help)" ;;
  esac
done

if [[ "${secret_from_flag}" -eq 1 ]]; then
  warn "--sso-client-secret was passed on the command line: its value is visible in"
  warn "  \`ps\` output to every user on this host and is saved to your shell history."
  warn "  Prefer:  export EVE_SSO_CLIENT_SECRET=...  &&  sudo -E $0 ..."
fi

APP_SRC="${APP_DIR}/app"
ENV_FILE="${APP_SRC}/.env"
BACKUP_DIR="/var/backups/forca"
as_app() { sudo -u "${APP_USER}" "$@"; }

# ---------------------------------------------------------------------------
# 1. Pre-flight checks
# ---------------------------------------------------------------------------
log "Pre-flight checks"
[[ "${EUID}" -eq 0 ]] || die "Run as root (sudo)."
[[ -r /etc/os-release ]] || die "Cannot read /etc/os-release."
# shellcheck disable=SC1091
. /etc/os-release
[[ "${ID:-}" == "ubuntu" ]] || warn "This script targets Ubuntu (found '${ID:-unknown}'); continuing."
[[ -n "${REPO_URL}" ]] || die "--repo is required (the application git URL)."
if [[ "${USE_TLS}" -eq 1 ]]; then
  [[ -n "${DOMAIN}" ]] || die "--domain is required for TLS (or pass --no-tls)."
  [[ -n "${ADMIN_EMAIL}" ]] || die "--admin-email is required for Let's Encrypt (or pass --no-tls)."
fi

# --- Host resources ---------------------------------------------------------
# Fail here with a clear message rather than 20 minutes later with an OOM-killed
# Postgres or a full disk halfway through the SDE import.
preflight_failed=0
resource_check() { # <label> <actual> <minimum> <unit>
  if [[ "$2" -lt "$3" ]]; then
    warn "$1: ${2}${4} — below the ${3}${4} minimum."
    preflight_failed=1
  else
    log "  $1: ${2}${4} (>= ${3}${4})"
  fi
}
CPU_COUNT="$(nproc)"
RAM_MB="$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)"
# Disk on the filesystem holding APP_DIR (may not exist yet — walk up to an existing parent).
disk_target="${APP_DIR}"
while [[ ! -d "${disk_target}" && "${disk_target}" != "/" ]]; do disk_target="$(dirname "${disk_target}")"; done
DISK_GB="$(df -BG --output=avail "${disk_target}" | tail -1 | tr -dc '0-9')"

# Without the SDE/image bootstrap the footprint is just Docker layers + an empty DB.
disk_min="${MIN_DISK_GB}"
[[ "${SKIP_BOOTSTRAP}" -eq 1 ]] && disk_min="${MIN_DISK_GB_NO_SDE}"

resource_check "CPU cores"      "${CPU_COUNT}" "${MIN_CPU}"      ""
resource_check "RAM"            "${RAM_MB}"    "${MIN_RAM_MB}"   " MB"
resource_check "Free disk"      "${DISK_GB}"   "${disk_min}"     " GB"
[[ "${SKIP_BOOTSTRAP}" -eq 1 ]] && log "  (--skip-bootstrap: ${MIN_DISK_GB} GB will be needed once you run 'make bootstrap')"

if [[ "${RAM_MB}" -lt 8000 ]]; then
  log "  Note: on a host this size keep the compose defaults. Raise POSTGRES_SHARED_BUFFERS /"
  log "        POSTGRES_EFFECTIVE_CACHE_SIZE / *_MEM_LIMIT in .env only on a larger box."
fi
if [[ -z "$(swapon --show --noheadings 2>/dev/null)" && "${RAM_MB}" -lt 8000 ]]; then
  warn "No swap is configured on a <8 GB host: a memory spike will be an OOM kill, not a stall."
fi

# --- Network reachability ---------------------------------------------------
for url in https://download.docker.com https://pypi.org; do
  curl -fsS --max-time 10 -o /dev/null "$url" 2>/dev/null \
    || { warn "Cannot reach ${url} — the build will fail without outbound HTTPS."; preflight_failed=1; }
done

if [[ "${preflight_failed}" -eq 1 ]]; then
  if [[ "${FORCE}" -eq 1 ]]; then
    warn "Preflight checks failed but --force was given; continuing anyway."
  else
    die "Preflight checks failed (see above). Re-run with --force to override."
  fi
fi

# --- SSH port (so the firewall step cannot lock us out) ----------------------
if [[ -z "${SSH_PORT}" ]]; then
  # The port of the current SSH session, if any; else sshd_config; else 22.
  if [[ -n "${SSH_CONNECTION:-}" ]]; then
    SSH_PORT="$(awk '{print $4}' <<<"${SSH_CONNECTION}")"
  else
    SSH_PORT="$(awk '/^[[:space:]]*Port[[:space:]]+[0-9]+/ {print $2; exit}' /etc/ssh/sshd_config 2>/dev/null || true)"
  fi
  SSH_PORT="${SSH_PORT:-22}"
fi
[[ "${SSH_PORT}" =~ ^[0-9]+$ ]] || die "--ssh-port must be numeric (got '${SSH_PORT}')."
log "  SSH port to keep open: ${SSH_PORT}"

if [[ "${DRY_RUN}" -eq 1 ]]; then
  log "--dry-run: preflight passed. Would now:"
  log "  install base packages, Docker Engine + Compose plugin"
  log "  create service user '${APP_USER}' and ${APP_DIR}"
  log "  clone ${REPO_URL} (${BRANCH}) into ${APP_DIR}/app"
  log "  generate ${APP_DIR}/app/.env with fresh secrets (if absent)"
  log "  configure ufw (allow ${SSH_PORT}/tcp, 80, 443)$([[ ${RESET_FIREWALL} -eq 1 ]] && echo ', after RESETTING existing rules')"
  log "  build and start the compose stack, migrate, collectstatic"
  [[ "${SKIP_BOOTSTRAP}" -eq 0 ]] && log "  import the EVE SDE + referenced images"
  [[ -n "${DOMAIN}" && "${USE_TLS}" -eq 1 ]] && log "  obtain a Let's Encrypt certificate for ${DOMAIN}"
  log "  install forca.service and a nightly backup cron"
  log "No changes were made."
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------------------------
# 2. System update + base packages
# ---------------------------------------------------------------------------
log "Updating system and installing base packages"
apt-get update -y
apt-get upgrade -y
apt-get install -y \
  ca-certificates curl git gnupg lsb-release ufw fail2ban \
  unattended-upgrades openssl

# ---------------------------------------------------------------------------
# 3. Docker Engine + Compose plugin (official repo)
# ---------------------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  log "Installing Docker Engine + Compose plugin"
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${VERSION_CODENAME}") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
else
  log "Docker already installed: $(docker --version)"
fi

# ---------------------------------------------------------------------------
# 4. Service user + directories
# ---------------------------------------------------------------------------
if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  log "Creating service user '${APP_USER}'"
  useradd --system --create-home --shell /usr/sbin/nologin "${APP_USER}"
fi
usermod -aG docker "${APP_USER}"
mkdir -p "${APP_DIR}" "${BACKUP_DIR}"
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"

# ---------------------------------------------------------------------------
# 5. Clone or update the application repository
# ---------------------------------------------------------------------------
if [[ -d "${APP_SRC}/.git" ]]; then
  log "Updating existing checkout (${BRANCH})"
  as_app git -C "${APP_SRC}" fetch --all --prune
  as_app git -C "${APP_SRC}" checkout "${BRANCH}"
  as_app git -C "${APP_SRC}" pull --ff-only origin "${BRANCH}"
else
  log "Cloning ${REPO_URL} (${BRANCH})"
  as_app git clone --branch "${BRANCH}" "${REPO_URL}" "${APP_SRC}"
fi

cd "${APP_SRC}"
[[ -f "${COMPOSE_FILE}" ]] || die "Repository has no ${COMPOSE_FILE}. This script deploys the containerized stack; ensure you cloned the full app."
[[ -f "Dockerfile" ]] || die "Repository has no Dockerfile — cannot build the app image."

# ---------------------------------------------------------------------------
# 6. Provision .env (generate secrets once; never overwrite an existing file)
# ---------------------------------------------------------------------------
gen_secret()  { openssl rand -base64 "${1:-48}" | tr -d '\n'; }
# Fernet key = url-safe base64 of 32 bytes WITH '=' padding — keep the padding.
gen_urlsafe() { openssl rand -base64 "${1:-32}" | tr '+/' '-_' | tr -d '\n'; }

if [[ ! -f "${ENV_FILE}" ]]; then
  # CCP requires a contactable address in the ESI User-Agent, and config.settings.prod
  # refuses to boot with a placeholder. Fall back to the admin email before giving up.
  CONTACT_EMAIL="${CONTACT_EMAIL:-${ADMIN_EMAIL}}"
  [[ -n "${CONTACT_EMAIL}" ]] \
    || die "--contact-email (or --admin-email) is required: CCP's ESI policy needs a real contact address."

  log "Generating ${ENV_FILE} (first run)"
  SECRET_KEY="$(gen_secret 50)"
  DB_PASSWORD="$(gen_secret 24 | tr -d '/+=')"
  TOKEN_ENCRYPTION_KEY="$(gen_urlsafe 32)"
  REDIS_PASSWORD="$(gen_secret 24 | tr -d '/+=')"

  umask 077
  cat > "${ENV_FILE}" <<EOF
# [FORCA] Command Grid — production environment (generated by deploy-ubuntu-26.04.sh).
# Keep secret (mode 600). Regenerating TOKEN_ENCRYPTION_KEY invalidates stored tokens.

# --- Django ---
DJANGO_SETTINGS_MODULE=config.settings.prod
DJANGO_SECRET_KEY=${SECRET_KEY}
DJANGO_DEBUG=0
DJANGO_ALLOWED_HOSTS=${DOMAIN:-localhost},127.0.0.1,localhost
DJANGO_CSRF_TRUSTED_ORIGINS=https://${DOMAIN:-localhost}

# --- Database (PostgreSQL 16) ---
POSTGRES_DB=forca
POSTGRES_USER=forca
POSTGRES_PASSWORD=${DB_PASSWORD}
DATABASE_URL=postgres://forca:${DB_PASSWORD}@postgres:5432/forca

# --- Redis (cache + Celery broker) ---
REDIS_PASSWORD=${REDIS_PASSWORD}
REDIS_URL=redis://:${REDIS_PASSWORD}@redis:6379/0
CELERY_BROKER_URL=redis://:${REDIS_PASSWORD}@redis:6379/1

# --- Token encryption at rest (OAuth refresh tokens) ---
TOKEN_ENCRYPTION_KEY=${TOKEN_ENCRYPTION_KEY}

# --- EVE SSO / ESI ---
EVE_SSO_CLIENT_ID=${SSO_CLIENT_ID}
EVE_SSO_CLIENT_SECRET=${SSO_CLIENT_SECRET}
EVE_SSO_CALLBACK_URL=https://${DOMAIN:-localhost}/auth/eve/callback/
ESI_USER_AGENT=forca-command-grid/1.0 (${CONTACT_EMAIL})
ESI_COMPATIBILITY_DATE=2026-06-21
FORCA_HOME_CORP_ID=${HOME_CORP_ID}
FORCA_SITE_URL=https://${DOMAIN:-localhost}

# --- Optional integrations (see .env.example for the full documented list) ---
# RECRUITMENT_SSO_CLIENT_ID=      RECRUITMENT_SSO_CLIENT_SECRET=
# DISCORD_BOT_TOKEN=              DISCORD_OAUTH_CLIENT_ID=   DISCORD_OAUTH_CLIENT_SECRET=
# LLM_API_KEY=                    PINGBOARD_SLACK_BOT_TOKEN=
EOF
  chown "${APP_USER}:${APP_USER}" "${ENV_FILE}"
  chmod 600 "${ENV_FILE}"
  log ".env created with freshly generated secrets (mode 600)."
else
  log "Existing ${ENV_FILE} found — leaving secrets untouched."
fi

# ---------------------------------------------------------------------------
# 7. Firewall (UFW) + fail2ban + unattended upgrades
# ---------------------------------------------------------------------------
log "Configuring firewall (allow ${SSH_PORT}/tcp, 80, 443)"
# `ufw --force reset` wipes every rule the operator already has, on EVERY run of this
# "idempotent" script. Only do it when explicitly asked. Allow the detected SSH port
# BEFORE enabling the firewall, or a non-standard port locks the operator out.
if [[ "${RESET_FIREWALL}" -eq 1 ]]; then
  warn "--reset-firewall: discarding all existing ufw rules."
  ufw --force reset >/dev/null
fi
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow "${SSH_PORT}/tcp" >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null
systemctl enable --now fail2ban
dpkg-reconfigure -f noninteractive unattended-upgrades || true

if [[ "${SKIP_APP}" -eq 1 ]]; then
  log "--skip-app set: host provisioned; application not built. Re-run without it to deploy."
  exit 0
fi

# ---------------------------------------------------------------------------
# 8. Build & start the stack
# ---------------------------------------------------------------------------
# nginx's config unconditionally loads certs/forca.{crt,key}, so it cannot start
# without them. certbot only runs in step 10 (and never with --no-tls), which would
# leave nginx crash-looping in between — and permanently under --no-tls. Lay down a
# self-signed placeholder first: the stack always comes up, and cert-init.sh later
# overwrites it with the real Let's Encrypt certificate.
CERT_DIR="${APP_SRC}/certs"
if [[ ! -s "${CERT_DIR}/forca.crt" || ! -s "${CERT_DIR}/forca.key" ]]; then
  log "Generating a self-signed placeholder certificate (replaced by certbot when TLS is on)"
  install -d -o "${APP_USER}" -g "${APP_USER}" -m 0755 "${CERT_DIR}"
  openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
    -keyout "${CERT_DIR}/forca.key" -out "${CERT_DIR}/forca.crt" \
    -subj "/CN=${DOMAIN:-localhost}" \
    -addext "subjectAltName=DNS:${DOMAIN:-localhost}" >/dev/null 2>&1 \
    || die "Could not generate the placeholder certificate (is openssl installed?)."
  chown "${APP_USER}:${APP_USER}" "${CERT_DIR}/forca.crt" "${CERT_DIR}/forca.key"
  chmod 644 "${CERT_DIR}/forca.crt"; chmod 600 "${CERT_DIR}/forca.key"
  if [[ "${USE_TLS}" -eq 0 ]]; then
    warn "--no-tls: serving a SELF-SIGNED certificate. Browsers will warn. Replace"
    warn "         ${CERT_DIR}/forca.{crt,key} with a real pair for anything public."
  fi
fi

# Stamp the deployed commit so the footer shows exactly what's running (.git is
# excluded from the image, so materialise the hash into a file the build copies in).
if [[ -x "${APP_SRC}/deploy/stamp-version.sh" ]]; then
  as_app "${APP_SRC}/deploy/stamp-version.sh" "${APP_SRC}" \
    || warn "Could not stamp the build revision; the footer will hide it."
fi

log "Building and starting the Docker Compose stack"
as_app docker compose -f "${COMPOSE_FILE}" up -d --build

# ---------------------------------------------------------------------------
# 9. First-run application tasks
# ---------------------------------------------------------------------------
log "Waiting for services to become ready"
as_app bash scripts/wait-for-services.sh 240 || warn "Services slow to start; continuing (check 'docker compose ps')."

run_web() { as_app docker compose -f "${COMPOSE_FILE}" exec -T web "$@"; }
log "Applying migrations and collecting static assets"
run_web python manage.py migrate --noinput
run_web python manage.py collectstatic --noinput

if [[ "${SKIP_BOOTSTRAP}" -eq 0 ]]; then
  log "Loading EVE reference data (full SDE + PI + referenced images; can take several minutes)"
  as_app bash scripts/bootstrap-data.sh || warn "Data bootstrap hit an error; re-run 'make bootstrap' later."
else
  warn "--skip-bootstrap set: SDE/images NOT loaded. Run 'make bootstrap' before using the UI."
fi

# A Django superuser is only a break-glass account for the stock /admin, which is
# disabled unless DJANGO_ENABLE_ADMIN=1; normal users log in with EVE SSO. Creating
# one with `--noinput` and no DJANGO_SUPERUSER_PASSWORD produces an account with an
# UNUSABLE password — nobody can log into it, yet it carries is_superuser=True. Only
# create it when a password was actually supplied, and never echo that password.
if [[ -n "${ADMIN_EMAIL}" && -n "${DJANGO_SUPERUSER_PASSWORD:-}" ]]; then
  log "Ensuring a Django superuser exists (${ADMIN_EMAIL})"
  as_app docker compose -f "${COMPOSE_FILE}" exec -T \
    -e DJANGO_SUPERUSER_PASSWORD \
    -e DJANGO_SUPERUSER_EMAIL="${ADMIN_EMAIL}" \
    -e DJANGO_SUPERUSER_USERNAME=admin \
    web python manage.py createsuperuser --noinput >/dev/null 2>&1 \
    && log "Superuser 'admin' ensured." \
    || warn "Superuser already exists — no change."
elif [[ -n "${ADMIN_EMAIL}" ]]; then
  log "No DJANGO_SUPERUSER_PASSWORD set — skipping superuser creation (EVE SSO is the"
  log "  normal login path). To create a break-glass admin later:"
  log "    DJANGO_SUPERUSER_PASSWORD=... scripts/create-admin.sh ${ADMIN_EMAIL}"
fi

# ---------------------------------------------------------------------------
# 10. TLS via certbot standalone (manages the nginx CONTAINER; no host nginx)
# ---------------------------------------------------------------------------
if [[ -n "${DOMAIN}" && "${USE_TLS}" -eq 1 ]]; then
  log "Setting up TLS for ${DOMAIN} (certbot standalone + container renewal hooks)"
  COMPOSE_FILE="${COMPOSE_FILE}" bash "${APP_SRC}/scripts/cert-init.sh" "${DOMAIN}" "${ADMIN_EMAIL}" "${APP_SRC}" \
    || warn "TLS setup failed; check DNS points to this host and port 80 is reachable."
elif [[ -n "${DOMAIN}" ]]; then
  warn "--no-tls: nginx is serving the self-signed placeholder in ${APP_SRC}/certs."
  warn "         Replace forca.crt/forca.key with a real pair and 'make restart' before"
  warn "         exposing this host publicly. Note that nginx answers 444 to requests whose"
  warn "         Host header is a bare IP address, so use a hostname even for staging."
fi

# ---------------------------------------------------------------------------
# 11. systemd unit (start stack on boot) + nightly backups
# ---------------------------------------------------------------------------
log "Installing systemd unit 'forca.service'"
cat > /etc/systemd/system/forca.service <<EOF
[Unit]
Description=[FORCA] Command Grid (Docker Compose stack)
Requires=docker.service
After=docker.service network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
User=${APP_USER}
WorkingDirectory=${APP_SRC}
ExecStart=/usr/bin/docker compose -f ${COMPOSE_FILE} up -d
ExecStop=/usr/bin/docker compose -f ${COMPOSE_FILE} down

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable forca.service

log "Installing nightly PostgreSQL backup cron"
cat > /etc/cron.daily/forca-db-backup <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "${APP_SRC}"
sudo -u ${APP_USER} BACKUP_KEEP=14 bash scripts/backup.sh "${BACKUP_DIR}"
EOF
chmod 750 /etc/cron.daily/forca-db-backup

# ---------------------------------------------------------------------------
# 12. Summary
# ---------------------------------------------------------------------------
echo
log "Deployment complete."
echo "  App directory : ${APP_SRC}"
echo "  Env file      : ${ENV_FILE} (mode 600 — secrets generated here)"
echo "  Compose file  : ${COMPOSE_FILE}"
if [[ -n "${DOMAIN}" ]]; then
  echo "  URL           : https://${DOMAIN}"
  echo "  Admin console : https://${DOMAIN}/ops/  (native, role-gated)"
fi
echo "  Backups       : ${BACKUP_DIR} (nightly, 14-day retention)"
echo "  Boot service  : systemctl status forca.service"
echo "  Health check  : cd ${APP_SRC} && make health"
echo
echo "Next steps:"
echo "  1. Set your EVE app redirect_uri to https://${DOMAIN:-<your-domain>}/auth/eve/callback/"
echo "  2. Confirm EVE_SSO_CLIENT_ID/SECRET + a real ESI_USER_AGENT contact in ${ENV_FILE}"
echo "     (edit, then 'docker compose -f ${COMPOSE_FILE} up -d' to apply)."
echo "  3. Log in, link a Director character, and authorise corp scopes for full data."
echo "  4. See handbooks/operator-handbook/deployment.md and handbooks/operator-handbook/troubleshooting.md."
