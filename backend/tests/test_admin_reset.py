"""
tests/test_admin_reset.py — DB maintenance: prune + reset functions.

Properties verified:
  1. prune_feed_events keeps the newest N rows, deletes the rest
  2. prune_market_regime keeps the newest N rows, deletes the rest
  3. prune on a table with fewer rows than limit is a no-op
  4. reset_book clears all operational tables and restores initial cash
  5. reset_book leaves portfolio_state intact with correct balance
  6. /api/admin/reset without ?confirm=yes returns 400
  7. /api/admin/reset?confirm=yes&mode=reset_book returns 200 with summary
  8. /api/admin/reset?confirm=yes&mode=prune_only returns 200 with prune summary
  9. Unknown mode returns 400
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from contextlib import asynccontextmanager

import pytest
import aiosqlite

import config

# Force SQLite in tests — do not touch operator DB
os.environ.setdefault("DATA_BACKEND", "mock")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _setup_db(db_path: str) -> None:
    """Initialise a fresh test DB using db.init_db()."""
    old = config.DB_PATH
    config.DB_PATH = db_path  # type: ignore[assignment]
    try:
        from api import db
        await db.init_db()
    finally:
        config.DB_PATH = old  # type: ignore[assignment]


@asynccontextmanager
async def _test_db():
    """Yield a connected aiosqlite handle to a fresh tmp DB."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    await _setup_db(path)
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    try:
        yield conn
    finally:
        await conn.close()
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Unit tests for prune_feed_events
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prune_feed_events_keeps_newest_n():
    """Inserting 10 rows then pruning to 5 leaves the 5 newest."""
    from api.db import prune_feed_events

    async with _test_db() as conn:
        # Insert 10 feed events (ids 1–10)
        for i in range(10):
            await conn.execute(
                """
                INSERT INTO feed_events (
                    ts, symbol, mint_address, candidate_snapshot,
                    verdict, rule_breakdown, failed_rule_ids,
                    regime_ok, grounding_flags, narration_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _now_iso(), f"SYM{i}", f"mint{i}", "{}",
                    "fail", "[]", "[]", 0, "[]", "",
                ),
            )
        await conn.commit()

        # Count before
        cur = await conn.execute("SELECT COUNT(*) FROM feed_events")
        assert (await cur.fetchone())[0] == 10

        deleted = await prune_feed_events(conn, 5)
        assert deleted == 5

        # After prune: exactly 5 rows remain
        cur = await conn.execute("SELECT COUNT(*) FROM feed_events")
        assert (await cur.fetchone())[0] == 5

        # They should be the 5 newest (highest ids: 6–10)
        cur = await conn.execute("SELECT id FROM feed_events ORDER BY id")
        ids = [r[0] for r in await cur.fetchall()]
        assert ids == [6, 7, 8, 9, 10]


@pytest.mark.asyncio
async def test_prune_feed_events_noop_when_under_limit():
    """Pruning a table with fewer rows than keep_rows deletes nothing."""
    from api.db import prune_feed_events

    async with _test_db() as conn:
        # Insert only 3 rows
        for i in range(3):
            await conn.execute(
                """
                INSERT INTO feed_events (
                    ts, symbol, mint_address, candidate_snapshot,
                    verdict, rule_breakdown, failed_rule_ids,
                    regime_ok, grounding_flags, narration_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (_now_iso(), f"SYM{i}", f"mint{i}", "{}", "fail", "[]", "[]", 0, "[]", ""),
            )
        await conn.commit()

        deleted = await prune_feed_events(conn, 100)
        # Nothing deleted — 3 < 100
        assert deleted == 0

        cur = await conn.execute("SELECT COUNT(*) FROM feed_events")
        assert (await cur.fetchone())[0] == 3


# ---------------------------------------------------------------------------
# Unit tests for prune_market_regime
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prune_market_regime_keeps_newest_n():
    """Inserting 8 regime rows then pruning to 3 leaves the 3 newest."""
    from api.db import prune_market_regime

    async with _test_db() as conn:
        for i in range(8):
            await conn.execute(
                """
                INSERT INTO market_regime (
                    computed_at, candidate_count, pct_candidates_green_1h,
                    median_volume_1h_usd, avg_buy_sell_ratio, regime_ok, regime_detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (_now_iso(), i + 1, 0.5, 10000.0, 1.2, 1, "test"),
            )
        await conn.commit()

        deleted = await prune_market_regime(conn, 3)
        assert deleted == 5

        cur = await conn.execute("SELECT COUNT(*) FROM market_regime")
        assert (await cur.fetchone())[0] == 3

        cur = await conn.execute("SELECT id FROM market_regime ORDER BY id")
        ids = [r[0] for r in await cur.fetchall()]
        assert ids == [6, 7, 8]


# ---------------------------------------------------------------------------
# Unit tests for reset_book
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reset_book_clears_operational_tables():
    """reset_book deletes rows from all operational tables."""
    from api.db import reset_book

    async with _test_db() as conn:
        # Insert a feed event and a regime row
        await conn.execute(
            """
            INSERT INTO feed_events (
                ts, symbol, mint_address, candidate_snapshot,
                verdict, rule_breakdown, failed_rule_ids,
                regime_ok, grounding_flags, narration_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (_now_iso(), "BONK", "mint123", "{}", "fail", "[]", "[]", 0, "[]", ""),
        )
        await conn.execute(
            """
            INSERT INTO market_regime (
                computed_at, candidate_count, pct_candidates_green_1h,
                median_volume_1h_usd, avg_buy_sell_ratio, regime_ok, regime_detail
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (_now_iso(), 5, 0.6, 20000.0, 1.1, 1, "ok"),
        )
        await conn.commit()

        result = await reset_book(conn, 1000.0)

        assert result["reset"] is True
        assert result["initial_cash_usd"] == 1000.0
        assert result["rows_deleted"]["feed_events"] >= 1
        assert result["rows_deleted"]["market_regime"] >= 1
        assert result["total_deleted"] >= 2

        # All operational tables should be empty
        for table in ("feed_events", "market_regime", "trades", "decision_commits",
                      "events", "memories", "theses", "daily_stats"):
            cur = await conn.execute(f"SELECT COUNT(*) FROM {table}")
            count = (await cur.fetchone())[0]
            assert count == 0, f"Table {table} should be empty after reset, has {count} rows"


@pytest.mark.asyncio
async def test_reset_book_restores_cash():
    """reset_book sets portfolio_state.cash_usd to initial_cash_usd."""
    from api.db import reset_book, get_cash_balance

    async with _test_db() as conn:
        # Drain some cash artificially
        await conn.execute(
            "UPDATE portfolio_state SET cash_usd = 42.0 WHERE id = 1"
        )
        await conn.commit()

        await reset_book(conn, 1000.0)

        cash = await get_cash_balance(conn)
        assert cash == 1000.0


# ---------------------------------------------------------------------------
# Endpoint tests for /api/admin/reset
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_admin_reset_requires_confirm():
    """Calling without ?confirm=yes returns 400 (token guard patched out —
    it is covered separately in test_security_hardening.py)."""
    from api.routes.admin import admin_reset
    from fastapi import HTTPException
    with patch("api.routes.admin.require_admin_token"):
        with pytest.raises(HTTPException) as exc_info:
            await admin_reset(None, confirm="", mode="reset_book")
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_admin_reset_unknown_mode_returns_400():
    """Passing an invalid mode returns 400."""
    from api.routes.admin import admin_reset
    from fastapi import HTTPException
    with patch("api.routes.admin.require_admin_token"):
        with pytest.raises(HTTPException) as exc_info:
            await admin_reset(None, confirm="yes", mode="wipe_everything")
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_admin_reset_book_returns_200():
    """?confirm=yes&mode=reset_book returns 200 with a summary dict."""
    from api.routes.admin import admin_reset

    mock_result = {
        "reset": True,
        "initial_cash_usd": 1000.0,
        "rows_deleted": {"feed_events": 5, "trades": 3},
        "total_deleted": 8,
    }

    @asynccontextmanager
    async def _get_db():
        yield None

    with (
        patch("api.routes.admin.db") as mock_db,
        patch("api.routes.admin.require_admin_token"),
    ):
        mock_db.get_db = _get_db
        mock_db.reset_book = AsyncMock(return_value=mock_result)

        result = await admin_reset(None, confirm="yes", mode="reset_book")

    assert result["mode"] == "reset_book"
    assert result["reset"] is True
    assert result["initial_cash_usd"] == 1000.0
    assert result["total_deleted"] == 8
    assert result["paper_trading_only"] is True


@pytest.mark.asyncio
async def test_admin_prune_only_returns_200():
    """?confirm=yes&mode=prune_only returns a prune summary."""
    from api.routes.admin import admin_reset

    @asynccontextmanager
    async def _get_db():
        yield None

    with (
        patch("api.routes.admin.db") as mock_db,
        patch("api.routes.admin.config") as mock_cfg,
        patch("api.routes.admin.require_admin_token"),
    ):
        mock_db.get_db = _get_db
        mock_db.prune_feed_events = AsyncMock(return_value=42)
        mock_db.prune_market_regime = AsyncMock(return_value=17)
        mock_cfg.FEED_PRUNE_KEEP = 2000
        mock_cfg.REGIME_PRUNE_KEEP = 500
        mock_cfg.INITIAL_CASH_USD = 1000.0
        mock_cfg.PAPER_TRADING_ONLY = True

        result = await admin_reset(None, confirm="yes", mode="prune_only")

    assert result["mode"] == "prune_only"
    assert result["prune_only"] is True
    assert result["feed_events_deleted"] == 42
    assert result["market_regime_deleted"] == 17
    assert result["paper_trading_only"] is True


# ---------------------------------------------------------------------------
# Unit tests for wipe_paper_book (scoped reset: feed + trades only)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wipe_paper_book_clears_feed_and_trades_keeps_proof():
    """wipe_paper_book empties feed_events + trades and resets cash, but
    PRESERVES the proof/observability tables (decision_commits, market_regime).
    """
    from api.db import wipe_paper_book, get_cash_balance

    async with _test_db() as conn:
        # Seed one row in each of: feed_events, trades, decision_commits,
        # market_regime.
        await conn.execute(
            """
            INSERT INTO feed_events (
                ts, symbol, mint_address, candidate_snapshot,
                verdict, rule_breakdown, failed_rule_ids,
                regime_ok, grounding_flags, narration_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (_now_iso(), "BONK", "mint123", "{}", "pass", "[]", "[]", 1, "[]", ""),
        )
        await conn.execute(
            """
            INSERT INTO trades (
                trade_id, symbol, mint_address, opened_at, entry_price_usd,
                position_size_usd, quantity, candidate_snapshot, thesis, is_open
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("t1", "BONK", "mint123", _now_iso(), 0.001, 50.0, 50000.0, "{}", "x", 1),
        )
        await conn.execute(
            """
            INSERT INTO decision_commits (
                created_at, tick_ts, symbol, mint_address, verdict,
                entry_allowed, nonce, payload_json, payload_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (_now_iso(), _now_iso(), "BONK", "mint123", "buy", 1,
             "nonce1", "{}", "hash_unique_1"),
        )
        await conn.execute(
            """
            INSERT INTO market_regime (
                computed_at, candidate_count, pct_candidates_green_1h,
                median_volume_1h_usd, avg_buy_sell_ratio, regime_ok, regime_detail
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (_now_iso(), 5, 0.6, 20000.0, 1.1, 1, "ok"),
        )
        # Drain cash so we can prove the reset restores it.
        await conn.execute(
            "UPDATE portfolio_state SET cash_usd = 12.5 WHERE id = 1"
        )
        await conn.commit()

        result = await wipe_paper_book(conn, 1000.0)

        assert result["reset"] is True
        assert result["scope"] == "wipe_paper"
        assert result["rows_deleted"]["feed_events"] == 1
        assert result["rows_deleted"]["trades"] == 1
        assert result["total_deleted"] == 2

        # Paper-display tables are empty...
        for table in ("feed_events", "trades"):
            cur = await conn.execute(f"SELECT COUNT(*) FROM {table}")
            assert (await cur.fetchone())[0] == 0, f"{table} should be empty"

        # ...but the proof/observability record is preserved.
        for table in ("decision_commits", "market_regime"):
            cur = await conn.execute(f"SELECT COUNT(*) FROM {table}")
            assert (await cur.fetchone())[0] == 1, f"{table} should be preserved"

        # Cash restored to the starting balance.
        assert await get_cash_balance(conn) == 1000.0


# ---------------------------------------------------------------------------
# Endpoint test for mode=wipe_paper
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_admin_wipe_paper_returns_200():
    """?confirm=yes&mode=wipe_paper returns the scoped-reset summary."""
    from api.routes.admin import admin_reset

    mock_result = {
        "reset": True,
        "scope": "wipe_paper",
        "initial_cash_usd": 1000.0,
        "rows_deleted": {"feed_events": 7, "trades": 2},
        "total_deleted": 9,
    }

    @asynccontextmanager
    async def _get_db():
        yield None

    with (
        patch("api.routes.admin.db") as mock_db,
        patch("api.routes.admin.require_admin_token"),
    ):
        mock_db.get_db = _get_db
        mock_db.wipe_paper_book = AsyncMock(return_value=mock_result)

        result = await admin_reset(None, confirm="yes", mode="wipe_paper")

    assert result["mode"] == "wipe_paper"
    assert result["scope"] == "wipe_paper"
    assert result["reset"] is True
    assert result["total_deleted"] == 9
    assert result["paper_trading_only"] is True
    mock_db.wipe_paper_book.assert_awaited_once()
