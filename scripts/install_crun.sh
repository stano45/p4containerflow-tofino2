#!/usr/bin/env bash
set -euo pipefail

if ! command -v sudo >/dev/null 2>&1; then
  echo "sudo required" >&2
  exit 1
fi

if ! grep -qi ubuntu /etc/os-release; then
  echo "This script is tailored for Ubuntu" >&2
  exit 1
fi

sudo apt update
# libcriu-dev/criu optional if CRIU already installed from source (e.g. install_criu.sh)
export PKG_CONFIG_PATH="/usr/local/lib/x86_64-linux-gnu/pkgconfig:/usr/local/lib/pkgconfig:/usr/local/lib64/pkgconfig:${PKG_CONFIG_PATH:-}"
DEPS="git build-essential autoconf automake libtool pkg-config libyajl-dev libseccomp-dev libcap-dev libsystemd-dev libprotobuf-c-dev libselinux1-dev go-md2man"
if ! pkg-config --exists criu 2>/dev/null; then
  DEPS="$DEPS libcriu-dev criu"
fi
sudo apt install -y $DEPS

mkdir -p "$HOME/src"
cd "$HOME/src"

if [ ! -d crun ]; then
  git clone https://github.com/containers/crun.git
fi

cd crun
git pull --rebase || true

./autogen.sh

export PKG_CONFIG=/usr/bin/pkg-config
# Prefer CRIU from /usr/local (e.g. from install_criu.sh) so crun gets criu_set_lsm_mount_context
export PKG_CONFIG_PATH="/usr/local/lib/x86_64-linux-gnu/pkgconfig:/usr/local/lib/pkgconfig:/usr/local/lib64/pkgconfig:/usr/lib/x86_64-linux-gnu/pkgconfig:${PKG_CONFIG_PATH-}"

./configure --with-criu
make -j"$(nproc)"
sudo make install

if ! crun features | grep -q 'checkpoint.enabled.*true'; then
  echo "crun built but checkpoint/restore not enabled" >&2
  exit 1
fi

CRUN_PATH="$(command -v crun)"

sudo mkdir -p /etc/containers
sudo tee /etc/containers/containers.conf >/dev/null <<EOF
[engine]
runtime = "${CRUN_PATH}"
EOF

sudo podman info --format '{{.Host.OCIRuntime.Path}} {{.Host.OCIRuntime.Name}}'
crun features | grep checkpoint
