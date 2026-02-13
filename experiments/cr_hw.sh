#!/bin/bash
# =============================================================================
# cr_hw.sh — Cross-node CRIU migration (lakewood → loveland)
# =============================================================================
# Run from your control machine. Orchestrates via SSH:
#   1. Checkpoint on lakewood
#   2. Pull checkpoint to control machine, push to loveland (proxy transfer)
#   3. Edit checkpoint IPs on loveland
#   4. Restore on loveland
#   5. Update switch tables via controller API
#   6. Write migration timing to flag file
#
# Usage:
#   ./cr_hw.sh
# =============================================================================

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/config_hw.env"

SOURCE_IP="$H2_IP"       # server on lakewood
TARGET_IP="$H3_IP"       # target on loveland

CONTAINER_NAME=webrtc-server
LOCAL_TMP="/tmp/cr_hw_transfer"

on_lakewood() { ssh $SSH_OPTS "$LAKEWOOD_SSH" "$@"; }
on_loveland() { ssh $SSH_OPTS "$LOVELAND_SSH" "$@"; }

cleanup_tmp() { rm -rf "$LOCAL_TMP" 2>/dev/null || true; }
trap cleanup_tmp EXIT

printf "===== Cross-node migration: lakewood (%s) -> loveland (%s) =====\n" \
    "$SOURCE_IP" "$TARGET_IP"

MIGRATION_START=$(date +%s%N)

# =============================================================================
# Step 1: Checkpoint on lakewood
# =============================================================================
printf "\n----- Step 1: Checkpoint %s on lakewood -----\n" "$CONTAINER_NAME"

on_lakewood "
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
# Step 2: Transfer checkpoint (lakewood → control → loveland)
# =============================================================================
printf "\n----- Step 2: Proxy transfer via control machine -----\n"

mkdir -p "$LOCAL_TMP"

# Pull from lakewood
ssh $SSH_OPTS "$LAKEWOOD_SSH" "sudo cat $CHECKPOINT_DIR/checkpoint.tar" \
    > "$LOCAL_TMP/checkpoint.tar"

CHECKPOINT_SIZE=$(stat -c%s "$LOCAL_TMP/checkpoint.tar" 2>/dev/null || stat -f%z "$LOCAL_TMP/checkpoint.tar" 2>/dev/null || echo 0)
printf "Downloaded %.1f MB from lakewood\n" "$(echo "scale=1; $CHECKPOINT_SIZE / 1048576" | bc)"

# Push to loveland
on_loveland "sudo mkdir -p $CHECKPOINT_DIR && sudo chmod 777 $CHECKPOINT_DIR"
cat "$LOCAL_TMP/checkpoint.tar" | ssh $SSH_OPTS "$LOVELAND_SSH" "cat > $CHECKPOINT_DIR/checkpoint.tar"

TRANSFER_DONE=$(date +%s%N)
TRANSFER_MS=$(( (TRANSFER_DONE - CHECKPOINT_DONE) / 1000000 ))
printf "Transfer completed in %d ms (%.1f MB)\n" "$TRANSFER_MS" \
    "$(echo "scale=1; $CHECKPOINT_SIZE / 1048576" | bc)"

# Clean local temp
rm -rf "$LOCAL_TMP"

# =============================================================================
# Step 3: Edit checkpoint IPs on loveland
# =============================================================================
printf "\n----- Step 3: Edit checkpoint IPs on loveland (%s -> %s) -----\n" \
    "$SOURCE_IP" "$TARGET_IP"

# Patch checkpoint: replace source image ID with short name so restore can resolve image on loveland (and CNI gets valid containerID)
CHECKPOINT_IMAGE_NAME="$SERVER_IMAGE"
on_loveland "
    export PATH=\"\$HOME/.local/bin:\$PATH\"
    python3 $REMOTE_EDIT_SCRIPT \
        $CHECKPOINT_DIR/checkpoint.tar \
        $SOURCE_IP $TARGET_IP \
        $CHECKPOINT_IMAGE_NAME
"

