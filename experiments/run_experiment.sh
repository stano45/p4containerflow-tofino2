#!/bin/bash
# =============================================================================
# run_experiment.sh — Fully end-to-end multi-node experiment
# =============================================================================
# Run from your control machine. Handles EVERYTHING:
#   1. SSH connectivity checks
#   2. Build container images on lakewood (if missing)
#   3. Start switchd on tofino (if not running)
#   4. Start controller on tofino (if not running)
#   5. Clean previous run
#   6. Create networks and containers on lakewood + loveland
#   7. Start metrics collector
#   8. Wait for steady-state streaming
#   9. CRIU migration lakewood → loveland
#  10. Wait post-migration
#  11. Collect results + generate plots
#
# Usage:
#   ./run_experiment.sh [--steady-state SECS] [--post-migration SECS]
# =============================================================================

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/config_hw.env"

# -----------------------------------------------------------------------------
# Options
# -----------------------------------------------------------------------------
STEADY_STATE_WAIT=${STEADY_STATE_WAIT:-15}
POST_MIGRATION_WAIT=${POST_MIGRATION_WAIT:-30}

while [[ $# -gt 0 ]]; do
    case $1 in
        --steady-state)    STEADY_STATE_WAIT="$2"; shift 2 ;;
        --post-migration)  POST_MIGRATION_WAIT="$2"; shift 2 ;;
        *)                 echo "Unknown option: $1"; exit 1 ;;
    esac
done

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
on_lakewood() { ssh $SSH_OPTS "$LAKEWOOD_SSH" "$@"; }
on_loveland() { ssh $SSH_OPTS "$LOVELAND_SSH" "$@"; }
on_tofino()   { ssh $SSH_OPTS "$TOFINO_SSH" "$@"; }

COLLECTOR_PID=""
CONTROLLER_STARTED=false

cleanup_on_exit() {
    if [[ -n "$COLLECTOR_PID" ]] && kill -0 "$COLLECTOR_PID" 2>/dev/null; then
        kill "$COLLECTOR_PID" 2>/dev/null || true
        wait "$COLLECTOR_PID" 2>/dev/null || true
    fi
}
trap cleanup_on_exit EXIT

# =============================================================================
# Step 1: SSH connectivity
# =============================================================================
printf "\n╔══════════════════════════════════════════╗\n"
printf "║  Step 1: SSH connectivity                ║\n"
printf "╚══════════════════════════════════════════╝\n\n"

on_lakewood "echo 'lakewood OK'" || { echo "FAIL: cannot SSH to lakewood ($LAKEWOOD_SSH)"; exit 1; }
on_loveland "echo 'loveland OK'" || { echo "FAIL: cannot SSH to loveland ($LOVELAND_SSH)"; exit 1; }
on_tofino   "echo 'tofino OK'"  || { echo "FAIL: cannot SSH to tofino ($TOFINO_SSH)"; exit 1; }

echo "--- Netronome NICs ---"
on_lakewood "ip link show $LAKEWOOD_NIC >/dev/null 2>&1" || { echo "FAIL: $LAKEWOOD_NIC on lakewood"; exit 1; }
on_loveland "ip link show $LOVELAND_NIC >/dev/null 2>&1" || { echo "FAIL: $LOVELAND_NIC on loveland"; exit 1; }
echo "NICs OK"

# =============================================================================
# Step 2: Build container images on lakewood (if missing)
# =============================================================================
printf "\n╔══════════════════════════════════════════╗\n"
printf "║  Step 2: Build container images          ║\n"
printf "╚══════════════════════════════════════════╝\n\n"

if on_lakewood "sudo podman image exists $SERVER_IMAGE 2>/dev/null"; then
    echo "Image $SERVER_IMAGE already exists on lakewood"
else
    echo "Building $SERVER_IMAGE on lakewood..."
    on_lakewood "cd $REMOTE_PROJECT_DIR/experiments && sudo podman build -t $SERVER_IMAGE -f cmd/server/Containerfile ."
fi
# Restore on loveland needs the same image (checkpoint uses short name for CNI)
if on_loveland "sudo podman image exists $SERVER_IMAGE 2>/dev/null"; then
    echo "Image $SERVER_IMAGE already exists on loveland"
