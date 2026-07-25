#!/usr/bin/env bash
# Stop wattracker. Asks politely first so an in-progress ride finalizes and
# saves; only escalates if the server ignores that.
set -euo pipefail

cd "$(dirname "$0")"

STATE_DIR="${WATTRACKER_HOME:-$HOME/.wattracker}"
PIDFILE="$STATE_DIR/server.pid"

# The process runs as ".venv/bin/python -m wattracker" with a relative path,
# so match the module invocation, not an absolute one. The bracket keeps
# pgrep from matching its own command line.
pids="$(pgrep -f "[p]ython -m wattracker" || true)"
if [ -z "$pids" ]; then
    echo "Not running."
    rm -f "$PIDFILE"
    exit 0
fi

for pid in $pids; do
    echo "Stopping pid $pid..."
    kill "$pid" 2>/dev/null || continue

    stopped=""
    for _ in $(seq 1 30); do
        if ! kill -0 "$pid" 2>/dev/null; then
            stopped=1
            break
        fi
        sleep 0.5
    done

    if [ -z "$stopped" ]; then
        # SIGKILL cannot be caught, so a ride in progress is lost. Only get
        # here if the graceful shutdown had 15 seconds and did not finish.
        echo "  did not exit after 15s; forcing." >&2
        kill -9 "$pid" 2>/dev/null || true
        sleep 1
    fi
done

rm -f "$PIDFILE"
echo "Stopped."
