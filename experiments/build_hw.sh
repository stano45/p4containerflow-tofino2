#!/bin/bash
# =============================================================================
# build_hw.sh — Set up the multi-node container migration experiment
# =============================================================================
# Run from your control machine. SSHs into lakewood + loveland to create
# macvlan networks and containers on their Netronome NICs.
#
# Prerequisites:
#   - SSH key-based access to lakewood and loveland
#   - Container images built on lakewood (stream-server, stream-client)
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

    # Promiscuous mode (accept packets with any MAC — needed after migration
    # when packets arrive with the old node's container MAC)
    sudo ip link set $LAKEWOOD_NIC promisc on 2>/dev/null || true

    # Drop RST (prevents kernel from resetting migrated TCP connections)
    sudo iptables -D OUTPUT -p tcp --tcp-flags RST RST -o $LAKEWOOD_NIC -j DROP 2>/dev/null || true
    sudo iptables -A OUTPUT -p tcp --tcp-flags RST RST -o $LAKEWOOD_NIC -j DROP 2>/dev/null || true

    # Server (h2) — no VIP alias; loadgen connects to .2 directly.
    # TCP connections bind to .2 so CRIU can restore them on the target.
    # Fixed MAC so the P4-switch path works identically before and after migration
    # (the same MAC is used when recreating the macvlan on the migration target).
    sudo podman run --replace --detach --privileged \
        --name stream-server --network $HW_NET --ip $H2_IP \
        --mac-address $H2_MAC \
        -e GODEBUG=multipathtcp=0 \
        $SERVER_IMAGE \
        ./stream-server -signaling-addr :${SIGNALING_PORT} -metrics-addr :${METRICS_PORT}

    echo \"stream-server started at ${H2_IP} (MAC ${H2_MAC})\"

    # Client (h1) — connects to server IP directly (not VIP).
    # Same-IP migration means .2 is always the server, P4 switch
    # updates the forward table to route .2 to the new physical port.
    # Fixed MAC so the server can pre-populate its ARP cache after migration.
    sudo podman run --replace --detach --privileged \
        --name stream-client --network $HW_NET --ip $H1_IP \
        --mac-address $H1_MAC \
        -e GODEBUG=multipathtcp=0 \
        $LOADGEN_IMAGE \
        ./stream-client -server http://${H2_IP}:${SIGNALING_PORT} -connections $LOADGEN_CONNECTIONS -metrics-port $LOADGEN_METRICS_PORT

    echo 'lakewood setup complete'
"

# -----------------------------------------------------------------------------
# Loveland: CRIU+crun (for restore), server image + macvlan network
# -----------------------------------------------------------------------------
printf "\n===== [loveland] Ensuring CRIU and crun (for restore) =====\n"
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

SERVER_IMAGE_ID=$(on_lakewood "sudo podman image inspect $SERVER_IMAGE --format '{{.Id}}' 2>/dev/null" | sed 's/^sha256://' || true)
if [[ -z "$SERVER_IMAGE_ID" || ${#SERVER_IMAGE_ID} -ne 64 ]]; then
    echo "ERROR: Server image $SERVER_IMAGE not found on lakewood."
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
    on_loveland "sudo podman load -i $SYNC_TMP.img && rm -f $SYNC_TMP.img && sudo podman tag $SERVER_IMAGE_ID $SERVER_IMAGE"
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

    # Promiscuous mode
    sudo ip link set $LOVELAND_NIC promisc on 2>/dev/null || true

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
printf "  Host 1 (client):  %s  container=stream-client\n" "$H1_IP"
printf "  Host 2 (server):  %s  container=stream-server\n" "$H2_IP"
printf "Loveland:\n"
printf "  Host 3 (target):  network ready (restore target)\n"
printf "VIP:                %s\n" "$VIP"
