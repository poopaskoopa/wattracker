#!/usr/bin/env bash
# Start wattracker in the background. Safe to run twice: if the server is
# already up, this reports it and exits without starting a second one.
set -euo pipefail

cd "$(dirname "$0")"

PYTHON=".venv/bin/python"
INSTALLER="scripts/install.sh"
MARKER=".venv/.wattracker-installed"
STATE_DIR="${WATTRACKER_HOME:-$HOME/.wattracker}"
LOG="$STATE_DIR/server.log"
PIDFILE="$STATE_DIR/server.pid"
PORT="${WATTRACKER_PORT:-8000}"
URL="http://127.0.0.1:$PORT"

if [ ! -x "$PYTHON" ] || [ ! -f "$MARKER" ] || \
   [ "$INSTALLER" -nt "$MARKER" ] || [ "pyproject.toml" -nt "$MARKER" ]; then
    if [ ! -x "$INSTALLER" ]; then
        echo "Missing installer: $PWD/$INSTALLER" >&2
        exit 1
    fi
    "$INSTALLER"
fi

mkdir -p "$STATE_DIR"

existing=""
if [ -f "$PIDFILE" ]; then
    recorded="$(sed -n '1p' "$PIDFILE" 2>/dev/null || true)"
    case "$recorded" in
        ''|*[!0-9]*) recorded="" ;;
    esac
    if [ -n "$recorded" ] && kill -0 "$recorded" 2>/dev/null && \
       lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -a -p "$recorded" >/dev/null 2>&1; then
        existing="$recorded"
    fi
fi

if [ -n "$existing" ]; then
    echo "Already running (pid $existing) at $URL"
    exit 0
fi

# Only one process can hold the port. Starting a second would burn CPU and
# quietly fail to bind, so refuse rather than leave a stray behind.
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Port $PORT is already in use by something else:" >&2
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2
    exit 1
fi

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
    # Confirm OUR process owns the port, not some other listener that would
    # make a plain HTTP check look successful.
    if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -a -p "$pid" >/dev/null 2>&1; then
        echo "Started (pid $pid) at $URL"
        echo "Log: $LOG"
        exit 0
    fi
    sleep 1
done

echo "Started (pid $pid) but it is not listening on $PORT after 60s." >&2
echo "Check $LOG" >&2
exit 1
