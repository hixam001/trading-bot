"""
backend/api/error_handler.py — centralized production error handling & secret redaction.

Guarantees:
1. Production exception shielding:
   - Unhandled exceptions (500) and 5xx HTTPExceptions return a generic message:
     {"detail": "Internal server error", "error_id": "<hex_id>"}
   - Raw exception details, Python stack traces, and internal variables are NEVER exposed to clients.
   - In local development mode (config.DEBUG=True), debug context is included for convenience.
2. Private, correlated server logging:
   - Full exception tracebacks and request metadata are logged privately server-side
     with a unique correlation error_id for debugging.
3. Secret redaction in logs:
   - Logging filter scrubs API keys, admin tokens, passwords, bearer tokens,
     basic auth URLs, and wallet keypairs before log messages hit disk.
4. Safe request validation errors (422):
   - Pydantic validation error objects have the raw "input" field removed to prevent
     passwords, secrets, or large payloads from echoing back to clients.
"""
from __future__ import annotations

import html
import logging
import re
import secrets
from typing import Any
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

import config

log = logging.getLogger("api.error_handler")

# Generic error message returned to clients in production
GENERIC_500_MESSAGE = "Internal server error"

# Sensitive parameter names whose values must be masked if found in query strings or logs
_SENSITIVE_PARAM_NAMES = (
    "token", "admin_token", "api_key", "apikey", "secret", "password",
    "privy_token", "refresh_token", "auth", "authorization", "keypair",
)

_SENSITIVE_PARAM_RE = re.compile(
    rf"(?i)\b({'|'.join(_SENSITIVE_PARAM_NAMES)})\b\s*[:=]\s*(?!\[?<REDACTED)([^\s,;&\"']+)",
)

_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_\-\.~+/]+=*")
_BASIC_AUTH_URL_RE = re.compile(r"(https?://)([^:\s/]+):([^@\s/]+)@")
_SOLANA_KEYPAIR_RE = re.compile(r"\[\s*\d+(?:\s*,\s*\d+){40,}\s*\]")


class SecretRedactorFilter(logging.Filter):
    """Logging filter that scrubs sensitive keys, passwords, and tokens from all log output."""

    def __init__(self) -> None:
        super().__init__()
        self._compiled_secrets: list[re.Pattern] = []
        self._refresh_secrets()

    def _refresh_secrets(self) -> None:
        patterns = []
        # Dynamic known secret values from config (only non-trivial strings >= 6 chars)
        configured_secrets = [
            getattr(config, "ADMIN_TOKEN", None),
            getattr(config, "DEEPSEEK_API_KEY", None),
            getattr(config, "GROQ_API_KEY", None),
            getattr(config, "SOCIAL_LLM_API_KEY", None),
            getattr(config, "BRAVE_SEARCH_API_KEY", None),
            getattr(config, "FIRECRAWL_API_KEY", None),
            getattr(config, "SCRAPINGBEE_API_KEY", None),
            getattr(config, "SCRAPINGDOG_API_KEY", None),
            getattr(config, "ZENROWS_API_KEY", None),
            getattr(config, "SCRAPEOPS_API_KEY", None),
            getattr(config, "FOMO_PRIVY_REFRESH_TOKEN", None),
            getattr(config, "SECRET_STORE_KEY", None),
        ]
        for sec in configured_secrets:
            if sec and isinstance(sec, str) and len(sec.strip()) >= 6:
                patterns.append(re.compile(re.escape(sec.strip())))
        self._compiled_secrets = patterns

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            original = msg

            # 1. Mask exact known config secrets
            for pat in self._compiled_secrets:
                msg = pat.sub("<REDACTED-SECRET>", msg)

            # 2. Mask Solana 64-int keypair arrays
            msg = _SOLANA_KEYPAIR_RE.sub("[<REDACTED-KEYPAIR>]", msg)

            # 3. Mask Authorization: Bearer tokens
            msg = _BEARER_RE.sub("Bearer <REDACTED>", msg)

            # 4. Mask basic auth credentials in URLs
            msg = _BASIC_AUTH_URL_RE.sub(r"\1***:***@", msg)

            # 5. Mask query params and header tokens
            msg = _SENSITIVE_PARAM_RE.sub(r"\1=<REDACTED>", msg)

            if msg != original:
                record.msg = msg
                record.args = None
        except Exception:
            # Redaction failure must never crash the application or prevent logging
            pass
        return True


def install_secret_redactor() -> None:
    """Install SecretRedactorFilter across all primary application and server loggers."""
    redactor = SecretRedactorFilter()
    target_loggers = ("", "uvicorn", "uvicorn.error", "uvicorn.access", "fastapi", "httpx", "api")
    for name in target_loggers:
        lg = logging.getLogger(name)
        if not any(isinstance(f, SecretRedactorFilter) for f in lg.filters):
            lg.addFilter(redactor)