EDIT_DONE=$(date +%s%N)
EDIT_MS=$(( (EDIT_DONE - TRANSFER_DONE) / 1000000 ))
printf "IP edit completed in %d ms\n" "$EDIT_MS"

# =============================================================================
# Step 4: Restore on loveland
# =============================================================================
printf "\n----- Step 4: Restore on loveland -----\n"

# Ensure the image exists on loveland under the short name (restore + CNI need it)
if ! on_loveland "sudo podman image exists $CHECKPOINT_IMAGE_NAME 2>/dev/null"; then
    echo "Tagging localhost/${SERVER_IMAGE}:latest as $CHECKPOINT_IMAGE_NAME on loveland..."
    if ! on_loveland "sudo podman tag localhost/${SERVER_IMAGE}:latest $CHECKPOINT_IMAGE_NAME"; then
        echo "ERROR: Image $CHECKPOINT_IMAGE_NAME not found on loveland (could not tag from localhost/${SERVER_IMAGE}:latest)."
        echo "       Images on loveland:"
        on_loveland "sudo podman images --format '{{.Repository}}:{{.Tag}}'" 2>/dev/null || true
        echo "       Run ./build_hw.sh or ./run_experiment.sh first so the image exists on the target."
        exit 1
    fi
fi
if ! on_loveland "sudo podman image exists $CHECKPOINT_IMAGE_NAME 2>/dev/null"; then
    echo "ERROR: Image $CHECKPOINT_IMAGE_NAME not found on loveland."
    echo "       Images on loveland:"
    on_loveland "sudo podman images --format '{{.Repository}}:{{.Tag}}'" 2>/dev/null || true
    echo "       Run ./build_hw.sh or ./run_experiment.sh first so the image exists on the target."
    exit 1
fi
echo "Image $CHECKPOINT_IMAGE_NAME found on loveland (listing below)."
on_loveland "sudo podman images --filter reference='*webrtc*' --format '  {{.Repository}}:{{.Tag}} ({{.ID}})'" 2>/dev/null || true

RESTORE_START=$(date +%s%N)
if ! on_loveland "
    sudo podman container rm -f h3 $CONTAINER_NAME 2>/dev/null || true
    sudo podman container restore \
        --import $CHECKPOINT_DIR/checkpoint.tar \
        --keep \
        --tcp-established \
        --ignore-static-ip \
        --ignore-static-mac
"; then
    echo "ERROR: Restore failed (see above)."
    exit 1
fi
on_loveland "sudo podman rename $CONTAINER_NAME h3 2>/dev/null || true"
RESTORE_DONE=$(date +%s%N)
RESTORE_MS=$(( (RESTORE_DONE - RESTORE_START) / 1000000 ))
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
# Step 6: Write migration timing
# =============================================================================
MIGRATION_END=$(date +%s%N)
TOTAL_MS=$(( (MIGRATION_END - MIGRATION_START) / 1000000 ))

RESULTS_PATH="$SCRIPT_DIR/$RESULTS_DIR"
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
source_ip=$SOURCE_IP
target_ip=$TARGET_IP
source_node=lakewood
target_node=loveland
EOF

# Also write flag file on lakewood (collector may watch it)
on_lakewood "cat > /tmp/migration_event" <<EOF
migration_end_ns=$MIGRATION_END
total_ms=$TOTAL_MS
source_ip=$SOURCE_IP
target_ip=$TARGET_IP
EOF

printf "\n===== Migration complete: lakewood -> loveland in %d ms =====\n" "$TOTAL_MS"
printf "  Checkpoint:    %4d ms\n" "$CHECKPOINT_MS"
printf "  Transfer:      %4d ms  (%.1f MB, via control machine)\n" "$TRANSFER_MS" \
    "$(echo "scale=1; $CHECKPOINT_SIZE / 1048576" | bc)"
printf "  IP edit:       %4d ms\n" "$EDIT_MS"
printf "  Restore:       %4d ms\n" "$RESTORE_MS"
printf "  Switch update: %4d ms\n" "$SWITCH_MS"
printf "  ─────────────────────\n"
printf "  Total:         %4d ms\n" "$TOTAL_MS"
