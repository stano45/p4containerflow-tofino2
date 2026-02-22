#!/bin/bash
# =============================================================================
# clean_hw.sh — Teardown the multi-node experiment (from control machine)
# =============================================================================

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/config_hw.env"
mkdir -p "$SSH_MUX_DIR"

on_lakewood() { ssh $SSH_OPTS "$LAKEWOOD_SSH" "$@"; }
on_loveland() { ssh $SSH_OPTS "$LOVELAND_SSH" "$@"; }

printf "===== Cleaning up multi-node experiment =====\n"

printf "\n----- [local] -----\n"
pkill -f '[s]tream-client' 2>/dev/null || true
pkill -f '[s]tream-collector' 2>/dev/null || true
# Kill SSH tunnels used for port forwarding to the experiment subnet
pkill -f "ssh.*-L.*${SSH_TUNNEL_LOCAL_PORT:-18080}:" 2>/dev/null || true
echo "local processes cleaned"

printf "\n----- [lakewood] -----\n"
if on_lakewood true 2>/dev/null; then
    on_lakewood "
        sudo pkill -f '[s]tream-client' 2>/dev/null || true
        sudo pkill -f '[s]tream-collector' 2>/dev/null || true
        for name in stream-server stream-client h2 h3; do
            sudo podman kill \$name 2>/dev/null || true
            sudo podman rm -f \$name 2>/dev/null || true
        done
        sudo podman network rm -f $HW_NET 2>/dev/null || true
        sudo ip link del ${MACSHIM_IF:-macshim} 2>/dev/null || true
        sudo iptables -D OUTPUT -p tcp --tcp-flags RST RST -o $LAKEWOOD_NIC -j DROP 2>/dev/null || true
        sudo rm -rf $CHECKPOINT_DIR 2>/dev/null || true
    "
    echo "lakewood cleaned"
else
    echo "WARNING: cannot SSH to lakewood — skipping"
fi

printf "\n----- [loveland] -----\n"
if on_loveland true 2>/dev/null; then
    on_loveland "
        for name in stream-server h3; do
            sudo podman kill \$name 2>/dev/null || true
            sudo podman rm -f \$name 2>/dev/null || true
        done
        sudo podman network rm -f $HW_NET 2>/dev/null || true
        sudo iptables -D OUTPUT -p tcp --tcp-flags RST RST -o $LOVELAND_NIC -j DROP 2>/dev/null || true
        sudo rm -rf $CHECKPOINT_DIR 2>/dev/null || true
    "
    echo "loveland cleaned"
else
    echo "WARNING: cannot SSH to loveland — skipping"
fi

printf "\n===== Cleanup complete =====\n"
