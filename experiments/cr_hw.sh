#!/bin/bash
# =============================================================================
# cr_hw.sh — Cross-node CRIU migration (configurable direction)
# =============================================================================
# Run from your control machine. Orchestrates via SSH:
#   1. Checkpoint on source node
#   2. Transfer checkpoint + image directly source → target (SSH agent forwarding)
#   3. Edit checkpoint IPs on target
#   4. Restore on target
#   5. Update switch tables via controller API
#   6. Write migration timing to RESULTS_PATH (or CR_HW_RESULTS_PATH)
#
# Usage:
#   ./cr_hw.sh [direction]
#   direction: lakewood_loveland (default) or loveland_lakewood
#   CR_HW_RESULTS_PATH: optional dir for migration_timing.txt (default: $SCRIPT_DIR/$RESULTS_DIR)
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

on_source() { ssh $SSH_OPTS "$SOURCE_SSH" "$@"; }
on_target() { ssh $SSH_OPTS "$TARGET_SSH" "$@"; }
on_tofino() { ssh $SSH_OPTS "$TOFINO_SSH" "$@"; }

printf "===== Cross-node migration: %s (%s) -> %s (%s) =====\n" \
    "$SOURCE_NODE" "$SOURCE_IP" "$TARGET_NODE" "$TARGET_IP"

MIGRATION_START=$(date +%s%N)

# =============================================================================
# Step 1: Checkpoint on source (and capture image ID before rm)
# =============================================================================
printf "\n----- Step 1: Checkpoint %s on %s -----\n" "$CONTAINER_NAME" "$SOURCE_NODE"

# Get container's image ID before we remove it (checkpoint stores this; we transfer the image so no patching)
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
        $CONTAINER_NAME
    sudo podman rm -f $CONTAINER_NAME
"

CHECKPOINT_DONE=$(date +%s%N)
CHECKPOINT_MS=$(( (CHECKPOINT_DONE - MIGRATION_START) / 1000000 ))
printf "Checkpoint completed in %d ms\n" "$CHECKPOINT_MS"

# =============================================================================
# Step 2: Transfer checkpoint + image (source → target, direct)
# =============================================================================
printf "\n----- Step 2: Direct transfer %s → %s -----\n" "$SOURCE_NODE" "$TARGET_NODE"

# Ensure target has the directory
on_target "sudo mkdir -p $CHECKPOINT_DIR && sudo chmod 777 $CHECKPOINT_DIR"

# Image must already exist on target (synced at experiment setup). Transfer only the checkpoint.
if ! on_target "sudo podman image exists $SOURCE_IMAGE_ID 2>/dev/null"; then
    echo "ERROR: Image $SOURCE_IMAGE_ID not found on $TARGET_NODE."
    echo "  Run full experiment setup (run_experiment.sh) so the server image is synced to both nodes."
    exit 1
fi

CHECKPOINT_SIZE=$(on_source "stat -c%s $CHECKPOINT_DIR/checkpoint.tar 2>/dev/null" || echo 0)
IMAGE_SIZE=0

# Direct link (Mellanox 25G DAC, topology.md): ncat over $TARGET_DIRECT_IP. No SSH in data path.
# Ensure source's direct-link interface is up (lakewood's enp179s0f0np0 is often DOWN after reboot).
on_source "sudo ip link set $SOURCE_DIRECT_IF up 2>/dev/null || true"

# Use random port (32000–33999) to avoid "Address already in use" from stale ncat; override with CR_TRANSFER_PORT if set
if [[ -n "${CR_TRANSFER_PORT:-}" ]]; then
  TRANSFER_PORT=$CR_TRANSFER_PORT
else
  TRANSFER_PORT=$(( 32000 + (RANDOM % 2000) ))
fi
printf "Transferring checkpoint only to %s (%.1f MB) via direct link (port %s)...\n" "$TARGET_NODE" "$(echo "scale=1; ${CHECKPOINT_SIZE:-0} / 1048576" | bc)" "$TRANSFER_PORT"

on_target "ncat -l -p $TRANSFER_PORT > $CHECKPOINT_DIR/checkpoint.tar" &
NC_PID=$!
sleep 0.5
# Bind sender to direct-link IP so traffic uses 25G DAC, not management path
on_source "sudo cat $CHECKPOINT_DIR/checkpoint.tar | ncat -w 30 -s $SOURCE_DIRECT_IP $TARGET_DIRECT_IP $TRANSFER_PORT" || {
  kill $NC_PID 2>/dev/null || true
  wait $NC_PID 2>/dev/null || true
  echo "ERROR: Direct-link transfer failed. Check $SOURCE_DIRECT_IF up on $SOURCE_NODE and ncat on both nodes."
  exit 1
}
wait $NC_PID 2>/dev/null || true

