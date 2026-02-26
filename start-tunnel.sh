#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Bambu A1 Monitor — Start with Cloudflare Tunnel
# ─────────────────────────────────────────────────────────────────────────────
# Runs the Flask app and exposes it via a Cloudflare Tunnel so you can
# access your printer dashboard from anywhere in the world.
#
# Usage:
#   ./start-tunnel.sh              # Quick tunnel (random URL each time)
#   ./start-tunnel.sh --named      # Named tunnel (permanent URL, requires setup)
#
# First-time named tunnel setup:
#   cloudflared tunnel login
#   cloudflared tunnel create bambu-monitor
#   cloudflared tunnel route dns bambu-monitor <subdomain.yourdomain.com>
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_PORT=5000
TUNNEL_NAME="bambu-monitor"
CLOUDFLARED=""

# ── Find cloudflared ────────────────────────────────────────────────────────
find_cloudflared() {
    if command -v cloudflared &>/dev/null; then
        CLOUDFLARED="cloudflared"
    elif [ -x "$HOME/.local/bin/cloudflared" ]; then
        CLOUDFLARED="$HOME/.local/bin/cloudflared"
    else
        echo "ERROR: cloudflared not found."
        echo ""
        echo "Install it:"
        echo "  # Debian/Ubuntu"
        echo "  sudo apt install cloudflared"
        echo ""
        echo "  # macOS"
        echo "  brew install cloudflared"
        echo ""
        echo "  # Or download from https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
        exit 1
    fi
    echo "[Tunnel] Using: $CLOUDFLARED ($($CLOUDFLARED --version 2>&1 | head -1))"
}

# ── Cleanup on exit ─────────────────────────────────────────────────────────
APP_PID=""
TUNNEL_PID=""

cleanup() {
    echo ""
    echo "[Shutdown] Stopping..."
    [ -n "$TUNNEL_PID" ] && kill "$TUNNEL_PID" 2>/dev/null && wait "$TUNNEL_PID" 2>/dev/null
    [ -n "$APP_PID" ] && kill "$APP_PID" 2>/dev/null && wait "$APP_PID" 2>/dev/null
    echo "[Shutdown] Done."
}
trap cleanup EXIT INT TERM

# ── Start the Flask app ─────────────────────────────────────────────────────
start_app() {
    cd "$SCRIPT_DIR"

    # Activate venv if it exists
    if [ -d "venv" ]; then
        source venv/bin/activate
    elif [ -d ".venv" ]; then
        source .venv/bin/activate
    fi

    # Load .env if it exists
    if [ -f ".env" ]; then
        set -a
        source .env
        set +a
    fi

    echo "[App] Starting Bambu A1 Monitor on port $APP_PORT..."
    python app.py &
    APP_PID=$!

    # Wait for the app to start accepting connections
    for i in $(seq 1 15); do
        if curl -s -o /dev/null "http://localhost:$APP_PORT" 2>/dev/null; then
            echo "[App] Ready on http://localhost:$APP_PORT"
            return 0
        fi
        sleep 1
    done
    echo "[App] WARNING: App may not have started properly. Continuing anyway..."
}

# ── Start the tunnel ────────────────────────────────────────────────────────
start_quick_tunnel() {
    echo "[Tunnel] Starting quick tunnel (random URL)..."
    echo "[Tunnel] Your public URL will appear below:"
    echo ""
    $CLOUDFLARED tunnel --url "http://localhost:$APP_PORT" &
    TUNNEL_PID=$!
}

start_named_tunnel() {
    # Check if the tunnel exists
    if ! $CLOUDFLARED tunnel list 2>/dev/null | grep -q "$TUNNEL_NAME"; then
        echo "ERROR: Named tunnel '$TUNNEL_NAME' not found."
        echo ""
        echo "Create it first:"
        echo "  $CLOUDFLARED tunnel login"
        echo "  $CLOUDFLARED tunnel create $TUNNEL_NAME"
        echo "  $CLOUDFLARED tunnel route dns $TUNNEL_NAME <subdomain.yourdomain.com>"
        exit 1
    fi

    echo "[Tunnel] Starting named tunnel '$TUNNEL_NAME'..."
    $CLOUDFLARED tunnel --url "http://localhost:$APP_PORT" run "$TUNNEL_NAME" &
    TUNNEL_PID=$!
}

# ── Main ────────────────────────────────────────────────────────────────────
MODE="quick"
if [ "${1:-}" = "--named" ]; then
    MODE="named"
fi

echo "==========================================="
echo "  BAMBU A1 MONITOR + CLOUDFLARE TUNNEL"
echo "==========================================="
echo ""

find_cloudflared
start_app

if [ "$MODE" = "named" ]; then
    start_named_tunnel
else
    start_quick_tunnel
fi

echo ""
echo "[Info] Press Ctrl+C to stop everything."
echo ""

# Wait for either process to exit
wait -n "$APP_PID" "$TUNNEL_PID" 2>/dev/null || true
