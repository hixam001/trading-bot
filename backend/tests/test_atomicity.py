"""
tests/test_atomicity.py — Tests for the atomic write ordering in
paper_trading_engine.open_position() and close_position().

Defense-first rule 4: every money-math change needs a test with a
known-correct expected output. These tests specifically verify that:

  1. A double-close call (simulating a restart/race after the first close)
     credits cash exactly once, not twice.
  2. open_position() inserts the trade record before deducting cash,
     so a crash-between-writes leaves a detectable (not silent) state.

These tests use an in-memory aiosqlite database so they don't touch the
production trading_bot.db. All tests are fully self-contained.

Run: pytest tests/test_atomicity.py -v
"""
from __future__ import annotations

import asyncio
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import aiosqlite

# ---------------------------------------------------------------------------
# Minimal in-memory schema — mirrors the production schema for the tables
# we exercise here.
# ---------------------------------------------------------------------------

_TEST_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS trades (
    trade_id                TEXT    PRIMARY KEY,
    symbol                  TEXT    NOT NULL,
    mint_address            TEXT    NOT NULL,
    opened_at               TEXT    NOT NULL,
    entry_price_usd         REAL    NOT NULL,
    position_size_usd       REAL    NOT NULL,
    quantity                REAL    NOT NULL,
    candidate_snapshot      TEXT    NOT NULL DEFAULT '{}',
    verdict_snapshot        TEXT    NOT NULL DEFAULT '{}',
    invalidation_condition  TEXT,
    closed_at               TEXT,
    exit_price_usd          REAL,
    exit_reason             TEXT,
    realized_pnl_usd        REAL,
    realized_pnl_pct        REAL,
    is_open                 INTEGER NOT NULL DEFAULT 1,
    reflection_text         TEXT
);