else
    echo "Building $SERVER_IMAGE on loveland (required for restore)..."
    on_loveland "cd $REMOTE_PROJECT_DIR/experiments && sudo podman build -t $SERVER_IMAGE -t localhost/${SERVER_IMAGE}:latest -f cmd/server/Containerfile ."
fi
on_loveland "sudo podman tag localhost/${SERVER_IMAGE}:latest $SERVER_IMAGE 2>/dev/null" || true

if on_lakewood "sudo podman image exists $LOADGEN_IMAGE 2>/dev/null"; then
    echo "Image $LOADGEN_IMAGE already exists on lakewood"
else
    echo "Building $LOADGEN_IMAGE on lakewood..."
    on_lakewood "cd $REMOTE_PROJECT_DIR/experiments && sudo podman build -t $LOADGEN_IMAGE -f cmd/loadgen/Containerfile ."
fi

# =============================================================================
# Step 3: Start switchd on tofino (if not running)
# =============================================================================
printf "\n╔══════════════════════════════════════════╗\n"
printf "║  Step 3: Ensure switchd is running       ║\n"
printf "╚══════════════════════════════════════════╝\n\n"

if on_tofino "pgrep -x bf_switchd >/dev/null 2>&1"; then
    echo "switchd already running on tofino"
else
    echo "Starting switchd on tofino (background)..."
    on_tofino "
        cd $REMOTE_PROJECT_DIR
        source ~/setup-open-p4studio.bash
        nohup bash -c 'make switch ARCH=tf1' > /tmp/switchd.log 2>&1 &
        echo \"switchd PID: \$!\"
    "
    echo "Waiting for switchd to initialize..."
    for i in $(seq 1 30); do
        if on_tofino "pgrep -x bf_switchd >/dev/null 2>&1"; then
            echo "switchd is up after ${i}s"
            break
        fi
        if [[ $i -eq 30 ]]; then
            echo "FAIL: switchd did not start within 30s"
            echo "Check /tmp/switchd.log on tofino"
            exit 1
        fi
        sleep 1
    done
    # Give it a few more seconds to fully bind the pipeline
    sleep 5
fi

# =============================================================================
# Step 4: Start controller on tofino (if not running)
# =============================================================================
printf "\n╔══════════════════════════════════════════╗\n"
printf "║  Step 4: Ensure controller is running    ║\n"
printf "╚══════════════════════════════════════════╝\n\n"

# Check if controller is already responding
CTRL_HTTP=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 3 \
    "${CONTROLLER_URL}/reinitialize" -X POST 2>/dev/null || echo "000")

if [[ "$CTRL_HTTP" != "000" ]]; then
    echo "Controller already running (HTTP $CTRL_HTTP)"
else
    echo "Starting controller on tofino (background)..."
    on_tofino "
        cd $REMOTE_PROJECT_DIR/controller
        source ~/setup-open-p4studio.bash
        nohup bash -c 'ARCH=tf1 CONFIG_FILE=$CONTROLLER_CONFIG ./run.sh' > /tmp/controller.log 2>&1 &
        echo \"controller PID: \$!\"
    "
    echo "Waiting for controller to start..."
    for i in $(seq 1 30); do
        CTRL_HTTP=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 2 \
            "${CONTROLLER_URL}/reinitialize" -X POST 2>/dev/null || echo "000")
        if [[ "$CTRL_HTTP" != "000" ]]; then
            echo "Controller is up after ${i}s (HTTP $CTRL_HTTP)"
            CONTROLLER_STARTED=true
            break
        fi
        if [[ $i -eq 30 ]]; then
            echo "FAIL: controller did not start within 30s"
            echo "Check /tmp/controller.log on tofino"
            exit 1
        fi
        sleep 1
    done
fi

# Reinitialize tables to clean state
echo "Reinitializing controller tables..."
curl -s -X POST "${CONTROLLER_URL}/reinitialize" \
    -H "Content-Type: application/json" | jq . 2>/dev/null || true

# =============================================================================
# Step 5: Clean previous run
# =============================================================================
printf "\n╔══════════════════════════════════════════╗\n"
printf "║  Step 5: Cleanup previous run            ║\n"
printf "╚══════════════════════════════════════════╝\n\n"

"$SCRIPT_DIR/clean_hw.sh" || true

# =============================================================================
# Step 6: Build networks and containers
# =============================================================================
printf "\n╔══════════════════════════════════════════╗\n"
printf "║  Step 6: Build experiment infrastructure ║\n"
printf "╚══════════════════════════════════════════╝\n\n"

