#!/bin/bash
# =============================================================================
# run_experiment.sh — End-to-end multi-node experiment orchestration
# =============================================================================
# Coordinates the full WebRTC migration experiment across:
#   lakewood  (131.130.124.71) — client + server
#   loveland  (131.130.124.72) — migration target
#   wedge100bf (131.130.124.74) — Tofino switch + controller
#
# Run from lakewood. Assumes:
#   - Container images already built on lakewood and loveland
#   - Switch (switchd) already running on wedge100bf
#   - SSH key-based access to loveland and tofino
#
# Usage:
#   ./run_experiment.sh [--steady-state SECS] [--post-migration SECS] [--skip-controller]
# =============================================================================

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/config_hw.env"

# -----------------------------------------------------------------------------
# Options
# -----------------------------------------------------------------------------
STEADY_STATE_WAIT=${STEADY_STATE_WAIT:-30}
POST_MIGRATION_WAIT=${POST_MIGRATION_WAIT:-30}
SKIP_CONTROLLER=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --steady-state)    STEADY_STATE_WAIT="$2"; shift 2 ;;
        --post-migration)  POST_MIGRATION_WAIT="$2"; shift 2 ;;
        --skip-controller) SKIP_CONTROLLER=true; shift ;;
        *)                 echo "Unknown option: $1"; exit 1 ;;
    esac
done

remote() {
    local host="$1"; shift
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$host" "$@"
}

cleanup_on_exit() {
    printf "\n===== Stopping background processes =====\n"
    # Kill collector if running
    if [ -n "${COLLECTOR_PID:-}" ] && kill -0 "$COLLECTOR_PID" 2>/dev/null; then
        kill "$COLLECTOR_PID" 2>/dev/null || true
        wait "$COLLECTOR_PID" 2>/dev/null || true
        printf "Collector stopped (PID %s)\n" "$COLLECTOR_PID"
    fi
}
trap cleanup_on_exit EXIT

# =============================================================================
# Step 1: Preflight checks
# =============================================================================
printf "\n╔══════════════════════════════════════════╗\n"
printf "║  Multi-Node Experiment — Preflight       ║\n"
printf "╚══════════════════════════════════════════╝\n\n"

echo "--- Checking SSH connectivity ---"
remote "$LOVELAND_SSH" "echo 'loveland OK'" || { echo "FAIL: cannot SSH to loveland"; exit 1; }
remote "$TOFINO_SSH"   "echo 'tofino OK'"   || { echo "FAIL: cannot SSH to tofino"; exit 1; }

echo "--- Checking Netronome NICs ---"
ip link show "$LAKEWOOD_NIC" >/dev/null 2>&1 || { echo "FAIL: $LAKEWOOD_NIC not found locally"; exit 1; }
remote "$LOVELAND_SSH" "ip link show $LOVELAND_NIC" >/dev/null 2>&1 || { echo "FAIL: $LOVELAND_NIC not found on loveland"; exit 1; }

echo "--- Checking controller reachability ---"
if ! $SKIP_CONTROLLER; then
    HTTP=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 3 "${CONTROLLER_URL}/reinitialize" -X POST 2>/dev/null || echo "000")
    if [ "$HTTP" = "000" ]; then
        echo "WARNING: Controller not reachable at $CONTROLLER_URL"
        echo "  Start it on tofino: cd controller && CONFIG_FILE=controller_config_hw.json ./run.sh"
        echo "  Or re-run with --skip-controller"
        exit 1
    fi
    printf "Controller OK (HTTP %s from /reinitialize)\n" "$HTTP"
fi

echo ""
echo "Preflight passed."

# =============================================================================
# Step 2: Clean previous run
# =============================================================================
printf "\n╔══════════════════════════════════════════╗\n"
printf "║  Step 2: Cleanup previous experiment     ║\n"
printf "╚══════════════════════════════════════════╝\n\n"

"$SCRIPT_DIR/clean_hw.sh" || true

# =============================================================================
# Step 3: Build (create networks, pods, containers)
# =============================================================================
printf "\n╔══════════════════════════════════════════╗\n"
printf "║  Step 3: Build experiment infrastructure ║\n"
printf "╚══════════════════════════════════════════╝\n\n"

"$SCRIPT_DIR/build_hw.sh"

# =============================================================================
# Step 4: Reinitialize controller (ensure clean table state)
# =============================================================================
if ! $SKIP_CONTROLLER; then
    printf "\n╔══════════════════════════════════════════╗\n"
    printf "║  Step 4: Reinitialize controller tables  ║\n"
    printf "╚══════════════════════════════════════════╝\n\n"

    curl -s -X POST "${CONTROLLER_URL}/reinitialize" \
        -H "Content-Type: application/json" | jq . 2>/dev/null || true
    echo "Controller tables reinitialized."
fi

