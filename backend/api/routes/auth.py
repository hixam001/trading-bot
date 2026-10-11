"""
api/routes/auth.py — operator session endpoints (login, logout, session status).

Implements Rule 9: HttpOnly, Secure, SameSite session cookies for operator
authentication, replacing any potential client-side token storage (localStorage).
Sessions expire server-side and client-side after SESSION_MAX_AGE_SECONDS.
"""
from __future__ import annotations

import hmac
import time

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

import config
from api.auth import (
    _FAILED_ATTEMPTS,
    _MAX_FAILED_ATTEMPTS,
    _prune_failures,
    clear_session_cookie,
    create_session_token,
    set_session_cookie,
    verify_session_token,
)
from api.bot_protection import create_bot_challenge, verify_bot_protection
from api.rate_limiter import get_client_ip

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    """Specific allowed fields for operator login.

    Includes CAPTCHA token and hidden honeypot field for bot protection (Rule 12).
    """
    token: str = Field(..., min_length=1, max_length=512, description="Operator admin token")
    captcha_token: str | None = Field(default=None, max_length=1024, description="Optional CAPTCHA or PoW token")
    honeypot: str | None = Field(default="", max_length=256, alias="_hp_website")

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    @field_validator("token")
    @classmethod
    def validate_token(cls, v: str) -> str:
        if "\x00" in v:
            raise ValueError("Null bytes are not allowed in token")
        v = v.strip()
        if not v:
            raise ValueError("Token cannot be empty or whitespace-only")
        return v

    @field_validator("captcha_token")
    @classmethod
    def validate_captcha_token(cls, v: str | None) -> str | None:
        if v is not None:
            if "\x00" in v:
                raise ValueError("Null bytes are not allowed in captcha_token")
            v = v.strip()
        return v

    @field_validator("honeypot")
    @classmethod
    def validate_honeypot(cls, v: str | None) -> str | None:
        if v is not None and "\x00" in v:
            raise ValueError("Null bytes are not allowed in honeypot")
        return v


class SignupRequest(BaseModel):
    """Signup request with bot protection challenge and honeypot validation."""
    username: str = Field(
        ...,
        min_length=3,
        max_length=50,
        pattern=r"^[a-zA-Z0-9_\-\.]+$",
        description="Username (alphanumeric, underscores, hyphens, and dots)",
    )
    password: str = Field(..., min_length=8, max_length=128, description="Password")
    captcha_token: str | None = Field(default=None, max_length=1024)
    honeypot: str | None = Field(default="", max_length=256, alias="_hp_website")

    model_config = ConfigDict(extra="ignore")

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        if "\x00" in v:
            raise ValueError("Null bytes are not allowed in username")
        return v.strip()

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if "\x00" in v:
            raise ValueError("Null bytes are not allowed in password")
        return v


@router.get("/challenge")
async def get_challenge():
    """Generate a server-side proof-of-work bot challenge for clients."""
    return create_bot_challenge()


@router.post("/login")
async def login(req: LoginRequest, request: Request, response: Response):
    """Authenticate operator and issue an HttpOnly, SameSite, Secure session cookie.

    Bot-protected (Rule 12): verifies CAPTCHA token and honeypot before processing.
    Brute-force defended: throttles repeated failed attempts per IP (429).
    Constant-time comparison protects against timing attacks.
    """
    client_ip = get_client_ip(request)

    # 1. Server-side Bot Protection check (executes BEFORE checking credentials)
    await verify_bot_protection(
        token=req.captcha_token,
        client_ip=client_ip,
        honeypot=req.honeypot,
        request=request,
    )

    now = time.time()
    attempts = _prune_failures(client_ip, now)

    configured = config.ADMIN_TOKEN
    if not configured or len(configured) < 32:
        raise HTTPException(
            status_code=403,
            detail="operator login is disabled (ADMIN_TOKEN not set)",
        )

    # Credential check first (mirroring §70 A7):
    # A valid credential always authenticates and clears lockout history.
    valid = bool(req.token) and hmac.compare_digest(req.token, configured)
    if valid:
        # Clear lockout history on successful login
        _FAILED_ATTEMPTS.pop(client_ip, None)
        session_token = create_session_token()
        set_session_cookie(response, session_token, request=request)
        return {
            "status": "authenticated",
            "expires_in": config.SESSION_MAX_AGE_SECONDS,
        }

    # Wrong credential: check if host is already over the lockout threshold
    if len(attempts) >= _MAX_FAILED_ATTEMPTS:
        try:
            from api.security_monitor import SecurityEventType, record_security_event
            record_security_event(
                SecurityEventType.AUTH_LOCKOUT,
                client_ip=client_ip,
                path="/api/auth/login",
                method="POST",
                detail=f"Login lockout triggered: {len(attempts)} failed attempts",
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
            path="/api/auth/login",
            method="POST",
            detail="Operator login failed: invalid credentials",
            severity="WARNING",
        )
    except Exception:
        pass
    raise HTTPException(status_code=403, detail="invalid credentials")


@router.post("/signup")
async def signup(req: SignupRequest, request: Request):
    """Registration endpoint protected with server-side bot challenge and honeypot."""
    client_ip = get_client_ip(request)
    # 1. Server-side Bot Protection check executes BEFORE anything else
    await verify_bot_protection(
        token=req.captcha_token,
        client_ip=client_ip,
        honeypot=req.honeypot,
        request=request,
    )
    # Single-operator bot architecture: public signups are disabled by default
    raise HTTPException(
        status_code=403,
        detail="Public registration is disabled. Use operator ADMIN_TOKEN to authenticate.",
    )


@router.post("/logout")
async def logout(response: Response, request: Request):
    """Clear the HttpOnly session cookie on logout."""
    clear_session_cookie(response, request=request)
    return {"status": "logged_out"}


@router.get("/session")
async def get_session(request: Request):
    """Check current session validity and report remaining session lifetime."""
    configured = config.ADMIN_TOKEN
    if not configured or len(configured) < 32:
        return {"authenticated": False, "reason": "auth_disabled"}

    # 1. Check header
    supplied = request.headers.get("X-Admin-Token", "")
    if supplied and hmac.compare_digest(supplied, configured):
        return {
            "authenticated": True,
            "auth_type": "header",
            "expires_in": None,
        }

    # 2. Check session cookie
    cookie = request.cookies.get(config.SESSION_COOKIE_NAME, "")
    if cookie and verify_session_token(cookie, secret=configured):
        parts = cookie.split(".")
        created_at = int(parts[0]) if len(parts) == 3 and parts[0].isdigit() else int(time.time())
        remaining = max(0, config.SESSION_MAX_AGE_SECONDS - int(time.time() - created_at))
        return {
            "authenticated": True,
            "auth_type": "cookie",
            "expires_in": remaining,
        }

    return {"authenticated": False}

