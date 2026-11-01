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

# §88 B: an explicit command (compose `command:` or `docker compose run …
# <cmd>`) is honoured BEFORE any role logic. Without this, `compose run live
# python …` silently started a SECOND live engine — a second wallet signer —
# instead of running the operator script. Relative paths resolve from
# /app/backend; docs use absolute /app/scripts/… paths.
if [ "$#" -gt 0 ]; then
  exec "$@"
fi

# Audit A8: anchor regex on actual assignment value, ignoring comments like '# was True'.
ARMED="$(grep -cE '^LIVE_TRADING_ENABLED\b[^=]*=[[:space:]]*True\b' live_execution/config.py 2>/dev/null || true)"

# §87 A2: a bind-mounted keypair keeps the HOST file's uid/mode. The image
# runs as uid 10001, so a root/ubuntu-owned chmod-600 file is readable on the
# host but NOT in the container. Test readability here, before claiming
# "ARMED + wallet configured" — a directory at the mount source (Docker's
# fallback when a bind source is missing) and an unreadable file both fail
# with the same config-error exit (78, sysexits EX_CONFIG). An UNSET path or
# JSON channel keeps the original clean "not started" behaviour.
wallet_path_invalid() {
  [ -n "${WALLET_KEYPAIR_PATH:-}" ] \
    && { [ ! -f "${WALLET_KEYPAIR_PATH}" ] || [ ! -r "${WALLET_KEYPAIR_PATH}" ]; }
}

fatal_wallet_path() {
  echo "[entrypoint] FATAL: wallet keypair not readable by uid $(id -u): ${WALLET_KEYPAIR_PATH}" >&2
  ls -ld "${WALLET_KEYPAIR_PATH}" >&2 2>/dev/null || echo "[entrypoint] (path does not exist)" >&2
  echo "[entrypoint] Fix: sudo chown 10001:10001 <host-keypair> && sudo chmod 600 <host-keypair>" >&2
  exit 78
}

WALLET_OK=0
if [ -n "${WALLET_KEYPAIR_JSON:-}" ]; then
  WALLET_OK=1
elif [ -n "${WALLET_KEYPAIR_PATH:-}" ] && [ -f "${WALLET_KEYPAIR_PATH}" ] && [ -r "${WALLET_KEYPAIR_PATH}" ]; then
  WALLET_OK=1
fi

# Port resolution: honours $PORT (platforms) and $API_PORT (.env default 8000)
PORT="$(echo "${PORT:-${API_PORT:-8000}}" | tr -d "[:space:]'\"")"
PORT="${PORT:-8000}"
FORWARDED_ALLOW_IPS="${FORWARDED_ALLOW_IPS:-127.0.0.1,172.16.0.0/12,::1/128}"

ROLE="${ROLE:-}"

if [ "$ROLE" = "api" ]; then
  echo "[entrypoint] starting in API-only role (web process has no signing key access)"
  exec uvicorn api.main:app --host 0.0.0.0 --port "$PORT" --proxy-headers --forwarded-allow-ips="$FORWARDED_ALLOW_IPS"
elif [ "$ROLE" = "live" ]; then
  echo "[entrypoint] starting in LIVE-only role"
  if wallet_path_invalid; then
    fatal_wallet_path
  fi
  if [ "${ARMED:-0}" -ge 1 ] && [ "$WALLET_OK" -eq 1 ]; then
    echo "[entrypoint] ARMED + wallet configured — starting live decision cycle"
    exec python run_live_cycle.py
  else
    # §87 A3: `restart: unless-stopped` also restarts after exit 0, so a
    # disarmed/no-wallet live container must NOT exit (else it flaps forever).
    # Stay up; the absent heartbeat makes the healthcheck fail, so
    # `docker compose ps` shows an unhealthy container for the operator.
    echo "[entrypoint] live cycle NOT started (disarmed or no wallet configured) — staying up without a heartbeat (healthcheck will fail)" >&2
    exec tail -f /dev/null
  fi
fi

# Fallback: single-container behaviour when ROLE is unset (with explicit security warning)
echo "[entrypoint] WARNING: ROLE unset — running unified api + live in single container (wallet key shares process space with web server)"

if wallet_path_invalid; then
  fatal_wallet_path
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