def sanitize_error_detail(detail: Any) -> Any:
    """Sanitize detail string to ensure no secrets or sensitive parameters leak in client responses."""
    if not isinstance(detail, str):
        return detail

    sanitized = detail
    # Mask known secrets
    configured = getattr(config, "ADMIN_TOKEN", "")
    if configured and len(configured) >= 6:
        sanitized = sanitized.replace(configured, "<REDACTED>")

    for attr in ("DEEPSEEK_API_KEY", "GROQ_API_KEY", "FIRECRAWL_API_KEY", "BRAVE_SEARCH_API_KEY"):
        val = getattr(config, attr, "")
        if val and isinstance(val, str) and len(val) >= 6:
            sanitized = sanitized.replace(val, "<REDACTED>")

    # Mask keypair patterns
    sanitized = _SOLANA_KEYPAIR_RE.sub("[<REDACTED-KEYPAIR>]", sanitized)
    sanitized = _BEARER_RE.sub("Bearer <REDACTED>", sanitized)
    sanitized = _BASIC_AUTH_URL_RE.sub(r"\1***:***@", sanitized)
    return sanitized


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Global handler for uncaught server exceptions (500).

    In production:
      - Generates an opaque error_id correlation token.
      - Logs the full traceback privately to server logs alongside the error_id.
      - Returns a clean, generic JSON error message to the client with zero stack trace or internal leaks.
    In debug mode:
      - Returns the exception message for local developer convenience.
    """
    error_id = secrets.token_hex(8)
    client_ip = getattr(getattr(request, "client", None), "host", "unknown")

    log.error(
        "Unhandled server error [error_id=%s] on %s %s from %s: %s",
        error_id,
        request.method,
        html.escape(request.url.path),
        client_ip,
        exc,
        exc_info=True,
    )

    is_debug = getattr(config, "DEBUG", False)
    content: dict[str, Any] = {
        "detail": GENERIC_500_MESSAGE,
        "error_id": error_id,
    }
    if is_debug:
        content["debug_message"] = str(exc)
        content["exception_type"] = type(exc).__name__

    return JSONResponse(
        status_code=500,
        content=content,
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Handler for Starlette and FastAPI HTTPExceptions.

    Shields 5xx internal server errors with generic messaging while preserving
    safe 4xx client errors (400 Bad Request, 401 Unauthorized, 403 Forbidden, 404 Not Found, 429).
    """
    headers = getattr(exc, "headers", None)

    if exc.status_code >= 500:
        error_id = secrets.token_hex(8)
        client_ip = getattr(getattr(request, "client", None), "host", "unknown")
        log.error(
            "HTTP %d error [error_id=%s] on %s %s from %s: %s",
            exc.status_code,
            error_id,
            request.method,
            html.escape(request.url.path),
            client_ip,
            exc.detail,
            exc_info=True,
        )
        is_debug = getattr(config, "DEBUG", False)
        content: dict[str, Any] = {
            "detail": GENERIC_500_MESSAGE,
            "error_id": error_id,
        }
        if is_debug:
            content["debug_message"] = str(exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content=content,
            headers=headers,
        )

    # For 4xx errors, sanitize detail to ensure no inadvertent secret leakage
    safe_detail = sanitize_error_detail(exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": safe_detail},
        headers=headers,
    )


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Sanitize Pydantic RequestValidationErrors (HTTP 422).

    Strips raw user 'input' fields from error payloads to prevent passwords,
    session tokens, or oversized malicious payloads from echoing back to the client.
    Uses jsonable_encoder to ensure nested validator exceptions are properly serialized.
    """
    raw_errors = jsonable_encoder(exc.errors())
    cleaned_errors: list[dict[str, Any]] = []
    for err in raw_errors:
        clean_item: dict[str, Any] = {
            "loc": err.get("loc", []),
            "msg": sanitize_error_detail(err.get("msg", "")),
            "type": err.get("type", ""),
        }
        ctx = err.get("ctx")
        if isinstance(ctx, dict):
            # Omit any context keys that might echo raw input strings
            safe_ctx = {
                k: sanitize_error_detail(v) if isinstance(v, str) else v
                for k, v in ctx.items()
                if k not in ("input", "raw", "value")
            }
            if safe_ctx:
                clean_item["ctx"] = safe_ctx

        cleaned_errors.append(clean_item)

    return JSONResponse(
        status_code=422,
        content={"detail": cleaned_errors},
    )


def register_error_handlers(app: FastAPI) -> None:
    """Register custom production error handlers on the FastAPI application."""
    app.add_exception_handler(Exception, unhandled_exception_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
