#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
cd "$SCRIPT_DIR"

if [ -z "${SDE_INSTALL:-}" ]; then
    echo "SDE_INSTALL is not set. Please source the BF SDE environment first." >&2
    exit 1
fi

export PATH="$SDE_INSTALL/bin:$PATH"
echo "Using PATH ${PATH}"
export ARCH="${ARCH:-tf2}"
echo "Arch is $ARCH"

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
SDE_PY_LIB="$SDE_INSTALL/lib/python${PY_VERSION}"

declare -a PYTHONPATH_ENTRIES=(
    "/home/vagrant/.local/bin"
    "$SDE_PY_LIB/site-packages/tofino/bfrt_grpc"
    "$SDE_PY_LIB/site-packages"
    "$SDE_PY_LIB/site-packages/tofino"
    "$SDE_PY_LIB/site-packages/${ARCH}pd"
    "$SDE_PY_LIB/site-packages/p4testutils"
    "$($SDE_INSTALL/bin/sdepythonpath.py)"
)

for entry in "${PYTHONPATH_ENTRIES[@]}"; do
    if [ -z "${PYTHONPATH:-}" ]; then
        PYTHONPATH="$entry"
    else
        PYTHONPATH="$entry:$PYTHONPATH"
    fi
done
export PYTHONPATH

echo "PYTHONPATH is $PYTHONPATH"

# Add ~/.local/bin to PATH first so we can find uv if installed
export PATH="$HOME/.local/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi

echo "Syncing dependencies with uv..."
uv sync --frozen --no-progress

VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"
if [ ! -x "$VENV_PYTHON" ]; then
    echo "uv sync did not create $VENV_PYTHON" >&2
    exit 1
fi

# Remove SDE's grpc from PYTHONPATH to avoid conflicts with venv's grpc
# The venv has its own grpc installed, and the SDE's version is incompatible
# We keep the SDE paths for tofino/bfrt_grpc but filter out the base site-packages grpc
VENV_SITE_PACKAGES="$SCRIPT_DIR/.venv/lib/python${PY_VERSION}/site-packages"

# Build a new PYTHONPATH with venv first, then only the specific SDE paths we need
# (excluding the generic site-packages which contains the conflicting grpc)
FILTERED_PYTHONPATH="$VENV_SITE_PACKAGES"
FILTERED_PYTHONPATH="$FILTERED_PYTHONPATH:$SDE_PY_LIB/site-packages/tofino/bfrt_grpc"
FILTERED_PYTHONPATH="$FILTERED_PYTHONPATH:$SDE_PY_LIB/site-packages/tofino"
FILTERED_PYTHONPATH="$FILTERED_PYTHONPATH:$SDE_PY_LIB/site-packages/${ARCH}pd"
FILTERED_PYTHONPATH="$FILTERED_PYTHONPATH:$SDE_PY_LIB/site-packages/p4testutils"
export PYTHONPATH="$FILTERED_PYTHONPATH"
echo "Filtered PYTHONPATH: $PYTHONPATH"

sudo env "PATH=$PATH" "PYTHONPATH=$PYTHONPATH" "$VENV_PYTHON" controller.py --config controller_config.json