#!/usr/bin/env bash
# Run this YOURSELF on the VM, in your own terminal. It asks for the Anthropic API key, so
# the key is typed by you and never passes through anyone else's tools or logs. Then it
# starts everything. The demo is open (no login); spending is capped per service by the
# *_BUDGET_USD settings (deploy/compose.prod.yml) and rate-limited per visitor.
#   Usage:  bash deploy/set-secrets.sh
set -euo pipefail
cd "$(dirname "$0")/.."

ip=$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')
domain="${ip//./-}.sslip.io"

read -rsp "Anthropic API key (input hidden): " anthropic_key; echo

# Only used if a login is switched back on (see deploy/Caddyfile); must exist for the mount.
[ -f deploy/auth.caddy ] || { touch deploy/auth.caddy; chmod 600 deploy/auth.caddy; }

umask 077
cat > .env <<EOF
ANTHROPIC_API_KEY=$anthropic_key
KB_API_KEY=$(openssl rand -hex 24)
DOMAIN=$domain
EOF

sudo docker compose -f docker-compose.yml -f deploy/compose.prod.yml up -d --build
# Caddy reads its config only at start: restart it so Caddyfile changes always apply.
sudo docker compose -f docker-compose.yml -f deploy/compose.prod.yml restart caddy
echo
echo "Starting. In 2-5 minutes, open https://$domain"
echo "API key for the Knowledge Brain's /docs (X-API-Key header): see KB_API_KEY in .env"
