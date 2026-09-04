"""
api/auth.py — operator-token guard for mutating endpoints (§38, finding F3).

The two mutating operator endpoints (POST /api/admin/reset, POST
/api/knowledge-base/ingest) require the X-Admin-Token header to match
config.ADMIN_TOKEN.

FAIL CLOSED by design:
  - token unset/empty in config  -> every request is refused (403). A
    destructive endpoint must never be open without a credential, even on
    loopback.
  - header missing or wrong      -> refused (403).
Comparison uses hmac.compare_digest (constant-time) so a wrong token leaks no
timing information. The token itself lives only in .env (never logged, never
echoed in error bodies).
"""
from __future__ import annotations

import hmac
import time
from collections import defaultdict

from fastapi import HTTPException, Request

import config

ADMIN_TOKEN_HEADER = "X-Admin-Token"

# Brute-force mitigation: track recent failed attempts per client host
_FAILED_ATTEMPTS: dict[str, list[float]] = defaultdict(list)
_MAX_FAILED_ATTEMPTS = 5
_LOCKOUT_WINDOW_SECONDS = 60.0


def require_admin_token(request: Request) -> None:
    """Raise 403 unless the request carries the configured operator token.

    Enforces rate limiting on repeated failed attempts (brute-force defense).
    """
    client_ip = getattr(getattr(request, "client", None), "host", "unknown")
    now = time.time()

    # Prune expired attempts outside the sliding window
    attempts = [t for t in _FAILED_ATTEMPTS[client_ip] if now - t < _LOCKOUT_WINDOW_SECONDS]
    _FAILED_ATTEMPTS[client_ip] = attempts

    if len(attempts) >= _MAX_FAILED_ATTEMPTS:
        raise HTTPException(
            status_code=429,
            detail="too many failed authentication attempts — rate limited",
        )

    configured = config.ADMIN_TOKEN
    if not configured:
        # Fail closed: no token configured -> endpoint disabled.
        raise HTTPException(
            status_code=403,
            detail="operator endpoints are disabled (ADMIN_TOKEN not set)",
        )
    supplied = request.headers.get(ADMIN_TOKEN_HEADER, "")
    if not supplied or not hmac.compare_digest(supplied, configured):
        _FAILED_ATTEMPTS[client_ip].append(now)
        raise HTTPException(status_code=403, detail="invalid operator token")

    # Clear recorded failures on successful authentication
    _FAILED_ATTEMPTS.pop(client_ip, None)


# ---------------------------------------------------------------------------
# §55 authz audit — proxy-aware loopback trust.
#
# "Local" must mean DIRECTLY local. The deploy guide (docs/12 Step 7) fronts
# the engine with `caddy reverse_proxy 127.0.0.1:8000`, and uvicorn runs
# without --proxy-headers — so EVERY internet visitor arrives at FastAPI
# with request.client.host == 127.0.0.1. Trusting that address would
# authenticate the whole internet as the operator on every loopback-gated
# surface. Any forwarding header proves the socket peer is a PROXY, not the
# operator's browser: the loopback shortcut is disabled and the operator
# token is required instead.
# ---------------------------------------------------------------------------
_FORWARDING_HEADERS = (
    "x-forwarded-for",
    "x-forwarded-proto",
    "x-forwarded-host",
    "forwarded",
)


def require_local_or_admin(request: Request) -> None:
    """Allow DIRECT loopback connections, or a valid operator token.

    - Direct loopback (SSH tunnel, local dashboard on :8000, test clients)
      with no forwarding headers -> allowed.
    - Any request carrying a forwarding header is treated as proxied: the
      loopback shortcut does NOT apply and require_admin_token governs
      (fail-closed when unset, constant-time compare, brute-force lockout).
    """
    client_ip = getattr(getattr(request, "client", None), "host", "unknown")
    proxied = any(
        request.headers.get(h) is not None for h in _FORWARDING_HEADERS)
    if not proxied and client_ip in (
            "127.0.0.1", "::1", "localhost", "testclient"):
        return
    require_admin_token(request)

