#!/usr/bin/env bash
#
# restart.sh — safely stop and (re)start the wattracker app.
#
# Usage:
#   scripts/restart.sh            # restart (stop if running, then start)
#   scripts/restart.sh stop       # graceful stop only
#   scripts/restart.sh start      # start only (no-op if already up)
#   scripts/restart.sh status     # report whether the app is running
#
# Safe shutdown: sends SIGTERM first so uvicorn/FastAPI can finish in-flight
# requests and close the SQLite connection cleanly, waits, and only escalates
# to SIGKILL if the process refuses to exit. Startup is verified with a health
# check before the script reports success.

set -euo pipefail

# --- config ---------------------------------------------------------------
PORT="${WATTRACKER_PORT:-8000}"
HOST="${WATTRACKER_HOST:-localhost}"
TERM_TIMEOUT="${WATTRACKER_TERM_TIMEOUT:-10}"   # seconds to wait for graceful exit
START_TIMEOUT="${WATTRACKER_START_TIMEOUT:-20}" # seconds to wait for health check

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"
PID_FILE="$ROOT/.wattracker.pid"
LOG_FILE="$ROOT/wattracker.log"
HEALTH_URL="http://$HOST:$PORT/login"

log() { printf '%s %s\n' "$(date '+%H:%M:%S')" "$*"; }

# --- identify a candidate PID as our server -------------------------------
# The server is always launched as `"$PYTHON" -m wattracker`, so identity is
# the module invocation run by a python interpreter — never the bare string
# "wattracker", which appears in unrelated command lines all the time (a CI
# checkout under .../_work/wattracker/wattracker/, this script's own pipeline,
# the connector's `-m wattracker_connector`). Matching that string got those
# bystanders SIGTERMed and then SIGKILLed.
#
# Do not tighten this to the literal "$PYTHON -m wattracker" either: framework
# Python builds (Homebrew python@3.x, the python.org installers) re-exec
# through Python.framework/.../Python.app/Contents/MacOS/Python and rewrite
# argv[0], so `ps -o command=` shows that path and the venv interpreter is
# nowhere in the command line. Constrain the interpreter by basename instead,
# exactly as start.sh's process_command_is_wattracker does.
process_is_wattracker_server() {
  local pid="$1" command_line interpreter
  case "$pid" in ''|*[!0-9]*) return 1 ;; esac
  # never our own shell, its pipeline, or whatever invoked us
  if [ "$pid" = "$$" ] || [ "$pid" = "${PPID:-}" ]; then return 1; fi
  command_line="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  # The module name must end there: `-m wattracker_connector` is a different
  # process and must not be killed by the app's restart script.
  case "$command_line" in
    *" -m wattracker") ;;
    *" -m wattracker "*) ;;
    *) return 1 ;;
  esac
  interpreter="${command_line%% -m wattracker*}"
  case "${interpreter##*/}" in
    [Pp]ython|[Pp]ython[0-9]*) return 0 ;;
    *) return 1 ;;
  esac
}

# --- discover running server PIDs -----------------------------------------
# Match both the recorded PID file and anything listening on the port /
# running the module, so a stale or externally-started server is still caught.
server_pids() {
  local pids=""
  if [ -f "$PID_FILE" ]; then
    local p; p="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then pids="$pids $p"; fi
  fi
  # port listeners (macOS/Linux lsof)
  if command -v lsof >/dev/null 2>&1; then
    pids="$pids $(lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
  fi
  # module matches (fallback): catches a stale or externally-started real
  # server that is neither recorded in the PID file nor holding the port.
  # pgrep -f is a substring match over the whole command line, so it only
  # narrows the field — every candidate is confirmed against ps below. The
  # bracket keeps the pattern from matching this pipeline's own pgrep.
  local candidate
  for candidate in $(pgrep -f '[-]m wattracker' 2>/dev/null || true); do
    if process_is_wattracker_server "$candidate"; then
      pids="$pids $candidate"
    fi
  done
  # dedupe
  printf '%s\n' $pids | sort -u | tr '\n' ' '
}

is_running() { [ -n "$(server_pids | tr -d ' ')" ]; }

# --- stop -----------------------------------------------------------------
stop() {
  local pids; pids="$(server_pids)"
  if [ -z "$(printf '%s' "$pids" | tr -d ' ')" ]; then
    log "not running (nothing to stop)"
    rm -f "$PID_FILE"
    return 0
  fi
  log "stopping PID(s):$pids"
  # graceful SIGTERM
  for pid in $pids; do kill -TERM "$pid" 2>/dev/null || true; done
  # wait for graceful exit
  local waited=0
  while [ "$waited" -lt "$TERM_TIMEOUT" ]; do
    is_running || { log "stopped cleanly"; rm -f "$PID_FILE"; return 0; }
    sleep 1; waited=$((waited + 1))
  done
  # escalate
  pids="$(server_pids)"
  log "did not exit in ${TERM_TIMEOUT}s; sending SIGKILL to:$pids"
  for pid in $pids; do kill -KILL "$pid" 2>/dev/null || true; done
  sleep 1
  if is_running; then
    log "ERROR: processes still alive after SIGKILL:$(server_pids)"
    return 1
  fi
  log "force-stopped"
  rm -f "$PID_FILE"
}

# --- start ----------------------------------------------------------------
start() {
  if is_running; then
    log "already running (PID(s):$(server_pids)) — leaving as is"
    return 0
  fi
  log "starting: $PYTHON -m wattracker  (logs -> $LOG_FILE)"
  cd "$ROOT"
  # detach so it survives this shell; disable auto-open browser on restart
  WATTRACKER_OPEN_BROWSER=0 nohup "$PYTHON" -m wattracker >>"$LOG_FILE" 2>&1 &
  local pid=$!
  echo "$pid" >"$PID_FILE"
  # health check
  local waited=0
  while [ "$waited" -lt "$START_TIMEOUT" ]; do
    if curl -sf -o /dev/null --max-time 2 "$HEALTH_URL" 2>/dev/null; then
      log "up at http://$HOST:$PORT (PID $pid)"
      return 0
    fi
    # bail early if the process died on startup
    if ! kill -0 "$pid" 2>/dev/null; then
      log "ERROR: process exited during startup — see $LOG_FILE"
      tail -n 20 "$LOG_FILE" || true
      rm -f "$PID_FILE"
      return 1
    fi
    sleep 1; waited=$((waited + 1))
  done
  log "ERROR: health check failed after ${START_TIMEOUT}s — see $LOG_FILE"
  return 1
}

status() {
  if is_running; then
    log "running (PID(s):$(server_pids)) at http://$HOST:$PORT"
  else
    log "not running"
  fi
}

case "${1:-restart}" in
  stop)    stop ;;
  start)   start ;;
  status)  status ;;
  restart) stop; start ;;
  *) echo "usage: $0 {restart|stop|start|status}" >&2; exit 2 ;;
esac
