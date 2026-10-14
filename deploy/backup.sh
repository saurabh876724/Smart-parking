#!/usr/bin/env bash
# backup.sh - Create a PostgreSQL database backup
# Usage: bash deploy/backup.sh [output_dir]
#
# Creates a timestamped pg_dump file in the backups directory.
# Retains only the last 7 backups to prevent disk exhaustion.

set -euo pipefail

BACKUP_DIR="${1:-$(cd "$(dirname "$0")" && pwd)/backups}"
mkdir -p "$BACKUP_DIR"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/smart_parking_${TIMESTAMP}.sql.gz"

echo "Creating backup: $BACKUP_FILE"

docker compose exec -T db pg_dump -U parking_admin smart_parking | gzip > "$BACKUP_FILE"

if [ -f "$BACKUP_FILE" ]; then
    SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
    echo "✓ Backup created ($SIZE)"
else
    echo "✗ Backup failed" >&2
    exit 1
fi

# Retain only last 7 backups
BACKUP_COUNT=$(ls -1 "$BACKUP_DIR"/smart_parking_*.sql.gz 2>/dev/null | wc -l)
if [ "$BACKUP_COUNT" -gt 7 ]; then
    ls -1t "$BACKUP_DIR"/smart_parking_*.sql.gz | tail -n +8 | xargs rm
    echo "Cleaned up old backups (kept 7 most recent)"
fi
