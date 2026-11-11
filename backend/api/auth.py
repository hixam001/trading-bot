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
import json
import logging
import os
import secrets
import threading
import time
from collections import OrderedDict, defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response

import config
from api.rate_limiter import get_client_ip

ADMIN_TOKEN_HEADER = "X-Admin-Token"

# Global brute-force ceiling: per-IP limits are bypassable by a distributed
# attacker (or by a real botnet behind many proxies). This window is not
# account-specific — there is one operator — so any 50 failures in five
# minutes throttle ALL wrong credentials. A valid credential still wins
# (operator availability beats lockout purity; see A7 below).
_GLOBAL_FAILED_ATTEMPTS: deque[float] = deque(maxlen=1000)
_MAX_GLOBAL_FAILED_ATTEMPTS = 50
_GLOBAL_LOCKOUT_WINDOW_SECONDS = 300.0

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


def _session_secrets() -> list[str]:
    """Session-signing candidates, strongest first.

    SESSION_SECRET (when configured) is the dedicated signer. ADMIN_TOKEN
    remains a fallback so an operator can add SESSION_SECRET without logging
    every browser out, and so a token issued before the split still verifies.
    """
    out: list[str] = []
    dedicated = getattr(config, "SESSION_SECRET", "") or ""
    admin = getattr(config, "ADMIN_TOKEN", "") or ""
    if dedicated and len(dedicated) >= 32:
        out.append(dedicated)
    if admin and len(admin) >= 32 and admin not in out:
        out.append(admin)
    return out


def _derive_session_key(secret: str) -> bytes:
    """Derive a dedicated HMAC session-signing key from the signing secret."""
    return hashlib.sha256((secret + ":session_auth_key_v1").encode("utf-8")).digest()


# Ordered oldest -> newest so eviction drops the least-recently-used key, not
# an arbitrary one (a set's pop() could discard the CURRENT ADMIN_TOKEN's key
# while keeping long-dead rotations, breaking logout revocation).
_ACTIVE_SIGNING_KEYS: OrderedDict[str, None] = OrderedDict()


def create_session_token(secret: str | None = None) -> str:
    """Generate a cryptographically signed, timestamped session token.

    Format: <timestamp>.<nonce>.<hmac_signature>
    Tamper-evident, stateless, and verifiable server-side without database queries.
    """
    if secret is not None:
        configured = secret
    else:
        candidates = _session_secrets()
        configured = candidates[0] if candidates else ""
    if not configured or len(configured) < 32:
        raise ValueError("Cannot create session token: session secret is not configured or shorter than 32 characters")
    _ACTIVE_SIGNING_KEYS[configured] = None
    _ACTIVE_SIGNING_KEYS.move_to_end(configured)
    if len(_ACTIVE_SIGNING_KEYS) > 100:
        _ACTIVE_SIGNING_KEYS.popitem(last=False)
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

# Revocations are persisted as a bounded JSON map of signature -> expiry.
# Only the signature is stored (never the bearer token), and the file is
# mode 0600 under a 0700 directory. A corrupt/unreadable file fails CLOSED:
# we would rather force a fresh login than resurrect a logged-out session.
_REVOCATIONS_LOADED = False
_REVOCATIONS_ERROR = False
_REVOCATION_LOCK = threading.RLock()


def _revocation_path() -> Path | None:
    raw = getattr(config, "SESSION_REVOCATION_FILE", "") or ""
    if not raw:
        return None
    try:
        return Path(raw).expanduser()
    except (TypeError, ValueError):
        return None


def _prune_revoked(now: float | None = None, reserve_slot: bool = False) -> None:
    moment = time.time() if now is None else now
    for sig, exp in list(_REVOKED_SESSION_SIGS.items()):
        if moment >= exp:
            _REVOKED_SESSION_SIGS.pop(sig, None)
    cap = getattr(config, "MAX_REVOKED_SESSIONS", MAX_REVOKED_SESSIONS)
    limit = max(0, cap - 1) if reserve_slot else max(0, cap)
    while len(_REVOKED_SESSION_SIGS) > limit:
        _REVOKED_SESSION_SIGS.popitem(last=False)


def _ensure_revocations_loaded() -> None:
    global _REVOCATIONS_LOADED, _REVOCATIONS_ERROR
    if _REVOCATIONS_LOADED:
        return
    with _REVOCATION_LOCK:
        if _REVOCATIONS_LOADED:
            return
        _REVOCATIONS_LOADED = True
        path = _revocation_path()
        if path is None or not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            entries = raw.get("revoked", raw) if isinstance(raw, dict) else {}
            now = time.time()
            for sig, exp in entries.items():
                if (isinstance(sig, str) and len(sig) == 64
                        and isinstance(exp, (int, float)) and exp > now):
                    _REVOKED_SESSION_SIGS[sig] = float(exp)
            _prune_revoked(now)
        except Exception:
            _REVOCATIONS_ERROR = True


