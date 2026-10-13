"""
backend/tests/test_audit_79_f.py — Tests for honest observability & config consistency (§79 Phase F).

Covers:
1. Safety counters: atomic persistence, category tallies, rolling 24h window, missing-file handling (null => unavailable).
2. /api/safety exposes safety_counters honestly (null when file missing, dict when present).
3. /api/stats real equity: wallet_equity_usd, equity_status, missing component list (null when any component is missing).
4. One source of truth for trusted proxies: FORWARDED_ALLOW_IPS / TRUSTED_PROXY_CIDRS parity, 10.x untrusted by default.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import pytest
from starlette.testclient import TestClient

import config
from live_execution.safety_counters import record_safety_refusal, load_safety_counters
from api.rate_limiter import get_client_ip, _get_trusted_proxy_networks


def test_safety_counters_persistence_and_categories(tmp_path):
    """
    Test recording and reading safety counters by category.
    Mutation check: If `data['total'][cat] += 1` in safety_counters.py is removed,
    counts stay 0 and fail assertions.
    """
    state_dir = tmp_path / "state"

    # Missing file returns None (zero-vs-None rule: null => unavailable)
    assert load_safety_counters(state_dir) is None

    # Record refusals across all categories
    t0 = 1000000.0
    record_safety_refusal("sim_refused", "slippage exceeded", state_dir=state_dir, now_fn=lambda: t0)
    record_safety_refusal("guard_refused", "unapproved instruction", state_dir=state_dir, now_fn=lambda: t0 + 10)
    record_safety_refusal("quorum_failed", "RPC pre-state divergence", state_dir=state_dir, now_fn=lambda: t0 + 20)
    record_safety_refusal("quote_mismatch", "inAmount mismatch", state_dir=state_dir, now_fn=lambda: t0 + 30)
    record_safety_refusal("sim_refused", "insufficient output", state_dir=state_dir, now_fn=lambda: t0 + 40)

    counters = load_safety_counters(state_dir, now_fn=lambda: t0 + 50)
    assert counters is not None
    assert counters["sim_refused"] == 2
    assert counters["guard_refused"] == 1
    assert counters["quorum_failed"] == 1
    assert counters["quote_mismatch"] == 1
    assert counters["last_reason"] == "insufficient output"
    assert counters["rolling_24h"]["sim_refused"] == 2

    # Advance time past 24h (86400s) -> rolling_24h counts decay
    counters_future = load_safety_counters(state_dir, now_fn=lambda: t0 + 90000.0)
    assert counters_future is not None
    assert counters_future["sim_refused"] == 2  # Total unchanged
    assert counters_future["rolling_24h"]["sim_refused"] == 0  # Rolling decayed


def test_api_safety_reflects_safety_counters(tmp_path, monkeypatch):
    """
    /api/safety must surface safety_counters as None when unavailable,
    and as populated metrics when safety_counters.json exists.
    """
    from api.main import app

    state_dir = tmp_path / "api_safety_state"
    state_dir.mkdir()
    monkeypatch.setattr("live_execution.config.STATE_DIR", state_dir)
    monkeypatch.setenv("LIVE_BOOK_PUBLIC", "1")
    monkeypatch.setattr(config, "LIVE_BOOK_PUBLIC", True)

    client = TestClient(app)

    # 1. Missing file -> safety_counters is None
    resp = client.get("/api/safety")
    assert resp.status_code == 200
    data = resp.json()
    assert data["safety_counters"] is None

    # 2. Populated counters -> returned verbatim
    record_safety_refusal("guard_refused", "lookup table forbidden", state_dir=state_dir)
    resp2 = client.get("/api/safety")
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["safety_counters"] is not None
    assert data2["safety_counters"]["guard_refused"] >= 1
    assert "lookup table forbidden" in data2["safety_counters"]["last_reason"]


@pytest.mark.asyncio
async def test_stats_real_equity_and_status(monkeypatch):
    """
    /api/stats returns wallet_equity_usd, equity_status, missing.
    Rule: If any component is None, the total is None, never a sum of the knowns.
    Mutation check: If `if not missing: ... else: wallet_equity_usd = None` is removed,
    partial sums will leak to wallet_equity_usd.
    """
    from api.main import app
    from models import Trade

    client = TestClient(app)

    class MockProvider:
        async def get_current_price(self, mint, decimals=None):
            return None

    mock_p = MockProvider()
    app.state.provider = mock_p

    # Case A: Partial / Missing (e.g. SOL price unavailable)
    # mock cash available, but sol_balance or sol_price is None
    async def mock_cash():
        return 500.0

    async def mock_sol_price(mint, decimals=None):
        return None  # Price unavailable

    monkeypatch.setattr("api.routes.stats.get_wallet_cash_usd", mock_cash)
    monkeypatch.setattr(mock_p, "get_current_price", mock_sol_price)

    resp = client.get("/api/stats")
    assert resp.status_code == 200
    data = resp.json()

    assert "wallet_equity_usd" in data
    assert "equity_status" in data
    assert "missing" in data

    # Any missing component must result in wallet_equity_usd is None
    assert data["wallet_equity_usd"] is None
    assert data["equity_status"] in ("partial", "unavailable")
    assert any("sol" in m for m in data["missing"])

    # Case B: Complete truth
    async def mock_sol_bal(addr):
        return 2.0

    async def mock_complete_price(mint, decimals=None):
        if "So111" in mint:
            return 150.0  # SOL price
        return 1.0  # token price

    from live_execution import solana, wallet
    class MockKeypair:
        pass
    monkeypatch.setattr(wallet, "load_keypair", lambda: MockKeypair())
    monkeypatch.setattr(wallet, "pubkey_string", lambda kp: "11111111111111111111111111111111")
    monkeypatch.setattr(solana, "get_sol_balance", mock_sol_bal)
    monkeypatch.setattr(mock_p, "get_current_price", mock_complete_price)

    # Also mock empty open trades so no missing position marks
    from api import db
    async def mock_open_trades(conn):
        return []
    monkeypatch.setattr(db, "get_open_trades", mock_open_trades)

    resp_complete = client.get("/api/stats")
    assert resp_complete.status_code == 200
    data_complete = resp_complete.json()
    assert data_complete["equity_status"] == "complete"
    assert data_complete["missing"] == []
    # 500 cash + 2 SOL * 150 = 800.0
    assert data_complete["wallet_equity_usd"] == 800.0


def test_trusted_proxy_cidrs_parity_and_isolation(monkeypatch):
    """
    Default trusted proxy networks:
    - 127.0.0.1, 172.16.0.0/12, ::1/128
    - 10.0.0.0/8 must NOT be trusted by default (Oracle VCN isolation).
    - Setting FORWARDED_ALLOW_IPS or TRUSTED_PROXY_CIDRS overrides it.
    """
    # 1. Parity check between config and entrypoint default
    assert "10.0.0.0" not in config.DEFAULT_FORWARDED_ALLOW_IPS
    assert "172.16.0.0/12" in config.DEFAULT_FORWARDED_ALLOW_IPS

    # 2. Direct request from 10.0.0.5 is untrusted by default
    class MockClient:
        host = "10.0.0.5"

    class MockRequest:
        client = MockClient()
        headers = {"x-forwarded-for": "198.51.100.42"}

    req = MockRequest()
    # By default, direct peer 10.0.0.5 is untrusted -> returns 10.0.0.5 (ignores forged XFF)
    ip_default = get_client_ip(req)
    assert ip_default == "10.0.0.5"

    # 3. Direct request from Docker bridge 172.18.0.2 is trusted by default -> unpacks XFF
    class MockDockerBridgeClient:
        host = "172.18.0.2"

    req_docker = MockRequest()
    req_docker.client = MockDockerBridgeClient()
    req_docker.headers = {"x-forwarded-for": "203.0.113.19"}
    assert get_client_ip(req_docker) == "203.0.113.19"

    # 4. Explicitly trusting 10.0.0.0/16 enables proxy trust
    monkeypatch.setattr(config, "TRUSTED_PROXY_CIDRS", f"{config.DEFAULT_FORWARDED_ALLOW_IPS},10.0.0.0/16")
    ip_trusted_vcn = get_client_ip(req)
    assert ip_trusted_vcn == "198.51.100.42"
