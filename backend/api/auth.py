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

