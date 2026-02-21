#!/bin/bash
# =============================================================================
# cr_hw.sh — Cross-node CRIU migration (same-IP, transparent TCP)
# =============================================================================
# Migrates a container between lakewood and loveland while keeping the same
# IP address (192.168.12.2). The P4 switch's forward table is updated to
# route .2 to the new physical port. TCP/WebSocket connections survive.
#
# Key design decisions:
#   - No --leave-running: TCP state must be frozen at checkpoint time so the
#     restored container has exactly the same seq/ack numbers as the client.
#   - No IP editing: server keeps 192.168.12.2 on both nodes.
#   - No SIGUSR1/reconnection: connections survive transparently.
#   - /updateForward instead of /migrateNode: only the forward table changes.
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

# Switch port mapping
LAKEWOOD_SW_PORT=${LAKEWOOD_SW_PORT:-140}
LOVELAND_SW_PORT=${LOVELAND_SW_PORT:-148}

if [[ "$MIGRATION_DIRECTION" = "loveland_lakewood" ]]; then
  SOURCE_SSH="$LOVELAND_SSH"
  TARGET_SSH="$LAKEWOOD_SSH"
  TARGET_DIRECT_IP="$LAKEWOOD_DIRECT_IP"
  SOURCE_DIRECT_IP="$LOVELAND_DIRECT_IP"
  SOURCE_DIRECT_IF="$LOVELAND_DIRECT_IF"
  SOURCE_NODE="loveland"
  TARGET_NODE="lakewood"
  CONTAINER_NAME="h3"
  RENAME_AFTER_RESTORE="stream-server"
  TARGET_SW_PORT=$LAKEWOOD_SW_PORT
  TARGET_NIC=$LAKEWOOD_NIC
else
  SOURCE_SSH="$LAKEWOOD_SSH"
  TARGET_SSH="$LOVELAND_SSH"
  TARGET_DIRECT_IP="$LOVELAND_DIRECT_IP"
  SOURCE_DIRECT_IP="$LAKEWOOD_DIRECT_IP"
  SOURCE_DIRECT_IF="$LAKEWOOD_DIRECT_IF"
  SOURCE_NODE="lakewood"
  TARGET_NODE="loveland"
  CONTAINER_NAME="stream-server"
  RENAME_AFTER_RESTORE="h3"
  TARGET_SW_PORT=$LOVELAND_SW_PORT
  TARGET_NIC=$LOVELAND_NIC
fi

# The server always keeps the same IP
SERVER_IP="$H2_IP"

if [[ "${CR_RUN_LOCAL:-}" = "1" ]]; then
  on_source() { bash -c "$*"; }
  on_target() { ssh $SSH_OPTS "$TARGET_DIRECT_IP" "$@"; }
else
  on_source() { ssh $SSH_OPTS "$SOURCE_SSH" "$@"; }
  on_target() { ssh $SSH_OPTS "$TARGET_SSH" "$@"; }
fi
on_tofino() { ssh $SSH_OPTS "$TOFINO_SSH" "$@"; }

printf "===== Cross-node migration: %s -> %s (same IP: %s, target port: %d) =====\n" \
    "$SOURCE_NODE" "$TARGET_NODE" "$SERVER_IP" "$TARGET_SW_PORT"

MIGRATION_START=$(date +%s%N)

# =============================================================================
# Step 1: Checkpoint on source (container STOPS — TCP state frozen)
# =============================================================================
printf "\n----- Step 1: Checkpoint %s on %s -----\n" "$CONTAINER_NAME" "$SOURCE_NODE"

