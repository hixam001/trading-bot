#!/usr/bin/env bash
# ============================================================================
# Stops what start.sh launched: the backend (API + dashboard) and the live
# decision cycle. The frontend build is served by the backend so nothing
# separate to stop there.
# ============================================================================
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN="$ROOT/.run"

API_PORT="$(grep -E '^API_PORT=' "$ROOT/.env" 2>/dev/null | cut -d= -f2- | tr -d "[:space:]'\"")"
API_PORT="${API_PORT:-${PORT:-8000}}"

stop_pidfile() {
  local file="$1" name="$2"
  if [ -f "$file" ]; then
    local pid
    pid="$(cat "$file" 2>/dev/null)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      echo "[stop] stopping $name (pid $pid)..."
      kill "$pid" 2>/dev/null
      local stopped=0
      for _ in $(seq 1 10); do
        if ! kill -0 "$pid" 2>/dev/null; then
          stopped=1
          break
        fi
        sleep 0.5
      done
      if [ "$stopped" -ne 1 ]; then
        kill -9 "$pid" 2>/dev/null || true
      fi
    else
      echo "[stop] $name not running"
    fi
    rm -f "$file"
  fi
}

stop_pidfile "$RUN/backend.pid" "backend API"
stop_pidfile "$RUN/live_cycle.pid" "live decision cycle"

# Sweep any stray backend of ours (e.g. after an unclean reboot).
if pgrep -f "uvicorn api.main:app" >/dev/null 2>&1; then
  echo "[stop] killing stray backend process(es)"
  pkill -f "uvicorn api.main:app"
fi

# Sweep any stray live cycle (same reason).
if pgrep -f "run_live_cycle.py" >/dev/null 2>&1; then
  echo "[stop] killing stray live cycle process(es)"
  pkill -f "run_live_cycle.py"
fi

echo "[stop] done — dashboard at http://localhost:${API_PORT} is now offline."
