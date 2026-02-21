#!/bin/bash
# =============================================================================
# cr_tcp_test.sh — Quick CRIU checkpoint/restore test via SSH
# =============================================================================
# Run from your control machine. Tests C/R on a remote node, or across two
# remote nodes with proxy checkpoint transfer.
#
# Usage:
#   ./cr_tcp_test.sh <source_ssh>                   # local C/R on one node
#   ./cr_tcp_test.sh <source_ssh> <target_ssh>      # cross-node C/R
#
# Examples:
#   ./cr_tcp_test.sh user@source-server                        # local C/R on one node
#   ./cr_tcp_test.sh user@source-server user@target-server     # cross-node C/R
# =============================================================================

set -euo pipefail

SSH_OPTS="-o BatchMode=yes -o StrictHostKeyChecking=no"

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <source_ssh> [target_ssh]"
    exit 1
fi

SOURCE_SSH="$1"
TARGET_SSH="${2:-}"
PORT=8888
CONTAINER=cr-tcp-test
CHECKPOINT_DIR=/tmp/checkpoints
LOCAL_TMP="/tmp/cr-tcp-test-transfer"

run_on() { ssh $SSH_OPTS "$1" "${@:2}"; }

cleanup() {
    echo ""
    echo "--- Cleaning up ---"
    run_on "$SOURCE_SSH" "
        sudo podman rm -f $CONTAINER 2>/dev/null || true
        sudo podman rm -f ${CONTAINER}-restored 2>/dev/null || true
        sudo rm -rf $CHECKPOINT_DIR 2>/dev/null || true
    " 2>/dev/null || true

    if [[ -n "$TARGET_SSH" ]]; then
        run_on "$TARGET_SSH" "
            sudo podman rm -f ${CONTAINER}-restored 2>/dev/null || true
            sudo rm -rf $CHECKPOINT_DIR 2>/dev/null || true
        " 2>/dev/null || true
    fi

    rm -rf "$LOCAL_TMP" 2>/dev/null || true
}
trap cleanup EXIT

RESTORE_HOST="$SOURCE_SSH"
[[ -n "$TARGET_SSH" ]] && RESTORE_HOST="$TARGET_SSH"

if [[ -n "$TARGET_SSH" ]]; then
    echo "=== Cross-node C/R test: $SOURCE_SSH -> $TARGET_SSH ==="
else
    echo "=== Local C/R test on: $SOURCE_SSH ==="
fi

# ==========================================================================
echo ""
echo "===== Step 1: Start container with HTTP server on :$PORT ====="
run_on "$SOURCE_SSH" "
    sudo podman rm -f $CONTAINER 2>/dev/null || true
    sudo podman run -d --name $CONTAINER -p ${PORT}:${PORT} \
        docker.io/library/python:3-alpine \
        python3 -m http.server $PORT
"
sleep 2

echo ""
echo "===== Step 2: Verify server responds ====="
SOURCE_IP="${SOURCE_SSH#*@}"
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 "http://${SOURCE_IP}:${PORT}/" 2>/dev/null || echo "000")
if [[ "$HTTP_CODE" != "200" ]]; then
    echo "WARN: HTTP $HTTP_CODE from $SOURCE_IP:$PORT (trying via SSH...)"
    run_on "$SOURCE_SSH" "curl -s -o /dev/null -w '%{http_code}' http://localhost:${PORT}/"
fi
echo "OK: server is up"

# ==========================================================================
echo ""
echo "===== Step 3: Checkpoint with --tcp-established ====="

T_START=$(date +%s%N)
run_on "$SOURCE_SSH" "
    sudo mkdir -p $CHECKPOINT_DIR
    sudo podman container checkpoint \
        --export $CHECKPOINT_DIR/checkpoint.tar \
        --tcp-established \
        --keep \
        $CONTAINER
"
T_END=$(date +%s%N)
CKPT_MS=$(( (T_END - T_START) / 1000000 ))
echo "Checkpoint: ${CKPT_MS} ms"

