#!/bin/bash
# ============================================================
# start_pi.sh — Pi Zero 2W Startup Script
# ============================================================
# Starts all Pi services:
#   1. filesender.py        — captures frames + uploads to EC2
#   2. livesender.py        — WebRTC live view, P2P to the phone (port 8080, TURN)
#   3. livesenderNoTurn.py  — WebRTC live view via the EC2 relay (port 8090, no TURN)
#      Streams only while someone watches through the relay.
#
# All files in: ~/camera_sender/sender/
#
# First time on a new Pi:
#   sudo ./install_dependencies.sh   # installs everything + Wi-Fi manager
#
# Usage:
#   chmod +x start_pi.sh
#   ./start_pi.sh          # start all
#   ./start_pi.sh stop     # stop all
#   ./start_pi.sh status   # check status
#   ./start_pi.sh restart  # restart all
#   ./start_pi.sh logs     # tail all logs
#   ./start_pi.sh logs-file
#   ./start_pi.sh logs-live
#   ./start_pi.sh logs-relay
#   ./start_pi.sh wifi        # Wi-Fi + Bluetooth status
#   ./start_pi.sh wifi-setup  # Bluetooth setup mode for 10 min (Wi-Fi keeps working)
#   ./start_pi.sh bt-on       # Bluetooth on (setup available) until bt-off / bt-auto
#   ./start_pi.sh bt-off      # Bluetooth off (frees it for GPIO / audio work)
#   ./start_pi.sh bt-auto     # back to automatic: on only when setup is needed
#   ./start_pi.sh bt-status   # Bluetooth state, as JSON
#   ./start_pi.sh logs-wifi
#   ./start_pi.sh qr          # setup key + QR code for the camera sticker
# ============================================================

# Resolve paths from this script's own location (works with or without sudo)
BASE_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
SENDER_DIR="$BASE_DIR/camera_sender/sender"
LOG_DIR="$BASE_DIR/camera_sender/logs"
if [ ! -d "$SENDER_DIR" ]; then
    echo "❌ $SENDER_DIR not found — keep start_pi.sh next to the camera_sender folder"
    exit 1
fi
FILESENDER_LOG="$LOG_DIR/filesender.log"
LIVESENDER_LOG="$LOG_DIR/livesender.log"
FILESENDER_PID="$LOG_DIR/filesender.pid"
LIVESENDER_PID="$LOG_DIR/livesender.pid"
RELAYSENDER_LOG="$LOG_DIR/livesenderNoTurn.log"
RELAYSENDER_PID="$LOG_DIR/livesenderNoTurn.pid"
WIFI_LOG="$LOG_DIR/wifi_manager.log"
WIFI_STATE="/run/wifi_manager/state.json"
CONTROL_API="http://127.0.0.1:8085"

# First run after an update: create camera.ini from the template, never overwrite it
if [ ! -f "$SENDER_DIR/camera.ini" ] && [ -f "$SENDER_DIR/camera.ini.example" ]; then
    cp "$SENDER_DIR/camera.ini.example" "$SENDER_DIR/camera.ini"
    echo "ℹ  Created camera.ini from camera.ini.example — set type and camera_uuid for this camera"
fi

# WebRTC signaling server on EC2
EC2_LIVESERVER="http://151.185.47.108:8080"
# WebRTC relay on EC2 (liveserverNoTurn.py)
EC2_RELAY="http://151.185.47.108:8090"
#CAMERA_ID="550e8400-e29b-41d4-a716-000000000010"
# Only the real "camera_uuid = ..." line (ignore comments / other mentions)
CAMERA_ID=$(grep -E '^[[:space:]]*camera_uuid[[:space:]]*=' "$SENDER_DIR/camera.ini" | head -1 | sed 's/.*= *//' | tr -d ' \r')
mkdir -p "$LOG_DIR"
if [ "$CAMERA_ID" = "SET-THIS-CAMERA-UUID" ] && [ "${1:-start}" != "qr" ]; then
    echo "❌ Set camera_uuid in $SENDER_DIR/camera.ini first (it is still the template value)"
    exit 1
