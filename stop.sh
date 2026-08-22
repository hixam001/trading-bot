#!/usr/bin/env bash
# ============================================================================
# Stops what start.sh launched: backend (+ tick loop), the frontend build is
# served by the backend so nothing separate to stop there, and ollama ONLY if
# start.sh launched it (a pre-existing ollama is left alone).
# ============================================================================
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN="$ROOT/.run"

stop_pidfile() {
  local file="$1" name="$2"
  if [ -f "$file" ]; then
    local pid
    pid="$(cat "$file" 2>/dev/null)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      echo "[stop] stopping $name (pid $pid)..."
      kill "$pid" 2>/dev/null
      for _ in $(seq 1 10); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.5
      done
      kill -9 "$pid" 2>/dev/null || true
    else
      echo "[stop] $name not running"
    fi
    rm -f "$file"
  fi
}

stop_pidfile "$RUN/backend.pid" "backend + tick loop"

if [ -f "$RUN/ollama_started_by_us" ] && [ "$(cat "$RUN/ollama_started_by_us")" = "1" ]; then
  stop_pidfile "$RUN/ollama.pid" "ollama (started by start.sh)"
  rm -f "$RUN/ollama_started_by_us"
else
  echo "[stop] ollama was already running before start.sh — leaving it up"
fi

# Sweep any stray backend of ours (e.g. after an unclean reboot).
if pgrep -f "uvicorn api.main:app" >/dev/null 2>&1; then
  echo "[stop] killing stray backend process(es)"
  pkill -f "uvicorn api.main:app"
fi

echo "[stop] done — dashboard at http://localhost:8000 is now offline."
