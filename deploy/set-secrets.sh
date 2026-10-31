#!/usr/bin/env bash
# Run this YOURSELF on the VM, in your own terminal. It asks how to reach Claude on Amazon
# Bedrock, so any token is typed by you and never passes through anyone else's tools or logs.
# On EC2, prefer an instance role allowing bedrock:InvokeModel(WithResponseStream); leave the token
# blank: no secret is stored. The containers reach the role through the instance metadata
# service, so set its hop limit to 2:
#   aws ec2 modify-instance-metadata-options --instance-id <id> --http-put-response-hop-limit 2
# Then it
# starts everything. The demo is open (no login); spending is capped per service by the
# *_BUDGET_USD settings (deploy/compose.prod.yml) and rate-limited per visitor.
#   Usage:  bash deploy/set-secrets.sh
set -euo pipefail
cd "$(dirname "$0")/.."

ip=$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')
domain="${ip//./-}.sslip.io"

read -rp "AWS region for Amazon Bedrock [ap-southeast-2]: " aws_region; aws_region=${aws_region:-ap-southeast-2}
read -rp "Inference profile prefix (global, au, us, eu, jp, apac) [au]: " bedrock_profile; bedrock_profile=${bedrock_profile:-au}
read -rsp "Bedrock bearer token (input hidden; blank = use the instance role): " bedrock_token; echo

# Only used if a login is switched back on (see deploy/Caddyfile); must exist for the mount.
[ -f deploy/auth.caddy ] || { touch deploy/auth.caddy; chmod 600 deploy/auth.caddy; }

umask 077
cat > .env <<EOF
CLAUDE_PROVIDER=bedrock
AWS_REGION=$aws_region
CLAUDE_BEDROCK_PROFILE=$bedrock_profile
${bedrock_token:+AWS_BEARER_TOKEN_BEDROCK=$bedrock_token}
KB_API_KEY=$(openssl rand -hex 24)
DOMAIN=$domain
EOF

sudo docker compose -f docker-compose.yml -f deploy/compose.prod.yml up -d --build
# Caddy reads its config only at start: restart it so Caddyfile changes always apply.
sudo docker compose -f docker-compose.yml -f deploy/compose.prod.yml restart caddy
echo
echo "Starting. In 2-5 minutes, open https://$domain"
echo "API key for the Knowledge Brain's /docs (X-API-Key header): see KB_API_KEY in .env"
