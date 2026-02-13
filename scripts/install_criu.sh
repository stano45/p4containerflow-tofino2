#!/usr/bin/env bash
# Install CRIU (Checkpoint/Restore In Userspace) on Ubuntu 22.04+
# Full install (criu + libcriu.so) so crun can use criu_set_lsm_mount_context etc.

set -xe

sudo apt-get install -y \
    build-essential \
    libprotobuf-dev \
    libprotobuf-c-dev \
    protobuf-c-compiler \
    protobuf-compiler \
    python3-protobuf \
    libbsd-dev \
    pkg-config \
    iproute2 \
    libnftables-dev \
    libcap-dev \
    libnl-3-dev \
    libnl-3-200 \
    libnet1-dev \
    libnet1 \
    libnet-dev \
    libaio-dev \
    libgnutls28-dev \
    python3-future \
    libdrm-dev \
    asciidoc \
    xmlto

mkdir -p "$HOME/src"
cd "$HOME/src"

if [ ! -d criu ]; then
  git clone --depth 1 --branch v3.19 https://github.com/checkpoint-restore/criu.git
fi
cd criu
git fetch --depth 1 origin v3.19
git checkout v3.19 2>/dev/null || true

make -j"$(nproc)"
# Full install so libcriu.so (with criu_set_lsm_mount_context) is available for crun
sudo make install
sudo ldconfig

pip3 install ./lib ./crit 2>/dev/null || true

criu --version
cd ..
