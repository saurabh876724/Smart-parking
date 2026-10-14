#!/usr/bin/env bash
# setup-secrets.sh - Generate and store secrets for Docker deployment
# Usage: bash deploy/setup-secrets.sh
#
# This script creates the secrets directory and generates secure random values
# for database passwords and JWT signing keys. Never commit these to version control.

set -euo pipefail

SECRETS_DIR="$(cd "$(dirname "$0")" && pwd)/secrets"
mkdir -p "$SECRETS_DIR"

echo "Generating secrets in $SECRETS_DIR ..."

# Database password (16 characters, alphanumeric + symbols)
if [ ! -f "$SECRETS_DIR/db_password.txt" ]; then
    openssl rand -base64 24 > "$SECRETS_DIR/db_password.txt"
    echo "✓ Generated db_password.txt"
else
    echo "⚠ db_password.txt already exists (skipping)"
fi

# JWT signing secret (32 bytes hex = 64 characters)
if [ ! -f "$SECRETS_DIR/jwt_secret.txt" ]; then
    openssl rand -hex 32 > "$SECRETS_DIR/jwt_secret.txt"
    echo "✓ Generated jwt_secret.txt"
else
    echo "⚠ jwt_secret.txt already exists (skipping)"
fi

# Set restrictive permissions
chmod 600 "$SECRETS_DIR"/*.txt

echo ""
echo "Secrets generated successfully."
echo "DO NOT commit the secrets/ directory to version control."
echo ""
echo "Next steps:"
echo "  1. Copy deploy/.env.example to deploy/.env and edit values"
echo "  2. Run: docker compose up -d"
