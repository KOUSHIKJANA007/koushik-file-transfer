#!/bin/bash
# ============================================================
# start_pi.sh — Pi Zero 2W Startup Script
# ============================================================
# Starts both Pi services:
#   1. filesender.py  — captures frames + uploads to EC2
#   2. livesender.py  — WebRTC live view sender
#
# All files in: ~/camera_sender/sender/
#
# Usage:
#   chmod +x start_pi.sh
#   ./start_pi.sh          # start both
#   ./start_pi.sh stop     # stop both
#   ./start_pi.sh status   # check status
#   ./start_pi.sh restart  # restart both
#   ./start_pi.sh logs     # tail both logs
#   ./start_pi.sh logs-file
#   ./start_pi.sh logs-live
# ============================================================

SENDER_DIR="$HOME/camera_sender/sender"
LOG_DIR="$HOME/camera_sender/logs"
FILESENDER_LOG="$LOG_DIR/filesender.log"
LIVESENDER_LOG="$LOG_DIR/livesender.log"
FILESENDER_PID="$LOG_DIR/filesender.pid"
LIVESENDER_PID="$LOG_DIR/livesender.pid"

# WebRTC signaling server on EC2
EC2_LIVESERVER="http://151.185.47.108:8080"
CAMERA_ID="550e8400-e29b-41d4-a716-000000000030"

mkdir -p "$LOG_DIR"

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

check_ec2() {
    info "Checking EC2 connection..."
    EC2_URL=$(grep "ec2_url" "$SENDER_DIR/camera.ini" 2>/dev/null | \
              sed 's/.*= *//' | tr -d ' ')
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
    nohup python3 filesender.py > "$FILESENDER_LOG" 2>&1 &
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
    nohup python3 livesender.py \
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

check_status() {
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Pi Zero 2W Status"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

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
        echo ""
        ;;

    restart)
        echo ""
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "  Restarting Pi Zero 2W Services"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        stop_filesender
        stop_livesender
        sleep 2
        check_pigpiod
        echo ""
        start_filesender
        start_livesender
        echo ""
        check_status
        ;;

    status)
        check_status
        ;;

    logs)
        echo "Tailing both logs (Ctrl+C to stop)..."
        tail -f "$FILESENDER_LOG" "$LIVESENDER_LOG"
        ;;

    logs-file)
        tail -f "$FILESENDER_LOG"
        ;;

    logs-live)
        tail -f "$LIVESENDER_LOG"
        ;;

    *)
        echo "Usage: $0 {start|stop|restart|status|logs|logs-file|logs-live}"
        exit 1
        ;;
esac
