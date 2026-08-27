#!/usr/bin/env bash
# ============================================================================
# trading-bot one-click launcher.
# Starts (in order): venv setup -> frontend build -> backend + tick loop
# -> dashboard served by the backend on http://localhost:8000 -> the live
# decision cycle (ONLY when live_execution is armed AND a wallet is
# configured in .env), then opens the browser. Idempotent: safe to click
# twice; already-running parts are reused. Use ./stop.sh to stop what this
# script started.
#
# LLM: main provider (Thinker/Narrator/reflections) is selected by
#      MAIN_LLM_PROVIDER in .env: "deepseek" (DeepSeek V4 Flash direct API,
#      the default main model) or "groq" (qwen/qwen3.8-27b, warm rollback).
#      Social reads always use SOCIAL_LLM_*. No local model is started —
#      Ollama is no longer part of the stack.
# ============================================================================
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN="$ROOT/.run"
LOGS="$ROOT/logs"
mkdir -p "$RUN" "$LOGS"

# --- 0) One-time migration: Ollama was retired from the stack (DeepSeek is
#        the main model). Remove stale marker files left by old launches. ---
rm -f "$RUN/ollama.pid" "$RUN/ollama_started_by_us"

up() { curl -s -o /dev/null --max-time 2 "$1"; }

# --- 1) Python venv + deps ---------------------------------------------------
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  echo "[setup] creating venv and installing backend deps..."
  python3 -m venv "$ROOT/.venv"
  "$ROOT/.venv/bin/pip" install -q -r "$ROOT/backend/requirements.txt"
fi

# --- 2) Verify the MAIN LLM provider key is set ------------------------------
MAIN_PROVIDER="$(grep -E '^MAIN_LLM_PROVIDER=' "$ROOT/.env" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')"
MAIN_PROVIDER="${MAIN_PROVIDER:-groq}"
if [ "$MAIN_PROVIDER" = "deepseek" ]; then
  MAIN_KEY="$(grep -E '^DEEPSEEK_API_KEY=' "$ROOT/.env" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')"
else
  MAIN_KEY="$(grep -E '^GROQ_API_KEY=' "$ROOT/.env" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')"
fi
if [ -z "$MAIN_KEY" ]; then
  echo "[WARNING] main LLM provider '$MAIN_PROVIDER' has no API key in .env — Thinker/Narrator will fall back to templates (fail-closed)."
fi

# --- 3) Frontend build (served by the backend on :8000) ----------------------
if [ ! -d "$ROOT/frontend/node_modules" ]; then
  echo "[setup] installing frontend deps..."
  (cd "$ROOT/frontend" && npm install --no-fund --no-audit --silent)
fi
echo "[frontend] building dashboard..."
if ! (cd "$ROOT/frontend" && npm run build --silent >"$LOGS/frontend-build.log" 2>&1); then
  echo "[frontend] WARNING: build failed — see logs/frontend-build.log"
  echo "[frontend] continuing with the last successful build if present."
fi

# --- 4) Backend (+ in-process tick loop) -------------------------------------
if up http://localhost:8000/api/system-status; then
  echo "[backend] already running on :8000 — reusing"
else
  echo "[backend] starting uvicorn + tick loop..."
  (
    cd "$ROOT/backend"
    # setsid detaches uvicorn into its own session: closing the terminal
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

# --- 5) Live execution cycle (ARMED repo) ------------------------------------
# The live runner is a SEPARATE process from the paper tick loop (two books,
# one brain). It starts here only when BOTH are true:
#   a) live_execution/config.py carries LIVE_TRADING_ENABLED = True (the
#      committed armed state — human-edit-only, never env), and
#   b) .env points WALLET_KEYPAIR_PATH at a wallet file that exists.
# Without a funded wallet configured, nothing real-money ever starts.
ARMED="$(grep -E '^LIVE_TRADING_ENABLED' "$ROOT/live_execution/config.py" 2>/dev/null | grep -c 'True')"
KP="$(grep -E '^WALLET_KEYPAIR_PATH=' "$ROOT/.env" 2>/dev/null | cut -d= -f2- | tr -d '[:space:]')"
LIVE_STATE="not started (disarmed or no wallet configured)"
if [ "$ARMED" -ge 1 ] && [ -n "$KP" ] && [ -f "$KP" ]; then
  if pgrep -f "run_live_cycle.py" >/dev/null 2>&1; then
    echo "[live] live cycle already running — reusing"
    LIVE_STATE="running (reused)"
  else
    echo "[live] ARMED + wallet configured — starting live decision cycle..."
    (
      cd "$ROOT"
      # setsid: same detachment contract as the backend — a closed terminal
      # tab or Ctrl+C here can never take the live cycle down with it.
      nohup setsid "$ROOT/.venv/bin/python" run_live_cycle.py \
        >"$LOGS/live_cycle.log" 2>&1 </dev/null &
      echo $! >"$RUN/live_cycle.pid"
    )
    LIVE_STATE="RUNNING — REAL MONEY (logs/live_cycle.log)"
  fi
fi

echo ""
echo "==========================================================="
if [ "$ARMED" -ge 1 ] && [ -n "$KP" ] && [ -f "$KP" ]; then
  echo " trading-bot is live   (LIVE TRADING ARMED — REAL FUNDS)"
else
  echo " trading-bot is live   (PAPER TRADING — NO REAL FUNDS)"
fi
echo " dashboard : http://localhost:8000"
echo " live cycle: $LIVE_STATE"
echo " LLM main  : $MAIN_PROVIDER (social: SOCIAL_LLM_*)"
echo " logs      : $LOGS/"
echo " stop      : $ROOT/stop.sh"
echo "==========================================================="
echo ""

xdg-open http://localhost:8000 >/dev/null 2>&1 || true