# =============================================================================
# Step 5: Start collector (background)
# =============================================================================
printf "\n╔══════════════════════════════════════════╗\n"
printf "║  Step 5: Start metrics collector         ║\n"
printf "╚══════════════════════════════════════════╝\n\n"

COLLECTOR_OUTPUT="$SCRIPT_DIR/$RESULTS_DIR/metrics.csv"
mkdir -p "$(dirname "$COLLECTOR_OUTPUT")"

go run "$SCRIPT_DIR/cmd/collector/" \
    -server-metrics "http://${VIP}:${METRICS_PORT}/metrics" \
    -ping-hosts "${H2_IP},${H3_IP}" \
    -containers "webrtc-server" \
    -ssh-host "$LOVELAND_SSH" \
    -migration-flag "$MIGRATION_FLAG_FILE" \
    -output "$COLLECTOR_OUTPUT" \
    -interval "$METRICS_INTERVAL" &
COLLECTOR_PID=$!

printf "Collector started (PID %s), writing to %s\n" "$COLLECTOR_PID" "$COLLECTOR_OUTPUT"

# =============================================================================
# Step 6: Wait for steady-state
# =============================================================================
printf "\n╔══════════════════════════════════════════╗\n"
printf "║  Step 6: Waiting %3ds for steady-state   ║\n" "$STEADY_STATE_WAIT"
printf "╚══════════════════════════════════════════╝\n\n"

# Quick health check — wait for server to respond
echo "Waiting for server to become healthy..."
for i in $(seq 1 30); do
    if curl -s --connect-timeout 2 "http://${H2_IP}:${METRICS_PORT}/health" >/dev/null 2>&1; then
        echo "Server healthy after ${i}s"
        break
    fi
    sleep 1
done

echo "Waiting ${STEADY_STATE_WAIT}s for steady-state streaming..."
sleep "$STEADY_STATE_WAIT"

# =============================================================================
# Step 7: Trigger migration
# =============================================================================
printf "\n╔══════════════════════════════════════════╗\n"
printf "║  Step 7: CRIU migration lakewood→loveland║\n"
printf "╚══════════════════════════════════════════╝\n\n"

"$SCRIPT_DIR/cr_hw.sh"

# =============================================================================
# Step 8: Wait for post-migration steady-state
# =============================================================================
printf "\n╔══════════════════════════════════════════╗\n"
printf "║  Step 8: Waiting %3ds post-migration     ║\n" "$POST_MIGRATION_WAIT"
printf "╚══════════════════════════════════════════╝\n\n"

sleep "$POST_MIGRATION_WAIT"

# =============================================================================
# Step 9: Stop collector, summarize
# =============================================================================
printf "\n╔══════════════════════════════════════════╗\n"
printf "║  Step 9: Collecting results              ║\n"
printf "╚══════════════════════════════════════════╝\n\n"

# Stop collector gracefully
if kill -0 "$COLLECTOR_PID" 2>/dev/null; then
    kill -TERM "$COLLECTOR_PID"
    wait "$COLLECTOR_PID" 2>/dev/null || true
fi
unset COLLECTOR_PID

# Copy migration timing to results
if [ -f "$MIGRATION_FLAG_FILE" ]; then
    cp "$MIGRATION_FLAG_FILE" "$SCRIPT_DIR/$RESULTS_DIR/migration_timing.txt"
    echo "Migration timing:"
    cat "$MIGRATION_FLAG_FILE"
fi

echo ""
echo "Results saved to: $SCRIPT_DIR/$RESULTS_DIR/"
ls -la "$SCRIPT_DIR/$RESULTS_DIR/"

# =============================================================================
# Step 10: Plot (optional)
# =============================================================================
printf "\n╔══════════════════════════════════════════╗\n"
printf "║  Step 10: Generate plots                 ║\n"
printf "╚══════════════════════════════════════════╝\n\n"

if [ -f "$SCRIPT_DIR/analysis/plot_metrics.py" ] && [ -f "$COLLECTOR_OUTPUT" ]; then
    cd "$SCRIPT_DIR/analysis"
    pip install -q -r requirements.txt 2>/dev/null || true
    python3 plot_metrics.py \
        --csv "$COLLECTOR_OUTPUT" \
        --output-dir "$SCRIPT_DIR/$RESULTS_DIR/" \
        2>/dev/null && echo "Plots generated." || echo "Plot generation failed (non-fatal)."
    cd "$SCRIPT_DIR"
else
    echo "Skipping plots (missing plot script or CSV)."
fi

printf "\n╔══════════════════════════════════════════╗\n"
printf "║  Experiment complete!                    ║\n"
printf "╚══════════════════════════════════════════╝\n\n"
printf "Results: %s/\n" "$SCRIPT_DIR/$RESULTS_DIR"
printf "To clean up: ./clean_hw.sh\n"
