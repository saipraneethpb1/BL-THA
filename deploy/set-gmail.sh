#!/usr/bin/env bash
# Run this YOURSELF on the VM. It asks for the Google OAuth client ID and secret (see
# docs/GMAIL_SETUP.md), so they are typed by you and never pass through anyone else's tools
# or logs. Then it restarts the Inbox Agent so "Connect Gmail" works.
#   Usage:  bash deploy/set-gmail.sh
set -euo pipefail
cd "$(dirname "$0")/.."

read -rp  "Google OAuth client ID: " client_id
read -rsp "Google OAuth client secret (input hidden): " client_secret; echo
[ -n "$client_id" ] && [ -n "$client_secret" ] || { echo "Both are needed."; exit 1; }

umask 077
touch .env
# Replace any earlier values, keep everything else.
grep -v -e '^GOOGLE_CLIENT_ID=' -e '^GOOGLE_CLIENT_SECRET=' .env > .env.tmp || true
printf 'GOOGLE_CLIENT_ID=%s\nGOOGLE_CLIENT_SECRET=%s\n' "$client_id" "$client_secret" >> .env.tmp
mv .env.tmp .env

sudo docker compose -f docker-compose.yml -f deploy/compose.prod.yml up -d inbox-agent
echo "Saved. Open the Inbox Agent and click Connect Gmail."