CREATE TABLE IF NOT EXISTS portfolio_state (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    cash_usd    REAL    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_is_open   ON trades(is_open);
CREATE INDEX IF NOT EXISTS idx_trades_mint_open ON trades(mint_address, is_open);
"""

INITIAL_CASH = 1000.0


async def _make_test_db() -> aiosqlite.Connection:
    """Open an in-memory database, initialise schema, seed cash."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(_TEST_SCHEMA)
    await conn.execute(
        "INSERT INTO portfolio_state (id, cash_usd, updated_at) VALUES (1, ?, '2026-01-01T00:00:00+00:00')",
        (INITIAL_CASH,),
    )
    await conn.commit()
    return conn


async def _insert_open_trade(conn: aiosqlite.Connection, trade_id: str) -> None:
    """Insert a minimal open trade row directly (bypasses engine, tests db layer)."""
    await conn.execute(
        """
        INSERT INTO trades
            (trade_id, symbol, mint_address, opened_at, entry_price_usd,
             position_size_usd, quantity, candidate_snapshot, verdict_snapshot,
             invalidation_condition, is_open)
        VALUES (?, 'TEST', 'mintABC', '2026-01-01T00:00:00+00:00',
                0.001, 100.0, 100000.0, '{}', '{}', '', 1)
        """,
        (trade_id,),
    )
    await conn.commit()


# ---------------------------------------------------------------------------
# Import the modules under test AFTER sys.path is set up.
# We patch config constants that the engine reads at import time.
# ---------------------------------------------------------------------------

import config  # noqa: E402

# Patch only the constants actually used by engine/db math — do NOT override
# config.DB_PATH here, as that leaks into other test modules collected in the
# same pytest session. Our tests pass aiosqlite connections directly, so the
# DB_PATH setting is irrelevant — we never call aiosqlite.connect(config.DB_PATH).
config.SLIPPAGE_PCT = 0.02
config.FEE_PCT = 0.01
config.POSITION_SIZE_PCT = 0.10
config.INITIAL_CASH_USD = INITIAL_CASH

from api import db                             # noqa: E402
from models import Trade                       # noqa: E402
import paper_trading_engine as engine          # noqa: E402


# ---------------------------------------------------------------------------
# Test: double-close credits cash exactly once
# ---------------------------------------------------------------------------

class TestDoubleClose:
    """
    Simulates a process restart or race where close_position() is called
    twice on the same trade. Cash must be credited exactly once.

    The fix: close_trade_in_db() uses WHERE is_open=1 and returns rowcount.
    close_position() checks rowcount and skips the cash credit if 0.
    """

    @pytest.mark.asyncio
    async def test_double_close_credits_cash_once(self):
        conn = await _make_test_db()
        trade_id = "trade-double-close-001"

        # Set up: deduct cash for the position, insert open trade
        await conn.execute(
            "UPDATE portfolio_state SET cash_usd = 900.0 WHERE id = 1"
        )
        await conn.commit()
        await _insert_open_trade(conn, trade_id)

        # Build a Trade object mirroring the DB row
        trade = Trade()
        trade.trade_id = trade_id
        trade.symbol = "TEST"
        trade.mint_address = "mintABC"
        trade.entry_price_usd = 0.001
        trade.position_size_usd = 100.0
        trade.quantity = 100_000.0
        trade.opened_at = "2026-01-01T00:00:00+00:00"
        trade.is_open = True

        exit_price = 0.0015  # +50% gross
        expected_pnl_usd, _ = engine.compute_realized_pnl(trade, exit_price)
        expected_proceeds = trade.position_size_usd + expected_pnl_usd

        # --- First close: should succeed and credit cash ---
        closed_trade = await engine.close_position(conn, trade, exit_price, "take_profit")
        assert not closed_trade.is_open
        assert closed_trade.realized_pnl_usd is not None

        cash_after_first_close = await db.get_cash_balance(conn)
        expected_cash_after_first = 900.0 + expected_proceeds
        assert abs(cash_after_first_close - expected_cash_after_first) < 0.01, (
            f"Expected cash ~{expected_cash_after_first:.4f} after first close, "
            f"got {cash_after_first_close:.4f}"
        )

        # --- Second close: simulate restart/race — trade.is_open is still True
        #     in our local object, but the DB has is_open=0.
        #     close_position should detect rows_affected=0 and NOT credit cash again.
        trade2 = Trade()
        trade2.trade_id = trade_id
        trade2.symbol = "TEST"
        trade2.mint_address = "mintABC"
        trade2.entry_price_usd = 0.001
        trade2.position_size_usd = 100.0
        trade2.quantity = 100_000.0
        trade2.opened_at = "2026-01-01T00:00:00+00:00"
        trade2.is_open = True  # stale in-memory state — DB says is_open=0

        second_result = await engine.close_position(conn, trade2, exit_price, "take_profit")
        cash_after_second_close = await db.get_cash_balance(conn)

        assert abs(cash_after_second_close - cash_after_first_close) < 0.001, (
            f"Double-close credited cash twice! "
            f"After first: ${cash_after_first_close:.4f}, "
            f"After second: ${cash_after_second_close:.4f}. "
            f"Difference: ${cash_after_second_close - cash_after_first_close:.4f}"
        )

        # The returned trade from the second call should be the already-closed one
        assert not second_result.is_open
        assert second_result.trade_id == trade_id

        await conn.close()

    @pytest.mark.asyncio
    async def test_single_close_credits_cash_correctly(self):
        """Sanity check: a single genuine close credits the right amount."""
        conn = await _make_test_db()
        trade_id = "trade-single-close-001"

        await conn.execute("UPDATE portfolio_state SET cash_usd = 900.0 WHERE id = 1")
        await conn.commit()
        await _insert_open_trade(conn, trade_id)

        trade = Trade()
        trade.trade_id = trade_id
        trade.symbol = "TEST"
        trade.mint_address = "mintABC"
        trade.entry_price_usd = 0.001
        trade.position_size_usd = 100.0
        trade.quantity = 100_000.0
        trade.opened_at = "2026-01-01T00:00:00+00:00"
        trade.is_open = True

        exit_price = 0.0015
        pnl_usd, pnl_pct = engine.compute_realized_pnl(trade, exit_price)
        proceeds = trade.position_size_usd + pnl_usd

        closed = await engine.close_position(conn, trade, exit_price, "take_profit")

        cash = await db.get_cash_balance(conn)
        assert abs(cash - (900.0 + proceeds)) < 0.01
        assert closed.realized_pnl_usd is not None
        assert abs(closed.realized_pnl_usd - pnl_usd) < 0.001

        await conn.close()


# ---------------------------------------------------------------------------
# Test: db.close_trade_in_db() returns correct rowcount
# ---------------------------------------------------------------------------

class TestCloseTradeInDbRowcount:
    """Unit tests for the db layer's rowcount return value."""

    @pytest.mark.asyncio
    async def test_returns_1_on_genuine_close(self):
        conn = await _make_test_db()
        trade_id = "trade-rowcount-001"
        await _insert_open_trade(conn, trade_id)

        rows = await db.close_trade_in_db(
            conn,
            trade_id=trade_id,
            closed_at="2026-01-02T00:00:00+00:00",
            exit_price_usd=0.002,
            exit_reason="take_profit",
            realized_pnl_usd=50.0,
            realized_pnl_pct=50.0,
        )
        assert rows == 1, f"Expected rowcount=1 on genuine close, got {rows}"
        await conn.close()

    @pytest.mark.asyncio
    async def test_returns_0_on_already_closed(self):
        conn = await _make_test_db()
        trade_id = "trade-rowcount-002"
        await _insert_open_trade(conn, trade_id)

        # First close
        await db.close_trade_in_db(
            conn,
            trade_id=trade_id,
            closed_at="2026-01-02T00:00:00+00:00",
            exit_price_usd=0.002,
            exit_reason="take_profit",
            realized_pnl_usd=50.0,
            realized_pnl_pct=50.0,
        )

        # Second close — should be a no-op, rowcount=0
        rows = await db.close_trade_in_db(
            conn,
            trade_id=trade_id,
            closed_at="2026-01-03T00:00:00+00:00",
            exit_price_usd=0.002,
            exit_reason="take_profit",
            realized_pnl_usd=50.0,
            realized_pnl_pct=50.0,
        )
        assert rows == 0, f"Expected rowcount=0 on already-closed trade, got {rows}"
        await conn.close()

    @pytest.mark.asyncio
    async def test_returns_0_on_nonexistent_trade(self):
        conn = await _make_test_db()

        rows = await db.close_trade_in_db(
            conn,
            trade_id="does-not-exist",
            closed_at="2026-01-02T00:00:00+00:00",
            exit_price_usd=0.001,
            exit_reason="timeout",
            realized_pnl_usd=0.0,
            realized_pnl_pct=0.0,
        )
        assert rows == 0
        await conn.close()


# ---------------------------------------------------------------------------
# Test: open_position() write order — trade inserted before cash deducted
# ---------------------------------------------------------------------------

class TestOpenPositionOrder:
    """
    Verifies that after open_position(), both the trade record exists AND
    cash has been deducted. The implementation now inserts the trade first,
    then deducts cash — but the observable postcondition is the same either
    way. What matters is that a failure to deduct cash leaves the trade
    visible in the DB (detectable), rather than the old bug where a failure
    to insert the trade left cash gone with no record.

    We test the happy path here. The failure-ordering property is structural
    (proven by code review of the swap) rather than injectable in a pure
    async test without process-kill simulation.
    """

    @pytest.mark.asyncio
    async def test_open_position_creates_trade_and_deducts_cash(self):
        from models import Candidate, Verdict

        conn = await _make_test_db()

        candidate = Candidate(
            symbol="TESTTOKEN",
            mint_address="mintXYZ999",
            price_usd=0.001,
            liquidity_usd=100_000.0,
            volume_24h_usd=50_000.0,
            holder_count=1000,
            top_holder_pct=5.0,
            age_hours=24.0,
            market_cap_usd=200_000.0,
            source="test",
        )
        verdict = Verdict(
            candidate=candidate,
            verdict="pass",
            confidence=0.8,
            risk_flags=[],
            thesis="Test thesis.",
            entry_condition="Entry at current price.",
            invalidation_condition="Price drops 20%.",
        )

        trade = await engine.open_position(conn, candidate, verdict)
        assert trade is not None, "Expected a trade to be created"

        # Cash should have been deducted
        cash = await db.get_cash_balance(conn)
        assert cash < INITIAL_CASH, f"Cash not deducted: still {cash}"
        assert abs(cash - (INITIAL_CASH - trade.position_size_usd)) < 0.01

        # Trade must exist in DB
        from_db = await db.get_trade_by_id(conn, trade.trade_id)
        assert from_db is not None, "Trade not found in DB after open_position"
        assert from_db.is_open
        assert from_db.symbol == "TESTTOKEN"

        await conn.close()

    @pytest.mark.asyncio
    async def test_open_position_idempotent_on_same_mint(self):
        """Second open_position on same mint address returns None (idempotency guard)."""
        from models import Candidate, Verdict

        conn = await _make_test_db()

        def _make_candidate():
            return Candidate(
                symbol="DUPTEST",
                mint_address="mintDUP111",
                price_usd=0.001,
                liquidity_usd=100_000.0,
                volume_24h_usd=50_000.0,
                holder_count=1000,
                top_holder_pct=5.0,
                age_hours=24.0,
                market_cap_usd=200_000.0,
                source="test",
            )

        def _make_verdict(c):
            return Verdict(
                candidate=c,
                verdict="pass",
                confidence=0.8,
                risk_flags=[],
                thesis="Dup test.",
                entry_condition="Now.",
                invalidation_condition="If drops.",
            )

        c1 = _make_candidate()
        v1 = _make_verdict(c1)
        trade1 = await engine.open_position(conn, c1, v1)
        assert trade1 is not None

        cash_after_first = await db.get_cash_balance(conn)

        c2 = _make_candidate()
        v2 = _make_verdict(c2)
        trade2 = await engine.open_position(conn, c2, v2)
        assert trade2 is None, "Second open on same mint should return None"

        # Cash must not have changed
        cash_after_second = await db.get_cash_balance(conn)
        assert abs(cash_after_second - cash_after_first) < 0.001, (
            "Cash changed on duplicate open_position call"
        )

        await conn.close()
