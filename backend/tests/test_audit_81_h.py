"""
backend/tests/test_audit_81_h.py — Tests for Phase H (H-1 through H-5) (§81).

Covers:
- H-1: Alert channel defaults & guessable ntfy topic refusal.
- H-2: Bounded mint extension cache with LRU eviction.
- H-3: Single-pass position pricing in /api/stats (exactly N price calls for N positions).
- H-4: Offline banner condition in frontend/src/App.tsx.
- H-5: Unknown-security sizing haircut below floor emits distinct reason and is counted in gate funnel report.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from collections import OrderedDict
from pathlib import Path

import pytest
from starlette.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# H-1: Alert Channel Defaults & Guessable Topic Refusal
# ---------------------------------------------------------------------------

def test_h1_ntfy_guessable_topic_refused(monkeypatch):
    """
    H-1: Refuse guessable / shared default topics on public ntfy without auth.
    Mutation check: If `topic.lower() in FORBIDDEN_NTFY_TOPICS` check in
    `is_ntfy_misconfigured` is removed, guessable topics like 'alerts' return
    misconfigured=False.
    """
    import alerts

    # Test 1: Guessable topic on public ntfy with no token -> misconfigured
    monkeypatch.setenv("ALERT_NTFY_URL", "https://ntfy.sh")
    monkeypatch.setenv("ALERT_NTFY_TOPIC", "trading-bot")
    monkeypatch.setenv("ALERT_NTFY_TOKEN", "")

    assert alerts.is_ntfy_misconfigured() is True
    channels = alerts.get_configured_channels()
    assert "ntfy" not in channels
    status = alerts.get_alerts_status()
    assert status["alerts"] == "misconfigured"

    # Other forbidden topics
    for topic in ("alerts", "trading-bot-alerts", "default", "test"):
        monkeypatch.setenv("ALERT_NTFY_TOPIC", topic)
        assert alerts.is_ntfy_misconfigured() is True

    # Test 2: Unguessable private topic -> OK
    monkeypatch.setenv("ALERT_NTFY_TOPIC", "trading-bot-a7f49b12c8e3")
    assert alerts.is_ntfy_misconfigured() is False
    assert "ntfy" in alerts.get_configured_channels()
    assert alerts.get_alerts_status()["alerts"] == "ok"

    # Test 3: Authenticated custom/self-hosted server with token -> OK even with standard topic
    monkeypatch.setenv("ALERT_NTFY_URL", "https://private-ntfy.internal")
    monkeypatch.setenv("ALERT_NTFY_TOPIC", "alerts")
    monkeypatch.setenv("ALERT_NTFY_TOKEN", "secret-token-12345")
    assert alerts.is_ntfy_misconfigured() is False
    assert "ntfy" in alerts.get_configured_channels()


# ---------------------------------------------------------------------------
# H-2: Bounded Mint Extension Cache
# ---------------------------------------------------------------------------

def test_h2_mint_extension_cache_bounded():
    """
    H-2: Unbounded extension cache is capped to MAX_MINT_EXTENSION_CACHE (2048) with LRU eviction.
    Mutation check: If `_MINT_EXTENSION_CACHE.popitem(last=False)` is removed from
    `_put_extension_cache`, cache length grows past 2048 to 3000.
    """
    from data_providers.onchain_security import (
        MAX_MINT_EXTENSION_CACHE,
        _MINT_EXTENSION_CACHE,
        _put_extension_cache,
    )

    assert MAX_MINT_EXTENSION_CACHE == 2048

    # Reset cache for clean test
    _MINT_EXTENSION_CACHE.clear()

    # Insert 3000 distinct mints
    for i in range(3000):
        mint = f"MintAddress_{i:05d}"
        data = {"extension_flags": [f"flag_{i}"]}
        _put_extension_cache(mint, data, now=float(i))

    # Assert cache size is strictly bounded
    assert len(_MINT_EXTENSION_CACHE) == MAX_MINT_EXTENSION_CACHE
    assert len(_MINT_EXTENSION_CACHE) == 2048

    # Oldest entries (0 .. 951) must have been evicted
    assert "MintAddress_00000" not in _MINT_EXTENSION_CACHE
    assert "MintAddress_00500" not in _MINT_EXTENSION_CACHE
    assert "MintAddress_00951" not in _MINT_EXTENSION_CACHE

    # Newest entries (952 .. 2999) must be present
    assert "MintAddress_00952" in _MINT_EXTENSION_CACHE
    assert "MintAddress_02000" in _MINT_EXTENSION_CACHE
    assert "MintAddress_02999" in _MINT_EXTENSION_CACHE


# ---------------------------------------------------------------------------
# H-3: Single-Pass Position Pricing in /api/stats
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_h3_stats_single_pass_pricing(monkeypatch):
    """
    H-3: Merged pricing loop in /api/stats.
    With N open positions, get_current_price is called exactly N times (once per position).
    Mutation check: If the loop is duplicated back to two separate loops,
    get_current_price is called 2N times.
    """
    from api.main import app
    from models import Trade
    import config

    # Enable stats endpoint and admin token bypass
    monkeypatch.setattr(config, "ADMIN_TOKEN", "")

    client = TestClient(app)

    # Track calls per mint
    call_counts: dict[str, int] = {}

    class MockProvider:
        async def get_current_price(self, mint, decimals=None):
            call_counts[mint] = call_counts.get(mint, 0) + 1
            if "So111" in mint:
                return 150.0  # SOL price
            return 2.50       # Token price

    mock_p = MockProvider()
    app.state.provider = mock_p

    # Mock wallet cash and SOL balance
    async def mock_cash():
        return 1000.0
    async def mock_sol_bal(addr):
        return 1.0
    monkeypatch.setattr("api.routes.stats.get_wallet_cash_usd", mock_cash)
    from live_execution import solana, wallet
    monkeypatch.setattr(wallet, "public_address", lambda: "11111111111111111111111111111111")
    monkeypatch.setattr(solana, "get_sol_balance", mock_sol_bal)

    # Setup 3 open trades
    trades = [
        Trade(trade_id="t1", symbol="AAA", mint_address="mint_AAA", position_size_usd=10.0, quantity=4.0, entry_price_usd=2.50),
        Trade(trade_id="t2", symbol="BBB", mint_address="mint_BBB", position_size_usd=20.0, quantity=8.0, entry_price_usd=2.50),
        Trade(trade_id="t3", symbol="CCC", mint_address="mint_CCC", position_size_usd=30.0, quantity=12.0, entry_price_usd=2.50),
    ]

    from api import db
    async def mock_open_trades(conn):
        return trades
    async def mock_closed_trades(conn):
        return []
    monkeypatch.setattr(db, "get_open_trades", mock_open_trades)
    monkeypatch.setattr(db, "get_all_closed_trades", mock_closed_trades)

    resp = client.get("/api/stats")
    assert resp.status_code == 200
    data = resp.json()

    sol_mint = "So11111111111111111111111111111111111111112"
    # SOL price is fetched once
    assert call_counts[sol_mint] == 1

    # Exactly 1 call per open position (single pass, not double)
    assert call_counts["mint_AAA"] == 1
    assert call_counts["mint_BBB"] == 1
    assert call_counts["mint_CCC"] == 1

    # Total position pricing calls must equal len(trades)
    position_calls = sum(count for mint, count in call_counts.items() if mint != sol_mint)
    assert position_calls == len(trades) == 3

    assert data["equity_status"] == "complete"
    assert data["open_positions"] == 3


# ---------------------------------------------------------------------------
# H-4: Offline Banner Condition in Frontend
# ---------------------------------------------------------------------------

def test_h4_frontend_offline_condition_documented():
    """
    H-4: Document why status.error is excluded from global offline banner per DESIGN.md §3.3/§3.4.
    Secondary feed errors render localized ErrorPanels; primary feeds (liveBook, safety)
    drive the global offline risk banner.
    """
    app_tsx = (REPO_ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
    assert "DESIGN.md §3.4" in app_tsx
    assert "status.error" in app_tsx
    # Verify primary feeds are strictly required in the offline check
    pattern = r"if\s*\([^)]*liveBook\.error[^)]*safety\.error[^)]*\)"
    assert re.search(pattern, app_tsx) is not None


# ---------------------------------------------------------------------------
# H-5: Unknown-Security Sizing Haircut Below Floor Reason & Funnel Report
# ---------------------------------------------------------------------------

def test_h5_compute_ticket_security_unknown_below_floor_reason(monkeypatch):
    """
    H-5: compute_ticket emits 'security_unknown_below_floor' when 0.5x haircut drops ticket below floor.
    Mutation check: If the reason string is changed or returns None, this test fails.
    """
    from sizing import compute_ticket, compute_ticket_with_reason
    import config

    # When ticket is $30 and floor is $25:
    # Haircut: 30.0 * 0.5 = 15.0 < 25.0 -> drops to 0.0 with reason
    monkeypatch.setattr(config, "SIZING_MODE", "fixed")
    monkeypatch.setattr(config, "INTENDED_POSITION_SIZE_USD", 30.0)
    monkeypatch.setattr(config, "MIN_TICKET_USD", 25.0)

    t, reason = compute_ticket_with_reason(
        cash_usd=100.0,
        heat=None,
        min_ticket_usd=25.0,
        security_unknown=True,
    )
    assert t == 0.0
    assert reason == "security_unknown_below_floor"

    # Backward compatibility: return_reason=False returns float 0.0
    val = compute_ticket(
        cash_usd=100.0,
        heat=None,
        min_ticket_usd=25.0,
        security_unknown=True,
        return_reason=False,
    )
    assert val == 0.0
    assert isinstance(val, float)

    # When security_unknown is False, ticket stays at full $30 and reason is None
    t_known, reason_known = compute_ticket_with_reason(
        cash_usd=100.0,
        heat=None,
        min_ticket_usd=25.0,
        security_unknown=False,
    )
    assert t_known == 30.0
    assert reason_known is None


def test_h5_gate_funnel_report_counts_security_unknown_below_floor(tmp_path):
    """
    H-5: scripts/gate_funnel_report.py counts security_unknown_below_floor as its own line.
    Mutation check: If _is_security_unknown_below_floor check is removed, count is 0.
    """
    from gate_funnel_report import generate_funnel_report, format_text_report

    db_file = tmp_path / "test_funnel_h5.db"
    conn = sqlite3.connect(str(db_file))
    conn.executescript("""
    CREATE TABLE feed_events (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        ts                      TEXT    NOT NULL,
        symbol                  TEXT    NOT NULL,
        mint_address            TEXT    NOT NULL,
        candidate_snapshot      TEXT    NOT NULL,
        verdict                 TEXT    NOT NULL,
        thesis                  TEXT,
        rule_breakdown          TEXT    NOT NULL DEFAULT '[]',
        failed_rule_ids         TEXT    NOT NULL DEFAULT '[]',
        regime_ok               INTEGER NOT NULL DEFAULT 1,
        grounding_flags         TEXT    NOT NULL DEFAULT '[]',
        narration_source        TEXT    NOT NULL DEFAULT '',
        led_to_trade_id         TEXT,
        model_version           TEXT,
        prompt_version          TEXT,
        risk_flags              TEXT    NOT NULL DEFAULT '[]'
    );
    CREATE TABLE trades (
        trade_id                TEXT    PRIMARY KEY,
        symbol                  TEXT    NOT NULL,
        mint_address            TEXT    NOT NULL,
        opened_at               TEXT    NOT NULL,
        entry_price_usd         REAL    NOT NULL,
        position_size_usd       REAL    NOT NULL,
        quantity                REAL    NOT NULL,
        candidate_snapshot      TEXT    NOT NULL DEFAULT '{}',
        thesis                  TEXT    NOT NULL DEFAULT '',
        closed_at               TEXT,
        exit_price_usd          REAL,
        exit_reason             TEXT,
        realized_pnl_usd        REAL,
        realized_pnl_pct        REAL,
        is_open                 INTEGER NOT NULL DEFAULT 1
    );
    """)

    # Seed candidates:
    # C1: pass gate, pass think, entered trade
    # C2: pass gate, pass think, blocked by security_unknown_below_floor (snapshot has security_unknown=True, no trade)
    # C3: pass gate, pass think, blocked by security_unknown_below_floor (explicit sizing_reason)
    # C4: failed gate rule_A
    rows = [
        (
            "2026-09-01T10:00:00+00:00", "SYM1", "mint1",
            json.dumps({"security_unknown": False}), "pass", "thesis1",
            json.dumps([]), json.dumps([]), "t1",
        ),
        (
            "2026-09-01T11:00:00+00:00", "SYM2", "mint2",
            json.dumps({"security_unknown": True}), "pass", "thesis2",
            json.dumps([]), json.dumps([]), None,
        ),
        (
            "2026-09-01T12:00:00+00:00", "SYM3", "mint3",
            json.dumps({"sizing_reason": "security_unknown_below_floor"}), "pass", "thesis3",
            json.dumps([]), json.dumps([]), None,
        ),
        (
            "2026-09-01T13:00:00+00:00", "SYM4", "mint4",
            json.dumps({"security_unknown": True}), "fail", "thesis4",
            json.dumps([]), json.dumps(["liquidity_floor"]), None,
        ),
    ]

    for r in rows:
        conn.execute(
            """
            INSERT INTO feed_events (
                ts, symbol, mint_address, candidate_snapshot, verdict, thesis,
                rule_breakdown, failed_rule_ids, led_to_trade_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            r,
        )

    conn.execute(
        """
        INSERT INTO trades (
            trade_id, symbol, mint_address, opened_at, entry_price_usd, position_size_usd, quantity
        ) VALUES ('t1', 'SYM1', 'mint1', '2026-09-01T10:00:05+00:00', 1.0, 10.0, 10.0)
        """
    )
    conn.commit()
    conn.close()

    report = generate_funnel_report(db_path=db_file)

    assert report["total_candidates"] == 4
    assert report["baseline_summary"]["gate_passed"] == 3
    assert report["baseline_summary"]["think_passed"] == 3
    assert report["baseline_summary"]["entered"] == 1

    # Exactly 2 blocked by security_unknown_below_floor (C2 and C3)
    assert report["security_unknown_below_floor"] == 2
    assert report["baseline_summary"]["security_unknown_below_floor"] == 2

    text = format_text_report(report)
    assert "Sizing blocked (security_unknown_below_floor): 2" in text
