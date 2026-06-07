#!/bin/bash
# install.sh - install dockable_probe.py into Klipper extras
# Usage:  bash install.sh [klipper_path]

KLIPPER_PATH="${1:-${HOME}/klipper}"
EXTRAS="${KLIPPER_PATH}/klippy/extras"

if [ ! -d "${EXTRAS}" ]; then
    echo "ERROR: Klipper extras directory not found at ${EXTRAS}"
    echo "Usage: bash install.sh /path/to/klipper"
    exit 1
fi

echo "Installing dockable_probe.py to ${EXTRAS} ..."
cp -v dockable_probe.py "${EXTRAS}/dockable_probe.py"

echo "Restarting Klipper service ..."
sudo systemctl restart klipper

echo "Done.  Add [include dockable-probe.cfg] to your printer.cfg."
