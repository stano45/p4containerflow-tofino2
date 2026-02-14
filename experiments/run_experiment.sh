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
#   ./run_experiment.sh [--steady-state SECS] [--post-migration SECS] [--migrations N]
# =============================================================================

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/config_hw.env"

# -----------------------------------------------------------------------------
# Options
# -----------------------------------------------------------------------------
STEADY_STATE_WAIT=${STEADY_STATE_WAIT:-15}
POST_MIGRATION_WAIT=${POST_MIGRATION_WAIT:-30}
MIGRATION_COUNT=${MIGRATION_COUNT:-1}

while [[ $# -gt 0 ]]; do
    case $1 in
        --steady-state)    STEADY_STATE_WAIT="$2"; shift 2 ;;
        --post-migration) POST_MIGRATION_WAIT="$2"; shift 2 ;;
        --migrations)     MIGRATION_COUNT="$2"; shift 2 ;;
        *)                echo "Unknown option: $1"; exit 1 ;;
    esac
done

# -----------------------------------------------------------------------------
# Run directory (timestamped); config + logs + results go here
# -----------------------------------------------------------------------------
RUN_DIR="$SCRIPT_DIR/$RESULTS_DIR/run_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$RUN_DIR"

# Log configuration
{
  echo "run_dir=$RUN_DIR"
  echo "steady_state_wait=$STEADY_STATE_WAIT"
  echo "post_migration_wait=$POST_MIGRATION_WAIT"
  echo "migration_count=$MIGRATION_COUNT"
  echo "h2_ip=$H2_IP"
  echo "h3_ip=$H3_IP"
  echo "vip=$VIP"
  echo "lakewood_ssh=$LAKEWOOD_SSH"
  echo "loveland_ssh=$LOVELAND_SSH"
  echo "checkpoint_dir=$CHECKPOINT_DIR"
  echo "server_image=$SERVER_IMAGE"
  echo "controller_url=$CONTROLLER_URL"
} > "$RUN_DIR/config.txt"

# Tee all output to experiment log
exec > >(tee "$RUN_DIR/experiment.log") 2>&1

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
on_lakewood() { ssh $SSH_OPTS "$LAKEWOOD_SSH" "$@"; }
on_loveland() { ssh $SSH_OPTS "$LOVELAND_SSH" "$@"; }
on_tofino()   { ssh $SSH_OPTS "$TOFINO_SSH" "$@"; }

COLLECTOR_PID=""          # local SSH wrapper PID
COLLECTOR_REMOTE_PID=""   # PID on lakewood
CONTROLLER_STARTED=false
REMOTE_COLLECTOR_CSV="/tmp/collector_metrics.csv"
REMOTE_COLLECTOR_BIN="/tmp/webrtc-collector"

cleanup_on_exit() {
    # Stop collector on lakewood
    if [[ -n "$COLLECTOR_REMOTE_PID" ]]; then
        on_lakewood "sudo kill $COLLECTOR_REMOTE_PID 2>/dev/null; true" 2>/dev/null || true
    fi
    if [[ -n "$COLLECTOR_PID" ]] && kill -0 "$COLLECTOR_PID" 2>/dev/null; then
        kill "$COLLECTOR_PID" 2>/dev/null || true
        wait "$COLLECTOR_PID" 2>/dev/null || true
    fi
}
exit_trap() {
    local ex=$?
    if [[ $ex -ne 0 ]] && [[ -n "${RUN_DIR-}" ]] && [[ -f "${RUN_DIR}/experiment.log" ]]; then
        echo "=== Experiment failed (exit $ex) ===" > "$RUN_DIR/error.log"
        tail -n 300 "$RUN_DIR/experiment.log" >> "$RUN_DIR/error.log" 2>/dev/null || true
    fi
    cleanup_on_exit
    exit $ex
}
trap exit_trap EXIT

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

echo "--- Syncing experiment scripts to lab nodes ---"
rsync -az --delete -e "ssh $SSH_OPTS" \
    "$SCRIPT_DIR/"  "$LAKEWOOD_SSH:$REMOTE_PROJECT_DIR/experiments/"
rsync -az --delete -e "ssh $SSH_OPTS" \
    "$SCRIPT_DIR/"  "$LOVELAND_SSH:$REMOTE_PROJECT_DIR/experiments/"
