#!/bin/bash

MODULE_DIR="/home/stan/sde/install/bin"
INSTALL_PATH="/home/stan/sde/install"
MODULES=("bf_fpga_mod_load" "bf_kdrv_mod_load" "bf_knet_mod_load" "bf_kpkt_mod_load")

for module in "${MODULES[@]}"; do
    echo "Loading $module..."
    sudo "$MODULE_DIR/$module" "$INSTALL_PATH"
    if [ $? -eq 0 ]; then
        echo "$module loaded successfully."
    else
        echo "Failed to load $module."
    fi
done

echo "Modules loaded:"
lsmod | grep "bf"

