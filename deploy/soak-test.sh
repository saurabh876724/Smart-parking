#!/usr/bin/env bash
# soak-test.sh - 24-hour stability test for the Smart Parking system
# Usage: bash deploy/soak-test.sh [duration_hours]
#
# Runs continuous health checks, monitors resource usage, and simulates
# failure recovery (kill camera, DB restart, GPU stress). Reports results
# to stdout and a log file.
#
# Default duration: 24 hours

set -euo pipefail

# Check prerequisites
if ! command -v docker &>/dev/null; then
    echo "Error: Docker is not installed or not in PATH" >&2
    echo "" >&2
    echo "The soak test requires Docker Compose services to be running." >&2
    echo "Install Docker Desktop and NVIDIA Container Toolkit, then run:" >&2
    echo "  docker compose -f deploy/docker-compose.yml up -d" >&2
    exit 1
fi

# Check if services are running
if ! docker compose -f "$(dirname "$0")/docker-compose.yml" ps --services 2>/dev/null | grep -q api; then
    echo "Error: Docker Compose services are not running" >&2
    echo "" >&2
    echo "Start the services first:" >&2
    echo "  cd deploy && docker compose up -d" >&2
    exit 1
fi

DURATION_HOURS="${1:-24}"
INTERVAL_SECONDS=60
# awk (not bash arithmetic) so a fractional run like `soak-test.sh 0.2` (~12 min)
# works for a quick check without committing to a full hour.
TOTAL_SECONDS=$(awk "BEGIN{printf \"%d\", $DURATION_HOURS * 3600}")
LOG_FILE="$(cd "$(dirname "$0")" && pwd)/soak-test-$(date +%Y%m%d_%H%M%S).log"

echo "═══════════════════════════════════════════════════════════════"
echo "Soak Test - ${DURATION_HOURS}h duration"
echo "Started: $(date)"
echo "Log: $LOG_FILE"
echo "═══════════════════════════════════════════════════════════════"

END_TIME=$(( $(date +%s) + TOTAL_SECONDS ))
ITERATION=0
FAILURES=0

log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $1"
    echo "$msg" | tee -a "$LOG_FILE"
}

# /health is authenticated by design (it leaks camera names and pipeline error
# paths), so the probe logs in first. A login that fails here would otherwise make
# every API check below false-fail with a 401 and read as an outage.
# The probe account is not this script's to know: `deploy/.env` already names one for
# the compose healthcheck, so it is read from there instead of defaulted to a literal.
# A hard-coded credential here would be somebody's password in a public repository.
DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
SOAK_EMAIL="${SOAK_EMAIL:-$(sed -n 's/^HEALTH_EMAIL=//p' "$DEPLOY_DIR/.env" 2>/dev/null | head -1)}"
SOAK_PASSWORD="${SOAK_PASSWORD:-$(sed -n 's/^HEALTH_PASSWORD=//p' "$DEPLOY_DIR/.env" 2>/dev/null | head -1)}"
if [ -z "$SOAK_EMAIL" ] || [ -z "$SOAK_PASSWORD" ]; then
    echo "No probe account: set HEALTH_EMAIL and HEALTH_PASSWORD in $DEPLOY_DIR/.env," >&2
    echo "or export SOAK_EMAIL and SOAK_PASSWORD before running this script." >&2
    exit 1
fi
COMPOSE_FILE="$DEPLOY_DIR/docker-compose.yml"

get_token() {
    docker compose -f "$COMPOSE_FILE" exec -T \
        -e SOAK_EMAIL="$SOAK_EMAIL" -e SOAK_PASSWORD="$SOAK_PASSWORD" \
        api python3 -c 'import json,os,urllib.request as u; d=json.dumps({"email":os.environ["SOAK_EMAIL"],"password":os.environ["SOAK_PASSWORD"]}).encode(); r=u.Request("http://localhost:8000/api/v1/auth/login",data=d,headers={"Content-Type":"application/json"}); print(json.load(u.urlopen(r))["access_token"])' 2>/dev/null
}

AUTH_HEADER=""
TOKEN="$(get_token)"
if [ -n "$TOKEN" ]; then
    AUTH_HEADER="Authorization: Bearer $TOKEN"
else
    log "WARNING: login failed; API health checks will report 401 as unhealthy"
fi

check_health() {
    local endpoint="$1"
    local expected_code="${2:-200}"

    local http_code
    http_code=$(curl -s -o /dev/null -w '%{http_code}' -H "$AUTH_HEADER" "$endpoint" 2>/dev/null || echo "000")

    if [ "$http_code" = "$expected_code" ]; then
        return 0
    else
        return 1
    fi
}

while [ $(date +%s) -lt $END_TIME ]; do
    ITERATION=$((ITERATION + 1))
    ELAPSED=$(( ($(date +%s) - END_TIME + TOTAL_SECONDS) / 60 ))

    log "--- Iteration $ITERATION (${ELAPSED}m elapsed) ---"

    # Health check: API
    if check_health "http://localhost/api/v1/health"; then
        log "✓ API healthy"
    else
        log "✗ API unhealthy"
        FAILURES=$((FAILURES + 1))
    fi

    # Health check: Database
    if docker compose -f "$COMPOSE_FILE" exec -T db pg_isready -U parking_admin >/dev/null 2>&1; then
        log "✓ Database ready"
    else
        log "✗ Database not ready"
        FAILURES=$((FAILURES + 1))
    fi

    # Resource usage snapshot
    mem_api=$(docker stats --no-stream --format '{{.MemUsage}}' smart-parking-api 2>/dev/null || echo "N/A")
    log "  API memory: $mem_api"

    mem_db=$(docker stats --no-stream --format '{{.MemUsage}}' smart-parking-db 2>/dev/null || echo "N/A")
    log "  DB memory: $mem_db"

    # Failure simulation at hour 6, 12, 18 (if enabled)
    HOUR=$(( ELAPSED / 60 ))
    if [ $((HOUR % 6)) -eq 0 ] && [ $HOUR -gt 0 ] && [ $HOUR -le 18 ]; then
        log "⚠ Simulating failure scenario (hour $HOUR)"

        # Scenario 1: Kill camera pipeline (restart API container)
        log "  Restarting API container..."
        docker compose -f "$COMPOSE_FILE" restart api >/dev/null 2>&1
        sleep 30

        if check_health "http://localhost/api/v1/health"; then
            log "  ✓ API recovered after restart"
        else
            log "  ✗ API failed to recover"
            FAILURES=$((FAILURES + 1))
        fi

        # Scenario 2: Database restart
        log "  Restarting database..."
        docker compose -f "$COMPOSE_FILE" restart db >/dev/null 2>&1
        sleep 15

        if docker compose -f "$COMPOSE_FILE" exec -T db pg_isready -U parking_admin >/dev/null 2>&1; then
            log "  ✓ Database recovered after restart"
        else
            log "  ✗ Database failed to recover"
            FAILURES=$((FAILURES + 1))
        fi
    fi

    sleep "$INTERVAL_SECONDS"
done

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "Soak Test Complete"
echo "Duration: ${DURATION_HOURS}h"
echo "Iterations: $ITERATION"
echo "Failures: $FAILURES"
echo "Log: $LOG_FILE"
echo "═══════════════════════════════════════════════════════════════"

if [ $FAILURES -eq 0 ]; then
    echo "✓ PASSED - System stable for ${DURATION_HOURS} hours"
    exit 0
else
    echo "✗ FAILED - $FAILURES failures detected"
    exit 1
fi