SOURCE_IMAGE_ID=$(on_source "sudo podman inspect $CONTAINER_NAME --format '{{.Image}}'" 2>/dev/null || true)
if [[ -z "$SOURCE_IMAGE_ID" || ${#SOURCE_IMAGE_ID} -ne 64 ]]; then
    echo "ERROR: Could not get image ID from container $CONTAINER_NAME on $SOURCE_NODE."
    exit 1
fi

# Start target prep in background (overlaps with checkpoint)
if [[ "${CR_SKIP_IMAGE_CHECK:-0}" = "1" ]]; then
  on_target "sudo mkdir -p $CHECKPOINT_DIR && sudo chmod 777 $CHECKPOINT_DIR && sudo rm -f $CHECKPOINT_DIR/checkpoint.tar; echo OK" > /tmp/cr_target_prep_$$.out 2>&1 &
else
  on_target "sudo mkdir -p $CHECKPOINT_DIR && sudo chmod 777 $CHECKPOINT_DIR && sudo podman image exists $SOURCE_IMAGE_ID 2>/dev/null && sudo rm -f $CHECKPOINT_DIR/checkpoint.tar; echo IMG_OK" > /tmp/cr_target_prep_$$.out 2>&1 &
fi
TARGET_PREP_PID=$!

# Ensure CRIU skips in-flight (half-open) connections.
on_source "sudo mkdir -p /etc/criu && echo 'skip-in-flight' | sudo tee /etc/criu/default.conf >/dev/null"

# Quiesce: pause server data frames AND echo handler so TCP send queue drains
# before checkpoint.  SIGUSR2 toggles the quiesce flag; writer goroutines stop
# generating frames and the echo handler stops writing responses.
on_source "sudo podman kill --signal SIGUSR2 $CONTAINER_NAME"

# Wait for TCP send queues to fully drain.  CRIU must replay the "not-sent"
# portion of each send queue on restore, which requires a working network
# interface.  After cross-node migration the macvlan is recreated but might
# have brief connectivity gaps.  An active drain loop avoids any timing gamble.
_SRV_PID=$(on_source "sudo podman inspect --format '{{.State.Pid}}' $CONTAINER_NAME 2>/dev/null" || true)
if [[ -n "$_SRV_PID" && "$_SRV_PID" != "0" ]]; then
    for _i in $(seq 1 20); do  # up to 20 × 100ms = 2s
        _SQ=$(on_source "sudo nsenter -t $_SRV_PID -n ss -tn state established 2>/dev/null | awk 'NR>1{s+=\$2} END{print s+0}'" 2>/dev/null | tr -d '[:space:]')
        if [[ "${_SQ:-0}" = "0" ]]; then
            printf "Send queues drained in %d00 ms\n" "$_i"
            break
        fi
        sleep 0.1
    done
    if [[ "${_SQ:-0}" != "0" ]]; then
        printf "WARNING: Send queues still have %s bytes after 2s drain\n" "$_SQ"
    fi
else
    sleep 0.2
fi

on_source "
    sudo mkdir -p $CHECKPOINT_DIR
    sudo podman container checkpoint \
        --export $CHECKPOINT_DIR/checkpoint.tar \
        --compress none \
        --keep \
        --tcp-established \
        $CONTAINER_NAME
"

CHECKPOINT_DONE=$(date +%s%N)
CHECKPOINT_MS=$(( (CHECKPOINT_DONE - MIGRATION_START) / 1000000 ))
printf "Checkpoint completed in %d ms (container stopped)\n" "$CHECKPOINT_MS"

# =============================================================================
# Step 2: Transfer checkpoint (source → target, direct)
# =============================================================================
printf "\n----- Step 2: Direct transfer %s → %s -----\n" "$SOURCE_NODE" "$TARGET_NODE"

PRE_TRANSFER_START=$(date +%s%N)
wait $TARGET_PREP_PID 2>/dev/null || true
TARGET_PREP=$(cat /tmp/cr_target_prep_$$.out 2>/dev/null); rm -f /tmp/cr_target_prep_$$.out
if [[ "${CR_SKIP_IMAGE_CHECK:-0}" = "1" ]]; then
  [[ "$TARGET_PREP" = *"OK"* ]] || { echo "ERROR: Target pre-transfer prep failed."; exit 1; }
else
  [[ "$TARGET_PREP" = *"IMG_OK"* ]] || { echo "ERROR: Image $SOURCE_IMAGE_ID not found on $TARGET_NODE or target prep failed."; exit 1; }
fi

CHECKPOINT_SIZE=$(on_source "sudo stat -c%s $CHECKPOINT_DIR/checkpoint.tar 2>/dev/null" || echo 0)

on_source "sudo ip link set $SOURCE_DIRECT_IF up 2>/dev/null || true"

if [[ -n "${CR_TRANSFER_PORT:-}" ]]; then
  TRANSFER_PORT=$CR_TRANSFER_PORT
else
  TRANSFER_PORT=$(( 32000 + (RANDOM % 2000) ))
fi
printf "Transferring %.1f MB to %s via direct link (port %s)...\n" "$(echo "scale=1; ${CHECKPOINT_SIZE:-0} / 1048576" | bc)" "$TARGET_NODE" "$TRANSFER_PORT"

on_source "sudo fuser -k ${TRANSFER_PORT}/tcp 2>/dev/null || true"

PRE_TRANSFER_DONE=$(date +%s%N)
PRE_TRANSFER_MS=$(( (PRE_TRANSFER_DONE - PRE_TRANSFER_START) / 1000000 ))

( sleep 0.005; on_target "socat STDIO TCP:${SOURCE_DIRECT_IP}:${TRANSFER_PORT} > $CHECKPOINT_DIR/checkpoint.tar" ) &
TARGET_PID=$!
TRANSFER_START=$(date +%s%N)
on_source "sudo bash -c \"socat TCP-LISTEN:${TRANSFER_PORT},bind=${SOURCE_DIRECT_IP},reuseaddr STDIN < ${CHECKPOINT_DIR}/checkpoint.tar\"" || {
  wait $TARGET_PID 2>/dev/null || true
  echo "ERROR: Direct-link transfer failed."
  exit 1
}
wait $TARGET_PID 2>/dev/null; TARGET_EXIT=$?
TRANSFER_DONE=$(date +%s%N)
if [[ ${TARGET_EXIT:-0} -ne 0 ]]; then
  echo "ERROR: Target connector failed."
  exit 1
fi

if [[ "${CR_SKIP_VERIFY:-0}" = "1" ]]; then
  POST_TRANSFER_MS=0
  RECV_SIZE=$CHECKPOINT_SIZE
else
  POST_TRANSFER_START=$(date +%s%N)
  RECV_SIZE=$(on_target "stat -c%s $CHECKPOINT_DIR/checkpoint.tar 2>/dev/null" || echo 0)
  if [[ -z "$RECV_SIZE" || "$RECV_SIZE" -eq 0 || "$RECV_SIZE" -ne "$CHECKPOINT_SIZE" ]]; then
    echo "ERROR: Transfer verification failed: got ${RECV_SIZE:-0} bytes, expected $CHECKPOINT_SIZE."
    exit 1
  fi
  POST_TRANSFER_DONE=$(date +%s%N)
  POST_TRANSFER_MS=$(( (POST_TRANSFER_DONE - POST_TRANSFER_START) / 1000000 ))
fi

TRANSFER_MS=$(( (TRANSFER_DONE - TRANSFER_START) / 1000000 ))
printf "Transfer completed in %d ms\n" "$TRANSFER_MS"

# =============================================================================
# Step 3: Restore on target (same IP — no IP editing needed)
# =============================================================================
printf "\n----- Step 3: Restore on %s (same IP %s) -----\n" "$TARGET_NODE" "$SERVER_IP"

# Remove the source container NOW.  Its macvlan still has the server IP and
# responds to ARP broadcasts on the source NIC.  If we leave it alive, the
# client's ARP re-resolution (~20-30 s later) picks up the old (local) MAC
# instead of the migrated server's MAC, breaking the data path.
# The checkpoint tar is already on the target, so the source is dispensable.
on_source "sudo podman rm -f $CONTAINER_NAME 2>/dev/null; true" &
SOURCE_RM_PID=$!

# Ensure CRIU skips in-flight (half-open) connections on the target.
on_target "sudo mkdir -p /etc/criu && echo 'skip-in-flight' | sudo tee /etc/criu/default.conf >/dev/null" 2>/dev/null

PRE_RESTORE_MS=0

# Ensure source container is fully removed before we restore (ARP interference fix)
wait $SOURCE_RM_PID 2>/dev/null || true

RESTORE_START=$(date +%s%N)
if ! on_target "
    sudo podman container rm -f $RENAME_AFTER_RESTORE $CONTAINER_NAME 2>/dev/null || true
    sudo podman container restore \
        --import $CHECKPOINT_DIR/checkpoint.tar \
        --keep \
        --tcp-established \
        --ignore-static-mac
"; then
    # Fetch CRIU restore log for diagnosis
    RESTORE_LOG=$(on_target "
        CID=\$(sudo podman ps -a --no-trunc --format '{{.ID}}' --filter name=$CONTAINER_NAME 2>/dev/null | head -1)
        if [ -z \"\$CID\" ]; then CID=$CONTAINER_NAME; fi
        LOG=/var/lib/containers/storage/overlay-containers/\${CID}/userdata/restore.log
        if [ -f \"\$LOG\" ]; then sudo grep -i 'error\|fail\|broken\|queue' \"\$LOG\" | tail -20; fi
    " 2>/dev/null || true)
    printf "\n===== CRIU RESTORE FAILED on %s =====\n" "$TARGET_NODE"
    printf "Direction:    %s -> %s\n" "$SOURCE_NODE" "$TARGET_NODE"
    printf "Container:    %s\n" "$CONTAINER_NAME"
    printf "Checkpoint:   %s/checkpoint.tar (%s bytes)\n" "$CHECKPOINT_DIR" "${CHECKPOINT_SIZE:-?}"
    if [[ -n "$RESTORE_LOG" ]]; then
        printf "\nCRIU restore log (errors):\n%s\n" "$RESTORE_LOG"
        if echo "$RESTORE_LOG" | grep -q "send queue data.*Broken pipe"; then
            printf "\nDiagnosis: TCP send-queue data could not be replayed (EPIPE).\n"
            printf "The server had unsent data buffered at checkpoint time. After multiple\n"
            printf "migration cycles the peer's TCP state drifted enough to reject the\n"
            printf "replayed queue. This is a known CRIU limitation with --tcp-established\n"
            printf "under sustained throughput. Reducing --fps or checkpoint frequency helps.\n"
        fi
    else
        printf "\n(Could not retrieve CRIU restore log)\n"
    fi
    printf "=====\n"
    exit 1
fi
on_target "sudo podman rename $CONTAINER_NAME $RENAME_AFTER_RESTORE 2>/dev/null || true"

# Fix the macvlan interface: CRIU restores the container's network namespace from
# the checkpoint, but the macvlan references the SOURCE host's NIC index, which
# is wrong on the target.  We recreate it with the SAME fixed MAC ($H2_MAC) that
# the container was created with, so the client's ARP cache stays valid and
# packets arriving from the switch are delivered to the correct macvlan.
on_target "
    _CTR_PID=\$(sudo podman inspect --format '{{.State.Pid}}' $RENAME_AFTER_RESTORE 2>/dev/null || sudo podman inspect --format '{{.State.Pid}}' $CONTAINER_NAME 2>/dev/null || echo 0)
    if [ \"\$_CTR_PID\" != '0' ] && [ -n \"\$_CTR_PID\" ]; then
        sudo nsenter -t \$_CTR_PID -n ip link del eth0 2>/dev/null || true
        sudo ip link add cr_mv_eth0 link $TARGET_NIC address $H2_MAC type macvlan mode vepa
        sudo ip link set cr_mv_eth0 netns \$_CTR_PID
        sudo nsenter -t \$_CTR_PID -n ip link set cr_mv_eth0 name eth0
        sudo nsenter -t \$_CTR_PID -n ip addr add $SERVER_IP/24 dev eth0
        sudo nsenter -t \$_CTR_PID -n ip link set eth0 up

        # Flush the route cache so TCP sockets do a fresh lookup through the new eth0
        sudo nsenter -t \$_CTR_PID -n ip route flush cache 2>/dev/null || true

        # Pre-populate ARP for the macvlan-shim (loadgen's SSH tunnel source)
        # so the first retransmission doesn't wait for ARP resolution
        sudo nsenter -t \$_CTR_PID -n ip neigh replace $H1_IP dev eth0 lladdr $H1_MAC nud reachable 2>/dev/null || true

        # Gratuitous ARP to update upstream caches (switch, NICs in promisc mode)
        sudo nsenter -t \$_CTR_PID -n arping -U -c 2 -I eth0 $SERVER_IP >/dev/null 2>&1 &

        echo \"macvlan recreated on $TARGET_NIC, $SERVER_IP/$H2_MAC assigned (PID \$_CTR_PID)\"
    else
        echo 'WARNING: Could not determine restored container PID'
    fi
"

# Resume data frames (server was quiesced before checkpoint, still quiesced after restore)
on_target "sudo podman kill --signal SIGUSR2 $RENAME_AFTER_RESTORE 2>/dev/null || sudo podman kill --signal SIGUSR2 $CONTAINER_NAME 2>/dev/null || true"

RESTORE_DONE=$(date +%s%N)
RESTORE_MS=$(( (RESTORE_DONE - RESTORE_START) / 1000000 ))
printf "Restore completed in %d ms\n" "$RESTORE_MS"

# =============================================================================
# Step 4: Update P4 switch forward table
# =============================================================================
printf "\n----- Step 4: Update switch forward table (.2 -> port %d) -----\n" "$TARGET_SW_PORT"

SWITCH_UPDATE_START=$(date +%s%N)

HTTP_CODE=$(on_source "curl -s -o /dev/null -w '%{http_code}' --connect-timeout 2 --max-time 4 \
    -X POST '${CONTROLLER_URL}/updateForward' \
    -H 'Content-Type: application/json' \
    -d '{\"ipv4\":\"${SERVER_IP}\", \"sw_port\":${TARGET_SW_PORT}, \"dst_mac\":\"${H2_MAC}\"}'" 2>/dev/null || true)
if [[ -z "$HTTP_CODE" || "$HTTP_CODE" = "000" ]]; then
  HTTP_CODE=$(on_tofino "curl -s -o /dev/null -w '%{http_code}' --connect-timeout 3 --max-time 6 \
      -X POST 'http://127.0.0.1:5000/updateForward' \
      -H 'Content-Type: application/json' \
      -d '{\"ipv4\":\"${SERVER_IP}\", \"sw_port\":${TARGET_SW_PORT}, \"dst_mac\":\"${H2_MAC}\"}'" 2>/dev/null || true)
fi
if [[ -z "$HTTP_CODE" ]]; then HTTP_CODE=000; fi

SWITCH_UPDATE_DONE=$(date +%s%N)
SWITCH_MS=$(( (SWITCH_UPDATE_DONE - SWITCH_UPDATE_START) / 1000000 ))

# Client-visible migration ends here
TIME_TO_READY_MS=$(( (SWITCH_UPDATE_DONE - MIGRATION_START) / 1000000 ))

if [ "$HTTP_CODE" = "200" ]; then
    printf "Switch update successful (HTTP %s) in %d ms\n" "$HTTP_CODE" "$SWITCH_MS"
else
    printf "WARNING: Switch update returned HTTP %s\n" "$HTTP_CODE"
fi

# No loadgen ARP update needed — loadgen connects through an SSH tunnel via
# lakewood's macvlan-shim. The shim's ARP cache is on lakewood's kernel.

# =============================================================================
# Step 5: Source container already removed (done during pre-restore to prevent
#         the old macvlan from interfering with ARP resolution)
# =============================================================================
printf "\n----- Step 5: Source container already removed -----\n"
SOURCE_STOP_MS=0

# =============================================================================
# Step 6: Write migration timing
# =============================================================================
POST_SWITCH_START=$(date +%s%N)

RESULTS_PATH="${CR_HW_RESULTS_PATH:-$SCRIPT_DIR/$RESULTS_DIR}"
mkdir -p "$RESULTS_PATH"

# Signal the collector that migration happened
on_source "touch /tmp/collector_migration_flag 2>/dev/null" || true
on_target "touch /tmp/collector_migration_flag 2>/dev/null" || true

MIGRATION_END=$(date +%s%N)
TOTAL_MS=$(( (MIGRATION_END - MIGRATION_START) / 1000000 ))
POST_SWITCH_MS=$(( (MIGRATION_END - POST_SWITCH_START) / 1000000 ))

cat > "$RESULTS_PATH/migration_timing.txt" <<EOF
migration_start_ns=$MIGRATION_START
checkpoint_done_ns=$CHECKPOINT_DONE
transfer_done_ns=$TRANSFER_DONE
restore_done_ns=$RESTORE_DONE
switch_update_done_ns=$SWITCH_UPDATE_DONE
migration_end_ns=$MIGRATION_END
total_ms=$TOTAL_MS
checkpoint_ms=$CHECKPOINT_MS
transfer_ms=$TRANSFER_MS
restore_ms=$RESTORE_MS
switch_ms=$SWITCH_MS
checkpoint_size_bytes=$CHECKPOINT_SIZE
source_node=$SOURCE_NODE
target_node=$TARGET_NODE
server_ip=$SERVER_IP
target_sw_port=$TARGET_SW_PORT
transfer_method=socat
pre_transfer_ms=$PRE_TRANSFER_MS
post_transfer_ms=$POST_TRANSFER_MS
pre_restore_ms=$PRE_RESTORE_MS
post_switch_ms=$POST_SWITCH_MS
source_stop_ms=$SOURCE_STOP_MS
time_to_ready_ms=$TIME_TO_READY_MS
EOF

PHASED_SUM=$(( CHECKPOINT_MS + PRE_TRANSFER_MS + TRANSFER_MS + POST_TRANSFER_MS + PRE_RESTORE_MS + RESTORE_MS + SWITCH_MS + SOURCE_STOP_MS + POST_SWITCH_MS ))
OVERHEAD_MS=$(( TOTAL_MS - PHASED_SUM ))
[[ $OVERHEAD_MS -lt 0 ]] && OVERHEAD_MS=0

READY_PHASED_SUM=$(( CHECKPOINT_MS + PRE_TRANSFER_MS + TRANSFER_MS + POST_TRANSFER_MS + PRE_RESTORE_MS + RESTORE_MS + SWITCH_MS ))
READY_OVERHEAD_MS=$(( TIME_TO_READY_MS - READY_PHASED_SUM ))
[[ $READY_OVERHEAD_MS -lt 0 ]] && READY_OVERHEAD_MS=0

printf "\n===== Migration: %s -> %s (same-IP: %s) =====\n" "$SOURCE_NODE" "$TARGET_NODE" "$SERVER_IP"
printf "  >>> Client-visible (downtime) : %5d ms  <<<\n" "$TIME_TO_READY_MS"
printf "      (TCP frozen at checkpoint; restored + switch updated)\n"
printf "  Checkpoint:    %4d ms  (CRIU dump, container stops)\n" "$CHECKPOINT_MS"
printf "  Pre-transfer: %4d ms\n" "$PRE_TRANSFER_MS"
printf "  Transfer:     %4d ms  (%.1f MB)\n" "$TRANSFER_MS" \
    "$(echo "scale=1; ${CHECKPOINT_SIZE:-0} / 1048576" | bc)"
printf "  Post-transfer: %4d ms\n" "$POST_TRANSFER_MS"
printf "  Restore:      %4d ms\n" "$RESTORE_MS"
printf "  Switch update:%4d ms\n" "$SWITCH_MS"
printf "  ─────────────────────\n"
printf "  (ready phased sum: %d ms)\n" "$READY_PHASED_SUM"
printf "\n  Cleanup (not part of client downtime):\n"
printf "  Source remove: %4d ms\n" "$SOURCE_STOP_MS"
printf "  Post-switch:  %4d ms  (flags, results)\n" "$POST_SWITCH_MS"
printf "  Overhead:     %4d ms\n" "$OVERHEAD_MS"
printf "  ─────────────────────\n"
printf "  Full script total: %d ms\n" "$TOTAL_MS"
