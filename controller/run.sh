export PATH=$SDE_INSTALL/bin:$PATH
echo "Using PATH ${PATH}"
export ARCH="tf2"
echo "Arch is $ARCH"

export PYTHON_LIB_DIR=$(python3 -c "import sysconfig; print(sysconfig.get_path('purelib', vars={'base': ''}))")
export PYTHONPATH=/home/vagrant/.local/bin:$PYTHONPATH
export PYTHONPATH=$SDE_INSTALL/$PYTHON_LIB_DIR/site-packages/tofino/bfrt_grpc:$PYTHONPATH
export PYTHONPATH=$SDE_INSTALL/$PYTHON_LIB_DIR/site-packages:$PYTHONPATH
export PYTHONPATH=$SDE_INSTALL/$PYTHON_LIB_DIR/site-packages/tofino:$PYTHONPATH
export PYTHONPATH=$SDE_INSTALL/$PYTHON_LIB_DIR/site-packages/${ARCH}pd:$PYTHONPATH
export PYTHONPATH=$SDE_INSTALL/$PYTHON_LIB_DIR/site-packages/p4testutils:$PYTHONPATH
export PYTHONPATH=$($SDE_INSTALL/bin/sdepythonpath.py):$PYTHONPATH

echo "PYTHONPATH is $PYTHONPATH"

# Install uv if not present
if ! command -v uv &> /dev/null; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

# Sync dependencies with uv
echo "Syncing dependencies with uv..."
uv sync

sudo env "PATH=$PATH" "PYTHONPATH=$PYTHONPATH" uv run python controller.py --config controller_config.json