fi

# ── Colours ───────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

ok()   { echo -e "${GREEN}✅ $1${NC}"; }
fail() { echo -e "${RED}❌ $1${NC}"; }
info() { echo -e "${YELLOW}ℹ  $1${NC}"; }


# ── Helpers ───────────────────────────────────────────────────────────────────

is_running() {
    local pidfile="$1"
    if [ -f "$pidfile" ]; then
        local pid=$(cat "$pidfile")
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

check_pigpiod() {
    if ! pigs t > /dev/null 2>&1; then
        info "pigpiod not running — starting..."
        sudo /usr/local/bin/pigpiod
        sleep 1
        if pigs t > /dev/null 2>&1; then
            ok "pigpiod started"
        else
            fail "pigpiod failed to start"
        fi
    else
        ok "pigpiod running"
    fi
}

api_call() {   # api_call METHOD PATH [JSON]  -> prints the reply, fails if the API is down
    if [ -n "$3" ]; then
        curl -s --max-time 30 -X "$1" -H 'Content-Type: application/json' -d "$3" "$CONTROL_API$2"
    else
        curl -s --max-time 10 -X "$1" "$CONTROL_API$2"
    fi
}

wifi_status() {
    if ! systemctl is-active --quiet wifi-manager 2>/dev/null; then
        fail "wifi-manager  NOT RUNNING (install: sudo $SENDER_DIR/install_wifi_manager.sh)"
        return
    fi
    local js
    js=$(api_call GET /api/status) || { info "Wi-Fi         manager starting..."; return; }
    echo "$js" | python3 -c '
import json, sys
s = json.load(sys.stdin); w, b, u = s["wifi"], s["bluetooth"], s["setup"]
G, Y, N = "\033[0;32m", "\033[1;33m", "\033[0m"
if w["connected"]:
    print(G + "✅ Wi-Fi         connected to %r (%s)" % (w["ssid"], w["ip"]) + N)
else:
    print(Y + "ℹ  Wi-Fi         NOT connected" + N)
if b["powered"]:
    print(Y + "🔵 Bluetooth     ON — setup available as %r (mode %s, %s)" % (b["name"], b["mode"], b["reason"]) + N)
else:
    print(G + "⚪ Bluetooth     off (mode %s)" % b["mode"] + N)
if u["state"] == "failed" and u["last_error"]:
    print("   Last setup error: " + u["last_error"])
'
}

check_ec2() {
    info "Checking EC2 connection..."
    EC2_URL=$(grep -E '^[[:space:]]*ec2_url[[:space:]]*=' "$SENDER_DIR/camera.ini" 2>/dev/null | \
              head -1 | sed 's/.*= *//' | tr -d ' \r')
    if [ -z "$EC2_URL" ]; then
        EC2_URL="http://151.185.47.108:8000"
    fi
    if curl -s --max-time 5 "$EC2_URL/health" > /dev/null 2>&1; then
        ok "EC2 reachable ($EC2_URL)"
        return 0
    else
        info "EC2 unreachable ($EC2_URL) — filesender will retry automatically"
        return 0
    fi
}

start_filesender() {
    if is_running "$FILESENDER_PID"; then
        info "filesender already running (PID $(cat $FILESENDER_PID))"
        return
    fi
    info "Starting filesender.py..."
    cd "$SENDER_DIR"
    # Kill any stale camera processes before starting
    pkill -f ffmpeg 2>/dev/null
    pkill -f filesender 2>/dev/null
    sleep 1
    setsid python3 filesender.py > "$FILESENDER_LOG" 2>&1 &
    echo $! > "$FILESENDER_PID"
    sleep 5
    if is_running "$FILESENDER_PID"; then
        ok "filesender.py started (PID $(cat $FILESENDER_PID))"
        info "If EC2 is down, filesender will retry every 30s automatically"
    else
        fail "filesender.py failed — camera issue? check $FILESENDER_LOG"
        tail -5 "$FILESENDER_LOG"
    fi
}

start_livesender() {
    if is_running "$LIVESENDER_PID"; then
        info "livesender already running (PID $(cat $LIVESENDER_PID))"
        return
    fi
    info "Starting livesender.py (id=$CAMERA_ID)..."
    cd "$SENDER_DIR"
    setsid python3 livesender.py \
        --id "$CAMERA_ID" \
        --server "$EC2_LIVESERVER" \
        > "$LIVESENDER_LOG" 2>&1 &
    echo $! > "$LIVESENDER_PID"
    sleep 2
    if is_running "$LIVESENDER_PID"; then
        ok "livesender.py started (PID $(cat $LIVESENDER_PID))"
    else
        fail "livesender.py failed — check $LIVESENDER_LOG"
        tail -5 "$LIVESENDER_LOG"
    fi
}

start_relaysender() {
    if is_running "$RELAYSENDER_PID"; then
        info "livesenderNoTurn already running (PID $(cat $RELAYSENDER_PID))"
        return
    fi
    info "Starting livesenderNoTurn.py (id=$CAMERA_ID → relay)..."
    cd "$SENDER_DIR"
    setsid python3 livesenderNoTurn.py \
        --id "$CAMERA_ID" \
        --server "$EC2_RELAY" \
        > "$RELAYSENDER_LOG" 2>&1 &
    echo $! > "$RELAYSENDER_PID"
    sleep 2
    if is_running "$RELAYSENDER_PID"; then
        ok "livesenderNoTurn.py started (PID $(cat $RELAYSENDER_PID))"
    else
        fail "livesenderNoTurn.py failed — check $RELAYSENDER_LOG"
        tail -5 "$RELAYSENDER_LOG"
    fi
}

stop_filesender() {
    if is_running "$FILESENDER_PID"; then
        kill $(cat "$FILESENDER_PID") 2>/dev/null
        rm -f "$FILESENDER_PID"
        ok "filesender.py stopped"
    else
        info "filesender.py not running"
    fi
}

stop_livesender() {
    if is_running "$LIVESENDER_PID"; then
        kill $(cat "$LIVESENDER_PID") 2>/dev/null
        rm -f "$LIVESENDER_PID"
        ok "livesender.py stopped"
    else
        info "livesender.py not running"
    fi
}

stop_relaysender() {
    if is_running "$RELAYSENDER_PID"; then
        kill $(cat "$RELAYSENDER_PID") 2>/dev/null
        rm -f "$RELAYSENDER_PID"
        ok "livesenderNoTurn.py stopped"
    else
        info "livesenderNoTurn.py not running"
    fi
}

check_status() {
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Pi Zero 2W Status"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    wifi_status
    echo ""

    # pigpiod
    if pigs t > /dev/null 2>&1; then
        ok "pigpiod       running"
    else
        fail "pigpiod       NOT RUNNING"
    fi

    echo ""

    # filesender
    if is_running "$FILESENDER_PID"; then
        ok "filesender.py PID=$(cat $FILESENDER_PID)"
        # Show last upload line
        last=$(grep "UPLOAD OK" "$FILESENDER_LOG" 2>/dev/null | tail -1)
        if [ -n "$last" ]; then
            echo "   Last: $last"
        fi
    else
        fail "filesender.py NOT RUNNING"
    fi

    echo ""

    # livesender
    if is_running "$LIVESENDER_PID"; then
        ok "livesender.py PID=$(cat $LIVESENDER_PID) → $EC2_LIVESERVER"
    else
        fail "livesender.py NOT RUNNING"
    fi

    # livesenderNoTurn (relay)
    if is_running "$RELAYSENDER_PID"; then
        ok "livesenderNoTurn.py PID=$(cat $RELAYSENDER_PID) → $EC2_RELAY"
        last=$(grep -E "Connected to relay|Offer posted|No viewers left|Relay" "$RELAYSENDER_LOG" 2>/dev/null | tail -1)
        [ -n "$last" ] && echo "   Last: $last"
    else
        fail "livesenderNoTurn.py NOT RUNNING"
    fi

    echo ""

    # Camera config
    CAMERA_TYPE=$(grep "^type" "$SENDER_DIR/camera.ini" 2>/dev/null | \
                  head -1 | sed 's/.*= *//' | tr -d ' ')
    PTZ_ENABLED=$(grep "^enabled" "$SENDER_DIR/camera.ini" 2>/dev/null | \
                  head -1 | sed 's/.*= *//' | tr -d ' ')
    case "$CAMERA_TYPE" in
        1) CAM_NAME="Raspberry Pi Camera" ;;
        2) CAM_NAME="USB Webcam" ;;
        3) CAM_NAME="Imou Ranger S2" ;;
        *) CAM_NAME="Unknown" ;;
    esac
    echo "  Camera : $CAM_NAME (type=$CAMERA_TYPE)"
    echo "  PTZ    : $([ "$PTZ_ENABLED" = "1" ] && echo enabled || echo disabled)"
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Logs"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  filesender : $FILESENDER_LOG"
    echo "  livesender : $LIVESENDER_LOG"
    echo "  relay      : $RELAYSENDER_LOG"
    echo ""
}


