#!/bin/bash
# =============================================================================
# cr_hw.sh — Cross-node CRIU migration (configurable direction)
# =============================================================================
# Best run ON the source node (CR_RUN_LOCAL=1, set by run_experiment.sh).
# Source commands run locally (zero overhead), target commands go over the
# direct 25G link (~0.2ms). Only the tofino call uses the university network.
#
# Can also run from a control machine (CR_RUN_LOCAL unset) — all commands
# go over SSH, adding ~700ms overhead per call.
#
# Usage:
#   ./cr_hw.sh [direction]
#   direction: lakewood_loveland (default) or loveland_lakewood
#   CR_RUN_LOCAL=1: run on the source node (source=local, target=direct link)
#   CR_HW_RESULTS_PATH: dir for migration_timing.txt
# =============================================================================

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/config_hw.env"

# Direction: lakewood_loveland (default) or loveland_lakewood
MIGRATION_DIRECTION="${1:-lakewood_loveland}"
if [[ "$MIGRATION_DIRECTION" = "loveland_lakewood" ]]; then
  SOURCE_IP="$H3_IP"
  TARGET_IP="$H2_IP"
  SOURCE_SSH="$LOVELAND_SSH"
  TARGET_SSH="$LAKEWOOD_SSH"
  TARGET_DIRECT_IP="$LAKEWOOD_DIRECT_IP"
  SOURCE_DIRECT_IP="$LOVELAND_DIRECT_IP"
  SOURCE_DIRECT_IF="$LOVELAND_DIRECT_IF"
  SOURCE_NODE="loveland"
  TARGET_NODE="lakewood"
  CONTAINER_NAME="h3"
  RENAME_AFTER_RESTORE="webrtc-server"
else
  SOURCE_IP="$H2_IP"
  TARGET_IP="$H3_IP"
  SOURCE_SSH="$LAKEWOOD_SSH"
  TARGET_SSH="$LOVELAND_SSH"
  TARGET_DIRECT_IP="$LOVELAND_DIRECT_IP"
  SOURCE_DIRECT_IP="$LAKEWOOD_DIRECT_IP"
  SOURCE_DIRECT_IF="$LAKEWOOD_DIRECT_IF"
  SOURCE_NODE="lakewood"
  TARGET_NODE="loveland"
  CONTAINER_NAME="webrtc-server"
  RENAME_AFTER_RESTORE="h3"
fi

if [[ "${CR_RUN_LOCAL:-}" = "1" ]]; then
  # Running ON the source node: source=local, target=direct link SSH
  on_source() { bash -c "$*"; }
  on_target() { ssh $SSH_OPTS "$TARGET_DIRECT_IP" "$@"; }
else
  # Running from control machine: everything over university-network SSH
  on_source() { ssh $SSH_OPTS "$SOURCE_SSH" "$@"; }
  on_target() { ssh $SSH_OPTS "$TARGET_SSH" "$@"; }
fi
on_tofino() { ssh $SSH_OPTS "$TOFINO_SSH" "$@"; }

printf "===== Cross-node migration: %s (%s) -> %s (%s) =====\n" \
    "$SOURCE_NODE" "$SOURCE_IP" "$TARGET_NODE" "$TARGET_IP"

MIGRATION_START=$(date +%s%N)

# =============================================================================
# Step 1: Checkpoint on source (leave container running until migration complete)
# =============================================================================
printf "\n----- Step 1: Checkpoint %s on %s (--leave-running) -----\n" "$CONTAINER_NAME" "$SOURCE_NODE"

