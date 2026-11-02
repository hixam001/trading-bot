#!/usr/bin/env bash
# ============================================================================
# Stops what start.sh launched: the live decision cycle and the backend
# (API + dashboard). The live cycle gets the same 150s graceful window as the
# compose `live` service (§87 B): SIGTERM sets the runner's stop event, an
# in-flight order/ledger write finishes, and only a process that misses the
# window is SIGKILLed (reported as an incident). Sweeps are scoped to this
# repo's backend cwd and a Python executable, so a Docker `live` container
# on the same host is never signalled by the local launcher.
# ============================================================================
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT" || exit 1
RUN="$ROOT/.run"

API_PORT="$(grep -E '^API_PORT=' "$ROOT/.env" 2>/dev/null | cut -d= -f2- | tr -d "[:space:]'\"")"
API_PORT="${API_PORT:-${PORT:-8000}}"

STOP_GRACE_SECONDS="${STOP_GRACE_SECONDS:-150}"
API_STOP_GRACE_SECONDS="${API_STOP_GRACE_SECONDS:-30}"
forced=0

pid_is_local_python() {
  local pid="$1" cwd exe
  [ -r "/proc/$pid/cwd" ] || return 1
  cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"
  [ "$cwd" = "$ROOT/backend" ] || return 1
  exe="$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)"
  case "$exe" in
    */python|*/python3|*/python3.*) return 0 ;;
    *) return 1 ;;
  esac
}

wait_for_exit() {
  local pid="$1" grace="$2" ticks=$((grace * 2)) i
  for ((i = 0; i < ticks; i++)); do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.5
  done
  ! kill -0 "$pid" 2>/dev/null
}

stop_pid() {
  local pid="$1" name="$2" grace="$3"
  if ! kill -0 "$pid" 2>/dev/null; then
    return 0
  fi
  if ! pid_is_local_python "$pid"; then
    echo "[stop] $name pid $pid is not this repo's engine — leaving it alone"
    return 0
  fi
  echo "[stop] stopping $name (pid $pid; graceful window ${grace}s)..."
  kill "$pid" 2>/dev/null || true
  if wait_for_exit "$pid" "$grace"; then
    echo "[stop] $name stopped"
    return 0
  fi
  echo "[stop] WARNING: $name did not exit within ${grace}s — SIGKILL; treat as an incident" >&2
  kill -9 "$pid" 2>/dev/null || true
  wait_for_exit "$pid" 5 || true
  return 1
}

stop_pidfile() {
  local file="$1" name="$2" grace="$3" pid
  [ -f "$file" ] || return 0
  pid="$(cat "$file" 2>/dev/null || true)"
  if [ -n "$pid" ]; then
    stop_pid "$pid" "$name" "$grace" || forced=1
  else
    echo "[stop] $name not running (empty pidfile)"
  fi
  rm -f "$file"
}

stop_matching() {
  local pattern="$1" name="$2" grace="$3" pid
  while IFS= read -r pid; do
    [ -n "$pid" ] || continue
    pid_is_local_python "$pid" || continue
    stop_pid "$pid" "$name" "$grace" || forced=1
  done < <(pgrep -f "$pattern" 2>/dev/null || true)
}

# Stop the live cycle first: it owns the money path and gets the full grace
# window. The API can then shut down without racing a live order.
stop_pidfile "$RUN/live_cycle.pid" "live decision cycle" "$STOP_GRACE_SECONDS"
stop_pidfile "$RUN/backend.pid" "backend API" "$API_STOP_GRACE_SECONDS"

# Sweep any stray engine of ours (e.g. after an unclean reboot) with the same
# scoping and grace. `pgrep -f` is broad; pid_is_local_python() rejects other
# repos, editors, and Docker container processes.
stop_matching "run_live_cycle.py" "stray live cycle" "$STOP_GRACE_SECONDS"
stop_matching "uvicorn api.main:app" "stray backend" "$API_STOP_GRACE_SECONDS"

if command -v docker >/dev/null 2>&1 && \
   [ -n "$(docker ps --filter 'name=^trading-bot-live$' \
       --filter 'status=running' --format '{{.ID}}' 2>/dev/null)" ]; then
  echo "[stop] NOTE: the docker compose 'live' container is still running; this script only stops the local launcher."
fi

if [ "$forced" -ne 0 ]; then
  echo "[stop] WARNING: at least one process required SIGKILL — inspect logs/live_cycle.log before resuming." >&2
  exit 1
fi

echo "[stop] done — dashboard at http://localhost:${API_PORT} is now offline."