def _persist_revocations() -> None:
    path = _revocation_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = json.dumps(
            {"version": 1, "revoked": dict(_REVOKED_SESSION_SIGS)},
            separators=(",", ":"),
            sort_keys=True,
        )
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception as exc:
        # §93 R2: a restart could resurrect revoked sessions. Persistence is
        # still best-effort at the HTTP layer, but the operator must see it.
        logger = logging.getLogger("api.auth")
        logger.error("failed to persist session revocation registry at %s: %s",
                     path, type(exc).__name__, exc_info=True)
        try:
            from api.security_monitor import (
                SecurityEventType, record_security_event,
            )
            record_security_event(
                SecurityEventType.INTERNAL_ERROR,
                client_ip="unknown",
                path="/api/auth/logout",
                method="POST",
                detail=("session revocation registry could not be persisted: "
                        f"{type(exc).__name__}"),
                severity="ERROR",
            )
        except Exception:
            pass


def reset_session_revocations(delete_file: bool = False) -> None:
    """Clear the in-memory registry (tests / explicit operator reset).

    ``delete_file=True`` also removes the on-disk registry. Startup is lazily
    reloaded, so callers must set this before issuing tokens.
    """
    global _REVOCATIONS_LOADED, _REVOCATIONS_ERROR
    with _REVOCATION_LOCK:
        _REVOKED_SESSION_SIGS.clear()
        _REVOCATIONS_LOADED = False
        _REVOCATIONS_ERROR = False
        if delete_file:
            path = _revocation_path()
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass


def revoke_session_token(token: str, secret: str | None = None) -> None:
    """Record token signature as revoked and prune expired entries (§77 E5, §78 E1–E3).

    Revocations are persisted (signature + expiry only) and survive process
    restart. Rotate all accepted signing secrets for cluster-wide revocation;
    nonempty ADMIN_TOKEN rotation preserves dedicated-secret sessions. This
    file covers a single host's logout semantics.

    Validates HMAC signature and timestamp before storing to prevent memory-growth
    attacks from unauthenticated forged cookies.
    """
    if not token or not isinstance(token, str):
        return

    _ensure_revocations_loaded()

    candidates: list[str] = []
    if secret and len(secret) >= 32:
        candidates.append(secret)
    else:
        for candidate in _session_secrets():
            if candidate not in candidates:
                candidates.append(candidate)
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

    with _REVOCATION_LOCK:
        _prune_revoked(now, reserve_slot=True)
        _REVOKED_SESSION_SIGS[sig] = expiry_timestamp
        _persist_revocations()


