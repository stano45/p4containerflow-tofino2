#!/bin/bash
# =============================================================================
# build_hw.sh — Set up the multi-node WebRTC migration experiment
# =============================================================================
# Creates macvlan networks and containers on real hardware:
#   - lakewood: client (loadgen) + server containers on Netronome NIC
#   - loveland: empty target pod on Netronome NIC (via SSH)
#
# Prerequisites:
#   - Netronome NICs have IPs assigned and static ARP in place
#   - SSH key-based access to loveland
#   - Container images built on both lakewood and loveland
#
# Usage:
#   ./build_hw.sh                    # run from lakewood
# =============================================================================

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/config_hw.env"

# -----------------------------------------------------------------------------
# Helper: run a command on a remote host via SSH
# -----------------------------------------------------------------------------
remote() {
    local host="$1"; shift
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$host" "$@"
}

# -----------------------------------------------------------------------------
# Preflight checks
# -----------------------------------------------------------------------------
printf "\n===== Preflight checks =====\n"

# Verify local Netronome NIC exists
if ! ip link show "$LAKEWOOD_NIC" &>/dev/null; then
    echo "ERROR: Netronome NIC $LAKEWOOD_NIC not found on this machine."
    echo "Are you running this on lakewood?"
    exit 1
fi

# Verify SSH to loveland
if ! remote "$LOVELAND_SSH" true 2>/dev/null; then
    echo "ERROR: Cannot SSH to loveland ($LOVELAND_SSH)"
    echo "Set up key-based SSH access first."
    exit 1
fi

# Verify loveland NIC
if ! remote "$LOVELAND_SSH" "ip link show $LOVELAND_NIC" &>/dev/null; then
    echo "ERROR: Netronome NIC $LOVELAND_NIC not found on loveland."
    exit 1
fi

printf "Preflight OK: local NIC=%s, remote NIC=%s\n" "$LAKEWOOD_NIC" "$LOVELAND_NIC"

# -----------------------------------------------------------------------------
# Local (lakewood): macvlan network + containers
# -----------------------------------------------------------------------------
printf "\n===== [lakewood] Creating macvlan network =====\n"

# Remove stale network if present
sudo podman network rm -f "$HW_NET" 2>/dev/null || true

sudo podman network create \
    --driver macvlan \
    --subnet "$HW_SUBNET" \
    --gateway "$HW_GATEWAY" \
    -o parent="$LAKEWOOD_NIC" \
    "$HW_NET"

# Configure NIC (disable offloading, set MTU)
printf "\n----- Configuring local NIC %s -----\n" "$LAKEWOOD_NIC"
sudo ethtool -K "$LAKEWOOD_NIC" tx off rx off 2>/dev/null || true
sudo ethtool -K "$LAKEWOOD_NIC" sg off 2>/dev/null || true
sudo ip link set "$LAKEWOOD_NIC" mtu 9000 2>/dev/null || true

# Drop RST on the NIC (prevents kernel from resetting migrated TCP connections)
sudo iptables -D OUTPUT -p tcp --tcp-flags RST RST -o "$LAKEWOOD_NIC" -j DROP 2>/dev/null || true
sudo iptables -A OUTPUT -p tcp --tcp-flags RST RST -o "$LAKEWOOD_NIC" -j DROP 2>/dev/null || true

# --- Host 2: WebRTC server pod ---
printf "\n----- [lakewood] Creating server pod (h2) -----\n"
sudo podman pod rm -f h2-pod 2>/dev/null || true
sudo podman pod create \
    --name h2-pod \
    --network "$HW_NET" \
    --ip "$H2_IP"

sudo podman run \
    --replace --detach --privileged \
    --name webrtc-server \
    --pod h2-pod \
    --cap-add NET_ADMIN \
    "$SERVER_IMAGE" \
    ./server -signaling-addr ":${SIGNALING_PORT}" -metrics-addr ":${METRICS_PORT}"

# --- Host 1: client pod (loadgen) ---
printf "\n----- [lakewood] Creating client pod (h1) -----\n"
sudo podman pod rm -f h1-pod 2>/dev/null || true
sudo podman pod create \
    --name h1-pod \
    --network "$HW_NET" \
    --ip "$H1_IP"

sudo podman run \
    --replace --detach --privileged \
    --name webrtc-loadgen \
    --pod h1-pod \
    --cap-add NET_ADMIN \
    "$LOADGEN_IMAGE" \
    ./loadgen -server "http://${VIP}:${SIGNALING_PORT}" -peers "$LOADGEN_PEERS"

# -----------------------------------------------------------------------------
# Remote (loveland): macvlan network + empty target pod
# -----------------------------------------------------------------------------
printf "\n===== [loveland] Creating macvlan network =====\n"

remote "$LOVELAND_SSH" "
    sudo podman network rm -f $HW_NET 2>/dev/null || true
    sudo podman network create \
        --driver macvlan \
        --subnet $HW_SUBNET \
        --gateway $HW_GATEWAY \
        -o parent=$LOVELAND_NIC \
        $HW_NET
"

printf "\n----- [loveland] Configuring NIC %s -----\n" "$LOVELAND_NIC"
remote "$LOVELAND_SSH" "
    sudo ethtool -K $LOVELAND_NIC tx off rx off 2>/dev/null || true
    sudo ethtool -K $LOVELAND_NIC sg off 2>/dev/null || true
    sudo ip link set $LOVELAND_NIC mtu 9000 2>/dev/null || true
    sudo iptables -D OUTPUT -p tcp --tcp-flags RST RST -o $LOVELAND_NIC -j DROP 2>/dev/null || true
    sudo iptables -A OUTPUT -p tcp --tcp-flags RST RST -o $LOVELAND_NIC -j DROP 2>/dev/null || true
"

printf "\n----- [loveland] Creating target pod (h3) -----\n"
remote "$LOVELAND_SSH" "
    sudo podman pod rm -f h3-pod 2>/dev/null || true
    sudo podman pod create \
        --name h3-pod \
        --network $HW_NET \
        --ip $H3_IP
    # Start a pause container so the pod's network namespace stays alive
    sudo podman run --replace --detach --name h3-pause --pod h3-pod \
        docker.io/hello-world:latest
"

# -----------------------------------------------------------------------------
# Create results directory & clean stale state
# -----------------------------------------------------------------------------
mkdir -p "$SCRIPT_DIR/$RESULTS_DIR"
rm -f "$MIGRATION_FLAG_FILE"

printf "\n===== Build complete =====\n"
printf "Lakewood (local):\n"
printf "  Host 1 (client):  %s  pod=h1-pod  container=webrtc-loadgen\n" "$H1_IP"
printf "  Host 2 (server):  %s  pod=h2-pod  container=webrtc-server\n" "$H2_IP"
printf "Loveland (remote):\n"
printf "  Host 3 (target):  %s  pod=h3-pod  (empty, ready for migration)\n" "$H3_IP"
printf "VIP:                %s\n" "$VIP"
printf "\nTo migrate: ./cr_hw.sh\n"
