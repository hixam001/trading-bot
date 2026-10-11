"""
backend/tests/test_audit_77_e.py — tests for Master Prompt §77 Phase E:
Hardening (runtime secret registration, broader JSON redaction, postgres reset_book,
websocket per-IP cap, CORS wildcard guard, session revocation).
"""
import asyncio
import json
import logging
import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi import FastAPI
from starlette.testclient import TestClient

import config
from api.error_handler import SecretRedactorFilter, register_secret, sanitize_error_detail
from api import db_pg
from api.websocket import FeedBroadcaster, MAX_WS_PER_IP
from api.auth import (
    create_session_token,
    verify_session_token,
    revoke_session_token,
    is_session_revoked,
)


# --- E1: Log redaction gaps ---------------------------------------------------

def test_runtime_secret_registration():
    """register_secret registers runtime secrets and masks them in subsequent logs."""
    runtime_token = "dynamic_runtime_privy_token_987654"
    register_secret(runtime_token)

    redactor = SecretRedactorFilter()
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname="test.py",
        lineno=10,
        msg=f"Privy rotated token to {runtime_token} successfully",
        args=(),
        exc_info=None,
    )
    assert redactor.filter(record) is True
    assert runtime_token not in record.getMessage()
    assert "<REDACTED-SECRET>" in record.getMessage()

    # Also verified in sanitize_error_detail
    scrubbed = sanitize_error_detail(f"Rotated key: {runtime_token}")
    assert runtime_token not in scrubbed
    assert "<REDACTED>" in scrubbed


def test_json_quoted_secret_redaction():
    """JSON-quoted secret keys like {"api_key": "secret"} are masked in logs."""
    redactor = SecretRedactorFilter()
    json_msg = 'Payload received: {"api_key": "my-secret-api-key-12345", "status": "ok"}'  # gitleaks:allow
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg=json_msg,
        args=(),
        exc_info=None,
    )
    assert redactor.filter(record) is True
    assert "my-secret-api-key-12345" not in record.getMessage()
    assert '"api_key": "<REDACTED>"' in record.getMessage()


# --- E2: Postgres reset_book --------------------------------------------------

@pytest.mark.asyncio
async def test_db_pg_reset_book_transaction_and_timestamp():
    """reset_book runs inside a transaction and uses valid ISO timestamp."""
    executed_queries = []
    transaction_entered = False
    transaction_exited = False

    class MockTransaction:
        async def __aenter__(self):
            nonlocal transaction_entered
            transaction_entered = True
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            nonlocal transaction_exited
            transaction_exited = True

    mock_conn = MagicMock()
    mock_conn.transaction = MagicMock(return_value=MockTransaction())

    async def mock_fetchrow(query, *args):
        executed_queries.append((query, args))
        return {"n": 5}

    async def mock_execute(query, *args):
        executed_queries.append((query, args))
        return "UPDATE 1"

    mock_conn.fetchrow = AsyncMock(side_effect=mock_fetchrow)
    mock_conn.execute = AsyncMock(side_effect=mock_execute)

    result = await db_pg.reset_book(mock_conn, initial_cash_usd=1000.0)

    assert result["reset"] is True
    assert result["initial_cash_usd"] == 1000.0
    assert transaction_entered is True
    assert transaction_exited is True

    # Verify timestamp in UPDATE statement
    update_calls = [q for q in executed_queries if "UPDATE portfolio_state" in q[0]]
    assert len(update_calls) == 1
    query, args = update_calls[0]
    assert args[0] == 1000.0
    # args[1] must be a valid ISO format timestamp
    iso_ts = args[1]
    from datetime import datetime
    parsed = datetime.fromisoformat(iso_ts)
    assert parsed.year >= 2026


# --- E3: WebSocket per-IP cap -------------------------------------------------

@pytest.mark.asyncio
async def test_websocket_per_ip_cap():
    """WebSocket per-IP limit rejects connection #5 with code 1008 and frees slot on disconnect."""
    broadcaster = FeedBroadcaster()

    class MockWS:
        def __init__(self):
            self.accepted = False
            self.closed = False
            self.close_code = None
            self.close_reason = None

        async def accept(self):
            self.accepted = True

        async def close(self, code=1000, reason=""):
            self.closed = True
            self.close_code = code
            self.close_reason = reason

    ip1 = "192.168.1.10"
    ip2 = "192.168.1.20"

    clients_ip1 = [MockWS() for _ in range(4)]
    # First 4 from ip1 succeed
    for ws in clients_ip1:
        assert await broadcaster.connect(ws, client_ip=ip1) is True
        assert ws.accepted is True

    # 5th connection from ip1 is rejected with code 1008
    ws_rejected = MockWS()
    assert await broadcaster.connect(ws_rejected, client_ip=ip1) is False
    assert ws_rejected.closed is True
    assert ws_rejected.close_code == 1008

    # Connection from different IP succeeds
    ws_ip2 = MockWS()
    assert await broadcaster.connect(ws_ip2, client_ip=ip2) is True
    assert ws_ip2.accepted is True

    # Disconnect one from ip1, now a new connection from ip1 succeeds
    broadcaster.disconnect(clients_ip1[0], client_ip=ip1)
    ws_new = MockWS()
    assert await broadcaster.connect(ws_new, client_ip=ip1) is True
    assert ws_new.accepted is True


# --- E4: CORS wildcard with credentials guard ---------------------------------

def test_cors_wildcard_stripped_when_credentials_allowed(monkeypatch):
    """Wildcard '*' is stripped from allow_origins when credentials are True."""
    from fastapi.middleware.cors import CORSMiddleware
    from api.main import app

    cors_middlewares = [m for m in app.user_middleware if m.cls == CORSMiddleware]
    assert len(cors_middlewares) > 0
    middleware_kwargs = cors_middlewares[0].kwargs
    assert "*" not in middleware_kwargs["allow_origins"]
    assert middleware_kwargs["allow_credentials"] is True


# --- E5: Session revocation on logout -----------------------------------------

def test_session_revocation_on_logout():
    """Revoked session token fails verify_session_token."""
    secret = "a" * 32
    token = create_session_token(secret=secret)
    assert verify_session_token(token, secret=secret) is True

    # Revoke token
    revoke_session_token(token)
    assert is_session_revoked(token) is True
    assert verify_session_token(token, secret=secret) is False
