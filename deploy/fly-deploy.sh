#!/usr/bin/env bash
# Byfrost Coordination Server - Fly.io Deployment
#
# First-time setup and deployment script.
# Run from the repository root: bash deploy/fly-deploy.sh
#
# Prerequisites:
#   - flyctl installed (https://fly.io/docs/flyctl/install/)
#   - Authenticated: fly auth login
#   - GitHub OAuth app created (https://github.com/settings/applications/new)
#     - Callback URL: https://api.byfrost.dev/auth/github/callback

set -euo pipefail

APP_NAME="byfrost-server"
REGION="iad"
PG_NAME="byfrost-db"
REDIS_NAME="byfrost-redis"

echo "=== Byfrost Server - Fly.io Deployment ==="
echo ""

# ------------------------------------------------------------------
# Step 1: Create the Fly app
# ------------------------------------------------------------------
echo "[1/6] Creating Fly app: $APP_NAME"
if fly apps list 2>/dev/null | grep -q "$APP_NAME"; then
    echo "  App already exists, skipping."
else
    fly apps create "$APP_NAME" --org personal
fi
echo ""

# ------------------------------------------------------------------
# Step 2: Create Fly Postgres cluster
# ------------------------------------------------------------------
echo "[2/6] Creating Fly Postgres: $PG_NAME"
echo "  Provisions managed Postgres 16 and sets DATABASE_URL automatically."
if fly postgres list 2>/dev/null | grep -q "$PG_NAME"; then
    echo "  Postgres cluster already exists, skipping creation."
    echo "  Ensure it is attached: fly postgres attach $PG_NAME --app $APP_NAME"
else
    fly postgres create \
        --name "$PG_NAME" \
        --region "$REGION" \
        --initial-cluster-size 1 \
        --vm-size shared-cpu-1x \
        --volume-size 1
    echo ""
    echo "  Attaching Postgres to app..."
    fly postgres attach "$PG_NAME" --app "$APP_NAME"
fi
echo ""

# ------------------------------------------------------------------
# Step 3: Create Upstash Redis
# ------------------------------------------------------------------
echo "[3/6] Creating Upstash Redis: $REDIS_NAME"
echo "  Provisions managed Redis and sets REDIS_URL automatically."
if fly redis list 2>/dev/null | grep -q "$REDIS_NAME"; then
    echo "  Redis already exists, skipping."
else
    fly redis create \
        --name "$REDIS_NAME" \
        --region "$REGION" \
        --no-eviction
fi
echo ""

# ------------------------------------------------------------------
# Step 4: Set secrets
# ------------------------------------------------------------------
echo "[4/6] Setting application secrets"
echo ""
echo "  Required values:"
echo "    - GITHUB_CLIENT_ID (from GitHub OAuth app)"
echo "    - GITHUB_CLIENT_SECRET (from GitHub OAuth app)"
echo "    - JWT_SECRET_KEY (auto-generated if not set)"
echo "    - ENCRYPTION_KEY (auto-generated if not set)"
echo ""

# Check if secrets are already set
EXISTING_SECRETS=$(fly secrets list --app "$APP_NAME" 2>/dev/null || echo "")

if echo "$EXISTING_SECRETS" | grep -q "JWT_SECRET_KEY"; then
    echo "  Secrets already configured. To update:"
    echo "    fly secrets set KEY=value --app $APP_NAME"
else
    read -rp "  GITHUB_CLIENT_ID: " GH_CLIENT_ID
    read -rp "  GITHUB_CLIENT_SECRET: " GH_CLIENT_SECRET

    # Generate secure keys
    JWT_KEY=$(openssl rand -hex 32)
    ENC_KEY=$(python3 -c "import secrets, base64; print(base64.b64encode(secrets.token_bytes(32)).decode())")

    echo ""
    echo "  Generated JWT_SECRET_KEY: $JWT_KEY"
    echo "  Generated ENCRYPTION_KEY: $ENC_KEY"
    echo "  (Save these somewhere secure)"
    echo ""

    fly secrets set \
        GITHUB_CLIENT_ID="$GH_CLIENT_ID" \
        GITHUB_CLIENT_SECRET="$GH_CLIENT_SECRET" \
        JWT_SECRET_KEY="$JWT_KEY" \
        ENCRYPTION_KEY="$ENC_KEY" \
        --app "$APP_NAME"
fi
echo ""

# ------------------------------------------------------------------
# Step 5: Deploy
# ------------------------------------------------------------------
echo "[5/6] Deploying application"
echo "  Building Docker image and running migrations..."
cd "$(dirname "$0")/../server"
fly deploy --app "$APP_NAME"
echo ""

# ------------------------------------------------------------------
# Step 6: Custom domain + TLS certificate
# ------------------------------------------------------------------
echo "[6/6] Custom domain setup"
echo ""
echo "  To configure api.byfrost.dev:"
echo ""
echo "  1. Add the certificate to Fly:"
echo "     fly certs create api.byfrost.dev --app $APP_NAME"
echo ""
echo "  2. Add DNS records at your registrar:"
echo "     CNAME: api.byfrost.dev -> $APP_NAME.fly.dev"
echo ""
echo "  3. Verify the certificate:"
echo "     fly certs show api.byfrost.dev --app $APP_NAME"
echo ""
echo "  TLS is automatic once DNS propagates (usually 1-5 minutes)."
echo ""

# ------------------------------------------------------------------
# Verify
# ------------------------------------------------------------------
echo "=== Deployment complete ==="
echo ""
echo "Verify:"
echo "  fly status --app $APP_NAME"
echo "  fly logs --app $APP_NAME"
echo "  curl https://$APP_NAME.fly.dev/health"
echo "  curl https://api.byfrost.dev/health  (after DNS setup)"
echo ""
echo "Useful commands:"
echo "  fly ssh console --app $APP_NAME          # SSH into the machine"
echo "  fly postgres connect -a $PG_NAME         # Connect to Postgres"
echo "  fly logs --app $APP_NAME                 # Stream logs"
echo "  fly secrets list --app $APP_NAME         # List secrets"
echo "  fly scale show --app $APP_NAME           # Show VM config"
