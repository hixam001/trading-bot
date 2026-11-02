#!/usr/bin/env bash
# ============================================================================
# trading-bot one-click launcher.
# Starts (in order): venv setup -> frontend build -> backend API
# -> dashboard served by the backend on http://localhost:8000 (or $API_PORT) -> the live
# decision cycle (ONLY when live_execution is armed AND a wallet is
# configured in .env), then opens the browser. Idempotent: safe to click
# twice; already-running parts are reused. Use ./stop.sh to stop what this
# script started; it gives the live cycle up to 150s to finish an in-flight
# order before escalating to SIGKILL (§87 B).
#
# LLM: main provider (Thinker/Narrator/reflections) is selected by
#      MAIN_LLM_PROVIDER in .env: "deepseek" (DeepSeek V4 Flash direct API,
#      the default main model) or "groq" (qwen/qwen3.8-27b, warm rollback).
#      Social reads always use SOCIAL_LLM_*. No local model is started —
#      Ollama is no longer part of the stack.
# ============================================================================
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT" || exit 1
RUN="$ROOT/.run"
LOGS="$ROOT/logs"
mkdir -p "$RUN" "$LOGS"
chmod 750 "$LOGS" 2>/dev/null || true

API_PORT="$(grep -E '^API_PORT=' "$ROOT/.env" 2>/dev/null | cut -d= -f2- | tr -d "[:space:]'\"")"
API_PORT="${API_PORT:-${PORT:-8000}}"

up() { curl -s -o /dev/null --max-time 2 "$1"; }

# --- 0) Live-execution gate (computed once) ---------------------------------
# The live runner starts only when BOTH are true:
#   a) live_execution/config.py carries LIVE_TRADING_ENABLED = True (the
#      committed armed state — human-edit-only, never env), and
#   b) .env points WALLET_KEYPAIR_PATH at a wallet file that exists (or
#      WALLET_KEYPAIR_JSON is set).
# Without a funded wallet configured, nothing real-money ever starts.
# A8 (repo audit): parse the FLAG'S VALUE, not "True anywhere in the line".
# The old `grep -c 'True'` counted `LIVE_TRADING_ENABLED = False # was True`
# as ARMED — starting the live-cycle process and printing "REAL FUNDS" while
# the code flag was off. This regex anchors on the actual assignment value
# ([^=]* tolerates the ": bool" type annotation; the FIRST "=" is the
# assignment, and True must be the value on its right).
ARMED="$(grep -cE '^LIVE_TRADING_ENABLED\b[^=]*=[[:space:]]*True\b' "$ROOT/backend/live_execution/config.py" 2>/dev/null || true)"
case "$ARMED" in
  ''|*[!0-9]*) ARMED=0 ;;
esac
KP="$(grep -E '^WALLET_KEYPAIR_PATH=' "$ROOT/.env" 2>/dev/null | cut -d= -f2- | tr -d "[:space:]'\"")"
KPJ="$(grep -E '^WALLET_KEYPAIR_JSON=' "$ROOT/.env" 2>/dev/null | cut -d= -f2- | tr -d "[:space:]'\"")"
# The live runner's cwd is $ROOT/backend, so a relative WALLET_KEYPAIR_PATH in
# .env resolves there. Resolve it the same way here; otherwise the gate could
# pass while the child fails to find the file (or vice versa).
if [ -n "$KP" ] && [ "${KP#/}" = "$KP" ]; then
  KP="$ROOT/backend/$KP"
fi
# Wallet configured = secret file exists (preferred channel) OR the env JSON
# channel is set (deployment parity with file-less hosts).
WALLET_OK=0
if { [ -n "$KP" ] && [ -f "$KP" ]; } || [ -n "$KPJ" ]; then
  WALLET_OK=1
fi

# Only this repo's engine counts: cwd $ROOT/backend AND a Python executable.
# A Docker `live` container on the same host has cwd /app/backend, and an
# editor with run_live_cycle.py open is not a Python process.
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

local_live_running() {
  local pid
  while IFS= read -r pid; do
    [ -n "$pid" ] || continue
    pid_is_local_python "$pid" && return 0
  done < <(pgrep -f "run_live_cycle.py" 2>/dev/null || true)
  return 1
}

docker_live_running() {
  command -v docker >/dev/null 2>&1 || return 1
  [ -n "$(docker ps --filter 'name=^trading-bot-live$' \
      --filter 'status=running' --format '{{.ID}}' 2>/dev/null)" ]
}

# §88/§13: one armed engine per wallet. If the compose `live` service is
# already running on this host, starting the local launcher too would create a
# second wallet signer. Refuse before starting anything.
if [ "$ARMED" -ge 1 ] && [ "$WALLET_OK" -eq 1 ] && docker_live_running; then
  echo "[live] FATAL: the docker compose 'live' service is running on this host."
  echo "[live] Refusing to start a second wallet signer. Stop it first:"
  echo "[live]   docker compose stop live"
  exit 1
fi

# --- 1) Python venv + deps ---------------------------------------------------
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  echo "[setup] creating venv and installing backend deps..."
  python3 -m venv "$ROOT/.venv"
  "$ROOT/.venv/bin/pip" install -q -r "$ROOT/backend/requirements.txt"