# ── Main ──────────────────────────────────────────────────────────────────────

case "${1:-start}" in

    start)
        echo ""
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "  Starting Pi Zero 2W Services"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        check_pigpiod
        echo ""
        check_ec2
        echo ""
        start_filesender
        start_livesender
        start_relaysender
        echo ""
        check_status
        ;;

    stop)
        echo ""
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "  Stopping Pi Zero 2W Services"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        stop_filesender
        stop_livesender
        stop_relaysender
        echo ""
        ;;

    restart)
        echo ""
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "  Restarting Pi Zero 2W Services"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        stop_filesender
        stop_livesender
        stop_relaysender
        sleep 2
        check_pigpiod
        echo ""
        start_filesender
        start_livesender
        start_relaysender
        echo ""
        check_status
        ;;

    status)
        check_status
        ;;

    logs)
        echo "Tailing all logs (Ctrl+C to stop)..."
        tail -f "$FILESENDER_LOG" "$LIVESENDER_LOG" "$RELAYSENDER_LOG"
        ;;

    logs-file)
        tail -f "$FILESENDER_LOG"
        ;;

    logs-live)
        tail -f "$LIVESENDER_LOG"
        ;;

    logs-relay)
        tail -f "$RELAYSENDER_LOG"
        ;;

    wifi)
        wifi_status
        ;;

    wifi-setup)
        info "Starting Bluetooth setup mode for 10 min (Wi-Fi stays connected)..."
        api_call POST /api/setup-mode '{"minutes": 10}' >/dev/null \
            || touch /run/wifi_manager/force_ap
        wifi_status
        info "Open the app and connect to the camera over Bluetooth."
        ;;

    bt-on|bt-off|bt-auto)
        state="${1#bt-}"
        api_call POST /api/bluetooth "{\"state\": \"$state\"}" >/dev/null \
            && ok "Bluetooth mode: $state" \
            || { fail "wifi-manager not reachable — is it installed and running?"; exit 1; }
        wifi_status
        ;;

    bt-status)
        api_call GET /api/status | python3 -m json.tool \
            || fail "wifi-manager not reachable"
        ;;

    logs-wifi)
        tail -f "$WIFI_LOG"
        ;;

    qr)
        sudo python3 "$SENDER_DIR/wifi_manager.py" --config "$SENDER_DIR/camera.ini" --qr
        ;;

    *)
        echo "Usage: $0 {start|stop|restart|status|logs|logs-file|logs-live|logs-relay|wifi|wifi-setup|bt-on|bt-off|bt-auto|bt-status|logs-wifi|qr}"
        exit 1
        ;;
esac
