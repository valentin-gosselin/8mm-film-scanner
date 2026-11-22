#!/bin/bash
# Film Scanner Tools - Toggle launcher
# Starts the server if not running, stops it if running

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="/tmp/film_scanner_tools.pid"
PORT=8080

is_running() {
    if [ -f "$PID_FILE" ]; then
        pid=$(cat "$PID_FILE")
        if ps -p "$pid" > /dev/null 2>&1; then
            return 0
        fi
    fi
    return 1
}

start_server() {
    cd "$SCRIPT_DIR"

    # Install dependencies if needed
    pip install -q fastapi uvicorn pidng numpy pillow 2>/dev/null

    # Start server in background
    nohup python3 film_scanner_tools.py > /tmp/film_scanner_tools.log 2>&1 &
    echo $! > "$PID_FILE"

    # Wait a bit and open browser
    sleep 1
    xdg-open "http://localhost:$PORT" 2>/dev/null || firefox "http://localhost:$PORT" 2>/dev/null &

    notify-send "Film Scanner Tools" "Serveur démarré sur http://localhost:$PORT" -i video-x-generic 2>/dev/null
}

stop_server() {
    if [ -f "$PID_FILE" ]; then
        pid=$(cat "$PID_FILE")
        kill "$pid" 2>/dev/null
        rm -f "$PID_FILE"
        notify-send "Film Scanner Tools" "Serveur arrêté" -i video-x-generic 2>/dev/null
    fi
}

# Toggle
if is_running; then
    stop_server
else
    start_server
fi
