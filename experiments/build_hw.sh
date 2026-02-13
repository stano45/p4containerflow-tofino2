#!/bin/bash
# =============================================================================
# build_hw.sh — Set up the multi-node WebRTC migration experiment
# =============================================================================
# Run from your control machine. SSHs into lakewood + loveland to create
# macvlan networks and containers on their Netronome NICs.
#
# Prerequisites:
#   - SSH key-based access to lakewood and loveland
#   - Container images built on lakewood (webrtc-server, webrtc-loadgen)
#   - Netronome NICs have IPs assigned and static ARP in place
#
# Usage:
#   ./build_hw.sh
# =============================================================================

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/config_hw.env"

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
on_lakewood() { ssh $SSH_OPTS "$LAKEWOOD_SSH" "$@"; }
on_loveland() { ssh $SSH_OPTS "$LOVELAND_SSH" "$@"; }

# -----------------------------------------------------------------------------
# Preflight checks
# -----------------------------------------------------------------------------
printf "\n===== Preflight checks =====\n"

on_lakewood "echo 'lakewood SSH OK'" || { echo "FAIL: cannot SSH to lakewood"; exit 1; }
on_loveland "echo 'loveland SSH OK'" || { echo "FAIL: cannot SSH to loveland"; exit 1; }

on_lakewood "ip link show $LAKEWOOD_NIC >/dev/null 2>&1" || { echo "FAIL: $LAKEWOOD_NIC not found on lakewood"; exit 1; }
on_loveland "ip link show $LOVELAND_NIC >/dev/null 2>&1" || { echo "FAIL: $LOVELAND_NIC not found on loveland"; exit 1; }

printf "Preflight OK\n"

# -----------------------------------------------------------------------------
# Lakewood: macvlan network + server + client containers
# -----------------------------------------------------------------------------
printf "\n===== [lakewood] Creating macvlan network + containers =====\n"

on_lakewood "
    set -euo pipefail

    # Clean stale state
    sudo podman network rm -f $HW_NET 2>/dev/null || true

    # macvlan on Netronome NIC
    sudo podman network create \
        --driver macvlan \
        --subnet $HW_SUBNET \
        --gateway $HW_GATEWAY \
        -o parent=$LAKEWOOD_NIC \
        $HW_NET

    # NIC tuning
    sudo ethtool -K $LAKEWOOD_NIC tx off rx off 2>/dev/null || true
    sudo ethtool -K $LAKEWOOD_NIC sg off 2>/dev/null || true
    sudo ip link set $LAKEWOOD_NIC mtu 9000 2>/dev/null || true

    # Drop RST (prevents kernel from resetting migrated TCP connections)
    sudo iptables -D OUTPUT -p tcp --tcp-flags RST RST -o $LAKEWOOD_NIC -j DROP 2>/dev/null || true
    sudo iptables -A OUTPUT -p tcp --tcp-flags RST RST -o $LAKEWOOD_NIC -j DROP 2>/dev/null || true

    # Server (h2)
    sudo podman run --replace --detach --privileged \
        --name webrtc-server --network $HW_NET --ip $H2_IP \
        -e GODEBUG=multipathtcp=0 \
        $SERVER_IMAGE \
        ./server -signaling-addr :${SIGNALING_PORT} -metrics-addr :${METRICS_PORT}

    # Client (h1)
    sudo podman run --replace --detach --privileged \
        --name webrtc-loadgen --network $HW_NET --ip $H1_IP \
        -e GODEBUG=multipathtcp=0 \
        $LOADGEN_IMAGE \
        ./loadgen -server http://${VIP}:${SIGNALING_PORT} -peers $LOADGEN_PEERS

    echo 'lakewood setup complete'
"

# -----------------------------------------------------------------------------
# Loveland: CRIU+crun (for restore), server image + macvlan network
# -----------------------------------------------------------------------------
printf "\n===== [loveland] Ensuring CRIU and crun (for restore) =====\n"
# Restore fails with \"could not find symbol criu_set_lsm_mount_context\" if libcriu is older than crun expects. Install CRIU from source (full install) then crun.
# Run install scripts via stdin so they work even if repo on loveland is not synced.
NEED_CRIU=false
if ! on_loveland "nm -D /usr/local/lib/x86_64-linux-gnu/libcriu.so 2>/dev/null | grep -q criu_set_lsm_mount_context" 2>/dev/null; then
  if ! on_loveland "nm -D /usr/local/lib64/libcriu.so 2>/dev/null | grep -q criu_set_lsm_mount_context" 2>/dev/null; then
    NEED_CRIU=true
  fi
