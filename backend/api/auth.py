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

import hashlib
import hmac
import secrets
import time
from collections import OrderedDict, defaultdict

from fastapi import HTTPException, Request, Response

import config
from api.rate_limiter import get_client_ip

ADMIN_TOKEN_HEADER = "X-Admin-Token"

# Brute-force mitigation: track recent failed attempts per client host
_FAILED_ATTEMPTS: dict[str, list[float]] = defaultdict(list)
_MAX_FAILED_ATTEMPTS = 5
_LOCKOUT_WINDOW_SECONDS = 60.0
# A6 (repo audit): cap the tracked hosts — a proxied flood of DISTINCT IPs
# (every request after the §55 proxy rule routes here) would otherwise grow
# the map without bound. Old single-host pruning only ran when THAT host
# called again, so abandoned attacker hosts were never cleaned. Beyond the
# cap the whole map is pruned once (expired entries of every host go).
_MAX_TRACKED_HOSTS = 10_000


def _derive_session_key(secret: str) -> bytes:
    """Derive a dedicated HMAC session-signing key from the admin token."""
    return hashlib.sha256((secret + ":session_auth_key_v1").encode("utf-8")).digest()


_ACTIVE_SIGNING_KEYS: set[str] = set()


def create_session_token(secret: str | None = None) -> str:
    """Generate a cryptographically signed, timestamped session token.

    Format: <timestamp>.<nonce>.<hmac_signature>
    Tamper-evident, stateless, and verifiable server-side without database queries.
    """
    configured = secret if secret is not None else config.ADMIN_TOKEN
    if not configured or len(configured) < 32:
        raise ValueError("Cannot create session token: ADMIN_TOKEN is not configured or shorter than 32 characters")
    _ACTIVE_SIGNING_KEYS.add(configured)
    if len(_ACTIVE_SIGNING_KEYS) > 100:
        _ACTIVE_SIGNING_KEYS.pop()
    now = int(time.time())
    nonce = secrets.token_hex(16)
    payload = f"{now}.{nonce}"
    sig = hmac.new(
        _derive_session_key(configured),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}.{sig}"


MAX_REVOKED_SESSIONS: int = 10_000
_REVOKED_SESSION_SIGS: OrderedDict[str, float] = OrderedDict()


