#!/bin/bash
# =============================================================================
# clean.sh — Teardown the WebRTC migration experiment
# =============================================================================
# Removes all containers and networks created by build.sh.
# Idempotent — safe to run multiple times.
# =============================================================================

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/config.env"

printf "===== Cleaning up experiment =====\n"

# Kill and remove named containers
for name in webrtc-server webrtc-loadgen webrtc-collector h2 h3; do
    printf "Removing container: %s\n" "$name"
    sudo podman kill "$name" 2>/dev/null || true
    sudo podman rm -f "$name" 2>/dev/null || true
done

# Remove networks
for i in $(seq 1 "$NUM_HOSTS"); do
    printf "\n----- Removing host %d -----\n" "$i"
    sudo podman network rm -f "h${i}-net" 2>/dev/null || true
done

# Remove iptables rules (best-effort)
for br in "$H1_BR" "$H2_BR" "$H3_BR"; do
    sudo iptables -D OUTPUT -p tcp --tcp-flags RST RST -o "$br" -j DROP 2>/dev/null || true
done

# Clean checkpoint directory
sudo rm -rf "$CHECKPOINT_DIR" 2>/dev/null || true

# Clean migration flag
rm -f "$MIGRATION_FLAG_FILE" 2>/dev/null || true

printf "\n===== Cleanup complete =====\n"