fi
NEED_CRUN=false
if [ "$NEED_CRIU" = true ]; then
  NEED_CRUN=true
elif ! on_loveland "crun features 2>/dev/null | grep -q 'checkpoint.enabled.*true'" 2>/dev/null; then
  NEED_CRUN=true
fi
if [ "$NEED_CRIU" = true ]; then
  echo "Installing CRIU (full) on loveland..."
  ssh $SSH_OPTS "$LOVELAND_SSH" "bash -s" < "$SCRIPT_DIR/../scripts/install_criu.sh"
fi
if [ "$NEED_CRUN" = true ]; then
  echo "Installing crun on loveland..."
  ssh $SSH_OPTS "$LOVELAND_SSH" "bash -s" < "$SCRIPT_DIR/../scripts/install_crun.sh"
fi

printf "\n===== [loveland] Server image (same ID as lakewood) + macvlan network =====\n"

# Restore needs the *same* image ID on loveland as on lakewood. Sync from lakewood (no per-migration transfer).
SERVER_IMAGE_ID=$(on_lakewood "sudo podman image inspect $SERVER_IMAGE --format '{{.Id}}' 2>/dev/null" | sed 's/^sha256://' || true)
if [[ -z "$SERVER_IMAGE_ID" || ${#SERVER_IMAGE_ID} -ne 64 ]]; then
    echo "ERROR: Server image $SERVER_IMAGE not found on lakewood. Build it there first (e.g. run_experiment.sh or: podman build -t $SERVER_IMAGE -f experiments/cmd/server/Containerfile .)."
    exit 1
fi
if on_loveland "sudo podman image exists $SERVER_IMAGE_ID 2>/dev/null"; then
    echo "Image $SERVER_IMAGE_ID already present on loveland"
else
    echo "Syncing server image lakewood→loveland..."
    SYNC_TMP=/tmp/cr_image_sync_$$
    on_lakewood "sudo podman save -o $SYNC_TMP.img $SERVER_IMAGE_ID && sudo chown \$(whoami) $SYNC_TMP.img"
    ssh $SSH_OPTS -o ForwardAgent=yes "$LAKEWOOD_SSH" "scp -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=60 $SYNC_TMP.img $LOVELAND_SSH:$SYNC_TMP.img"
    on_lakewood "rm -f $SYNC_TMP.img"
    on_loveland "sudo podman load -i $SYNC_TMP.img && rm -f $SYNC_TMP.img"
    echo "Server image synced."
fi

on_loveland "
    set -euo pipefail

    sudo podman network rm -f $HW_NET 2>/dev/null || true

    sudo podman network create \
        --driver macvlan \
        --subnet $HW_SUBNET \
        --gateway $HW_GATEWAY \
        -o parent=$LOVELAND_NIC \
        $HW_NET

    # NIC tuning
    sudo ethtool -K $LOVELAND_NIC tx off rx off 2>/dev/null || true
    sudo ethtool -K $LOVELAND_NIC sg off 2>/dev/null || true
    sudo ip link set $LOVELAND_NIC mtu 9000 2>/dev/null || true

    # Drop RST
    sudo iptables -D OUTPUT -p tcp --tcp-flags RST RST -o $LOVELAND_NIC -j DROP 2>/dev/null || true
    sudo iptables -A OUTPUT -p tcp --tcp-flags RST RST -o $LOVELAND_NIC -j DROP 2>/dev/null || true

    echo 'loveland setup complete'
"

# -----------------------------------------------------------------------------
# Done
# -----------------------------------------------------------------------------
printf "\n===== Build complete =====\n"
printf "Lakewood:\n"
printf "  Host 1 (client):  %s  container=webrtc-loadgen\n" "$H1_IP"
printf "  Host 2 (server):  %s  container=webrtc-server\n" "$H2_IP"
printf "Loveland:\n"
printf "  Host 3 (target):  %s  network only (restore target)\n" "$H3_IP"
printf "VIP:                %s\n" "$VIP"
