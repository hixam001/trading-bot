#!/usr/bin/env bash
# ============================================================================
# trading-bot — container entrypoint (single deployable module).
#
# Always starts: the FastAPI engine (API + WebSocket + dashboard).
# Conditionally starts: the live decision cycle — ONLY when the hardcoded ARM
# flag in backend/live_execution/config.py is True AND a wallet secret is
# configured (existing file path, or the env JSON channel). This mirrors
# start.sh's gating so "no wallet → no real money" holds in a container too.
#
# Secrets arrive at runtime ONLY: --env-file / platform env / mounted files.
# The image itself contains none.
# ============================================================================
set -u

# Overridable so the script is testable outside a container; the image sets
# the real location. A missing app dir MUST abort: continuing would start
# uvicorn from the wrong working directory (wrong config, wrong state paths).
APP_DIR="${APP_DIR:-/app/backend}"
if ! cd "$APP_DIR"; then
  echo "[entrypoint] FATAL: app dir '$APP_DIR' not found — refusing to start" >&2
  exit 1
fi

# Audit A8: anchor regex on actual assignment value, ignoring comments like '# was True'.
ARMED="$(grep -cE '^LIVE_TRADING_ENABLED\b[^=]*=[[:space:]]*True\b' live_execution/config.py 2>/dev/null || true)"

WALLET_OK=0
if [ -n "${WALLET_KEYPAIR_JSON:-}" ]; then
  WALLET_OK=1
elif [ -n "${WALLET_KEYPAIR_PATH:-}" ] && [ -f "${WALLET_KEYPAIR_PATH}" ]; then
  WALLET_OK=1
fi

LIVE_PID=""
API_PID=""
if [ "${ARMED:-0}" -ge 1 ] && [ "$WALLET_OK" -eq 1 ]; then
  echo "[entrypoint] ARMED + wallet configured — starting live decision cycle"
  python run_live_cycle.py &
  LIVE_PID=$!
else
  echo "[entrypoint] live cycle NOT started (disarmed or no wallet configured)"
fi

cleanup() {
  trap - TERM INT
  [ -n "$LIVE_PID" ] && kill "$LIVE_PID" 2>/dev/null
  [ -n "$API_PID" ] && kill "$API_PID" 2>/dev/null
  wait 2>/dev/null
  exit 0
}
trap cleanup TERM INT

# Port resolution: honours $PORT (platforms) and $API_PORT (.env default 8000)
PORT="$(echo "${PORT:-${API_PORT:-8000}}" | tr -d "[:space:]'\"")"
FORWARDED_ALLOW_IPS="${FORWARDED_ALLOW_IPS:-127.0.0.1,172.16.0.0/12}"
uvicorn api.main:app --host 0.0.0.0 --port "$PORT" --proxy-headers --forwarded-allow-ips="$FORWARDED_ALLOW_IPS" &
API_PID=$!

# Supervise BOTH processes. `wait -n` returns as soon as either exits, so a
# crashed live cycle can never hide behind a healthy dashboard: the container
# exits non-zero and the platform's restart policy brings the whole engine
# back. Waiting only on the API would leave an armed deployment silently not
# trading while every health check stayed green.
if [ -n "$LIVE_PID" ]; then
  wait -n "$API_PID" "$LIVE_PID"
  status=$?
  if kill -0 "$LIVE_PID" 2>/dev/null; then
    echo "[entrypoint] FATAL: API exited (status $status) — stopping live cycle" >&2
  else
    echo "[entrypoint] FATAL: live cycle exited (status $status) — stopping API" >&2
  fi
  kill "$API_PID" "$LIVE_PID" 2>/dev/null
  wait 2>/dev/null
  exit "${status:-1}"
fi

wait "$API_PID"