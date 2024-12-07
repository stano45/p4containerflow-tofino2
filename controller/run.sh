export PATH=$SDE_INSTALL/bin:$PATH
echo "Using PATH ${PATH}"
export ARCH="tf2"
echo "Arch is $ARCH"

export PYTHON_LIB_DIR=$(python3 -c "from distutils import sysconfig; print(sysconfig.get_python_lib(prefix='', standard_lib=True, plat_specific=True))")
export PYTHONPATH=/home/vagrant/.local/bin:$PYTHONPATH
export PYTHONPATH=$SDE_INSTALL/$PYTHON_LIB_DIR/site-packages/tofino/bfrt_grpc:$PYTHONPATH
export PYTHONPATH=$SDE_INSTALL/$PYTHON_LIB_DIR/site-packages:$PYTHONPATH
export PYTHONPATH=$SDE_INSTALL/$PYTHON_LIB_DIR/site-packages/tofino:$PYTHONPATH
export PYTHONPATH=$SDE_INSTALL/$PYTHON_LIB_DIR/site-packages/${ARCH}pd:$PYTHONPATH
export PYTHONPATH=$SDE_INSTALL/$PYTHON_LIB_DIR/site-packages/p4testutils:$PYTHONPATH
export PYTHONPATH=$($SDE_INSTALL/bin/sdepythonpath.py):$PYTHONPATH

echo "PYTHONPATH is $PYTHONPATH"

sudo env "PATH=$PATH" "PYTHONPATH=$PYTHONPATH" python3 controller.py --config controller_config.json