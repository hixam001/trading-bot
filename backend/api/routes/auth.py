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

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    """Specific allowed fields for operator login.

    Includes CAPTCHA token and hidden honeypot field for bot protection (Rule 12).
    """
    token: str = Field(..., min_length=1, max_length=512, description="Operator admin token")
    captcha_token: str | None = Field(default=None, max_length=1024, description="Optional CAPTCHA or PoW token")
    honeypot: str | None = Field(default="", max_length=256, alias="_hp_website")

    model_config = ConfigDict(extra="ignore")

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
    client_ip = getattr(getattr(request, "client", None), "host", "unknown")

    # 1. Server-side Bot Protection check (executes BEFORE checking credentials)
    await verify_bot_protection(
        token=req.captcha_token,
        client_ip=client_ip,
        honeypot=req.honeypot,
    )

    now = time.time()
    attempts = _prune_failures(client_ip, now)

    configured = config.ADMIN_TOKEN
    if not configured:
        raise HTTPException(
            status_code=403,
            detail="operator login is disabled (ADMIN_TOKEN not set)",
        )

    if len(attempts) >= _MAX_FAILED_ATTEMPTS:
        raise HTTPException(
            status_code=429,
            detail="too many failed authentication attempts — rate limited",
        )

    valid = bool(req.token) and hmac.compare_digest(req.token, configured)
    if not valid:
        _FAILED_ATTEMPTS[client_ip].append(now)
        raise HTTPException(status_code=403, detail="invalid credentials")

    # Clear lockout history on successful login
    _FAILED_ATTEMPTS.pop(client_ip, None)

    session_token = create_session_token()
    set_session_cookie(response, session_token, request=request)
    return {
        "status": "authenticated",
        "expires_in": config.SESSION_MAX_AGE_SECONDS,
    }


@router.post("/signup")
async def signup(req: SignupRequest, request: Request):
    """Registration endpoint protected with server-side bot challenge and honeypot."""
    client_ip = getattr(getattr(request, "client", None), "host", "unknown")
    # 1. Server-side Bot Protection check executes BEFORE anything else
    await verify_bot_protection(
        token=req.captcha_token,
        client_ip=client_ip,
        honeypot=req.honeypot,
    )
    # Single-operator bot architecture: public signups are disabled by default
    raise HTTPException(
        status_code=403,
        detail="Public registration is disabled. Use operator ADMIN_TOKEN to authenticate.",
    )


@router.post("/logout")
async def logout(response: Response):
    """Clear the HttpOnly session cookie on logout."""
    clear_session_cookie(response)
    return {"status": "logged_out"}


@router.get("/session")
async def get_session(request: Request):
    """Check current session validity and report remaining session lifetime."""
    configured = config.ADMIN_TOKEN
    if not configured:
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

