#!/usr/bin/env bash
#
# restart.sh — safely stop and (re)start the TRanalyzer app.
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
PORT="${TRANALYZER_PORT:-8000}"
HOST="${TRANALYZER_HOST:-localhost}"
TERM_TIMEOUT="${TRANALYZER_TERM_TIMEOUT:-10}"   # seconds to wait for graceful exit
START_TIMEOUT="${TRANALYZER_START_TIMEOUT:-20}" # seconds to wait for health check

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"
PID_FILE="$ROOT/.tranalyzer.pid"
LOG_FILE="$ROOT/tranalyzer.log"
HEALTH_URL="http://$HOST:$PORT/login"

log() { printf '%s %s\n' "$(date '+%H:%M:%S')" "$*"; }

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
  # module matches (fallback)
  pids="$pids $(pgrep -f 'tranalyzer' 2>/dev/null || true)"
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
  log "starting: $PYTHON -m tranalyzer  (logs -> $LOG_FILE)"
  cd "$ROOT"
  # detach so it survives this shell; disable auto-open browser on restart
  BROWSER=true nohup "$PYTHON" -m tranalyzer >>"$LOG_FILE" 2>&1 &
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