# Verify checkpoint arrived (non-empty and correct size); avoid empty-file in edit step
RECV_SIZE=$(on_target "stat -c%s $CHECKPOINT_DIR/checkpoint.tar 2>/dev/null" || echo 0)
if [[ -z "$RECV_SIZE" || "$RECV_SIZE" -eq 0 || "$RECV_SIZE" -ne "$CHECKPOINT_SIZE" ]]; then
  echo "ERROR: Transfer verification failed: got ${RECV_SIZE:-0} bytes, expected $CHECKPOINT_SIZE. Listener may have failed (e.g. port in use)."
  exit 1
fi

TRANSFER_DONE=$(date +%s%N)
TRANSFER_MS=$(( (TRANSFER_DONE - CHECKPOINT_DONE) / 1000000 ))
printf "Transfer completed in %d ms (checkpoint only, no image)\n" "$TRANSFER_MS"

# =============================================================================
# Step 3: Edit checkpoint IPs on target (IP only; no image patching)
# =============================================================================
printf "\n----- Step 3: Edit checkpoint IPs on %s (%s -> %s) -----\n" \
    "$TARGET_NODE" "$SOURCE_IP" "$TARGET_IP"

on_target "
    export PATH=\"\$HOME/.local/bin:\$PATH\"
    python3 $REMOTE_EDIT_SCRIPT \
        $CHECKPOINT_DIR/checkpoint.tar \
        $SOURCE_IP $TARGET_IP
"

EDIT_DONE=$(date +%s%N)
EDIT_MS=$(( (EDIT_DONE - TRANSFER_DONE) / 1000000 ))
printf "IP edit completed in %d ms\n" "$EDIT_MS"

# =============================================================================
# Step 4: Restore on target
# =============================================================================
printf "\n----- Step 4: Restore on %s -----\n" "$TARGET_NODE"

# Checkpoint references source image ID; we require it on target (synced at setup)
if ! on_target "sudo podman image exists $SOURCE_IMAGE_ID 2>/dev/null"; then
    echo "ERROR: Image $SOURCE_IMAGE_ID not found on $TARGET_NODE."
    exit 1
fi
echo "Image ${SOURCE_IMAGE_ID:0:12}... found on $TARGET_NODE."

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

# =============================================================================
# Step 5: Update switch tables via controller
# =============================================================================
printf "\n----- Step 5: Update switch tables -----\n"

SWITCH_UPDATE_START=$(date +%s%N)

# Call controller from tofino (localhost:5000) so it works even when control machine can't reach CONTROLLER_URL
HTTP_CODE=$(on_tofino "curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 10 \
    -X POST 'http://127.0.0.1:5000/migrateNode' \
    -H 'Content-Type: application/json' \
    -d '{\"old_ipv4\":\"${SOURCE_IP}\", \"new_ipv4\":\"${TARGET_IP}\"}'" 2>/dev/null || true)
if [[ -z "$HTTP_CODE" ]]; then HTTP_CODE=000; fi

SWITCH_UPDATE_DONE=$(date +%s%N)
SWITCH_MS=$(( (SWITCH_UPDATE_DONE - SWITCH_UPDATE_START) / 1000000 ))

if [ "$HTTP_CODE" = "200" ]; then
    printf "Switch update successful (HTTP %s) in %d ms\n" "$HTTP_CODE" "$SWITCH_MS"
else
    printf "WARNING: Switch update returned HTTP %s\n" "$HTTP_CODE"
fi

# =============================================================================
# Step 6: Write migration timing
# =============================================================================
MIGRATION_END=$(date +%s%N)
TOTAL_MS=$(( (MIGRATION_END - MIGRATION_START) / 1000000 ))

RESULTS_PATH="${CR_HW_RESULTS_PATH:-$SCRIPT_DIR/$RESULTS_DIR}"
mkdir -p "$RESULTS_PATH"

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
transfer_method=ncat
EOF

printf "\n===== Migration complete: %s -> %s in %d ms =====\n" "$SOURCE_NODE" "$TARGET_NODE" "$TOTAL_MS"
printf "  Checkpoint:    %4d ms\n" "$CHECKPOINT_MS"
printf "  Transfer:      %4d ms  (checkpoint only %.1f MB, direct %s→%s)\n" "$TRANSFER_MS" \
    "$(echo "scale=1; ${CHECKPOINT_SIZE:-0} / 1048576" | bc)" "$SOURCE_NODE" "$TARGET_NODE"
printf "  IP edit:       %4d ms\n" "$EDIT_MS"
printf "  Restore:       %4d ms\n" "$RESTORE_MS"
printf "  Switch update: %4d ms\n" "$SWITCH_MS"
printf "  ─────────────────────\n"
printf "  Total:         %4d ms\n" "$TOTAL_MS"