echo "Scripts synced"

echo "--- Building and deploying edit_checkpoint (Rust) to lab nodes ---"
EDIT_CRATE="$SCRIPT_DIR/../scripts/edit_checkpoint"
if [[ -d "$EDIT_CRATE" ]] && command -v cargo >/dev/null 2>&1; then
  REMOTE_EDIT_BIN="${REMOTE_EDIT_BIN:-/tmp/edit_checkpoint}"
  EDIT_BIN=""
  # Prefer musl static binary (runs on nodes with older glibc)
  if (cd "$EDIT_CRATE" && cargo build --release --target x86_64-unknown-linux-musl 2>/dev/null); then
    EDIT_BIN="$EDIT_CRATE/target/x86_64-unknown-linux-musl/release/edit_checkpoint"
  fi
  if [[ -z "$EDIT_BIN" || ! -f "$EDIT_BIN" ]]; then
    (cd "$EDIT_CRATE" && cargo build --release 2>/dev/null) && EDIT_BIN="$EDIT_CRATE/target/release/edit_checkpoint"
  fi
  if [[ -n "$EDIT_BIN" && -f "$EDIT_BIN" ]]; then
    on_lakewood "sudo rm -f $REMOTE_EDIT_BIN; rm -f $REMOTE_EDIT_BIN" 2>/dev/null || true
    on_loveland "sudo rm -f $REMOTE_EDIT_BIN; rm -f $REMOTE_EDIT_BIN" 2>/dev/null || true
    scp $SSH_OPTS "$EDIT_BIN" "$LAKEWOOD_SSH:$REMOTE_EDIT_BIN" 2>/dev/null && \
    scp $SSH_OPTS "$EDIT_BIN" "$LOVELAND_SSH:$REMOTE_EDIT_BIN" 2>/dev/null && \
    on_lakewood "chmod +x $REMOTE_EDIT_BIN" 2>/dev/null || true && \
    on_loveland "chmod +x $REMOTE_EDIT_BIN" 2>/dev/null || true && \
    echo "edit_checkpoint deployed to both nodes." || echo "edit_checkpoint deploy failed (will use Python)."
  else
    echo "edit_checkpoint build failed; will use Python edit on target."
  fi
else
  echo "edit_checkpoint skipped (no cargo or script dir); will use Python edit on target."
fi

echo "--- Ensuring socat on lakewood and loveland (for direct-link transfer) ---"
on_lakewood "command -v socat >/dev/null 2>&1 || { sudo dnf install -y socat 2>/dev/null || sudo apt-get install -y socat; }"
on_loveland "command -v socat >/dev/null 2>&1 || { sudo dnf install -y socat 2>/dev/null || sudo apt-get install -y socat; }"
echo "socat OK"

echo "--- Ensuring root@lakewood can SSH to loveland via direct link (for collector) ---"
# The collector runs as root on lakewood. It needs SSH to loveland (192.168.10.3)
# to fetch metrics when the server migrates there. Set up root's SSH key.
on_lakewood "sudo test -f /root/.ssh/id_ed25519 || sudo ssh-keygen -t ed25519 -N '' -f /root/.ssh/id_ed25519 -q"
ROOT_PUB=$(on_lakewood "sudo cat /root/.ssh/id_ed25519.pub" 2>/dev/null)
if [[ -n "$ROOT_PUB" ]]; then
    # Authorize root@lakewood's key on loveland (kosorins32 account)
    on_loveland "mkdir -p ~/.ssh && chmod 700 ~/.ssh && grep -qF '$(echo "$ROOT_PUB" | head -1)' ~/.ssh/authorized_keys 2>/dev/null || echo '$(echo "$ROOT_PUB" | head -1)' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
    echo "root SSH key authorized on loveland"
else
    echo "WARNING: Could not set up root SSH key (remote metrics may fail)"
fi

# =============================================================================
# Step 2: Build container images on lakewood (always — ensures latest code)
# =============================================================================
printf "\n╔══════════════════════════════════════════╗\n"
printf "║  Step 2: Build container images          ║\n"
printf "╚══════════════════════════════════════════╝\n\n"

echo "Building $SERVER_IMAGE on lakewood..."
on_lakewood "cd $REMOTE_PROJECT_DIR/experiments && sudo podman build -t $SERVER_IMAGE -f cmd/server/Containerfile ."

