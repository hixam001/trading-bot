"""
tests/test_security_audit_remediations.py — Automated verification of SEC-01 through SEC-07.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch


import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from fastapi.testclient import TestClient

import config
from api.main import app
from data_providers.discovery import is_valid_solana_address
from rule_engine import liveness


# ---------------------------------------------------------------------------
# SEC-01: Blind transaction signing & program whitelist
# ---------------------------------------------------------------------------

def test_sec01_blind_signing_rejects_rogue_program():
    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction
    from solders.message import MessageV0
    from solders.instruction import Instruction, AccountMeta
    from solders.pubkey import Pubkey
    from solders.hash import Hash
    from live_execution.jupiter_executor import inspect_swap_transaction, ExecutionError

    payer = Keypair()
    # Rogue / unapproved drainer program ID
    rogue_prog = Pubkey.from_string("Drainer111111111111111111111111111111111111")
    ix = Instruction(rogue_prog, b"", [AccountMeta(payer.pubkey(), True, False)])
    msg = MessageV0.try_compile(payer.pubkey(), [ix], [], Hash.default())
    tx = VersionedTransaction(msg, [payer])

    with pytest.raises(ExecutionError, match="unapproved program ID"):
        inspect_swap_transaction(tx, str(payer.pubkey()))


def test_sec01_blind_signing_accepts_jupiter_v6():
    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction
    from solders.message import MessageV0
    from solders.instruction import Instruction, AccountMeta
    from solders.pubkey import Pubkey
    from solders.hash import Hash
    from live_execution.jupiter_executor import inspect_swap_transaction

    payer = Keypair()
    # Approved Jupiter V6 Aggregator
    jup_prog = Pubkey.from_string("JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4")
    ix = Instruction(jup_prog, b"", [AccountMeta(payer.pubkey(), True, False)])
    msg = MessageV0.try_compile(payer.pubkey(), [ix], [], Hash.default())
    tx = VersionedTransaction(msg, [payer])

    # Must pass without raising
    inspect_swap_transaction(tx, str(payer.pubkey()))


# ---------------------------------------------------------------------------
# SEC-02: Live Book Access Control
# ---------------------------------------------------------------------------

def test_sec02_live_book_requires_auth_when_not_public(monkeypatch):
    from api.routes.live_book import _check_access

    monkeypatch.setattr(config, "LIVE_BOOK_PUBLIC", False)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "secret-token")

    # 1. External client without token -> 403
    req_external = MagicMock()
    req_external.client.host = "203.0.113.195"  # public IP
    req_external.headers = {}
    with pytest.raises(HTTPException) as exc:
        _check_access(req_external)
    assert exc.value.status_code == 403

    # 2. External client with valid X-Admin-Token -> pass
    req_with_token = MagicMock()
    req_with_token.client.host = "203.0.113.195"
    req_with_token.headers = {"X-Admin-Token": "secret-token"}
    _check_access(req_with_token)  # should not raise

    # 3. Loopback client -> pass
    req_loopback = MagicMock()
    req_loopback.client.host = "127.0.0.1"
    req_loopback.headers = {}
    _check_access(req_loopback)  # should not raise

    # 4. Public mode enabled -> pass for everyone
    monkeypatch.setattr(config, "LIVE_BOOK_PUBLIC", True)
    _check_access(req_external)  # should not raise


# ---------------------------------------------------------------------------
# SEC-03: Self-Regulating Break Clamping (DoS Mitigation)
# ---------------------------------------------------------------------------

def test_sec03_break_minutes_strictly_clamped(tmp_path, monkeypatch):
    monkeypatch.setattr(liveness, "_state_path", lambda: tmp_path / "break.json")

    # Multi-year prompt injection attempt
    liveness.set_break(True, minutes=525600, reason="injected DoS")
    state = liveness._read()
    assert state is not None
    assert state["minutes"] == liveness.MAX_BREAK_MINUTES  # 60
    assert state["break_until"] <= time.time() + 3605

    # Negative / zero minutes defaults to safe minimum or default
    liveness.set_break(True, minutes=-10, reason="negative")
    state = liveness._read()
    assert state["minutes"] == 15

    # Disarm
    liveness.set_break(False)
    assert liveness.is_on_break() is False


# ---------------------------------------------------------------------------
# SEC-04: WebSocket Origin Check & Capacity Cap
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sec04_websocket_origin_and_capacity(monkeypatch):
    from api.websocket import FeedBroadcaster, MAX_WS_CLIENTS, websocket_endpoint

    broadcaster = FeedBroadcaster()

    # 1. Reject unallowed origin
    ws_bad_origin = MagicMock()
    ws_bad_origin.headers = {"origin": "http://malicious-site.com"}
    ws_bad_origin.close = AsyncMock()

    await websocket_endpoint(ws_bad_origin, broadcaster)
    ws_bad_origin.close.assert_awaited_once_with(code=1008, reason="origin not allowed")

    # 2. Capacity cap
    for _ in range(MAX_WS_CLIENTS):
        dummy_ws = MagicMock()
        dummy_ws.accept = AsyncMock()
        await broadcaster.connect(dummy_ws)

    assert len(broadcaster._clients) == MAX_WS_CLIENTS

    # Next connection over limit must be rejected
    ws_overflow = MagicMock()
    ws_overflow.close = AsyncMock()
    ok = await broadcaster.connect(ws_overflow)
    assert ok is False
    ws_overflow.close.assert_awaited_once_with(code=1008, reason="maximum connections reached")


# ---------------------------------------------------------------------------
# SEC-05: CORS Header Preflight Configuration
# ---------------------------------------------------------------------------

def test_sec05_cors_allows_admin_token_header():
    from fastapi.middleware.cors import CORSMiddleware
    cors = next(m for m in app.user_middleware if m.cls is CORSMiddleware)
    assert "X-Admin-Token" in cors.kwargs["allow_headers"]


# ---------------------------------------------------------------------------
# SEC-07: Solana Mint Address Validation
# ---------------------------------------------------------------------------

def test_sec07_solana_address_sanitization():
    # Valid Solana mint addresses
    assert is_valid_solana_address("So11111111111111111111111111111111111111112") is True
    assert is_valid_solana_address("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v") is True
    assert is_valid_solana_address("JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4") is True

    # Malicious or malformed inputs
    assert is_valid_solana_address("../../../etc/passwd") is False
    assert is_valid_solana_address("SELECT * FROM tokens") is False
    assert is_valid_solana_address("<script>alert(1)</script>") is False
    assert is_valid_solana_address("") is False
    assert is_valid_solana_address(None) is False  # type: ignore
    assert is_valid_solana_address("0" * 44) is False  # '0' is not in Base58 alphabet
    assert is_valid_solana_address("O" * 44) is False  # 'O' is not in Base58 alphabet
    assert is_valid_solana_address("I" * 44) is False  # 'I' is not in Base58 alphabet
    assert is_valid_solana_address("l" * 44) is False  # 'l' is not in Base58 alphabet
