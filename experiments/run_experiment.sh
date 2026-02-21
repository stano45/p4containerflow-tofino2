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
RUN_DIR="$SCRIPT_DIR/$RESULTS_DIR/run_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$RUN_DIR"

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

exec > >(tee "$RUN_DIR/experiment.log") 2>&1

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
on_lakewood() { ssh $SSH_OPTS "$LAKEWOOD_SSH" "$@"; }
on_loveland() { ssh $SSH_OPTS "$LOVELAND_SSH" "$@"; }
on_tofino()   { ssh $SSH_OPTS "$TOFINO_SSH" "$@"; }

COLLECTOR_PID=""          # local collector process PID
SSH_TUNNEL_PID=""         # SSH tunnel process PID
CONTROLLER_STARTED=false

cleanup_on_exit() {
    # Kill remote loadgen on lakewood
    ssh $SSH_OPTS "$LAKEWOOD_SSH" "sudo pkill -f '[s]tream-client' 2>/dev/null || true" 2>/dev/null || true
    if [[ -n "$COLLECTOR_PID" ]] && kill -0 "$COLLECTOR_PID" 2>/dev/null; then
        kill "$COLLECTOR_PID" 2>/dev/null || true
        wait "$COLLECTOR_PID" 2>/dev/null || true
    fi
    if [[ -n "$SSH_TUNNEL_PID" ]] && kill -0 "$SSH_TUNNEL_PID" 2>/dev/null; then
        kill "$SSH_TUNNEL_PID" 2>/dev/null || true
        wait "$SSH_TUNNEL_PID" 2>/dev/null || true
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

echo "--- Syncing controller code to tofino ---"
rsync -az -e "ssh $SSH_OPTS" \
    "$SCRIPT_DIR/../controller/"  "$TOFINO_SSH:$REMOTE_PROJECT_DIR/controller/" \
    --exclude '.venv' --exclude '__pycache__' --exclude '*.pyc'
echo "Controller synced"

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

echo "--- root@lakewood SSH to loveland (for collector fallback / manual debug) ---"
on_lakewood "sudo test -f /root/.ssh/id_ed25519 || sudo ssh-keygen -t ed25519 -N '' -f /root/.ssh/id_ed25519 -q" 2>/dev/null || true
ROOT_PUB=$(on_lakewood "sudo cat /root/.ssh/id_ed25519.pub" 2>/dev/null || true)
if [[ -n "$ROOT_PUB" ]]; then
    on_loveland "mkdir -p ~/.ssh && chmod 700 ~/.ssh && grep -qF '$(echo "$ROOT_PUB" | head -1)' ~/.ssh/authorized_keys 2>/dev/null || echo '$(echo "$ROOT_PUB" | head -1)' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys" 2>/dev/null || true
    echo "root SSH key authorized on loveland"
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
on_loveland "sudo podman load -i $SYNC_TMP.img && rm -f $SYNC_TMP.img && sudo podman tag $SERVER_IMAGE_ID $SERVER_IMAGE"
echo "Server image synced."

echo "Building loadgen binary locally..."
(cd "$SCRIPT_DIR" && CGO_ENABLED=0 go build -o "$SCRIPT_DIR/bin/stream-client" ./cmd/loadgen/)
echo "Loadgen built: $SCRIPT_DIR/bin/stream-client"

# =============================================================================
# Step 3: Start switchd on tofino (if not running)
# =============================================================================
printf "\n╔══════════════════════════════════════════╗\n"
printf "║  Step 3: Ensure switchd is running       ║\n"
printf "╚══════════════════════════════════════════╝\n\n"

SWITCHD_FRESH=false
if on_tofino "pgrep -x bf_switchd >/dev/null 2>&1"; then
    echo "switchd already running on tofino"
else
    SWITCHD_FRESH=true
    echo "Starting switchd on tofino (background)..."
    on_tofino "
        cd $REMOTE_PROJECT_DIR
        source ~/setup-open-p4studio.bash
        nohup bash -c 'make switch ARCH=tf1' > /tmp/switchd.log 2>&1 &
        echo \"switchd PID: \$!\"
    "
    echo "Waiting for switchd process to appear..."
    for i in $(seq 1 30); do
        if on_tofino "pgrep -x bf_switchd >/dev/null 2>&1"; then
            echo "switchd process up after ${i}s"
            break
        fi
        if [[ $i -eq 30 ]]; then
            echo "FAIL: switchd did not start within 30s"
            echo "Check /tmp/switchd.log on tofino"
            exit 1
        fi
        sleep 1
    done
    echo "Waiting for switchd to finish loading (server started marker)..."
    for i in $(seq 1 120); do
        if on_tofino "grep -q 'server started - listening on port 9999' /tmp/switchd.log 2>/dev/null" 2>/dev/null; then
            echo "switchd fully loaded after ${i}s"
            sleep 5
            break
        fi
        if [[ $i -eq 120 ]]; then
            echo "WARNING: switchd load not confirmed after 120s — continuing anyway"
        fi
        sleep 1
    done
fi

# =============================================================================
# Step 4: Start controller on tofino (if not running)
# =============================================================================
printf "\n╔══════════════════════════════════════════╗\n"
printf "║  Step 4: Ensure controller is running    ║\n"
printf "╚══════════════════════════════════════════╝\n\n"

# Helper: call controller API via SSH to tofino (avoids firewall issues)
ctrl_api() {
    local endpoint="$1"
    shift
    on_tofino "curl -sf $* http://127.0.0.1:5000${endpoint}" 2>/dev/null
}
ctrl_api_code() {
    local endpoint="$1"
    local code
    code=$(on_tofino "curl -s -o /dev/null -w '%{http_code}' --connect-timeout 3 http://127.0.0.1:5000${endpoint} -X POST" 2>/dev/null) || true
    echo "${code:-000}"
}

# Check if controller is already responding
CTRL_HTTP=$(ctrl_api_code "/reinitialize")
NEED_RESTART=false

if $SWITCHD_FRESH; then
    echo "switchd was freshly started — controller needs restart for fresh gRPC connection"
    NEED_RESTART=true
elif [[ "$CTRL_HTTP" != "200" && "$CTRL_HTTP" != "000" ]]; then
    echo "Controller returned HTTP $CTRL_HTTP — restarting for clean state"
    NEED_RESTART=true
fi

if [[ "$CTRL_HTTP" == "200" ]] && ! $NEED_RESTART; then
    echo "Controller already running (HTTP $CTRL_HTTP)"
else
    if [[ "$CTRL_HTTP" != "000" ]] || $NEED_RESTART; then
        echo "Killing stale controller..."
        on_tofino "fuser -k 5000/tcp 2>/dev/null; sleep 1; fuser -k 5000/tcp 2>/dev/null; true" || true
        sleep 3
    fi
    echo "Starting controller on tofino (background)..."
    on_tofino "
        cd $REMOTE_PROJECT_DIR/controller
        source ~/setup-open-p4studio.bash
        nohup bash -c 'ARCH=tf1 CONFIG_FILE=$CONTROLLER_CONFIG ./run.sh' > /tmp/controller.log 2>&1 &
        echo \"controller PID: \$!\"
    "
    echo "Waiting for controller to start..."
    for i in $(seq 1 30); do
        CTRL_HTTP=$(ctrl_api_code "/reinitialize")
        if [[ "$CTRL_HTTP" == "200" ]]; then
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
ctrl_api "/reinitialize" "-X POST -H 'Content-Type: application/json'" | jq . 2>/dev/null || true

# Delete the client_snat entry.  The SNAT rule rewrites the src IP of server
# responses (src_port=8080) from the real server IP (192.168.12.2) to the VIP
# (192.168.12.10).  This is correct for VIP-based load balancing where clients
# connect to the VIP, but BREAKS same-IP migration where the client connects
# directly to 192.168.12.2 — the client's TCP stack drops the response because
# the source IP doesn't match the established connection.
echo "Deleting client_snat entry (not needed for same-IP migration)..."
ctrl_api "/deleteClientSnat" "-X POST -H 'Content-Type: application/json'" | jq . 2>/dev/null || true

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
# Step 7: Deploy loadgen to lakewood + start collector locally
# =============================================================================
printf "\n╔══════════════════════════════════════════╗\n"
printf "║  Step 7: Start loadgen + collector       ║\n"
printf "╚══════════════════════════════════════════╝\n\n"

COLLECTOR_OUTPUT="$RUN_DIR/metrics.csv"
MIGRATION_FLAG="$RUN_DIR/migration_event"

printf "Building collector binary...\n"
(cd "$SCRIPT_DIR" && CGO_ENABLED=0 go build -o "$SCRIPT_DIR/bin/stream-collector" ./cmd/collector/)

printf "Building loadgen binary for lakewood (linux/amd64)...\n"
(cd "$SCRIPT_DIR" && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o /tmp/stream-client-build ./cmd/loadgen/)
scp $SSH_OPTS /tmp/stream-client-build "$LAKEWOOD_SSH:/tmp/stream-client"
rm -f /tmp/stream-client-build
echo "Loadgen deployed to lakewood:/tmp/stream-client"

on_lakewood "sudo pkill -f '[s]tream-client' 2>/dev/null || true"

# Start loadgen on lakewood — connects directly to the server container
# via the macvlan-shim. Measures true network RTT (sub-ms) without
# SSH tunnel overhead in the data path.
printf "Starting loadgen on lakewood: %d connections to http://%s:%s\n" \
    "$LOADGEN_CONNECTIONS" "$H2_IP" "$SIGNALING_PORT"
on_lakewood "nohup /tmp/stream-client \
    -server 'http://${H2_IP}:${SIGNALING_PORT}' \
    -connections $LOADGEN_CONNECTIONS \
    -metrics-port $LOADGEN_METRICS_PORT \
    > /tmp/loadgen.log 2>&1 &"
sleep 2
if ! on_lakewood "pgrep -f stream-client >/dev/null 2>&1"; then
    echo "FAIL: Loadgen did not start on lakewood."
    on_lakewood "cat /tmp/loadgen.log 2>/dev/null" || true
    exit 1
fi
echo "Loadgen running on lakewood"

# SSH tunnel for metrics collection only (not data path):
#   - loadgen metrics: lakewood:9090 → localhost:19090
#   - server metrics:  macshim→192.168.12.2:8081 → localhost:18081
printf "Starting SSH tunnel (metrics only): localhost:%s → lakewood:%s, localhost:%s → %s:%s\n" \
    "$SSH_TUNNEL_LOCAL_PORT" "$LOADGEN_METRICS_PORT" \
    "$SSH_TUNNEL_METRICS_PORT" "$H2_IP" "$METRICS_PORT"
ssh -N \
    -L "${SSH_TUNNEL_LOCAL_PORT}:localhost:${LOADGEN_METRICS_PORT}" \
    -L "${SSH_TUNNEL_METRICS_PORT}:${H2_IP}:${METRICS_PORT}" \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=10 \
    -o ServerAliveCountMax=6 \
    $SSH_OPTS "$LAKEWOOD_SSH" &
SSH_TUNNEL_PID=$!
sleep 2
if ! kill -0 "$SSH_TUNNEL_PID" 2>/dev/null; then
    echo "FAIL: SSH tunnel died immediately. Check SSH access to $LAKEWOOD_SSH."
    exit 1
fi
echo "SSH tunnel started (PID $SSH_TUNNEL_PID)"

# Start collector locally — scrapes loadgen + server metrics through SSH tunnel.
# The tunnel is only for metrics; the data path is loadgen→macshim→server.
printf "Starting collector...\n"
"$SCRIPT_DIR/bin/stream-collector" \
    -server-metrics-url "http://localhost:${SSH_TUNNEL_METRICS_PORT}" \
    -loadgen-url "http://localhost:${SSH_TUNNEL_LOCAL_PORT}" \
    -migration-flag "$MIGRATION_FLAG" \
    -output "$COLLECTOR_OUTPUT" \
    -interval "$METRICS_INTERVAL" \
    > "$RUN_DIR/collector.log" 2>&1 &
COLLECTOR_PID=$!
echo "Collector started (PID $COLLECTOR_PID)"
sleep 2

# =============================================================================
# Step 8: Wait for steady-state
# =============================================================================
printf "\n╔══════════════════════════════════════════╗\n"
printf "║  Step 8: Healthy, then %3ds steady-state ║\n" "$STEADY_STATE_WAIT"
printf "╚══════════════════════════════════════════╝\n\n"

echo "Waiting for server to become healthy (via lakewood macshim)..."
sleep 2
for i in $(seq 1 40); do
    if on_lakewood "curl -sf --connect-timeout 2 'http://${H2_IP}:${SIGNALING_PORT}/health'" >/dev/null 2>&1; then
        echo "Server reachable through macshim after $(( 2 + (i - 1) / 2 ))s"
        break
    fi
    if [[ $i -eq 40 ]]; then
        echo "WARNING: Server health check failed after 20s. Check macshim + container."
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

  touch "$MIGRATION_FLAG"

  # Run cr_hw.sh ON the source node — local commands, direct-link to target
  # ForwardAgent so the source node can SSH to tofino for the switch update
  ssh $SSH_OPTS -o ForwardAgent=yes "$migration_ssh" \
    "cd $REMOTE_PROJECT_DIR/experiments && CR_RUN_LOCAL=1 CR_HW_RESULTS_PATH=$REMOTE_RESULTS_DIR bash cr_hw.sh $direction"

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

# Stop collector FIRST so the last CSV row still has live data
if [[ -n "$COLLECTOR_PID" ]] && kill -0 "$COLLECTOR_PID" 2>/dev/null; then
    kill -TERM "$COLLECTOR_PID" 2>/dev/null || true
    wait "$COLLECTOR_PID" 2>/dev/null || true
fi
COLLECTOR_PID=""

# Stop remote loadgen on lakewood and copy its log back
on_lakewood "sudo pkill -f '[s]tream-client' 2>/dev/null || true"
scp $SSH_OPTS "$LAKEWOOD_SSH:/tmp/loadgen.log" "$RUN_DIR/loadgen.log" 2>/dev/null || true

if [[ -n "$SSH_TUNNEL_PID" ]] && kill -0 "$SSH_TUNNEL_PID" 2>/dev/null; then
    kill -TERM "$SSH_TUNNEL_PID" 2>/dev/null || true
    wait "$SSH_TUNNEL_PID" 2>/dev/null || true
fi
SSH_TUNNEL_PID=""

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
