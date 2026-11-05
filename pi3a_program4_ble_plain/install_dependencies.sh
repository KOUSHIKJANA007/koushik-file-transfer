#!/bin/bash
# ============================================================
# install_dependencies.sh — one-time setup for a new camera Pi
# ============================================================
# Installs everything camera_sender needs:
#   • system tools : ffmpeg, rpicam-apps, v4l-utils, curl, pigpio, qrencode, bluez,
#                    NetworkManager, iw, rfkill
#   • python libs  : aiortc, av, opencv, numpy, aiohttp, requests,
#                    python-dotenv, onvif-zeep
#   • wifi-manager : Wi-Fi auto-connect + Bluetooth (BLE) setup service
#
# Usage (from the folder that holds start_pi.sh and camera_sender/):
#   chmod +x install_dependencies.sh
#   sudo ./install_dependencies.sh            # everything
#   sudo ./install_dependencies.sh --no-wifi  # skip wifi-manager service
#
# Safe to run again — already-installed parts are skipped.
# ============================================================

set -u

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✅ $1${NC}"; }
fail() { echo -e "${RED}❌ $1${NC}"; }
info() { echo -e "${YELLOW}ℹ  $1${NC}"; }
step() { echo ""; echo "━━━━ $1 ━━━━"; }

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo:  sudo $0"
    exit 1
fi

INSTALL_WIFI=1
[ "${1:-}" = "--no-wifi" ] && INSTALL_WIFI=0

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$BASE_DIR/camera_sender"
SENDER_DIR="$APP_DIR/sender"
LOG_DIR="$APP_DIR/logs"
TARGET_USER="${SUDO_USER:-$(logname 2>/dev/null || echo pi)}"
PIP="python3 -m pip install --break-system-packages --prefer-binary --no-cache-dir"
FAILED=()

if [ ! -d "$SENDER_DIR" ]; then
    fail "camera_sender/sender not found next to this script ($BASE_DIR)"
    exit 1
fi

# Install apt packages one by one so a single missing name doesn't stop the rest
apt_install() {
    local rc=0
    for pkg in "$@"; do
        if dpkg -s "$pkg" 2>/dev/null | grep -q "^Status: install ok installed"; then
            echo "   • $pkg (already installed)"
        elif apt-get install -y --no-install-recommends "$pkg" >/dev/null 2>&1; then
            echo "   • $pkg installed"
        else
            echo "   • $pkg — not available"
            rc=1
        fi
    done
    return $rc
}

py_has() { python3 -c "import $1" >/dev/null 2>&1; }

# ── 1. System packages ──────────────────────────────────────────────────────
step "1/6  System packages (apt)"
info "Updating package lists..."
apt-get update -y >/dev/null 2>&1 || info "apt-get update had errors — continuing"

apt_install python3 python3-pip ffmpeg v4l-utils curl iw rfkill wget unzip \
    || true

# Pi camera tools: 'rpicam-apps' on Bookworm, 'libcamera-apps' on older OS
apt_install rpicam-apps-lite || apt_install rpicam-apps || apt_install libcamera-apps \
    || info "Pi camera tools not installed (only needed for camera type=1)"
if ! command -v rpicam-vid >/dev/null && command -v libcamera-vid >/dev/null; then
    ln -sf "$(command -v libcamera-vid)" /usr/local/bin/rpicam-vid
    info "Linked libcamera-vid → rpicam-vid"
fi

# qrencode — makes the camera's setup QR sticker (./start_pi.sh qr)
info "Installing qrencode (QR sticker)..."
apt-get install -y qrencode >/dev/null 2>&1 && echo "   • qrencode installed" \
    || { echo "   • qrencode failed"; FAILED+=("qrencode"); }

# NetworkManager (for wifi-manager)
apt_install network-manager || FAILED+=("network-manager")

# Bluetooth setup (BLE): BlueZ + Python D-Bus bindings
apt_install bluez python3-dbus python3-gi || FAILED+=("bluez/python3-dbus/python3-gi")
systemctl enable --now bluetooth >/dev/null 2>&1 || true

# ── 2. pigpio (start_pi.sh runs /usr/local/bin/pigpiod) ─────────────────────
step "2/6  pigpio"
if [ -x /usr/local/bin/pigpiod ]; then
    ok "pigpiod already at /usr/local/bin/pigpiod"
else
    if apt_install pigpio python3-pigpio && [ -x /usr/bin/pigpiod ]; then
        ln -sf /usr/bin/pigpiod /usr/local/bin/pigpiod
        # start_pi.sh starts it itself; stop apt's service so they don't clash
        systemctl disable --now pigpiod >/dev/null 2>&1 || true
        ok "pigpio installed from apt (linked to /usr/local/bin/pigpiod)"
    else
        info "pigpio not in apt — building from source (a few minutes)..."
        apt_install build-essential >/dev/null || true
        TMP=$(mktemp -d)
        if wget -q -O "$TMP/pigpio.zip" https://github.com/joan2937/pigpio/archive/refs/heads/master.zip \
           && unzip -q "$TMP/pigpio.zip" -d "$TMP" \
           && make -C "$TMP/pigpio-master" -j2 >/dev/null 2>&1 \
           && make -C "$TMP/pigpio-master" install >/dev/null 2>&1; then
            ldconfig
            ok "pigpio built and installed"
        else
            fail "pigpio build failed"
            FAILED+=("pigpio")
        fi
        rm -rf "$TMP"
    fi
