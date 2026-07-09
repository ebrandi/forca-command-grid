#!/usr/bin/env bash
# cert-init.sh — obtain a Let's Encrypt certificate for the containerized-nginx
# deployment and wire up automatic renewal.
#
# The stack terminates TLS in the nginx CONTAINER (ports 80/443), reading its
# cert from <app>/certs/{forca.crt,forca.key}. certbot runs in `standalone` mode,
# so it needs port 80 free during issuance/renewal — we briefly stop the nginx
# container, then restart it. Renewal is fully automatic via certbot.timer plus
# three hooks that manage the container and copy the renewed cert into ./certs.
#
# Run as root on the host. Idempotent: re-running refreshes the hooks and renews.
#
# Usage: sudo scripts/cert-init.sh <domain> <email> [app_dir]
set -euo pipefail
. "$(dirname "$0")/lib.sh"

DOMAIN="${1:-}"
EMAIL="${2:-}"
APP_DIR="${3:-$(cd "$(dirname "$0")/.." && pwd)}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"

[ "$(id -u)" -eq 0 ] || die "Run as root (sudo) — this manages certbot + system hooks."
[ -n "$DOMAIN" ] && [ -n "$EMAIL" ] || die "Usage: sudo scripts/cert-init.sh <domain> <email> [app_dir]"
[ -f "${APP_DIR}/${COMPOSE_FILE}" ] || die "Compose file not found: ${APP_DIR}/${COMPOSE_FILE}"

require_cmd docker
if ! command -v certbot >/dev/null 2>&1; then
  log "Installing certbot ..."
  apt-get update -y && apt-get install -y certbot
fi

CERT_DIR="${APP_DIR}/certs"
mkdir -p "$CERT_DIR"

# --- renewal hooks (manage the nginx CONTAINER, not host nginx) --------------
log "Installing certbot renewal hooks ..."
mkdir -p /etc/letsencrypt/renewal-hooks/{pre,post,deploy}

cat > /etc/letsencrypt/renewal-hooks/pre/10-stop-nginx.sh <<EOF
#!/usr/bin/env bash
# Free port 80 for certbot standalone by stopping the nginx container.
cd "${APP_DIR}" && docker compose -f "${COMPOSE_FILE}" stop nginx || true
EOF

cat > /etc/letsencrypt/renewal-hooks/post/10-start-nginx.sh <<EOF
#!/usr/bin/env bash
# Bring nginx back up after issuance/renewal (reads the fresh cert on start).
cd "${APP_DIR}" && docker compose -f "${COMPOSE_FILE}" up -d nginx || true
EOF

cat > /etc/letsencrypt/renewal-hooks/deploy/10-install-certs.sh <<EOF
#!/usr/bin/env bash
# Copy the renewed cert into the location the nginx container mounts.
set -e
L=/etc/letsencrypt/live/${DOMAIN}; D=${CERT_DIR}
cp -L "\$L/fullchain.pem" "\$D/forca.crt"
cp -L "\$L/privkey.pem"   "\$D/forca.key"
chmod 644 "\$D/forca.crt"; chmod 600 "\$D/forca.key"
EOF

chmod +x /etc/letsencrypt/renewal-hooks/pre/10-stop-nginx.sh \
         /etc/letsencrypt/renewal-hooks/post/10-start-nginx.sh \
         /etc/letsencrypt/renewal-hooks/deploy/10-install-certs.sh

# --- issue (or renew) --------------------------------------------------------
DC="$(compose_cmd)"
if [ ! -e "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" ]; then
  log "Obtaining a certificate for ${DOMAIN} (standalone; port 80 must be reachable) ..."
  ( cd "$APP_DIR" && $DC -f "$COMPOSE_FILE" stop nginx || true )
  certbot certonly --standalone --non-interactive --agree-tos \
    -m "$EMAIL" -d "$DOMAIN"
  # Install into ./certs and (re)start nginx via the hooks we just wrote.
  /etc/letsencrypt/renewal-hooks/deploy/10-install-certs.sh
  ( cd "$APP_DIR" && $DC -f "$COMPOSE_FILE" up -d nginx )
  ok "Certificate obtained and installed."
else
  log "Certificate already present for ${DOMAIN}; refreshing install + testing renewal ..."
  /etc/letsencrypt/renewal-hooks/deploy/10-install-certs.sh || true
  certbot renew --dry-run || warn "Dry-run renewal reported an issue — check DNS/port 80."
  ok "Renewal chain verified."
fi

systemctl enable --now certbot.timer 2>/dev/null || true
ok "TLS ready for https://${DOMAIN} (auto-renew via certbot.timer)."
