#!/bin/bash
# install.sh - symlink dockable_probe.py into Klipper extras
# Usage:  bash install.sh [klipper_path]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KLIPPER_PATH="${1:-${HOME}/klipper}"
EXTRAS="${KLIPPER_PATH}/klippy/extras"
TARGET="${EXTRAS}/dockable_probe.py"
SOURCE="${SCRIPT_DIR}/dockable_probe.py"

if [ ! -d "${EXTRAS}" ]; then
    echo "ERROR: Klipper extras directory not found at ${EXTRAS}"
    echo "Usage: bash install.sh /path/to/klipper"
    exit 1
fi

if [ ! -f "${SOURCE}" ]; then
    echo "ERROR: dockable_probe.py not found at ${SOURCE}"
    exit 1
fi

# Remove old copy or stale symlink if present
if [ -e "${TARGET}" ] || [ -L "${TARGET}" ]; then
    echo "Removing existing ${TARGET} ..."
    rm "${TARGET}"
fi

echo "Creating symlink: ${TARGET} -> ${SOURCE}"
ln -s "${SOURCE}" "${TARGET}"

echo "Restarting Klipper service ..."
sudo systemctl restart klipper

echo "Done."
echo "Future updates: git pull in ${SCRIPT_DIR}, then sudo systemctl restart klipper"
