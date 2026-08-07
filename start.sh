#!/usr/bin/env bash
# Start wattracker in the background. Safe to run twice: if the server is
# already up, this reports it and exits without starting a second one.
set -euo pipefail

cd "$(dirname "$0")"

PYTHON="$PWD/.venv/bin/python"
INSTALLER="scripts/install.sh"
MARKER=".venv/.wattracker-installed"
STATE_DIR="${WATTRACKER_HOME:-$HOME/.wattracker}"
LOG="$STATE_DIR/server.log"
PIDFILE="$STATE_DIR/server.pid"
HOST="${WATTRACKER_HOST:-127.0.0.1}"
PORT="${WATTRACKER_PORT:-8000}"
URL="http://$HOST:$PORT"

if [ ! -x "$PYTHON" ] || [ ! -f "$MARKER" ] || \
   [ "$INSTALLER" -nt "$MARKER" ] || [ "pyproject.toml" -nt "$MARKER" ]; then
    if [ ! -x "$INSTALLER" ]; then
        echo "Missing installer: $PWD/$INSTALLER" >&2
        exit 1
    fi
    "$INSTALLER"
fi

mkdir -p "$STATE_DIR"

port_is_listening() {
    "$PYTHON" -c 'import socket, sys; s = socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=0.2); s.close()' \
        "$HOST" "$PORT" >/dev/null 2>&1
}

port_is_owned_by() {
    if command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -a -p "$1" >/dev/null 2>&1
    else
        return 1
    fi
}

log_has_fresh_bind() {
    [ -f "$LOG" ] || return 1
    tail -c "+$((LOG_OFFSET + 1))" "$LOG" |
        grep -F "Uvicorn running on" |
        grep -F ":$PORT" >/dev/null 2>&1
}

server_is_ready() {
    if command -v lsof >/dev/null 2>&1; then
        port_is_owned_by "$1"
    else
        log_has_fresh_bind
    fi
}

existing=""
if [ -f "$PIDFILE" ]; then
    recorded="$(sed -n '1p' "$PIDFILE" 2>/dev/null || true)"
    case "$recorded" in
        ''|*[!0-9]*) recorded="" ;;
    esac
    if [ -n "$recorded" ] && kill -0 "$recorded" 2>/dev/null && \
       port_is_owned_by "$recorded"; then
        existing="$recorded"
    fi
fi

if [ -n "$existing" ]; then
    echo "Already running (pid $existing) at $URL"
    exit 0
fi

# Only one process can hold the port. Starting a second would burn CPU and
# quietly fail to bind, so refuse rather than leave a stray behind.
if port_is_listening; then
    echo "Port $PORT is already in use by something else:" >&2
    if command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2
    fi
    exit 1
fi

LOG_OFFSET="$("$PYTHON" -c 'import os, sys; print(os.path.getsize(sys.argv[1]) if os.path.exists(sys.argv[1]) else 0)' "$LOG")"
nohup "$PYTHON" -m wattracker >>"$LOG" 2>&1 &
pid=$!
echo "$pid" >"$PIDFILE"

# The database migrates on startup, which takes a moment on a large one.
for _ in $(seq 1 60); do
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "Server exited during startup. Last lines of $LOG:" >&2
        tail -20 "$LOG" >&2
        rm -f "$PIDFILE"
        exit 1
    fi
    # Confirm the launched process is alive and the configured local port is
    # accepting connections. Without lsof, also require a fresh Uvicorn bind
    # line from this launch, so another listener cannot look healthy.
    if server_is_ready "$pid"; then
        echo "Started (pid $pid) at $URL"
        echo "Log: $LOG"
        exit 0
    fi
    sleep 1
done

echo "Started (pid $pid) but it is not listening on $PORT after 60s." >&2
echo "Check $LOG" >&2
exit 1
