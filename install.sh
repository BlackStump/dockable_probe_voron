#!/bin/bash

SRCDIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )"/ && pwd )"
KLIPPER_PATH="${HOME}/klipper"
SYSTEMDDIR="/etc/systemd/system"
MOONRAKER_CONFIG="${HOME}/printer_data/config/moonraker.conf"

# Force script to exit if an error occurs
set -e

# Step 1: Check for root user
verify_ready()
{
    # check for root user
    if [ "$EUID" -eq 0 ]; then
        echo "This script must not run as root"
        exit -1
    fi
}

# Step 2:  Verify Klipper has been installed
check_klipper()
{
    if [ "$(sudo systemctl list-units --full -all -t service --no-legend | grep -F "klipper.service")" ]; then
            echo "Klipper service found!"
        else
            echo "Klipper service not found, please install Klipper first"
            exit -1
        fi
}

# Step 3: Check folders
check_requirements()
{
    if [ ! -d "${KLIPPER_PATH}/klippy/extras/" ]; then
        echo "Error: Klipper not found in directory: ${KLIPPER_PATH}. Exiting.."
        exit -1
    fi
    echo "Klipper found at ${KLIPPER_PATH}"

    if [ ! -f "$MOONRAKER_CONFIG" ]; then
        echo "Error: Moonraker configuration not found: ${MOONRAKER_CONFIG}. Exiting.."
        exit -1
    fi
    echo "Moonraker configuration found at ${MOONRAKER_CONFIG}"
}

# Step 4: Link extension to Klipper
link_extension()
{
    echo -n "Linking extension to Klipper... "
    ln -sf "${SRCDIR}/dockable_probe.py" "${KLIPPER_PATH}/klippy/extras/dockable_probe.py"
    echo "[OK]"
}

# Step 5: Add updater to moonraker.conf
add_updater()
{
    echo -n "Adding update manager to moonraker.conf... "
    update_section=$(grep -c '\[update_manager[a-z ]* dockable_probe_voron\]' $MOONRAKER_CONFIG || true)
    if [ "$update_section" -eq 0 ]; then
        echo -e "\n[update_manager dockable_probe_voron]" >> "$MOONRAKER_CONFIG"
        echo "type: git_repo" >> "$MOONRAKER_CONFIG"
        echo "path: ${SRCDIR}" >> "$MOONRAKER_CONFIG"
        echo "origin: https://github.com/BlackStump/dockable_probe_voron.git" >> "$MOONRAKER_CONFIG"
        echo "managed_services: klipper" >> "$MOONRAKER_CONFIG"
        echo -e "\n" >> "$MOONRAKER_CONFIG"
        echo "[OK]"

        echo -n "Restarting Moonraker... "
        sudo systemctl restart moonraker
        echo "[OK]"
    else
        echo "[SKIPPED]"
    fi
}

# Step 6: Restarting Klipper
restart_klipper()
{
        echo -n "Restarting Klipper... "
        sudo systemctl restart klipper
        echo "[OK]"
 }


# Run steps
verify_ready
check_klipper
check_requirements
link_extension
restart_klipper
