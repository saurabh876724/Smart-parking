#!/usr/bin/env bash
# restore.sh - Restore a PostgreSQL database from backup
# Usage: bash deploy/restore.sh <backup_file.sql.gz>
#
# WARNING: This will DROP and recreate all tables. Use with caution in production.

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "Usage: $0 <backup_file.sql.gz>"
    echo ""
    echo "Available backups:"
    ls -lh "$(dirname "$0")/backups/"*.sql.gz 2>/dev/null || echo "  (none found)"
    exit 1
fi

BACKUP_FILE="$1"

if [ ! -f "$BACKUP_FILE" ]; then
    echo "Error: Backup file not found: $BACKUP_FILE" >&2
    exit 1
fi

echo "WARNING: This will destroy all existing data in the database."
read -rp "Are you sure? Type 'yes' to continue: " CONFIRM

if [ "$CONFIRM" != "yes" ]; then
    echo "Aborted."
    exit 0
fi

echo "Restoring from: $BACKUP_FILE"

# Drop and recreate database
docker compose exec -T db psql -U parking_admin -d postgres -c "DROP DATABASE IF EXISTS smart_parking;"
docker compose exec -T db psql -U parking_admin -d postgres -c "CREATE DATABASE smart_parking OWNER parking_admin;"

# Run migrations to create schema
docker compose exec -T api python scripts/migrate.py upgrade head

# Restore data
gunzip -c "$BACKUP_FILE" | docker compose exec -T db psql -U parking_admin -d smart_parking

echo "✓ Restore complete"
