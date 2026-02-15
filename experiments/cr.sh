#!/bin/bash
# =============================================================================
# cr.sh — CRIU-based container migration + switch table update (local)
# =============================================================================
# Usage:
#   ./cr.sh <source_idx> <target_idx>
#   ./cr.sh 2 3    # migrate server from h2 (10.0.2.2) to h3 (10.0.3.3)
# =============================================================================

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/config.env"

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
CONTAINER_NAME=stream-server

printf "===== Migration: %s (%s) -> %s (%s) =====\n" \
    "$SOURCE_HOST" "$SOURCE_IP" "$TARGET_HOST" "$TARGET_IP"

MIGRATION_START=$(date +%s%N)

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

sudo podman rm -f "$CONTAINER_NAME"

printf "\n----- Step 2: Edit checkpoint IPs (%s -> %s) -----\n" "$SOURCE_IP" "$TARGET_IP"

EDIT_SCRIPT="$SCRIPT_DIR/$EDIT_FILES_IMG"
if [ ! -f "$EDIT_SCRIPT" ]; then
    EDIT_SCRIPT="$EDIT_FILES_IMG"
fi
if [ ! -f "$EDIT_SCRIPT" ]; then
    echo "ERROR: edit_files_img.py not found at $EDIT_FILES_IMG"
    exit 1
fi

sudo -E env PATH="$PATH" "$EDIT_SCRIPT" "$CHECKPOINT_PATH" "$SOURCE_IP" "$TARGET_IP"

EDIT_DONE=$(date +%s%N)
printf "IP edit completed in %d ms\n" $(( (EDIT_DONE - CHECKPOINT_DONE) / 1000000 ))

printf "\n----- Step 3: Restore on %s -----\n" "$TARGET_HOST"

sudo podman container rm -f "${TARGET_HOST}" "$CONTAINER_NAME" 2>/dev/null || true

sudo podman container restore \
    --import "$CHECKPOINT_PATH" \
    --keep \
    --tcp-established \
    --ignore-static-ip \
    --ignore-static-mac

RESTORE_DONE=$(date +%s%N)
printf "Restore completed in %d ms\n" $(( (RESTORE_DONE - EDIT_DONE) / 1000000 ))

sudo podman rename "$CONTAINER_NAME" "${TARGET_HOST}" 2>/dev/null || true

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
    printf "WARNING: Switch update returned HTTP %s\n" "$HTTP_CODE"
fi

printf "\n----- Step 5: Signal migration event -----\n"
MIGRATION_END=$(date +%s%N)
TOTAL_MS=$(( (MIGRATION_END - MIGRATION_START) / 1000000 ))

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
