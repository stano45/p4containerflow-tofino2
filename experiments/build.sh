#!/bin/bash
# =============================================================================
# build.sh — Set up the local container migration experiment
# =============================================================================
# Creates isolated Podman networks and containers (no pods).
#
# Usage:
#   ./build.sh
#   H2_IP=10.0.2.2 ./build.sh
# =============================================================================

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/config.env"

printf "\n===== Creating Host 1 (client) =====\n"
sudo podman network create \
    --driver bridge \
    --opt isolate=1 \
    --disable-dns \
    --interface-name "$H1_BR" \
    --gateway "$H1_GATEWAY" \
    --subnet "$H1_SUBNET" \
    "$H1_NET"

sudo podman run \
    --replace --detach --privileged \
    --name stream-client \
    --network "$H1_NET" \
    --mac-address "$H1_MAC" \
    --ip "$H1_IP" \
    --cap-add NET_ADMIN \
    "$LOADGEN_IMAGE" \
    ./stream-client -server "http://${VIP}:${SIGNALING_PORT}" -connections "$LOADGEN_CONNECTIONS"

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

sudo podman run \
    --replace --detach --privileged \
    --name stream-server \
    --network "$H2_NET" \
    --mac-address "$H2_MAC" \
    --ip "$H2_IP" \
    --cap-add NET_ADMIN \
    "$SERVER_IMAGE" \
    ./stream-server -signaling-addr ":${SIGNALING_PORT}" -metrics-addr ":${METRICS_PORT}"

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

printf "\n===== Configuring iptables =====\n"
sudo iptables -A OUTPUT -p tcp --tcp-flags RST RST -o "$H1_BR" -j DROP 2>/dev/null || true
sudo iptables -A OUTPUT -p tcp --tcp-flags RST RST -o "$H2_BR" -j DROP 2>/dev/null || true
sudo iptables -A OUTPUT -p tcp --tcp-flags RST RST -o "$H3_BR" -j DROP 2>/dev/null || true

printf "\n===== Configuring interfaces =====\n"
for iface in "$H1_BR" "$H2_BR" "$H3_BR"; do
    printf "Configuring %s\n" "$iface"
    sudo sysctl -q "net.ipv6.conf.${iface}.disable_ipv6=1" 2>/dev/null || true
    sudo ethtool -K "$iface" tx off 2>/dev/null || true
    sudo ethtool -K "$iface" rx off 2>/dev/null || true
    sudo ethtool -K "$iface" sg off 2>/dev/null || true
    sudo ip link set "$iface" mtu 9500 2>/dev/null || true
done

mkdir -p "$SCRIPT_DIR/$RESULTS_DIR"
rm -f "$MIGRATION_FLAG_FILE"

printf "\n===== Build complete =====\n"
printf "Host 1 (client):   %s  container=stream-client\n" "$H1_IP"
printf "Host 2 (server):   %s  container=stream-server\n" "$H2_IP"
printf "Host 3 (target):   %s  network only (restore target)\n" "$H3_IP"
printf "VIP:               %s\n" "$VIP"
printf "\nTo migrate server from h2 to h3:  ./cr.sh 2 3\n"