# Get container's image ID (checkpoint stores this; we transfer the image so no patching)
SOURCE_IMAGE_ID=$(on_source "sudo podman inspect $CONTAINER_NAME --format '{{.Image}}'" 2>/dev/null || true)
if [[ -z "$SOURCE_IMAGE_ID" || ${#SOURCE_IMAGE_ID} -ne 64 ]]; then
    echo "ERROR: Could not get image ID from container $CONTAINER_NAME on $SOURCE_NODE."
    exit 1
fi

on_source "
    sudo mkdir -p $CHECKPOINT_DIR
    sudo podman container checkpoint \
        --export $CHECKPOINT_DIR/checkpoint.tar \
        --compress none \
        --keep \
        --tcp-established \
        --leave-running \
        $CONTAINER_NAME
"
# (--compress none: direct cable is fast; no compress/decompress saves CPU and latency)

CHECKPOINT_DONE=$(date +%s%N)
CHECKPOINT_MS=$(( (CHECKPOINT_DONE - MIGRATION_START) / 1000000 ))
printf "Checkpoint completed in %d ms\n" "$CHECKPOINT_MS"

# =============================================================================
# Step 2: Transfer checkpoint + image (source → target, direct)
# =============================================================================
printf "\n----- Step 2: Direct transfer %s → %s -----\n" "$SOURCE_NODE" "$TARGET_NODE"

# --- Pre-transfer: target dir, image check, size, port setup (measured separately)
PRE_TRANSFER_START=$(date +%s%N)
# Ensure target has the directory
on_target "sudo mkdir -p $CHECKPOINT_DIR && sudo chmod 777 $CHECKPOINT_DIR"

# Image must already exist on target (synced at experiment setup). Transfer only the checkpoint.
if ! on_target "sudo podman image exists $SOURCE_IMAGE_ID 2>/dev/null"; then
    echo "ERROR: Image $SOURCE_IMAGE_ID not found on $TARGET_NODE."
    echo "  Run full experiment setup (run_experiment.sh) so the server image is synced to both nodes."
    exit 1
fi

CHECKPOINT_SIZE=$(on_source "sudo stat -c%s $CHECKPOINT_DIR/checkpoint.tar 2>/dev/null" || echo 0)
IMAGE_SIZE=0

# Direct link (Mellanox 25G DAC). Use socat with reverse connection: source listens, target
# connects — no sleep needed; listener is up before connector runs.
# Ensure source's direct-link interface is up (may be DOWN after reboot).
on_source "sudo ip link set $SOURCE_DIRECT_IF up 2>/dev/null || true"

# Use random port (32000–33999); override with CR_TRANSFER_PORT if set
if [[ -n "${CR_TRANSFER_PORT:-}" ]]; then
  TRANSFER_PORT=$CR_TRANSFER_PORT
else
  TRANSFER_PORT=$(( 32000 + (RANDOM % 2000) ))
fi
printf "Transferring %.1f MB to %s via direct link (port %s)...\n" "$(echo "scale=1; ${CHECKPOINT_SIZE:-0} / 1048576" | bc)" "$TARGET_NODE" "$TRANSFER_PORT"

# Stale listener on source port; old file on target
on_source "sudo fuser -k ${TRANSFER_PORT}/tcp 2>/dev/null || true"
on_target "sudo rm -f $CHECKPOINT_DIR/checkpoint.tar; true"

PRE_TRANSFER_DONE=$(date +%s%N)
PRE_TRANSFER_MS=$(( (PRE_TRANSFER_DONE - PRE_TRANSFER_START) / 1000000 ))

# Reverse connection: target connects, source listens. Start connector with short delay so
# listener is up first; run listener in foreground so we need no PID and no long sleep.
( sleep 0.02; on_target "socat STDIO TCP:${SOURCE_DIRECT_IP}:${TRANSFER_PORT} > $CHECKPOINT_DIR/checkpoint.tar" ) &
TARGET_PID=$!
TRANSFER_START=$(date +%s%N)
on_source "sudo bash -c \"socat TCP-LISTEN:${TRANSFER_PORT},bind=${SOURCE_DIRECT_IP},reuseaddr STDIN < ${CHECKPOINT_DIR}/checkpoint.tar\"" || {
  wait $TARGET_PID 2>/dev/null || true
  echo "ERROR: Direct-link transfer failed. Need socat on both nodes; check $SOURCE_DIRECT_IF on $SOURCE_NODE."
  exit 1
}
wait $TARGET_PID 2>/dev/null; TARGET_EXIT=$?
TRANSFER_DONE=$(date +%s%N)
if [[ ${TARGET_EXIT:-0} -ne 0 ]]; then
  echo "ERROR: Target connector failed (e.g. connection refused). Ensure socat on both nodes."
  exit 1
fi

# Verify checkpoint arrived (optional; set CR_SKIP_VERIFY=1 to skip and save ~1 SSH RTT)
if [[ "${CR_SKIP_VERIFY:-0}" = "1" ]]; then
  POST_TRANSFER_MS=0
  RECV_SIZE=$CHECKPOINT_SIZE
else
  POST_TRANSFER_START=$(date +%s%N)
  RECV_SIZE=$(on_target "stat -c%s $CHECKPOINT_DIR/checkpoint.tar 2>/dev/null" || echo 0)
  if [[ -z "$RECV_SIZE" || "$RECV_SIZE" -eq 0 || "$RECV_SIZE" -ne "$CHECKPOINT_SIZE" ]]; then
    echo "ERROR: Transfer verification failed: got ${RECV_SIZE:-0} bytes, expected $CHECKPOINT_SIZE. Listener may have failed (e.g. port in use)."
    exit 1
  fi
  POST_TRANSFER_DONE=$(date +%s%N)
  POST_TRANSFER_MS=$(( (POST_TRANSFER_DONE - POST_TRANSFER_START) / 1000000 ))
fi

TRANSFER_MS=$(( (TRANSFER_DONE - TRANSFER_START) / 1000000 ))
printf "Transfer completed in %d ms\n" "$TRANSFER_MS"

# =============================================================================
# Step 3: Edit checkpoint IPs on target
# =============================================================================
printf "\n----- Step 3: Edit checkpoint IPs on %s (%s -> %s) -----\n" \
    "$TARGET_NODE" "$SOURCE_IP" "$TARGET_IP"

EDIT_START=$(date +%s%N)
on_target "
    export PATH=\"\$HOME/.local/bin:\$PATH\"
    if test -x ${REMOTE_EDIT_BIN} 2>/dev/null; then
      ${REMOTE_EDIT_BIN} $CHECKPOINT_DIR/checkpoint.tar $SOURCE_IP $TARGET_IP
    else
      python3 $REMOTE_EDIT_SCRIPT $CHECKPOINT_DIR/checkpoint.tar $SOURCE_IP $TARGET_IP
    fi
"

EDIT_DONE=$(date +%s%N)
EDIT_MS=$(( (EDIT_DONE - EDIT_START) / 1000000 ))
printf "IP edit completed in %d ms\n" "$EDIT_MS"

# =============================================================================
# Step 4: Restore on target
# =============================================================================
printf "\n----- Step 4: Restore on %s -----\n" "$TARGET_NODE"

# Image already verified on target in Step 2; skip redundant check to save ~1 SSH RTT
PRE_RESTORE_MS=0
echo "Image ${SOURCE_IMAGE_ID:0:12}... (verified in Step 2)."

RESTORE_START=$(date +%s%N)
if ! on_target "
    sudo podman container rm -f $RENAME_AFTER_RESTORE $CONTAINER_NAME 2>/dev/null || true
    sudo podman container restore \
        --import $CHECKPOINT_DIR/checkpoint.tar \
        --keep \
        --tcp-established \
        --ignore-static-ip \
        --ignore-static-mac
"; then
    echo "ERROR: Restore failed (see above)."
    echo "On $TARGET_NODE check: sudo cat /var/lib/containers/storage/overlay-containers/<container-id>/userdata/restore.log"
    exit 1
fi
on_target "sudo podman rename $CONTAINER_NAME $RENAME_AFTER_RESTORE 2>/dev/null || true"
RESTORE_DONE=$(date +%s%N)
RESTORE_MS=$(( (RESTORE_DONE - RESTORE_START) / 1000000 ))
printf "Restore completed in %d ms\n" "$RESTORE_MS"

POST_RESTORE_MS=0
SWITCH_UPDATE_START=$(date +%s%N)

# =============================================================================
# Step 5: Update switch tables via controller
# =============================================================================
printf "\n----- Step 5: Update switch tables -----\n"

# Call controller from source node (direct HTTP) when reachable; else fall back to SSH to tofino
HTTP_CODE=$(on_source "curl -s -o /dev/null -w '%{http_code}' --connect-timeout 3 --max-time 5 \
    -X POST '${CONTROLLER_URL}/migrateNode' \
    -H 'Content-Type: application/json' \
    -d '{\"old_ipv4\":\"${SOURCE_IP}\", \"new_ipv4\":\"${TARGET_IP}\"}'" 2>/dev/null || true)
if [[ -z "$HTTP_CODE" || "$HTTP_CODE" = "000" ]]; then
  HTTP_CODE=$(on_tofino "curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 10 \
      -X POST 'http://127.0.0.1:5000/migrateNode' \
      -H 'Content-Type: application/json' \
      -d '{\"old_ipv4\":\"${SOURCE_IP}\", \"new_ipv4\":\"${TARGET_IP}\"}'" 2>/dev/null || true)
fi
if [[ -z "$HTTP_CODE" ]]; then HTTP_CODE=000; fi

SWITCH_UPDATE_DONE=$(date +%s%N)
SWITCH_MS=$(( (SWITCH_UPDATE_DONE - SWITCH_UPDATE_START) / 1000000 ))

# Client-visible migration ends here: new node is restored and switch points to it (downtime metric)
TIME_TO_READY_MS=$(( (SWITCH_UPDATE_DONE - MIGRATION_START) / 1000000 ))

if [ "$HTTP_CODE" = "200" ]; then
    printf "Switch update successful (HTTP %s) in %d ms\n" "$HTTP_CODE" "$SWITCH_MS"
else
    printf "WARNING: Switch update returned HTTP %s\n" "$HTTP_CODE"
fi

# =============================================================================
# Step 5b: Stop and remove source container (was left running during transfer/restore)
# =============================================================================
printf "\n----- Step 5b: Stop source container on %s -----\n" "$SOURCE_NODE"
SOURCE_STOP_START=$(date +%s%N)
on_source "sudo podman stop $CONTAINER_NAME 2>/dev/null; sudo podman rm -f $CONTAINER_NAME 2>/dev/null; true"
SOURCE_STOP_DONE=$(date +%s%N)
SOURCE_STOP_MS=$(( (SOURCE_STOP_DONE - SOURCE_STOP_START) / 1000000 ))
echo "Source container stopped and removed (${SOURCE_STOP_MS} ms)."

# =============================================================================
# Step 6: Write migration timing
# =============================================================================
POST_SWITCH_START=$(date +%s%N)

RESULTS_PATH="${CR_HW_RESULTS_PATH:-$SCRIPT_DIR/$RESULTS_DIR}"
mkdir -p "$RESULTS_PATH"

# Signal the collector (running on lakewood) that migration happened
on_source "touch /tmp/collector_migration_flag 2>/dev/null" || true
on_target "touch /tmp/collector_migration_flag 2>/dev/null" || true

MIGRATION_END=$(date +%s%N)
TOTAL_MS=$(( (MIGRATION_END - MIGRATION_START) / 1000000 ))
POST_SWITCH_MS=$(( (MIGRATION_END - POST_SWITCH_START) / 1000000 ))

cat > "$RESULTS_PATH/migration_timing.txt" <<EOF
migration_start_ns=$MIGRATION_START
checkpoint_done_ns=$CHECKPOINT_DONE
transfer_done_ns=$TRANSFER_DONE
edit_done_ns=$EDIT_DONE
restore_done_ns=$RESTORE_DONE
switch_update_done_ns=$SWITCH_UPDATE_DONE
migration_end_ns=$MIGRATION_END
total_ms=$TOTAL_MS
checkpoint_ms=$CHECKPOINT_MS
transfer_ms=$TRANSFER_MS
edit_ms=$EDIT_MS
restore_ms=$RESTORE_MS
switch_ms=$SWITCH_MS
checkpoint_size_bytes=$CHECKPOINT_SIZE
image_size_bytes=${IMAGE_SIZE:-0}
source_ip=$SOURCE_IP
target_ip=$TARGET_IP
source_node=$SOURCE_NODE
target_node=$TARGET_NODE
transfer_method=socat
pre_transfer_ms=$PRE_TRANSFER_MS
post_transfer_ms=$POST_TRANSFER_MS
pre_restore_ms=$PRE_RESTORE_MS
post_switch_ms=$POST_SWITCH_MS
source_stop_ms=$SOURCE_STOP_MS
time_to_ready_ms=$TIME_TO_READY_MS
EOF

# Phased sum for full script (all phases)
PHASED_SUM=$(( CHECKPOINT_MS + PRE_TRANSFER_MS + TRANSFER_MS + POST_TRANSFER_MS + EDIT_MS + PRE_RESTORE_MS + RESTORE_MS + SWITCH_MS + SOURCE_STOP_MS + POST_SWITCH_MS ))
OVERHEAD_MS=$(( TOTAL_MS - PHASED_SUM ))
[[ $OVERHEAD_MS -lt 0 ]] && OVERHEAD_MS=0

# Phased sum up to "client-visible ready" (excludes source stop, post-switch, overhead)
READY_PHASED_SUM=$(( CHECKPOINT_MS + PRE_TRANSFER_MS + TRANSFER_MS + POST_TRANSFER_MS + EDIT_MS + PRE_RESTORE_MS + RESTORE_MS + SWITCH_MS ))
READY_OVERHEAD_MS=$(( TIME_TO_READY_MS - READY_PHASED_SUM ))
[[ $READY_OVERHEAD_MS -lt 0 ]] && READY_OVERHEAD_MS=0

printf "\n===== Migration: %s -> %s =====\n" "$SOURCE_NODE" "$TARGET_NODE"
printf "  >>> Client-visible (downtime) : %5d ms  <<<\n" "$TIME_TO_READY_MS"
printf "      (new node ready + switch updated; source stop is after this)\n"
printf "  Checkpoint:    %4d ms  (CRIU dump, leave-running)\n" "$CHECKPOINT_MS"
printf "  Pre-transfer: %4d ms\n" "$PRE_TRANSFER_MS"
printf "  Transfer:     %4d ms  (%.1f MB)\n" "$TRANSFER_MS" \
    "$(echo "scale=1; ${CHECKPOINT_SIZE:-0} / 1048576" | bc)"
printf "  Post-transfer: %4d ms\n" "$POST_TRANSFER_MS"
printf "  IP edit:      %4d ms\n" "$EDIT_MS"
printf "  Pre-restore:  %4d ms\n" "$PRE_RESTORE_MS"
printf "  Restore:      %4d ms\n" "$RESTORE_MS"
printf "  Switch update:%4d ms\n" "$SWITCH_MS"
printf "  ─────────────────────\n"
printf "  (ready phased sum: %d ms)\n" "$READY_PHASED_SUM"
printf "\n  Cleanup (not part of client downtime):\n"
printf "  Source stop:  %4d ms\n" "$SOURCE_STOP_MS"
printf "  Post-switch:  %4d ms  (flags, results)\n" "$POST_SWITCH_MS"
printf "  Overhead:     %4d ms\n" "$OVERHEAD_MS"
printf "  ─────────────────────\n"
printf "  Full script total: %d ms\n" "$TOTAL_MS"
