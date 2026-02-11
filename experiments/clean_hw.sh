#!/bin/bash
# =============================================================================
# clean_hw.sh — Teardown the multi-node WebRTC migration experiment
# =============================================================================
# Removes containers, pods, and networks on lakewood (local) and loveland (SSH).
# Idempotent — safe to run multiple times.
# =============================================================================

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/config_hw.env"

remote() {
    local host="$1"; shift
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$host" "$@"
}

printf "===== Cleaning up multi-node experiment =====\n"

# -----------------------------------------------------------------------------
# Local (lakewood)
# -----------------------------------------------------------------------------
printf "\n----- [lakewood] Removing containers & pods -----\n"
for name in webrtc-server webrtc-loadgen h2 h3; do
    sudo podman kill "$name" 2>/dev/null || true
    sudo podman rm -f "$name" 2>/dev/null || true
done
sudo podman pod rm -f h1-pod 2>/dev/null || true
sudo podman pod rm -f h2-pod 2>/dev/null || true
sudo podman network rm -f "$HW_NET" 2>/dev/null || true

# Remove iptables RST drop rule
sudo iptables -D OUTPUT -p tcp --tcp-flags RST RST -o "$LAKEWOOD_NIC" -j DROP 2>/dev/null || true

# Clean checkpoint directory
sudo rm -rf "$CHECKPOINT_DIR" 2>/dev/null || true
rm -f "$MIGRATION_FLAG_FILE" 2>/dev/null || true

# -----------------------------------------------------------------------------
# Remote (loveland)
# -----------------------------------------------------------------------------
printf "\n----- [loveland] Removing containers & pods -----\n"
if remote "$LOVELAND_SSH" true 2>/dev/null; then
    remote "$LOVELAND_SSH" "
        for name in webrtc-server h3-pause h3; do
            sudo podman kill \$name 2>/dev/null || true
            sudo podman rm -f \$name 2>/dev/null || true
        done
        sudo podman pod rm -f h3-pod 2>/dev/null || true
        sudo podman network rm -f $HW_NET 2>/dev/null || true
        sudo iptables -D OUTPUT -p tcp --tcp-flags RST RST -o $LOVELAND_NIC -j DROP 2>/dev/null || true
        sudo rm -rf $REMOTE_CHECKPOINT_DIR 2>/dev/null || true
    "
else
    printf "WARNING: Cannot SSH to loveland (%s) — skipping remote cleanup\n" "$LOVELAND_SSH"
fi

printf "\n===== Cleanup complete =====\n"
