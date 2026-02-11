#!/bin/bash
# =============================================================================
# cr_hw.sh — Cross-node CRIU migration (lakewood → loveland)
# =============================================================================
# Multi-node version of cr.sh:
#   1. Checkpoint the server container on lakewood (local)
#   2. SCP the checkpoint tarball to loveland
#   3. Edit checkpoint IPs on loveland (via SSH)
#   4. Restore on loveland's target pod (via SSH)
#   5. Update switch tables via controller /migrateNode API
#   6. Signal the metrics collector
#
# Usage:
#   ./cr_hw.sh              # migrate webrtc-server from lakewood to loveland
# =============================================================================

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/config_hw.env"

# Source and target are fixed for the hardware topology
SOURCE_IP="$H2_IP"       # server on lakewood
TARGET_IP="$H3_IP"       # target on loveland

CHECKPOINT_PATH="${CHECKPOINT_DIR}/checkpoint.tar"
CONTAINER_NAME=webrtc-server

remote() {
    local host="$1"; shift
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$host" "$@"
}

printf "===== Cross-node migration: lakewood (%s) -> loveland (%s) =====\n" \
    "$SOURCE_IP" "$TARGET_IP"

# Record start time for migration duration measurement
MIGRATION_START=$(date +%s%N)

# =============================================================================
# Step 1: Checkpoint the source container (on lakewood)
# =============================================================================
printf "\n----- Step 1: Checkpoint %s on lakewood -----\n" "$CONTAINER_NAME"
sudo mkdir -p "$CHECKPOINT_DIR"

sudo podman container checkpoint \
    --export "$CHECKPOINT_PATH" \
    --compress none \
    --keep \
    --tcp-established \
    "$CONTAINER_NAME"

CHECKPOINT_DONE=$(date +%s%N)
CHECKPOINT_MS=$(( (CHECKPOINT_DONE - MIGRATION_START) / 1000000 ))
printf "Checkpoint completed in %d ms\n" "$CHECKPOINT_MS"

# Remove the source container
sudo podman rm -f "$CONTAINER_NAME"

# =============================================================================
# Step 2: Transfer checkpoint to loveland (SCP)
# =============================================================================
printf "\n----- Step 2: SCP checkpoint to loveland -----\n"

# Ensure remote checkpoint dir exists
remote "$LOVELAND_SSH" "sudo mkdir -p $REMOTE_CHECKPOINT_DIR && sudo chmod 777 $REMOTE_CHECKPOINT_DIR"

# Transfer (sudo tar read → ssh → remote write to handle root-owned checkpoint)
sudo cat "$CHECKPOINT_PATH" | ssh -o BatchMode=yes "$LOVELAND_SSH" \
    "cat > ${REMOTE_CHECKPOINT_DIR}/checkpoint.tar"

TRANSFER_DONE=$(date +%s%N)
TRANSFER_MS=$(( (TRANSFER_DONE - CHECKPOINT_DONE) / 1000000 ))
CHECKPOINT_SIZE=$(sudo stat -c%s "$CHECKPOINT_PATH" 2>/dev/null || echo 0)
printf "Transfer completed in %d ms (%.1f MB)\n" \
    "$TRANSFER_MS" "$(echo "scale=1; $CHECKPOINT_SIZE / 1048576" | bc)"

# =============================================================================
# Step 3: Edit checkpoint IPs on loveland
# =============================================================================
printf "\n----- Step 3: Edit checkpoint IPs on loveland (%s -> %s) -----\n" \
    "$SOURCE_IP" "$TARGET_IP"

remote "$LOVELAND_SSH" "
    sudo -E env PATH=\"\$PATH\" python3 $REMOTE_EDIT_SCRIPT \
        ${REMOTE_CHECKPOINT_DIR}/checkpoint.tar \
        $SOURCE_IP $TARGET_IP
"

EDIT_DONE=$(date +%s%N)
EDIT_MS=$(( (EDIT_DONE - TRANSFER_DONE) / 1000000 ))
printf "IP edit completed in %d ms\n" "$EDIT_MS"

# =============================================================================
# Step 4: Restore on loveland's target pod
# =============================================================================
printf "\n----- Step 4: Restore on h3-pod (loveland) -----\n"

remote "$LOVELAND_SSH" "
    # Remove any existing container on target
    sudo podman container rm -f h3 2>/dev/null || true

    sudo podman container restore \
        --import ${REMOTE_CHECKPOINT_DIR}/checkpoint.tar \
        --keep \
        --tcp-established \
        --ignore-static-ip \
        --ignore-static-mac \
        --pod h3-pod

    # Rename the restored container
    sudo podman rename $CONTAINER_NAME h3 2>/dev/null || true
"

RESTORE_DONE=$(date +%s%N)
RESTORE_MS=$(( (RESTORE_DONE - EDIT_DONE) / 1000000 ))
printf "Restore completed in %d ms\n" "$RESTORE_MS"

# =============================================================================
# Step 5: Update switch tables via controller
# =============================================================================
printf "\n----- Step 5: Update switch tables -----\n"

SWITCH_UPDATE_START=$(date +%s%N)

HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    --connect-timeout 5 --max-time 10 \
    -X POST "${CONTROLLER_URL}/migrateNode" \
    -H "Content-Type: application/json" \
    -d "{\"old_ipv4\":\"${SOURCE_IP}\", \"new_ipv4\":\"${TARGET_IP}\"}")

SWITCH_UPDATE_DONE=$(date +%s%N)
SWITCH_MS=$(( (SWITCH_UPDATE_DONE - SWITCH_UPDATE_START) / 1000000 ))

if [ "$HTTP_CODE" = "200" ]; then
    printf "Switch update successful (HTTP %s) in %d ms\n" "$HTTP_CODE" "$SWITCH_MS"
else
    printf "WARNING: Switch update returned HTTP %s\n" "$HTTP_CODE"
fi

# =============================================================================
# Step 6: Signal migration event to collector
# =============================================================================
printf "\n----- Step 6: Signal migration event -----\n"
MIGRATION_END=$(date +%s%N)
TOTAL_MS=$(( (MIGRATION_END - MIGRATION_START) / 1000000 ))

cat > "$MIGRATION_FLAG_FILE" <<EOF
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
source_ip=$SOURCE_IP
target_ip=$TARGET_IP
source_node=lakewood
target_node=loveland
EOF

printf "\n===== Migration complete: lakewood -> loveland in %d ms =====\n" "$TOTAL_MS"
printf "  Checkpoint:    %4d ms\n" "$CHECKPOINT_MS"
printf "  Transfer:      %4d ms  (%.1f MB)\n" "$TRANSFER_MS" \
    "$(echo "scale=1; $CHECKPOINT_SIZE / 1048576" | bc)"
printf "  IP edit:       %4d ms\n" "$EDIT_MS"
printf "  Restore:       %4d ms\n" "$RESTORE_MS"
printf "  Switch update: %4d ms\n" "$SWITCH_MS"
printf "  ─────────────────────\n"
printf "  Total:         %4d ms\n" "$TOTAL_MS"
