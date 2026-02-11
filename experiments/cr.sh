#!/bin/bash
# =============================================================================
# cr.sh — CRIU-based container migration + switch table update
# =============================================================================
# Follows the same pattern as p4containerflow/examples/redis/cr.sh:
#   1. Checkpoint the source container with --tcp-established
#   2. Edit checkpoint IP addresses with edit_files_img.py
#   3. Restore on the target pod
#   4. Update switch tables via controller /migrateNode API
#   5. Signal the metrics collector
#
# Usage:
#   ./cr.sh <source_idx> <target_idx>
#   ./cr.sh 2 3    # migrate WebRTC server from h2 (10.0.2.2) to h3 (10.0.3.3)
# =============================================================================

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/config.env"

# -----------------------------------------------------------------------------
# Argument handling
# -----------------------------------------------------------------------------
if [ "$#" -ne 2 ]; then
    echo "Usage: $0 <source_idx> <target_idx>"
    echo "  Example: $0 2 3  (migrate from h2 to h3)"
    exit 1
fi

SOURCE_IDX=$1
TARGET_IDX=$2

SOURCE_HOST=h${SOURCE_IDX}
SOURCE_IP=10.0.${SOURCE_IDX}.${SOURCE_IDX}

TARGET_HOST=h${TARGET_IDX}
TARGET_IP=10.0.${TARGET_IDX}.${TARGET_IDX}
TARGET_MAC=08:00:00:00:0${TARGET_IDX}:0${TARGET_IDX}

CHECKPOINT_PATH=${CHECKPOINT_DIR}/checkpoint.tar
CONTAINER_NAME=webrtc-server

printf "===== Migration: %s (%s) -> %s (%s) =====\n" \
    "$SOURCE_HOST" "$SOURCE_IP" "$TARGET_HOST" "$TARGET_IP"

# Record start time for migration duration measurement
MIGRATION_START=$(date +%s%N)

# -----------------------------------------------------------------------------
# Step 1: Checkpoint the source container
# -----------------------------------------------------------------------------
printf "\n----- Step 1: Checkpoint %s -----\n" "$CONTAINER_NAME"
sudo mkdir -p "$CHECKPOINT_DIR"

sudo podman container checkpoint \
    --export "$CHECKPOINT_PATH" \
    --compress none \
    --keep \
    --tcp-established \
    "$CONTAINER_NAME"

CHECKPOINT_DONE=$(date +%s%N)
printf "Checkpoint completed in %d ms\n" $(( (CHECKPOINT_DONE - MIGRATION_START) / 1000000 ))

# Remove the source container
sudo podman rm -f "$CONTAINER_NAME"

# -----------------------------------------------------------------------------
# Step 2: Edit checkpoint IP addresses
# -----------------------------------------------------------------------------
printf "\n----- Step 2: Edit checkpoint IPs (%s -> %s) -----\n" "$SOURCE_IP" "$TARGET_IP"

# Resolve edit_files_img.py path
EDIT_SCRIPT="$SCRIPT_DIR/$EDIT_FILES_IMG"
if [ ! -f "$EDIT_SCRIPT" ]; then
    # Try absolute path
    EDIT_SCRIPT="$EDIT_FILES_IMG"
fi
if [ ! -f "$EDIT_SCRIPT" ]; then
    echo "ERROR: edit_files_img.py not found at $EDIT_FILES_IMG"
    echo "Copy it from p4containerflow/scripts/edit_files_img.py"
    exit 1
fi

sudo -E env PATH="$PATH" "$EDIT_SCRIPT" "$CHECKPOINT_PATH" "$SOURCE_IP" "$TARGET_IP"

EDIT_DONE=$(date +%s%N)
printf "IP edit completed in %d ms\n" $(( (EDIT_DONE - CHECKPOINT_DONE) / 1000000 ))

# -----------------------------------------------------------------------------
# Step 3: Remove any existing container on target, then restore
# -----------------------------------------------------------------------------
printf "\n----- Step 3: Restore on %s-pod -----\n" "$TARGET_HOST"

# Remove existing container on target if present
sudo podman container rm -f "${TARGET_HOST}" 2>/dev/null || true

sudo podman container restore \
    --import "$CHECKPOINT_PATH" \
    --keep \
    --tcp-established \
    --ignore-static-ip \
    --ignore-static-mac \
    --pod "${TARGET_HOST}-pod"

RESTORE_DONE=$(date +%s%N)
printf "Restore completed in %d ms\n" $(( (RESTORE_DONE - EDIT_DONE) / 1000000 ))

# Rename the restored container (--name cannot be used with --tcp-established)
sudo podman rename "$CONTAINER_NAME" "${TARGET_HOST}" 2>/dev/null || true

# -----------------------------------------------------------------------------
# Step 4: Update switch tables via controller
# -----------------------------------------------------------------------------
printf "\n----- Step 4: Update switch tables -----\n"

SWITCH_UPDATE_START=$(date +%s%N)

HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST "${CONTROLLER_URL}/migrateNode" \
    -H "Content-Type: application/json" \
    -d "{\"old_ipv4\":\"${SOURCE_IP}\", \"new_ipv4\":\"${TARGET_IP}\"}")

SWITCH_UPDATE_DONE=$(date +%s%N)

if [ "$HTTP_CODE" = "200" ]; then
    printf "Switch update successful (HTTP %s) in %d ms\n" \
        "$HTTP_CODE" $(( (SWITCH_UPDATE_DONE - SWITCH_UPDATE_START) / 1000000 ))
else
    printf "WARNING: Switch update returned HTTP %s (controller may not be running)\n" "$HTTP_CODE"
    printf "  This is expected when running locally without the Tofino switch/model.\n"
fi

# -----------------------------------------------------------------------------
# Step 5: Signal migration event to collector
# -----------------------------------------------------------------------------
printf "\n----- Step 5: Signal migration event -----\n"
MIGRATION_END=$(date +%s%N)
TOTAL_MS=$(( (MIGRATION_END - MIGRATION_START) / 1000000 ))

# Write migration event file with timing info
cat > "$MIGRATION_FLAG_FILE" <<EOF
migration_start_ns=$MIGRATION_START
checkpoint_done_ns=$CHECKPOINT_DONE
edit_done_ns=$EDIT_DONE
restore_done_ns=$RESTORE_DONE
switch_update_done_ns=$SWITCH_UPDATE_DONE
migration_end_ns=$MIGRATION_END
total_ms=$TOTAL_MS
source_ip=$SOURCE_IP
target_ip=$TARGET_IP
EOF

printf "\n===== Migration complete: %s -> %s in %d ms =====\n" \
    "$SOURCE_IP" "$TARGET_IP" "$TOTAL_MS"
printf "  Checkpoint:    %d ms\n" $(( (CHECKPOINT_DONE - MIGRATION_START) / 1000000 ))
printf "  IP edit:       %d ms\n" $(( (EDIT_DONE - CHECKPOINT_DONE) / 1000000 ))
printf "  Restore:       %d ms\n" $(( (RESTORE_DONE - EDIT_DONE) / 1000000 ))
printf "  Switch update: %d ms\n" $(( (SWITCH_UPDATE_DONE - SWITCH_UPDATE_START) / 1000000 ))
