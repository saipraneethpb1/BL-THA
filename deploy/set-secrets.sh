#!/usr/bin/env bash
# Run this YOURSELF on the VM, in your own terminal. It asks for the secrets, so they are
# typed by you and never pass through anyone else's tools or logs:
#   - the Anthropic API key
#   - a username and password for the login prompt on every page
# Then it starts everything.   Usage:  bash deploy/set-secrets.sh
set -euo pipefail
cd "$(dirname "$0")/.."

ip=$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')
domain="${ip//./-}.sslip.io"

read -rsp "Anthropic API key (input hidden): " anthropic_key; echo
read -rp  "Login username [reviewer]: " user; user=${user:-reviewer}
read -rsp "Login password (input hidden): " password; echo
read -rsp "Repeat password: " password2; echo
[ "$password" = "$password2" ] || { echo "Passwords differ."; exit 1; }
[ ${#password} -ge 10 ] || { echo "Use at least 10 characters."; exit 1; }

hash=$(sudo docker run --rm caddy:2.8.4-alpine caddy hash-password --plaintext "$password")
printf '%s %s\n' "$user" "$hash" > deploy/auth.caddy
chmod 600 deploy/auth.caddy

umask 077
cat > .env <<EOF
ANTHROPIC_API_KEY=$anthropic_key
KB_API_KEY=$(openssl rand -hex 24)
DOMAIN=$domain
EOF

sudo docker compose -f docker-compose.yml -f deploy/compose.prod.yml up -d --build
echo
echo "Starting. In 2-5 minutes, open https://$domain (user: $user)."
echo "API key for the Knowledge Brain's /docs (X-API-Key header): see KB_API_KEY in .env"
