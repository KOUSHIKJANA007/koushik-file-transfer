#!/bin/bash
# Installs wifi_manager.py as a boot-time systemd service.
# Run once on the Pi:   cd ~/camera_sender/sender && sudo ./install_wifi_manager.sh
set -e
SENDER_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$(dirname "$SENDER_DIR")/logs"

if [ "$(id -u)" -ne 0 ]; then echo "Run with sudo"; exit 1; fi

if ! command -v nmcli >/dev/null || ! systemctl is-active --quiet NetworkManager; then
    echo "❌ NetworkManager is not active."
    echo "   Raspberry Pi OS Bookworm uses it by default. On Bullseye run:"
    echo "   sudo raspi-config → 6 Advanced Options → AA Network Config → NetworkManager, then reboot."
    exit 1
fi

# Wi-Fi needs a country to be set
if command -v raspi-config >/dev/null; then
    COUNTRY=$(raspi-config nonint get_wifi_country 2>/dev/null || true)
    if [ -z "$COUNTRY" ]; then
        echo "ℹ  Setting Wi-Fi country to IN (change with: sudo raspi-config nonint do_wifi_country XX)"
        raspi-config nonint do_wifi_country IN || true
    fi
fi

command -v iw >/dev/null || apt-get install -y iw || true

# Bluetooth setup needs BlueZ + the Python D-Bus bindings
apt-get install -y --no-install-recommends bluez python3-dbus python3-gi >/dev/null \
    || { echo "❌ Could not install bluez python3-dbus python3-gi"; exit 1; }
systemctl enable --now bluetooth >/dev/null 2>&1 || true
if ! python3 -c "import dbus, gi" 2>/dev/null; then
    echo "❌ python3 can't import dbus/gi — Bluetooth setup won't work"; exit 1
fi

mkdir -p "$LOG_DIR"
sed -e "s#__SENDER_DIR__#$SENDER_DIR#g" -e "s#__LOG_DIR__#$LOG_DIR#g" \
    "$SENDER_DIR/wifi-manager.service" > /etc/systemd/system/wifi-manager.service
chmod +x "$SENDER_DIR/wifi_manager.py"
systemctl daemon-reload
systemctl enable wifi-manager.service
systemctl restart wifi-manager.service
echo "✅ wifi-manager installed and running"
echo "   Logs:   tail -f $LOG_DIR/wifi_manager.log"
echo "   Status: ~/start_pi.sh wifi    Bluetooth: ~/start_pi.sh bt-status"