"$SCRIPT_DIR/build_hw.sh"

# =============================================================================
# Step 7: Start collector
# =============================================================================
printf "\n╔══════════════════════════════════════════╗\n"
printf "║  Step 7: Start metrics collector         ║\n"
printf "╚══════════════════════════════════════════╝\n\n"

COLLECTOR_OUTPUT="$SCRIPT_DIR/$RESULTS_DIR/metrics.csv"
mkdir -p "$(dirname "$COLLECTOR_OUTPUT")"

(cd "$SCRIPT_DIR" && go run "./cmd/collector/" \
    -server-metrics "http://${VIP}:${METRICS_PORT}/metrics" \
    -ping-hosts "${H2_IP},${H3_IP}" \
    -containers "webrtc-server" \
    -ssh-host "$LOVELAND_SSH" \
    -migration-flag "$SCRIPT_DIR/$RESULTS_DIR/migration_timing.txt" \
    -output "$COLLECTOR_OUTPUT" \
    -interval "$METRICS_INTERVAL") &
COLLECTOR_PID=$!
printf "Collector started (PID %s)\n" "$COLLECTOR_PID"

# =============================================================================
# Step 8: Wait for steady-state
# =============================================================================
printf "\n╔══════════════════════════════════════════╗\n"
printf "║  Step 8: Healthy, then %3ds steady-state ║\n" "$STEADY_STATE_WAIT"
printf "╚══════════════════════════════════════════╝\n\n"

echo "Waiting for server to become healthy..."
# Brief delay so we don't spam before the container is listening
sleep 2
for i in $(seq 1 40); do
    if on_lakewood "curl -s --connect-timeout 1 http://${H2_IP}:${METRICS_PORT}/health" >/dev/null 2>&1; then
        echo "Server healthy after $(( 2 + (i - 1) / 2 ))s"
        break
    fi
    sleep 0.5
done

echo "Waiting ${STEADY_STATE_WAIT}s for steady-state streaming..."
sleep "$STEADY_STATE_WAIT"

# =============================================================================
# Step 9: Migrate
# =============================================================================
printf "\n╔══════════════════════════════════════════╗\n"
printf "║  Step 9: CRIU migration lakewood→loveland║\n"
printf "╚══════════════════════════════════════════╝\n\n"

"$SCRIPT_DIR/cr_hw.sh"

# =============================================================================
# Step 10: Post-migration wait
# =============================================================================
printf "\n╔══════════════════════════════════════════╗\n"
printf "║  Step 10: Waiting %3ds post-migration    ║\n" "$POST_MIGRATION_WAIT"
printf "╚══════════════════════════════════════════╝\n\n"

sleep "$POST_MIGRATION_WAIT"

# =============================================================================
# Step 11: Collect results + plot
# =============================================================================
printf "\n╔══════════════════════════════════════════╗\n"
printf "║  Step 11: Results & plots                ║\n"
printf "╚══════════════════════════════════════════╝\n\n"

if [[ -n "$COLLECTOR_PID" ]] && kill -0 "$COLLECTOR_PID" 2>/dev/null; then
    kill -TERM "$COLLECTOR_PID"
    wait "$COLLECTOR_PID" 2>/dev/null || true
fi
COLLECTOR_PID=""

if [[ -f "$SCRIPT_DIR/analysis/plot_metrics.py" ]] && [[ -f "$COLLECTOR_OUTPUT" ]]; then
    cd "$SCRIPT_DIR/analysis"
    pip install -q -r requirements.txt 2>/dev/null || true
    python3 plot_metrics.py \
        --csv "$COLLECTOR_OUTPUT" \
        --output-dir "$SCRIPT_DIR/$RESULTS_DIR/" \
        2>/dev/null && echo "Plots generated." || echo "Plot generation failed (non-fatal)."
    cd "$SCRIPT_DIR"
fi

echo ""
echo "Results saved to: $SCRIPT_DIR/$RESULTS_DIR/"
ls -la "$SCRIPT_DIR/$RESULTS_DIR/" 2>/dev/null || true

printf "\n╔══════════════════════════════════════════╗\n"
printf "║  Experiment complete!                    ║\n"
printf "╚══════════════════════════════════════════╝\n\n"
printf "To clean up: ./clean_hw.sh\n"