def revoke_session_token(token: str, secret: str | None = None) -> None:
    """Record token signature as revoked and prune expired entries (§77 E5, §78 E1–E3).

    Revocations are stored in memory and reset on process restart. An operator who
    needs immediate cluster-wide revocation across restarts should rotate
    ADMIN_TOKEN / SESSION_SECRET.

    Validates HMAC signature and timestamp before storing to prevent memory-growth
    attacks from unauthenticated forged cookies.
    """
    if not token or not isinstance(token, str):
        return

    candidates: list[str] = []
    if secret and len(secret) >= 32:
        candidates.append(secret)
    else:
        admin_tok = getattr(config, "ADMIN_TOKEN", "")
        if admin_tok and len(admin_tok) >= 32:
            candidates.append(admin_tok)
        for k in list(_ACTIVE_SIGNING_KEYS):
            if k not in candidates:
                candidates.append(k)

    if not candidates:
        return

    parts = token.split(".")
    if len(parts) != 3:
        return
    ts_str, nonce, sig = parts
    try:
        created_at = int(ts_str)
    except ValueError:
        return

    now = time.time()
    max_age = getattr(config, "SESSION_MAX_AGE_SECONDS", 3600)
    # If the token has already expired or has invalid future timestamp, discard without storing
    if now - created_at > max_age or created_at > now + 5:
        return

    # Verify cryptographic HMAC signature against known valid server keys
    payload = f"{created_at}.{nonce}"
    valid_sig = False
    for candidate in candidates:
        expected_sig = hmac.new(
            _derive_session_key(candidate),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if hmac.compare_digest(sig, expected_sig):
            valid_sig = True
            break

    if not valid_sig:
        return  # Forged or corrupted cookie; ignore silently without storing

    expiry_timestamp = created_at + max_age

    # Prune expired entries
    for s, exp in list(_REVOKED_SESSION_SIGS.items()):
        if now >= exp:
            _REVOKED_SESSION_SIGS.pop(s, None)

    # Evict oldest if at capacity
    cap = getattr(config, "MAX_REVOKED_SESSIONS", MAX_REVOKED_SESSIONS)
    while len(_REVOKED_SESSION_SIGS) >= cap:
        _REVOKED_SESSION_SIGS.popitem(last=False)

    _REVOKED_SESSION_SIGS[sig] = expiry_timestamp


def is_session_revoked(token: str) -> bool:
    """Return True if the token's signature is in the revocation registry."""
    if not token or not isinstance(token, str):
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    sig = parts[2]
    if sig in _REVOKED_SESSION_SIGS:
        exp = _REVOKED_SESSION_SIGS[sig]
        if time.time() >= exp:
            _REVOKED_SESSION_SIGS.pop(sig, None)
            return False
        return True
    return False


def verify_session_token(
    token: str,
    secret: str | None = None,
    max_age: int | None = None,
) -> bool:
    """Verify session token HMAC signature and age. Fail-closed on any error."""
    if is_session_revoked(token):
        return False
    configured = secret if secret is not None else config.ADMIN_TOKEN
    if not configured or len(configured) < 32 or not token:
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    ts_str, nonce, sig = parts
    try:
        created_at = int(ts_str)
    except ValueError:
        return False

    now = time.time()
    allowed_age = max_age if max_age is not None else config.SESSION_MAX_AGE_SECONDS
    # Expired or timestamp too far in future (> 5s clock skew)
    if now - created_at > allowed_age or created_at > now + 5:
        return False

    payload = f"{created_at}.{nonce}"
    expected_sig = hmac.new(
        _derive_session_key(configured),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(sig, expected_sig)


def _is_request_https(request: Request | None) -> bool:
    """Check if request is HTTPS or proxied HTTPS, respecting loopback dev exemption."""
    if request is None:
        return getattr(config, "FORCE_HTTPS", True)

    is_https = (
        request.url.scheme == "https"
        or request.headers.get("x-forwarded-proto") == "https"
        or request.headers.get("x-forwarded-ssl") == "on"
        or "proto=https" in (request.headers.get("forwarded") or "").lower()
    )
    if is_https:
        return True

    client_host = get_client_ip(request) if request else ""
    request_host = getattr(request.url, "hostname", "") or ""
    loopback_hosts = {"127.0.0.1", "::1", "localhost", "testclient", "test", "testserver", "app"}
    is_loopback = (
        (client_host in loopback_hosts or not client_host)
        and (request_host in loopback_hosts or not request_host)
    )
    if is_loopback:
        return False
    return getattr(config, "FORCE_HTTPS", True)


def set_session_cookie(
    response: Response,
    session_token: str,
    request: Request | None = None,
    max_age: int | None = None,
) -> None:
    """Set HttpOnly, SameSite=Lax, Secure session cookie with expiration.

    Guarantees:
      - httponly=True: JavaScript cannot read the token (mitigates XSS theft).
      - samesite="lax": Browser will not send cookie on cross-site subrequests (mitigates CSRF).
      - secure=True: Forced whenever HTTPS or proxy indicates TLS (exempts unencrypted loopback dev).
      - max_age: Cookie automatically dropped by browser after expiration.
    """
    age = max_age if max_age is not None else config.SESSION_MAX_AGE_SECONDS
    secure = _is_request_https(request)
    response.set_cookie(
        key=config.SESSION_COOKIE_NAME,
        value=session_token,
        max_age=age,
        expires=age,
        path="/",
        httponly=True,
        secure=secure,
        samesite="lax",
    )


def clear_session_cookie(
    response: Response,
    request: Request | None = None,
) -> None:
    """Clear session cookie on logout."""
    secure = _is_request_https(request)
    response.delete_cookie(
        key=config.SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=secure,
        samesite="lax",
    )


def _prune_failures(client_ip: str, now: float) -> list[float]:
    """Drop expired attempts for ONE host; bound the whole map (A6).

    Returns the host's still-valid attempts so the caller keeps its
    lockout check without re-reading the map.
    """
    attempts = [t for t in _FAILED_ATTEMPTS[client_ip]
                if now - t < _LOCKOUT_WINDOW_SECONDS]
    _FAILED_ATTEMPTS[client_ip] = attempts
    if len(_FAILED_ATTEMPTS) > _MAX_TRACKED_HOSTS:
        for ip in list(_FAILED_ATTEMPTS):
            fresh = [t for t in _FAILED_ATTEMPTS[ip]
                     if now - t < _LOCKOUT_WINDOW_SECONDS]
            if fresh:
                _FAILED_ATTEMPTS[ip] = fresh
            else:
                del _FAILED_ATTEMPTS[ip]
    return attempts


def require_admin_token(request: Request) -> None:
    """Raise 403 unless the request carries the configured operator token
    or a valid, non-expired HttpOnly session cookie.

    Enforces rate limiting on repeated failed attempts (brute-force defense).

    A7 (repo audit): the token is verified BEFORE the lockout check.
    The old order (lockout first) let an attacker brick the operator:
    5 bad guesses from the shared proxy address locked out even the
    CORRECT token for a renewable 60s window. The lockout now governs
    wrong attempts only — a valid credential always authenticates, while
    guessers are still throttled exactly as before.
    """
    client_ip = get_client_ip(request)
    now = time.time()

    # Prune expired attempts outside the sliding window (A6: map stays bounded)
    attempts = _prune_failures(client_ip, now)

    configured = config.ADMIN_TOKEN

    # 1. Header authentication (API, CLI, scripts)
    supplied = request.headers.get(ADMIN_TOKEN_HEADER, "")
    token_ok = bool(configured and len(configured) >= 32) and bool(supplied) and hmac.compare_digest(
        supplied, configured)
    if token_ok:
        # Clear recorded failures on successful authentication
        _FAILED_ATTEMPTS.pop(client_ip, None)
        return

    # 2. HttpOnly Cookie authentication (Browser dashboard sessions)
    session_cookie = request.cookies.get(config.SESSION_COOKIE_NAME, "")
    if session_cookie and verify_session_token(session_cookie, secret=configured):
        # Clear recorded failures on successful session authentication
        _FAILED_ATTEMPTS.pop(client_ip, None)
        return

    if not configured or len(configured) < 32:
        # Fail closed: no token configured or too short -> endpoint disabled.
        raise HTTPException(
            status_code=403,
            detail="operator endpoints are disabled (ADMIN_TOKEN not set)",
        )
    if len(attempts) >= _MAX_FAILED_ATTEMPTS:
        try:
            from api.security_monitor import SecurityEventType, record_security_event
            record_security_event(
                SecurityEventType.AUTH_LOCKOUT,
                client_ip=client_ip,
                path=request.url.path,
                method=request.method,
                detail=f"Operator auth lockout threshold reached ({len(attempts)} failed attempts)",
                severity="ERROR",
            )
        except Exception:
            pass
        raise HTTPException(
            status_code=429,
            detail="too many failed authentication attempts — rate limited",
        )
    _FAILED_ATTEMPTS[client_ip].append(now)
    try:
        from api.security_monitor import SecurityEventType, record_security_event
        record_security_event(
            SecurityEventType.AUTH_FAILURE,
            client_ip=client_ip,
            path=request.url.path,
            method=request.method,
            detail="Operator auth failed: invalid operator token or expired session cookie",
            severity="WARNING",
        )
    except Exception:
        pass
    raise HTTPException(status_code=403, detail="invalid operator token")


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
    client_ip = get_client_ip(request)
    proxied = any(
        request.headers.get(h) is not None for h in _FORWARDING_HEADERS)
    if not proxied and client_ip in (
            "127.0.0.1", "::1", "localhost", "testclient"):
        return
    require_admin_token(request)