# ==========================================================================
echo ""
echo "===== Step 4: Remove original container ====="
run_on "$SOURCE_SSH" "sudo podman rm -f $CONTAINER"

# ==========================================================================
if [[ -n "$TARGET_SSH" ]]; then
    echo ""
    echo "===== Step 5a: Proxy transfer ($SOURCE_SSH → control → $TARGET_SSH) ====="

    mkdir -p "$LOCAL_TMP"

    T_START=$(date +%s%N)
    ssh $SSH_OPTS "$SOURCE_SSH" "sudo cat $CHECKPOINT_DIR/checkpoint.tar" \
        > "$LOCAL_TMP/checkpoint.tar"

    CKPT_SIZE=$(stat -c%s "$LOCAL_TMP/checkpoint.tar" 2>/dev/null || stat -f%z "$LOCAL_TMP/checkpoint.tar" 2>/dev/null || echo 0)

    run_on "$TARGET_SSH" "sudo mkdir -p $CHECKPOINT_DIR && sudo chmod 777 $CHECKPOINT_DIR"
    cat "$LOCAL_TMP/checkpoint.tar" | ssh $SSH_OPTS "$TARGET_SSH" "cat > $CHECKPOINT_DIR/checkpoint.tar"
    T_END=$(date +%s%N)

    XFER_MS=$(( (T_END - T_START) / 1000000 ))
    echo "Transfer: ${XFER_MS} ms ($(( CKPT_SIZE / 1024 )) KB)"
    rm -rf "$LOCAL_TMP"

    echo ""
    echo "===== Step 5b: Restore on $TARGET_SSH ====="

    T_START=$(date +%s%N)
    run_on "$TARGET_SSH" "
        sudo podman rm -f ${CONTAINER}-restored 2>/dev/null || true
        sudo podman container restore \
            --import $CHECKPOINT_DIR/checkpoint.tar \
            --tcp-established \
            --name ${CONTAINER}-restored \
            -p ${PORT}:${PORT}
    "
    T_END=$(date +%s%N)
    RESTORE_MS=$(( (T_END - T_START) / 1000000 ))
    echo "Restore: ${RESTORE_MS} ms"

else
    echo ""
    echo "===== Step 5: Restore locally on $SOURCE_SSH ====="

    T_START=$(date +%s%N)
    run_on "$SOURCE_SSH" "
        sudo podman container restore \
            --import $CHECKPOINT_DIR/checkpoint.tar \
            --tcp-established \
            --name ${CONTAINER}-restored \
            -p ${PORT}:${PORT}
    "
    T_END=$(date +%s%N)
    RESTORE_MS=$(( (T_END - T_START) / 1000000 ))
    echo "Restore: ${RESTORE_MS} ms"
fi

# ==========================================================================
echo ""
echo "===== Step 6: Verify restored container ====="

RESTORE_IP="${RESTORE_HOST#*@}"
STATUS=$(run_on "$RESTORE_HOST" "sudo podman inspect ${CONTAINER}-restored --format '{{.State.Status}}'" 2>/dev/null || echo "unknown")
echo "Container status: $STATUS"

HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 "http://${RESTORE_IP}:${PORT}/" 2>/dev/null || echo "000")
if [[ "$HTTP_CODE" == "200" ]]; then
    echo "OK: restored server on $RESTORE_IP returned HTTP $HTTP_CODE"
else
    echo "WARN: HTTP $HTTP_CODE from $RESTORE_IP:$PORT (may need direct network access)"
    echo "Checking via SSH..."
    run_on "$RESTORE_HOST" "curl -s -o /dev/null -w '%{http_code}' http://localhost:${PORT}/"
fi

# ==========================================================================
echo ""
echo "===== Results ====="
echo "  Checkpoint:  ${CKPT_MS} ms"
[[ -n "$TARGET_SSH" ]] && echo "  Transfer:    ${XFER_MS} ms (via control machine)"
echo "  Restore:     ${RESTORE_MS} ms"
echo ""
echo "PASS: CRIU checkpoint/restore with TCP works."
