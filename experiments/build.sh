#!/bin/bash
# =============================================================================
# build.sh — Set up the WebRTC migration experiment
# =============================================================================
# Creates isolated Podman networks, pods, and containers following the same
# pattern as p4containerflow/examples/redis/build.sh.
#
# Usage:
#   ./build.sh            # Uses defaults from config.env
#   H2_IP=10.0.2.2 ./build.sh  # Override specific settings
# =============================================================================

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/config.env"

# -----------------------------------------------------------------------------
# Host 1: client pod (load generator + collector)
# -----------------------------------------------------------------------------
printf "\n===== Creating Host 1 (client) =====\n"
sudo podman network create \
    --driver bridge \
    --opt isolate=1 \
    --disable-dns \
    --interface-name "$H1_BR" \
    --gateway "$H1_GATEWAY" \
    --subnet "$H1_SUBNET" \
    "$H1_NET"

sudo podman pod create \
    --name h1-pod \
    --network "$H1_NET" \
    --mac-address "$H1_MAC" \
    --ip "$H1_IP"

# Run load generator (connects to server via VIP)
sudo podman run \
    --replace --detach --privileged \
    --name webrtc-loadgen \
    --pod h1-pod \
    --cap-add NET_ADMIN \
    "$LOADGEN_IMAGE" \
    ./loadgen -server "http://${VIP}:${SIGNALING_PORT}" -peers "$LOADGEN_PEERS"

# -----------------------------------------------------------------------------
# Host 2: WebRTC server (initial location)
# -----------------------------------------------------------------------------
printf "\n===== Creating Host 2 (server) =====\n"
sudo podman network create \
    --driver bridge \
    --opt isolate=1 \
    --disable-dns \
    --interface-name "$H2_BR" \
    --gateway "$H2_GATEWAY" \
    --route "${H1_SUBNET},${H2_GATEWAY}" \
    --subnet "$H2_SUBNET" \
    "$H2_NET"

sudo podman pod create \
    --name h2-pod \
    --network "$H2_NET" \
    --mac-address "$H2_MAC" \
    --ip "$H2_IP"

sudo podman run \
    --replace --detach --privileged \
    --name webrtc-server \
    --pod h2-pod \
    --cap-add NET_ADMIN \
    "$SERVER_IMAGE" \
    ./server -signaling-addr ":${SIGNALING_PORT}" -metrics-addr ":${METRICS_PORT}"

# -----------------------------------------------------------------------------
# Host 3: migration target (initially just a pause container)
# -----------------------------------------------------------------------------
printf "\n===== Creating Host 3 (migration target) =====\n"
sudo podman network create \
    --driver bridge \
    --opt isolate=1 \
    --disable-dns \
    --interface-name "$H3_BR" \
    --gateway "$H3_GATEWAY" \
    --route "${H1_SUBNET},${H3_GATEWAY}" \
    --subnet "$H3_SUBNET" \
    "$H3_NET"

sudo podman pod create \
    --name h3-pod \
    --network "$H3_NET" \
    --mac-address "$H3_MAC" \
    --ip "$H3_IP"

# Start a pause container so the pod's network namespace stays alive
sudo podman run \
    --replace --detach \
    --name h3-pause \
    --pod h3-pod \
    docker.io/hello-world:latest

# -----------------------------------------------------------------------------
# Drop RST packets on bridges (prevents kernel from resetting migrated
# TCP connections — same as the redis example)
# -----------------------------------------------------------------------------
printf "\n===== Configuring iptables =====\n"
sudo iptables -A OUTPUT -p tcp --tcp-flags RST RST -o "$H1_BR" -j DROP 2>/dev/null || true
sudo iptables -A OUTPUT -p tcp --tcp-flags RST RST -o "$H2_BR" -j DROP 2>/dev/null || true
sudo iptables -A OUTPUT -p tcp --tcp-flags RST RST -o "$H3_BR" -j DROP 2>/dev/null || true

# -----------------------------------------------------------------------------
# Configure bridge interfaces (disable IPv6, offloading, set MTU)
# -----------------------------------------------------------------------------
printf "\n===== Configuring interfaces =====\n"
for iface in "$H1_BR" "$H2_BR" "$H3_BR"; do
    printf "Configuring %s\n" "$iface"
    sudo sysctl -q "net.ipv6.conf.${iface}.disable_ipv6=1" 2>/dev/null || true
    sudo ethtool -K "$iface" tx off 2>/dev/null || true
    sudo ethtool -K "$iface" rx off 2>/dev/null || true
    sudo ethtool -K "$iface" sg off 2>/dev/null || true
    sudo ip link set "$iface" mtu 9500 2>/dev/null || true
done

# -----------------------------------------------------------------------------
# Create results directory
# -----------------------------------------------------------------------------
mkdir -p "$SCRIPT_DIR/$RESULTS_DIR"

# Clean any stale migration flag
rm -f "$MIGRATION_FLAG_FILE"

printf "\n===== Build complete =====\n"
printf "Host 1 (client):   %s  pod=h1-pod\n" "$H1_IP"
printf "Host 2 (server):   %s  pod=h2-pod  container=webrtc-server\n" "$H2_IP"
printf "Host 3 (target):   %s  pod=h3-pod  (empty, ready for migration)\n" "$H3_IP"
printf "VIP:               %s\n" "$VIP"
printf "\nTo migrate server from h2 to h3:  ./cr.sh 2 3\n"