SERVER_IMAGE_ID=$(on_lakewood "sudo podman image inspect $SERVER_IMAGE --format '{{.Id}}' 2>/dev/null" | sed 's/^sha256://' || true)
if [[ -z "$SERVER_IMAGE_ID" || ${#SERVER_IMAGE_ID} -ne 64 ]]; then
    echo "ERROR: Could not get image ID for $SERVER_IMAGE on lakewood."
    exit 1
fi
echo "Syncing server image lakewood→loveland..."
SYNC_TMP=/tmp/cr_image_sync_$$
on_lakewood "sudo podman save -o $SYNC_TMP.img $SERVER_IMAGE_ID && sudo chown \$(whoami) $SYNC_TMP.img"
ssh $SSH_OPTS -o ForwardAgent=yes "$LAKEWOOD_SSH" "scp -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=60 $SYNC_TMP.img $LOVELAND_SSH:$SYNC_TMP.img"
on_lakewood "rm -f $SYNC_TMP.img"
on_loveland "sudo podman load -i $SYNC_TMP.img && rm -f $SYNC_TMP.img"
echo "Server image synced."

echo "Building $LOADGEN_IMAGE on lakewood..."
on_lakewood "cd $REMOTE_PROJECT_DIR/experiments && sudo podman build -t $LOADGEN_IMAGE -f cmd/loadgen/Containerfile ."

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

COLLECTOR_OUTPUT="$RUN_DIR/metrics.csv"
MIGRATION_FLAG="$RUN_DIR/migration_timing.txt"
REMOTE_MIGRATION_FLAG="/tmp/collector_migration_flag"
# No stale flag: RUN_DIR is fresh; cr_hw.sh will write migration_timing.txt on each migration

# Build collector binary for linux/amd64 and deploy to lakewood
printf "Building collector binary...\n"
(cd "$SCRIPT_DIR" && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o /tmp/webrtc-collector-build ./cmd/collector/)
on_lakewood "sudo rm -f $REMOTE_COLLECTOR_BIN; rm -f $REMOTE_COLLECTOR_BIN"
scp $SSH_OPTS /tmp/webrtc-collector-build "$LAKEWOOD_SSH:$REMOTE_COLLECTOR_BIN"
rm -f /tmp/webrtc-collector-build

# Clean stale state on lakewood
on_lakewood "sudo rm -f $REMOTE_COLLECTOR_CSV $REMOTE_MIGRATION_FLAG"

# Extract SSH user for remote stats (collector runs as root but root can't SSH; use the lab user)
REMOTE_SSH_USER="${LOVELAND_SSH%%@*}"

printf "Remote SSH user: %s\n" "$REMOTE_SSH_USER"

# Start collector on lakewood (runs locally there — no SSH overhead per tick)
# The collector runs as root (for podman/nsenter) but drops to REMOTE_SSH_USER
# for SSH commands via "sudo -u <user> ssh" so the user's SSH keys work.
on_lakewood "sudo nohup $REMOTE_COLLECTOR_BIN \
    -remote-direct-ip '$LOVELAND_DIRECT_IP' \
    -remote-ssh-user '$REMOTE_SSH_USER' \
    -metrics-port $METRICS_PORT \
    -ping-hosts '${H2_IP},${H3_IP}' \
    -server-names 'webrtc-server,h3' \
    -loadgen-container 'webrtc-loadgen' \
    -migration-flag '$REMOTE_MIGRATION_FLAG' \
    -output '$REMOTE_COLLECTOR_CSV' \
    -interval '$METRICS_INTERVAL' \
    > /tmp/collector.log 2>&1 &
    echo \$!" &
COLLECTOR_PID=$!
sleep 2
COLLECTOR_REMOTE_PID=$(on_lakewood "pgrep -f webrtc-collector | head -1" 2>/dev/null || true)
printf "Collector started on lakewood (remote PID %s)\n" "$COLLECTOR_REMOTE_PID"

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
# Step 9: CRIU migration(s) — N chained migrations without cleaning state
# =============================================================================
REMOTE_RESULTS_DIR="/tmp/migration_results"
for (( i=1; i <= MIGRATION_COUNT; i++ )); do
  if [[ $(( i % 2 )) -eq 1 ]]; then
    direction="lakewood_loveland"
    migration_ssh="$LAKEWOOD_SSH"
  else
    direction="loveland_lakewood"
    migration_ssh="$LOVELAND_SSH"
  fi
  printf "\n╔══════════════════════════════════════════╗\n"
  printf "║  Step 9.%d: CRIU migration %s (%d/%d)   ║\n" "$i" "$direction" "$i" "$MIGRATION_COUNT"
  printf "╚══════════════════════════════════════════╝\n\n"

  # Run cr_hw.sh ON the source node — local commands, direct-link to target
  # ForwardAgent so the source node can SSH to tofino for the switch update
  ssh $SSH_OPTS -o ForwardAgent=yes "$migration_ssh" \
    "cd $REMOTE_PROJECT_DIR/experiments && CR_RUN_LOCAL=1 CR_HW_RESULTS_PATH=$REMOTE_RESULTS_DIR bash cr_hw.sh $direction"

  # Copy migration_timing.txt back (per-migration file + latest as migration_timing.txt)
  scp $SSH_OPTS "$migration_ssh:$REMOTE_RESULTS_DIR/migration_timing.txt" "$RUN_DIR/migration_timing_${i}.txt"
  cp "$RUN_DIR/migration_timing_${i}.txt" "$RUN_DIR/migration_timing.txt"

  if [[ $i -lt $MIGRATION_COUNT ]]; then
    printf "\nWaiting %3ds before next migration...\n" "$POST_MIGRATION_WAIT"
    sleep "$POST_MIGRATION_WAIT"
  fi
done

# =============================================================================
# Step 10: Post-migration wait (after last migration)
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

# Stop collector on lakewood and copy CSV back
if [[ -n "$COLLECTOR_REMOTE_PID" ]]; then
    on_lakewood "sudo kill $COLLECTOR_REMOTE_PID 2>/dev/null; true" || true
    sleep 1
fi
if [[ -n "$COLLECTOR_PID" ]] && kill -0 "$COLLECTOR_PID" 2>/dev/null; then
    kill -TERM "$COLLECTOR_PID" 2>/dev/null || true
    wait "$COLLECTOR_PID" 2>/dev/null || true
fi
COLLECTOR_PID=""
COLLECTOR_REMOTE_PID=""

# Copy CSV from lakewood (chown first — collector ran as root)
on_lakewood "sudo chown \$(whoami) $REMOTE_COLLECTOR_CSV /tmp/collector.log 2>/dev/null; true"
scp $SSH_OPTS "$LAKEWOOD_SSH:$REMOTE_COLLECTOR_CSV" "$COLLECTOR_OUTPUT" 2>/dev/null || true
scp $SSH_OPTS "$LAKEWOOD_SSH:/tmp/collector.log" "$RUN_DIR/collector.log" 2>/dev/null || true

if [[ -f "$SCRIPT_DIR/analysis/plot_metrics.py" ]] && [[ -f "$COLLECTOR_OUTPUT" ]]; then
    cd "$SCRIPT_DIR/analysis"
    pip install -q -r requirements.txt 2>/dev/null || true
    # Pillow may be needed for matplotlib; upgrade if plot fails with _imaging error
    (pip install -q --upgrade Pillow 2>/dev/null || true)
    if python3 plot_metrics.py \
        --csv "$COLLECTOR_OUTPUT" \
        --migration-flag "$RUN_DIR" \
        --output-dir "$RUN_DIR"; then
        echo "Plots generated."
    else
        echo "Plot generation failed (non-fatal). See above for errors."
    fi
    cd "$SCRIPT_DIR"
fi

echo ""
echo "Results in: $RUN_DIR"
ls -la "$RUN_DIR" 2>/dev/null || true

printf "\n╔══════════════════════════════════════════╗\n"
printf "║  Experiment complete!                    ║\n"
printf "╚══════════════════════════════════════════╝\n\n"
printf "Run dir: %s\n" "$RUN_DIR"
printf "  config.txt, experiment.log, metrics.csv, migration_timing.txt"
[[ "$MIGRATION_COUNT" -gt 1 ]] && printf ", migration_timing_1.txt ... migration_timing_%d.txt" "$MIGRATION_COUNT"
printf "\n"
printf "  *.png (plots), error.log (only if failed)\n"
printf "To clean up: ./clean_hw.sh\n"
