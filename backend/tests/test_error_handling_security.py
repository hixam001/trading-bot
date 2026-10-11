"""
backend/tests/test_error_handling_security.py — production error handling and secret shielding tests.

Verifies:
1. Production exception shielding (500):
   - Unhandled exceptions return generic {"detail": "Internal server error", "error_id": "..."}.
   - Python stack traces, file paths, line numbers, and internal variables are never sent to the client.
   - Server logs record the complete traceback privately with the matching error_id.
2. 5xx HTTPException shielding:
   - Internal 5xx HTTPExceptions mask internal details and return generic 500 messages to clients.
3. 4xx client errors preserved & sanitized:
   - Standard 4xx errors (400, 403, 404, 429) retain safe details; sensitive tokens are stripped.
4. Request validation error (422) sanitization:
   - Raw user input (passwords, tokens, oversized payloads) is stripped from 422 JSON bodies.
5. Logging secret redactor:
   - Log output automatically redacts admin tokens, API keys, Bearer headers, basic auth URLs,
     and Solana keypairs.
6. Debug mode behavior:
   - When DEBUG=False (production), debug messages are strictly omitted.
   - When DEBUG=True, debug messages are available for local development.
"""
from __future__ import annotations

import logging
import httpx
import pytest
import pytest_asyncio
from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel, Field

import config
from api import db
from api.error_handler import (
    GENERIC_500_MESSAGE,
    SecretRedactorFilter,
    http_exception_handler,
    register_error_handlers,
    sanitize_error_detail,
    unhandled_exception_handler,
    validation_exception_handler,
)
from api.main import app

# Ephemeral test router to trigger specific error scenarios
error_test_router = APIRouter(prefix="/api/test-errors", tags=["test-errors"])


@error_test_router.get("/crash-zero-division")
async def crash_zero_division():
    """Trigger an unhandled ZeroDivisionError."""
    return 1 / 0


@error_test_router.get("/crash-key-error")
async def crash_key_error():
    """Trigger an unhandled KeyError with an internal secret name."""
    d = {}
    return d["internal_secret_key_12345"]


@error_test_router.get("/internal-500-http-exception")
async def crash_500_http():
    """Trigger a 500 HTTPException that contains sensitive database connection strings."""
    raise HTTPException(
        status_code=500,
        detail="Postgres connect error: postgresql://bot_user:secret_db_password_987@10.0.0.5:5432/live_db",
    )


class SensitivePayload(BaseModel):
    username: str = Field(min_length=3, max_length=16)
    token: str = Field(min_length=8, max_length=32)


@error_test_router.post("/validate-sensitive-payload")
async def validate_sensitive_payload(payload: SensitivePayload):
    return {"status": "ok"}


err_app = FastAPI()
register_error_handlers(err_app)
err_app.include_router(error_test_router)


@pytest_asyncio.fixture
async def err_env(tmp_path, monkeypatch):
    config.DB_PATH = tmp_path / "test_err.db"
    monkeypatch.setattr(config, "DATA_BACKEND", "mock")
    monkeypatch.setattr(config, "ADMIN_TOKEN", "super-secret-admin-token-123456")
    monkeypatch.setattr(config, "DEBUG", False)
    await db.init_db()
    yield


