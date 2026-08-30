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

APP_DIR="/app/backend"
cd "$APP_DIR"

ARMED="$(grep -E '^LIVE_TRADING_ENABLED' live_execution/config.py 2>/dev/null | grep -c 'True' || true)"

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
  [ -n "$LIVE_PID" ] && kill "$LIVE_PID" 2>/dev/null
  [ -n "$API_PID" ] && kill "$API_PID" 2>/dev/null
  wait 2>/dev/null
  exit 0
}
trap cleanup TERM INT

uvicorn api.main:app --host 0.0.0.0 --port "${PORT:-8000}" &
API_PID=$!
wait "$API_PID"