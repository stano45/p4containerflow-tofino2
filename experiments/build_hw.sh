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
# Loveland: server image + macvlan network (target for migration restore)
# -----------------------------------------------------------------------------
printf "\n===== [loveland] Building server image + creating macvlan network =====\n"

# Restore needs the same image on loveland (checkpoint stores image reference)
if on_loveland "sudo podman image exists $SERVER_IMAGE 2>/dev/null"; then
    echo "Image $SERVER_IMAGE already exists on loveland"
else
    echo "Building $SERVER_IMAGE on loveland (required for restore)..."
    on_loveland "cd $REMOTE_PROJECT_DIR/experiments && sudo podman build -t $SERVER_IMAGE -f cmd/server/Containerfile ."
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
