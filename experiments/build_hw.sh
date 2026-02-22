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
mkdir -p "$SSH_MUX_DIR"

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
printf "\n===== [lakewood] Creating macvlan network + server container =====\n"

on_lakewood "
    set -euo pipefail

    sudo podman network rm -f $HW_NET 2>/dev/null || true

    sudo podman network create \
        --driver macvlan \
        --subnet $HW_SUBNET \
        --gateway $HW_GATEWAY \
        -o parent=$LAKEWOOD_NIC \
        -o mode=vepa \
        $HW_NET

    # NIC tuning
    sudo ethtool -K $LAKEWOOD_NIC tx off rx off 2>/dev/null || true
    sudo ethtool -K $LAKEWOOD_NIC sg off 2>/dev/null || true
    sudo ip link set $LAKEWOOD_NIC mtu 9000 2>/dev/null || true

    # Promiscuous mode (needed after migration for packets with old node's MAC)
    sudo ip link set $LAKEWOOD_NIC promisc on 2>/dev/null || true

    # Drop RST (prevents kernel from resetting migrated TCP connections)
    sudo iptables -D OUTPUT -p tcp --tcp-flags RST RST -o $LAKEWOOD_NIC -j DROP 2>/dev/null || true
    sudo iptables -A OUTPUT -p tcp --tcp-flags RST RST -o $LAKEWOOD_NIC -j DROP 2>/dev/null || true

    # Server only — loadgen runs locally and reaches the server through
    # an SSH tunnel + macvlan-shim on this host.
    sudo podman run --replace --detach --privileged \
        --name stream-server --network $HW_NET --ip $H2_IP \
        --mac-address $H2_MAC \
        -e GODEBUG=multipathtcp=0 \
        $SERVER_IMAGE \
        ./stream-server -signaling-addr :${SIGNALING_PORT} -metrics-addr :${METRICS_PORT}

    echo \"stream-server started at ${H2_IP} (MAC ${H2_MAC})\"
    echo 'lakewood setup complete'
"

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
    ssh -o ControlPath=none -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o ForwardAgent=yes "$LAKEWOOD_SSH" \
        "scp -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=60 $SYNC_TMP.img $LOVELAND_SSH:$SYNC_TMP.img"
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
        -o mode=vepa \
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
printf "\n===== [lakewood] Creating macvlan-shim (%s) =====\n" "$MACSHIM_IF"

on_lakewood "
    set -euo pipefail

    # Remove stale shim if it exists
    sudo ip link del $MACSHIM_IF 2>/dev/null || true

    # Create a macvlan sub-interface on the same parent NIC as the containers.
    # VEPA mode forces all traffic through the physical NIC and external
    # P4 switch, even for local container traffic (hairpin).
    sudo ip link add $MACSHIM_IF link $LAKEWOOD_NIC type macvlan mode vepa
    sudo ip link set $MACSHIM_IF address $H1_MAC
    sudo ip addr add ${H1_IP}/24 dev $MACSHIM_IF
    sudo ip link set $MACSHIM_IF up

    echo 'macvlan-shim created: ${MACSHIM_IF} ${H1_IP}/24 (MAC ${H1_MAC}) on ${LAKEWOOD_NIC}'
"

# Pre-populate ARP in the server container for the macshim so the first
# response doesn't wait for ARP resolution.
printf "Setting static ARP on server container for macshim...\n"
SERVER_PID=$(on_lakewood "sudo podman inspect --format '{{.State.Pid}}' stream-server 2>/dev/null" || true)
if [[ -n "$SERVER_PID" && "$SERVER_PID" != "0" ]]; then
    on_lakewood "sudo nsenter -t $SERVER_PID -n ip neigh replace ${H1_IP} lladdr ${H1_MAC} dev eth0 nud reachable"
    echo "Static ARP set: ${H1_IP} → ${H1_MAC} (in server container)"
else
    echo "WARNING: Could not set static ARP on server container (PID not found)"
fi

# -----------------------------------------------------------------------------
# Done
# -----------------------------------------------------------------------------
printf "\n===== Build complete =====\n"
printf "Lakewood:\n"
printf "  Server:   %s  container=stream-server  (MAC %s)\n" "$H2_IP" "$H2_MAC"
printf "  Macshim:  %s  iface=%s  (MAC %s)\n" "$H1_IP" "$MACSHIM_IF" "$H1_MAC"
printf "Loveland:\n"
printf "  Target:   network ready (restore target)\n"
printf "Client:\n"
printf "  Loadgen runs locally → SSH tunnel → %s:%s\n" "$H2_IP" "$SIGNALING_PORT"
