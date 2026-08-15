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

# Match the module invocation, not the exact interpreter path. Framework
# Python builds (Homebrew python@3.x, the python.org installers) re-exec the
# interpreter through
# Python.framework/Versions/X.Y/Resources/Python.app/Contents/MacOS/Python and
# rewrite argv[0] as they go, so `ps -o command=` reports that path and the
# venv's "$PYTHON" is nowhere in the command line. Requiring the literal
# "$PYTHON" here made start.sh unable to recognise its own server on most
# macOS installs. Do not tighten this back: identity is not established by
# this function alone. Callers pair it with the pidfile's recorded PID and its
# recorded `lstart` start time (which is what rules out PID reuse) and with
# port_is_owned_by; the no-lsof branch of port_is_owned_by additionally
# requires log_has_pid_bind.
process_command_is_wattracker() {
    local command_line interpreter
    command_line="$(ps -p "$1" -o command= 2>/dev/null || true)"
    case "$command_line" in
        *"$PYTHON -m wattracker"*) return 0 ;;
        *" -m wattracker"*) ;;
        *) return 1 ;;
    esac
    interpreter="${command_line%% -m wattracker*}"
    case "${interpreter##*/}" in
        [Pp]ython|[Pp]ython[0-9]*) return 0 ;;
        *) return 1 ;;
    esac
}

process_start_time() {
    ps -p "$1" -o lstart= 2>/dev/null |
        sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
}

# The start time is what rules out PID reuse, so a missing one fails closed.
# The earlier form wrapped the comparison in an `if` with no `else`: a bash
# function whose last statement is an `if` with a false condition returns 0,
# so a pidfile carrying only a PID — a legacy file written by a pre-lstart
# start.sh, or one where `ps -o lstart=` came back empty — skipped the check
# and passed. Requiring both values costs nothing on upgrade: every successful
# start rewrites the pidfile with the PID *and* its lstart, so a one-line file
# self-heals after a single launch.
recorded_process_is_wattracker() {
    local actual_start
    process_command_is_wattracker "$1" || return 1
    [ "$#" -ge 2 ] && [ -n "$2" ] || return 1
    actual_start="$(process_start_time "$1")"
    [ -n "$actual_start" ] || return 1
    [ "$actual_start" = "$2" ]
}

log_has_pid_bind() {
    [ -f "$LOG" ] || return 1
    grep -F "Started server process [$1]" "$LOG" >/dev/null 2>&1 &&
        grep -F "Uvicorn running on" "$LOG" |
        grep -F ":$PORT" >/dev/null 2>&1
}

port_is_owned_by() {
    if command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -a -p "$1" >/dev/null 2>&1
    else
        process_command_is_wattracker "$1" &&
            port_is_listening &&
            log_has_pid_bind "$1"
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
    recorded_start="$(sed -n '2p' "$PIDFILE" 2>/dev/null || true)"
    case "$recorded" in
        ''|*[!0-9]*) recorded="" ;;
    esac
    if [ -n "$recorded" ] && kill -0 "$recorded" 2>/dev/null && \
       recorded_process_is_wattracker "$recorded" "$recorded_start" && \
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
{
    printf '%s\n' "$pid"
    process_start_time "$pid"
} >"$PIDFILE"

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