fi

# ── 3. Python packages (apt first: prebuilt and fast on the Pi) ─────────────
step "3/6  Python packages"
apt_install python3-numpy     || true
apt_install python3-opencv    || true
apt_install python3-requests  || true
apt_install python3-aiohttp   || true
apt_install python3-dotenv    || true
apt_install python3-av        || true
apt_install python3-cryptography python3-openssl || true

# Anything apt couldn't provide -> pip
declare -A PIP_NAME=(
    [numpy]=numpy
    [cv2]=opencv-python-headless
    [requests]=requests
    [aiohttp]=aiohttp
    [dotenv]=python-dotenv
    [av]=av
)
for mod in numpy cv2 requests aiohttp dotenv av; do
    if py_has "$mod"; then
        echo "   • $mod ok"
    else
        info "Installing ${PIP_NAME[$mod]} with pip..."
        $PIP "${PIP_NAME[$mod]}" || FAILED+=("${PIP_NAME[$mod]}")
    fi
done

# aiortc: keep the already-installed 'av' so pip picks a compatible aiortc
# instead of trying to compile a newer av (hours on a Pi Zero).
if py_has aiortc; then
    echo "   • aiortc ok"
else
    if ! apt_install python3-aiortc; then
        info "Installing aiortc with pip..."
        CONSTRAINT=$(mktemp)
        AV_VER=$(python3 -c "import av; print(av.__version__)" 2>/dev/null || true)
        [ -n "$AV_VER" ] && echo "av==$AV_VER" > "$CONSTRAINT"
        $PIP -c "$CONSTRAINT" aiortc || FAILED+=("aiortc")
        rm -f "$CONSTRAINT"
    fi
fi

# ONVIF (only needed for Imou PTZ, camera type=3)
if py_has onvif; then
    echo "   • onvif ok"
else
    $PIP onvif-zeep >/dev/null 2>&1 && echo "   • onvif-zeep installed" \
        || info "onvif-zeep failed (only needed for Imou PTZ)"
fi

# camera.ini is per camera: create it from the template only if missing
if [ ! -f "$SENDER_DIR/camera.ini" ] && [ -f "$SENDER_DIR/camera.ini.example" ]; then
    cp "$SENDER_DIR/camera.ini.example" "$SENDER_DIR/camera.ini"
    info "Created camera.ini from the template — set type and camera_uuid for this camera"
fi

# ── 4. Folders and permissions ──────────────────────────────────────────────
step "4/6  Folders & permissions"
mkdir -p "$LOG_DIR" /tmp/frames
chown -R "$TARGET_USER":"$TARGET_USER" "$APP_DIR"
chmod +x "$BASE_DIR"/start_pi.sh "$SENDER_DIR"/*.sh "$SENDER_DIR"/*.py 2>/dev/null
for grp in video gpio; do
    getent group "$grp" >/dev/null && usermod -aG "$grp" "$TARGET_USER"
done
# start_pi.sh runs 'sudo pigpiod' — allow without a password
cat > /etc/sudoers.d/camera_sender <<EOF
$TARGET_USER ALL=(root) NOPASSWD: /usr/local/bin/pigpiod
EOF
chmod 440 /etc/sudoers.d/camera_sender
visudo -cf /etc/sudoers.d/camera_sender >/dev/null || rm -f /etc/sudoers.d/camera_sender
ok "Logs in $LOG_DIR, user '$TARGET_USER' added to video/gpio"

# ── 5. Wi-Fi manager service ────────────────────────────────────────────────
step "5/6  Wi-Fi manager"
if [ "$INSTALL_WIFI" -eq 1 ]; then
    if systemctl is-active --quiet NetworkManager; then
        "$SENDER_DIR/install_wifi_manager.sh" || FAILED+=("wifi-manager")
    else
        info "NetworkManager is installed but not active (older Pi OS)."
        info "Run: sudo raspi-config → 6 Advanced → AA Network Config → NetworkManager"
        info "Reboot, then:  sudo $SENDER_DIR/install_wifi_manager.sh"
        FAILED+=("wifi-manager (NetworkManager not active)")
    fi
else
    info "Skipped (--no-wifi)"
fi

# ── 6. Verify ───────────────────────────────────────────────────────────────
step "6/6  Check"
for cmd in ffmpeg curl nmcli iw qrencode bluetoothctl rfkill; do
    command -v "$cmd" >/dev/null && ok "$cmd" || fail "$cmd missing"
done
command -v rpicam-vid >/dev/null && ok "rpicam-vid" || info "rpicam-vid missing (only for camera type=1)"
[ -x /usr/local/bin/pigpiod ] && ok "pigpiod" || fail "pigpiod missing"
for mod in aiortc av cv2 numpy aiohttp requests dotenv dbus gi; do
    py_has "$mod" && ok "python: $mod" || fail "python: $mod missing"
done
py_has onvif && ok "python: onvif" || info "python: onvif missing (only for Imou PTZ)"
[ -f "$SENDER_DIR/ptz_servo.py" ] || info "ptz_servo.py not in project — PTZ type 'servo' won't work"

echo ""
if [ ${#FAILED[@]} -eq 0 ]; then
    ok "All dependencies installed. Start the camera with:  ./start_pi.sh"
    info "Log out and back in (or reboot) once so the new group permissions apply."
else
    fail "Finished with problems: ${FAILED[*]}"
    exit 1
fi