fi

# --- 2) Verify the MAIN LLM provider key is set ------------------------------
MAIN_PROVIDER="$(grep -E '^MAIN_LLM_PROVIDER=' "$ROOT/.env" 2>/dev/null | cut -d= -f2- | tr -d "[:space:]'\"")"
MAIN_PROVIDER="${MAIN_PROVIDER:-deepseek}"
if [ "$MAIN_PROVIDER" = "deepseek" ]; then
  MAIN_KEY="$(grep -E '^DEEPSEEK_API_KEY=' "$ROOT/.env" 2>/dev/null | cut -d= -f2- | tr -d "[:space:]'\"")"
else
  MAIN_KEY="$(grep -E '^GROQ_API_KEY=' "$ROOT/.env" 2>/dev/null | cut -d= -f2- | tr -d "[:space:]'\"")"
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

# --- 4) Backend (API + WebSocket + dashboard) --------------------------------
# In the §52 single-book architecture, the backend serves the API, the
# WebSocket feed, and the static dashboard. The live decision cycle runs
# separately in step 5 (guarded by arming flags and wallet configuration).
if up "http://127.0.0.1:${API_PORT}/api/system-status"; then
  echo "[backend] already running on :${API_PORT} — reusing"
else
  echo "[backend] starting uvicorn (API + WebSocket + dashboard)..."
  (
    cd "$ROOT/backend"
    # setsid detaches uvicorn into its own session: closing the terminal
    # tab or Ctrl+C-ing the script can never take the backend down with it
    # (nohup alone only blocks SIGHUP — SIGINT still killed it).
    nohup setsid \
      "$ROOT/.venv/bin/python" -m uvicorn \
      api.main:app --host 127.0.0.1 --port "$API_PORT" \
      --proxy-headers --forwarded-allow-ips=127.0.0.1 \
      >"$LOGS/backend.log" 2>&1 </dev/null &
    echo $! >"$RUN/backend.pid"
  )
  backend_pid="$(cat "$RUN/backend.pid" 2>/dev/null || true)"
  ok=0
  for _ in $(seq 1 60); do
    if up "http://127.0.0.1:${API_PORT}/api/system-status"; then ok=1; break; fi
    if [ -n "$backend_pid" ] && ! kill -0 "$backend_pid" 2>/dev/null; then
      break
    fi
    sleep 1
  done
  if [ "$ok" -ne 1 ]; then
    echo "[backend] FAILED to start — last log lines:"
    tail -20 "$LOGS/backend.log"
    exit 1
  fi
fi

# --- 5) Live execution cycle (ARMED repo) ------------------------------------
# The live runner drives the autonomous decision cycle (single-book live
# execution). ARMED/KP/WALLET_OK were computed once in step 0; a relative
# wallet path is already resolved and is passed explicitly to the child.
LIVE_STATE="not started (disarmed or no wallet configured)"
if [ "$ARMED" -ge 1 ] && [ "$WALLET_OK" -eq 1 ]; then
  if local_live_running; then
    echo "[live] live cycle already running — reusing"
    LIVE_STATE="running (reused)"
  else
    echo "[live] ARMED + wallet configured — starting live decision cycle..."
    (
      cd "$ROOT/backend"
      # setsid: same detachment contract as the backend — a closed terminal
      # tab or Ctrl+C here can never take the live cycle down with it.
      if [ -n "$KP" ]; then
        WALLET_KEYPAIR_PATH="$KP" nohup setsid "$ROOT/.venv/bin/python" run_live_cycle.py \
          >"$LOGS/live_cycle.log" 2>&1 </dev/null &
      else
        nohup setsid "$ROOT/.venv/bin/python" run_live_cycle.py \
          >"$LOGS/live_cycle.log" 2>&1 </dev/null &
      fi
      echo $! >"$RUN/live_cycle.pid"
    )
    live_pid="$(cat "$RUN/live_cycle.pid" 2>/dev/null || true)"
    sleep 2
    if [ -n "$live_pid" ] && ! kill -0 "$live_pid" 2>/dev/null; then
      echo "[live] FAILED to start — last log lines:"
      tail -20 "$LOGS/live_cycle.log"
      rm -f "$RUN/live_cycle.pid"
      exit 1
    fi
    LIVE_STATE="RUNNING — REAL MONEY (logs/live_cycle.log)"
  fi
fi

echo ""
echo "==========================================================="
if [ "$ARMED" -ge 1 ] && [ "$WALLET_OK" -eq 1 ]; then
  echo " trading-bot is live   (LIVE TRADING ARMED — REAL FUNDS)"
else
  echo " trading-bot is live   (DISARMED — NO LIVE TRADING)"
fi
echo " dashboard : http://localhost:${API_PORT}"
echo " live cycle: $LIVE_STATE"
echo " LLM main  : $MAIN_PROVIDER (social: SOCIAL_LLM_*)"
echo " logs      : $LOGS/"
echo " stop      : $ROOT/stop.sh"
echo "==========================================================="
echo ""

xdg-open "http://localhost:${API_PORT}" >/dev/null 2>&1 || true