def is_session_revoked(token: str) -> bool:
    """Return True if the token's signature is in the revocation registry."""
    if not token or not isinstance(token, str):
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    _ensure_revocations_loaded()
    if _REVOCATIONS_ERROR:
        # Unreadable/corrupt registry: fail closed rather than resurrect.
        return True
    sig = parts[2]
    with _REVOCATION_LOCK:
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
    if not token:
        return False
    if secret is not None:
        candidates = [secret]
    else:
        candidates = _session_secrets()
    candidates = [c for c in candidates if c and len(c) >= 32]
    if not candidates:
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
    for configured in candidates:
        expected_sig = hmac.new(
            _derive_session_key(configured),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if hmac.compare_digest(sig, expected_sig):
            return True
    return False


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
    loopback_hosts = {"127.0.0.1", "::1", "localhost"}
    is_loopback = (
        (client_host in loopback_hosts or config.is_test_host(client_host) or not client_host)
        and (request_host in loopback_hosts or config.is_test_host(request_host) or not request_host)
    )
    if is_loopback:
        return False
    return getattr(config, "FORCE_HTTPS", True)


def _cookie_samesite(request: Request | None) -> str:
    """SameSite=None for an HTTPS cross-origin dashboard, Lax otherwise.

    Split deployments (dashboard on Vercel, API on another host) are documented
    in docs/13 and use credentialed CORS. A Lax cookie is never attached to
    those cross-site requests, so login appeared to succeed and the next poll
    still returned 403. SameSite=None requires Secure, so it is only selected
    when the request is HTTPS.
    """
    if request is None or not _is_request_https(request):
        return "lax"
    origin = request.headers.get("origin")
    if not origin:
        return "lax"
    try:
        origin_host = urlsplit(origin).hostname
    except ValueError:
        return "lax"
    request_host = request.url.hostname
    if origin_host and request_host and origin_host != request_host:
        return "none"
    return "lax"


def set_session_cookie(
    response: Response,
    session_token: str,
    request: Request | None = None,
    max_age: int | None = None,
) -> None:
    """Set HttpOnly, SameSite, Secure session cookie with expiration.

    Guarantees:
      - httponly=True: JavaScript cannot read the token (mitigates XSS theft).
      - samesite: "lax" by default; "none" only for HTTPS cross-origin split deployments.
      - secure=True: Forced whenever HTTPS or proxy indicates TLS (exempts unencrypted loopback dev).
      - max_age: Cookie automatically dropped by browser after expiration.
    """
    age = max_age if max_age is not None else config.SESSION_MAX_AGE_SECONDS
    secure = _is_request_https(request)
    response.set_cookie(
        key=config.SESSION_COOKIE_NAME,
        value=session_token,
        max_age=age,
        # §84: Starlette's `expires` is a DATE, not a number of seconds.
        # Passing the int emitted `Expires=3600` (an invalid date near 1970),
        # so clients could treat the fresh cookie as already expired.
        expires=datetime.now(timezone.utc) + timedelta(seconds=age),
        path="/",
        httponly=True,
        secure=secure,
        samesite=_cookie_samesite(request),
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
        samesite=_cookie_samesite(request),
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


def _prune_global_failures(now: float) -> None:
    while (_GLOBAL_FAILED_ATTEMPTS
           and now - _GLOBAL_FAILED_ATTEMPTS[0] >= _GLOBAL_LOCKOUT_WINDOW_SECONDS):
        _GLOBAL_FAILED_ATTEMPTS.popleft()


def _global_lockout_active(now: float) -> bool:
    _prune_global_failures(now)
    return len(_GLOBAL_FAILED_ATTEMPTS) >= _MAX_GLOBAL_FAILED_ATTEMPTS


def _record_global_failure(now: float) -> None:
    _prune_global_failures(now)
    _GLOBAL_FAILED_ATTEMPTS.append(now)


def _clear_auth_failures(client_ip: str) -> None:
    _FAILED_ATTEMPTS.pop(client_ip, None)
    _GLOBAL_FAILED_ATTEMPTS.clear()


def admin_auth_enabled() -> bool:
    """One disabled-state policy for authorization, login and session status.

    Configuration normalizes weak configured secrets to an empty string.
    Keep malformed/short runtime values fail closed as well.
    """
    token = config.ADMIN_TOKEN
    return isinstance(token, str) and len(token) >= 32


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
    # Disabling operator auth wins over every credential, including sessions
    # signed by an unchanged dedicated secret and restored after a restart.
    if not admin_auth_enabled():
        raise HTTPException(
            status_code=403,
            detail="operator endpoints are disabled (ADMIN_TOKEN not set)",
        )

    # 1. Header authentication (API, CLI, scripts)
    supplied = request.headers.get(ADMIN_TOKEN_HEADER, "")
    token_ok = bool(configured and len(configured) >= 32) and bool(supplied) and hmac.compare_digest(
        supplied, configured)
    if token_ok:
        # Clear recorded failures on successful authentication
        _clear_auth_failures(client_ip)
        return

    # 2. HttpOnly Cookie authentication (Browser dashboard sessions)
    session_cookie = request.cookies.get(config.SESSION_COOKIE_NAME, "")
    if session_cookie and verify_session_token(session_cookie):
        # Clear recorded failures on successful session authentication
        _clear_auth_failures(client_ip)
        return

    if len(attempts) >= _MAX_FAILED_ATTEMPTS or _global_lockout_active(now):
        try:
            from api.security_monitor import SecurityEventType, record_security_event
            record_security_event(
                SecurityEventType.AUTH_LOCKOUT,
                client_ip=client_ip,
                path=request.url.path,
                method=request.method,
                detail=(
                    "Operator auth lockout threshold reached "
                    f"(per-ip={len(attempts)}, global={len(_GLOBAL_FAILED_ATTEMPTS)})"
                ),
                severity="ERROR",
            )
        except Exception:
            pass
        raise HTTPException(
            status_code=429,
            detail="too many failed authentication attempts — rate limited",
        )
    _FAILED_ATTEMPTS[client_ip].append(now)
    _record_global_failure(now)
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
    if not proxied and (
            client_ip in ("127.0.0.1", "::1", "localhost")
            or config.is_test_host(client_ip)):
        return
    require_admin_token(request)

