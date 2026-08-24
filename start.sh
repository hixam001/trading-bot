#!/usr/bin/env bash
# ============================================================================
# trading-bot one-click launcher.
# Starts (in order): ollama serve (if not already up) -> backend + tick loop
# -> built frontend served by the backend on http://localhost:8000, then opens
# the browser. Idempotent: safe to click twice; already-running parts are
# reused. Use ./stop.sh to stop what this script started.
# ============================================================================
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN="$ROOT/.run"
LOGS="$ROOT/logs"
mkdir -p "$RUN" "$LOGS"

up() { curl -s -o /dev/null --max-time 2 "$1"; }

# --- 1) Python venv + deps ---------------------------------------------------
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  echo "[setup] creating venv and installing backend deps..."
  python3 -m venv "$ROOT/.venv"
  "$ROOT/.venv/bin/pip" install -q -r "$ROOT/backend/requirements.txt"
fi

# --- 2) Frontend build (served by the backend on :8000) ----------------------
if [ ! -d "$ROOT/frontend/node_modules" ]; then
  echo "[setup] installing frontend deps..."
  (cd "$ROOT/frontend" && npm install --no-fund --no-audit --silent)
fi
echo "[frontend] building dashboard..."
if ! (cd "$ROOT/frontend" && npm run build --silent >"$LOGS/frontend-build.log" 2>&1); then
  echo "[frontend] WARNING: build failed — see logs/frontend-build.log"
  echo "[frontend] continuing with the last successful build if present."
fi

# --- 3) Ollama (only started if not already running) -------------------------
MODEL_NAME="$(grep -E '^MODEL_NAME=' "$ROOT/.env" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')"
MODEL_NAME="${MODEL_NAME:-qwen3:8b}"

OLLAMA_STARTED_BY_US=0
if up http://localhost:11434/api/tags; then
  echo "[ollama] already running — reusing"
else
  if command -v ollama >/dev/null 2>&1; then
    echo "[ollama] starting ollama serve..."
    nohup setsid ollama serve >"$LOGS/ollama.log" 2>&1 </dev/null &
    echo $! >"$RUN/ollama.pid"
    OLLAMA_STARTED_BY_US=1
    for _ in $(seq 1 30); do
      up http://localhost:11434/api/tags && break
      sleep 1
    done
    up http://localhost:11434/api/tags \
      && echo "[ollama] up" \
      || echo "[ollama] WARNING: did not come up in 30s — narration falls back to templates"
  else
    echo "[ollama] WARNING: ollama not installed — narration falls back to templates"
  fi
fi
echo "$OLLAMA_STARTED_BY_US" >"$RUN/ollama_started_by_us"

if command -v ollama >/dev/null 2>&1; then
  # timeout guard: a wedged `ollama list` must never block the backend launch
  if ! timeout 10 ollama list 2>/dev/null | grep -q "$MODEL_NAME"; then
    echo "[ollama] NOTE: model '$MODEL_NAME' not found locally."
    echo "[ollama]       Run 'ollama pull $MODEL_NAME' for LLM narration;"
    echo "[ollama]       until then deterministic template narration is used."
  fi
fi

# --- 4) Backend (+ in-process tick loop) -------------------------------------
if up http://localhost:8000/api/system-status; then
  echo "[backend] already running on :8000 — reusing"
else
  echo "[backend] starting uvicorn + tick loop..."
  (
    cd "$ROOT/backend"
    # setsid detaches uvicorn into its own session: closing the Konsole
    # tab or Ctrl+C-ing the script can never take the backend down with it
    # (nohup alone only blocks SIGHUP — SIGINT still killed it).
    TICK_LOOP_IN_PROCESS=1 nohup setsid \
      "$ROOT/.venv/bin/python" -m uvicorn \
      api.main:app --host 127.0.0.1 --port 8000 \
      >"$LOGS/backend.log" 2>&1 </dev/null &
    echo $! >"$RUN/backend.pid"
  )
  ok=0
  for _ in $(seq 1 60); do
    if up http://localhost:8000/api/system-status; then ok=1; break; fi
    sleep 1
  done
  if [ "$ok" -ne 1 ]; then
    echo "[backend] FAILED to start — last log lines:"
    tail -20 "$LOGS/backend.log"
    exit 1
  fi
fi

echo ""
echo "==========================================================="
echo " trading-bot is live   (PAPER TRADING — NO REAL FUNDS)"
echo " dashboard : http://localhost:8000"
echo " logs      : $LOGS/"
echo " stop      : $ROOT/stop.sh"
echo "==========================================================="
echo ""

xdg-open http://localhost:8000 >/dev/null 2>&1 || true
