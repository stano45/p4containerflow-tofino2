#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
cd "$SCRIPT_DIR"

usage() {
    echo "Usage: $0 [dataplane|controller] [PYTEST_ARGS...]"
    echo ""
    echo "Examples:"
    echo "  $0 dataplane              # Run hardware dataplane tests"
    echo "  $0 controller             # Run hardware controller API tests"
    echo "  $0 dataplane -k migrate   # Run only migrate tests"
    echo "  $0 controller -v          # Run with verbose output"
    echo ""
    echo "Environment:"
    echo "  ARCH=tf1|tf2              # Tofino architecture (default: tf2)"
    echo "  SDE_INSTALL               # Required for dataplane tests"
    exit 1
}

if [ $# -lt 1 ]; then
    usage
fi

TEST_TYPE="$1"
shift
PYTEST_ARGS="${*:-}"

export ARCH="${ARCH:-tf2}"

export PATH="$HOME/.local/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi

echo "Syncing dependencies with uv..."
uv sync --frozen --no-progress

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"
VENV_SITE_PACKAGES="$SCRIPT_DIR/.venv/lib/python${PY_VERSION}/site-packages"

if [ ! -x "$VENV_PYTHON" ]; then
    echo "ERROR: uv sync did not create $VENV_PYTHON" >&2
    exit 1
fi

case "$TEST_TYPE" in
    dataplane)
        if [ -z "${SDE_INSTALL:-}" ]; then
            echo "ERROR: SDE_INSTALL not set. Source SDE environment first:" >&2
            echo "  source ~/setup-open-p4studio.bash" >&2
            exit 1
        fi

        SDE_PY="$SDE_INSTALL/lib/python${PY_VERSION}/site-packages"
        
        PYTHONPATH="$VENV_SITE_PACKAGES"
        PYTHONPATH="$PYTHONPATH:$SDE_PY/tofino/bfrt_grpc"
        PYTHONPATH="$PYTHONPATH:$SDE_PY/tofino"
        PYTHONPATH="$PYTHONPATH:$SDE_PY/${ARCH}pd"
        PYTHONPATH="$PYTHONPATH:$SDE_PY/p4testutils"
        export PYTHONPATH

        echo "Running hardware dataplane tests (ARCH=$ARCH)..."
        echo "PYTHONPATH=$PYTHONPATH"
        exec "$VENV_PYTHON" -m pytest test_dataplane.py --arch "$ARCH" $PYTEST_ARGS
        ;;

    controller)
        export PYTHONPATH="$VENV_SITE_PACKAGES"

        echo "Running hardware controller API tests..."
        exec "$VENV_PYTHON" -m pytest test_controller.py $PYTEST_ARGS
        ;;

    *)
        echo "ERROR: Unknown test type '$TEST_TYPE'" >&2
        usage
        ;;
esac
