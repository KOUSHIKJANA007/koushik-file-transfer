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
# A "masked" unit is a link to /dev/null: unmask first, or nothing below takes effect
systemctl unmask wifi-manager.service >/dev/null 2>&1 || true
rm -f /etc/systemd/system/wifi-manager.service

# Service definition written here, so no template file is needed
cat > /etc/systemd/system/wifi-manager.service <<UNIT
[Unit]
Description=Camera Wi-Fi manager (auto-connect, Bluetooth setup)
After=NetworkManager.service bluetooth.service
Wants=NetworkManager.service bluetooth.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 $SENDER_DIR/wifi_manager.py --config $SENDER_DIR/camera.ini
Restart=always
RestartSec=5
StandardOutput=append:$LOG_DIR/wifi_manager.log
StandardError=append:$LOG_DIR/wifi_manager.log

[Install]
WantedBy=multi-user.target
UNIT

if [ ! -f "$SENDER_DIR/camera.ini" ]; then
    if [ -f "$SENDER_DIR/camera.ini.example" ]; then
        cp "$SENDER_DIR/camera.ini.example" "$SENDER_DIR/camera.ini"
        echo "ℹ  Created camera.ini from the template — set type and camera_uuid"
    else
        echo "❌ $SENDER_DIR/camera.ini not found"; exit 1
    fi
fi
chmod +x "$SENDER_DIR/wifi_manager.py"
systemctl daemon-reload
systemctl enable wifi-manager.service
systemctl restart wifi-manager.service
echo "✅ wifi-manager installed and running"
echo "   Logs:   tail -f $LOG_DIR/wifi_manager.log"
echo "   Status: ~/start_pi.sh wifi    Bluetooth: ~/start_pi.sh bt-status"