@pytest_asyncio.fixture
async def client(err_env):
    transport = httpx.ASGITransport(app=err_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# 1. Unhandled Server Exceptions (500) Shielding
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unhandled_exception_returns_generic_message_and_error_id(client, caplog):
    """Unhandled exceptions return a generic 500 error with an error_id and no stack trace."""
    with caplog.at_level(logging.ERROR):
        r = await client.get("/api/test-errors/crash-zero-division")

    assert r.status_code == 500
    data = r.json()
    assert data["detail"] == GENERIC_500_MESSAGE
    assert "error_id" in data
    assert len(data["error_id"]) >= 8

    # Ensure stack trace and internal identifiers are NOT present in client response
    response_text = r.text
    assert "ZeroDivisionError" not in response_text
    assert "Traceback" not in response_text
    assert "division by zero" not in response_text
    assert "main.py" not in response_text

    # Verify full exception details were logged privately server-side
    log_text = caplog.text
    assert data["error_id"] in log_text
    assert "division by zero" in log_text or "ZeroDivisionError" in log_text


@pytest.mark.asyncio
async def test_key_error_never_leaks_missing_key_to_client(client, caplog):
    """KeyError exceptions do not leak internal variable/key names to the client."""
    with caplog.at_level(logging.ERROR):
        r = await client.get("/api/test-errors/crash-key-error")

    assert r.status_code == 500
    data = r.json()
    assert data["detail"] == GENERIC_500_MESSAGE
    assert "internal_secret_key_12345" not in r.text

    # But logged privately
    assert "internal_secret_key_12345" in caplog.text


# ---------------------------------------------------------------------------
# 2. 5xx HTTPException Shielding
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_500_http_exception_shields_sensitive_details(client, caplog):
    """500 HTTPExceptions shield database credentials and connection strings from clients."""
    with caplog.at_level(logging.ERROR):
        r = await client.get("/api/test-errors/internal-500-http-exception")

    assert r.status_code == 500
    data = r.json()
    assert data["detail"] == GENERIC_500_MESSAGE
    assert "secret_db_password_987" not in r.text
    assert "postgresql://" not in r.text
    assert "error_id" in data

    # Verify logged privately
    assert data["error_id"] in caplog.text


# ---------------------------------------------------------------------------
# 3. 4xx Client Errors Preserved and Sanitized
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_404_not_found_preserved(client, err_env):
    """Standard 404 client errors return normal detail."""
    # On err_app with register_error_handlers
    r = await client.get("/api/unknown-endpoint-xyz")
    assert r.status_code == 404
    assert r.json()["detail"] == "Not Found"

    # On main app with SPA/API catchall
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r_main = await c.get("/api/unknown-endpoint-xyz")
        assert r_main.status_code == 404
        assert "Unknown API path" in r_main.json()["detail"]


def test_main_app_has_error_handlers_registered():
    """Verify that backend.api.main.app has production error handlers installed."""
    from starlette.exceptions import HTTPException as StarletteHTTPException
    from fastapi.exceptions import RequestValidationError

    assert app.exception_handlers.get(Exception) is unhandled_exception_handler
    assert app.exception_handlers.get(StarletteHTTPException) is http_exception_handler
    assert app.exception_handlers.get(RequestValidationError) is validation_exception_handler


@pytest.mark.asyncio
async def test_sanitize_error_detail_strips_secrets():
    """sanitize_error_detail scrubs configured admin tokens and keys."""
    raw = f"Failed to authenticate with token: {config.ADMIN_TOKEN}"
    scrubbed = sanitize_error_detail(raw)
    assert config.ADMIN_TOKEN not in scrubbed
    assert "<REDACTED>" in scrubbed


# ---------------------------------------------------------------------------
# 4. Request Validation Error (422) Input Stripping
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_validation_error_strips_raw_sensitive_input(client):
    """422 RequestValidationError omits raw password/token input strings from response."""
    secret_password = "MySuperSecretPassword#2026!"
    r = await client.post(
        "/api/test-errors/validate-sensitive-payload",
        json={"username": "valid_user", "token": secret_password * 4},  # exceeds max_length 32
    )
    assert r.status_code == 422
    data = r.json()

    # The raw secret password must NOT appear anywhere in the 422 response body
    assert secret_password not in r.text
    for err in data["detail"]:
        assert "input" not in err, f"Found raw 'input' field in error: {err}"
        assert "loc" in err
        assert "msg" in err


# ---------------------------------------------------------------------------
# 5. Secret Redactor Filter in Logging
# ---------------------------------------------------------------------------

def test_secret_redactor_masks_admin_token():
    """SecretRedactorFilter masks the configured ADMIN_TOKEN in log records."""
    redactor = SecretRedactorFilter()
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname="test.py",
        lineno=10,
        msg=f"Operator token {config.ADMIN_TOKEN} rejected during auth",
        args=(),
        exc_info=None,
    )
    assert redactor.filter(record) is True
    assert config.ADMIN_TOKEN not in record.getMessage()
    assert "<REDACTED-SECRET>" in record.getMessage()


def test_secret_redactor_masks_bearer_and_urls():
    """SecretRedactorFilter masks Bearer tokens and basic auth URLs in logs."""
    redactor = SecretRedactorFilter()
    msg = "Call to https://user:pass12345@rpc.solana.com with Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname="test.py",
        lineno=10,
        msg=msg,
        args=(),
        exc_info=None,
    )
    assert redactor.filter(record) is True
    formatted = record.getMessage()
    assert "pass12345" not in formatted
    assert "***:***@" in formatted
    assert "eyJhbGci" not in formatted
    assert "Bearer <REDACTED>" in formatted


def test_secret_redactor_masks_solana_keypair():
    """SecretRedactorFilter masks 64-integer Solana wallet keypairs."""
    keypair_array = str(list(range(64)))
    redactor = SecretRedactorFilter()
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname="test.py",
        lineno=10,
        msg=f"Loaded keypair: {keypair_array}",
        args=(),
        exc_info=None,
    )
    assert redactor.filter(record) is True
    formatted = record.getMessage()
    assert "[<REDACTED-KEYPAIR>]" in formatted


# ---------------------------------------------------------------------------
# 6. Debug Mode Toggle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_debug_mode_includes_debug_message(client, monkeypatch):
    """When config.DEBUG is True, debug_message is included for local development."""
    monkeypatch.setattr(config, "DEBUG", True)
    r = await client.get("/api/test-errors/crash-zero-division")
    assert r.status_code == 500
    data = r.json()
    assert "debug_message" in data
    assert "division by zero" in data["debug_message"]
    assert data["exception_type"] == "ZeroDivisionError"
