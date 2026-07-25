#!/usr/bin/env bash
# Start wattracker in the background. Safe to run twice: if the server is
# already up, this reports it and exits without starting a second one.
set -euo pipefail

cd "$(dirname "$0")"

PYTHON=".venv/bin/python"
STATE_DIR="${WATTRACKER_HOME:-$HOME/.wattracker}"
LOG="$STATE_DIR/server.log"
PIDFILE="$STATE_DIR/server.pid"
PORT="${WATTRACKER_PORT:-8000}"
URL="http://127.0.0.1:$PORT"

if [ ! -x "$PYTHON" ]; then
    echo "No virtualenv at $PWD/$PYTHON" >&2
    echo "Create one with: python3 -m venv .venv && .venv/bin/pip install -e ." >&2
    exit 1
fi

mkdir -p "$STATE_DIR"

# The process runs as ".venv/bin/python -m wattracker" with a relative path,
# so match the module invocation, not an absolute one. The bracket keeps
# pgrep from matching its own command line.
PATTERN="[p]ython -m wattracker"

existing="$(pgrep -f "$PATTERN" | head -1 || true)"
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
