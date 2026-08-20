"""
api/db.py — SQLite repository layer for trading-bot.

Uses aiosqlite for async access. WAL mode allows one concurrent writer
(the tick loop) and multiple readers (the API) without blocking each other.

All writes use explicit transactions. Queries are parameterized — no string
interpolation near user data (defense-first rule 1).

Performance notes (performance-discipline rules 4):
  - Indexes on ts, is_open, closed_at avoid full-table scans on hot queries.
  - Pagination is pushed into SQL (LIMIT/OFFSET), not done in Python.
  - Every public function accepts an open aiosqlite.Connection — callers
    that need to batch multiple ops can reuse the same connection/transaction.
"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

import aiosqlite

import config
from models import Candidate, DailyStats, FeedEvent, Trade

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema — created idempotently via init_db() on every startup
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS feed_events (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                      TEXT    NOT NULL,
    symbol                  TEXT    NOT NULL,
    mint_address            TEXT    NOT NULL,
    candidate_snapshot      TEXT    NOT NULL,
    verdict                 TEXT    NOT NULL CHECK (verdict IN ('pass', 'fail')),
    confidence              REAL,
    risk_flags              TEXT    NOT NULL DEFAULT '[]',
    entry_condition         TEXT,
    invalidation_condition  TEXT,
    thesis                  TEXT,
    led_to_trade_id         TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    trade_id                TEXT    PRIMARY KEY,
    symbol                  TEXT    NOT NULL,
    mint_address            TEXT    NOT NULL,
    opened_at               TEXT    NOT NULL,
    entry_price_usd         REAL    NOT NULL,
    position_size_usd       REAL    NOT NULL,
    quantity                REAL    NOT NULL,
    candidate_snapshot      TEXT    NOT NULL,
    verdict_snapshot        TEXT    NOT NULL,
    invalidation_condition  TEXT,
    closed_at               TEXT,
    exit_price_usd          REAL,
    exit_reason             TEXT,
    realized_pnl_usd        REAL,
    realized_pnl_pct        REAL,
    is_open                 INTEGER NOT NULL DEFAULT 1,
    reflection_text         TEXT
);

CREATE TABLE IF NOT EXISTS daily_stats (
    date                TEXT    PRIMARY KEY,
    open_positions      INTEGER NOT NULL DEFAULT 0,
    closed_trades       INTEGER NOT NULL DEFAULT 0,
    recommendations     TEXT    NOT NULL DEFAULT '{}'
);

-- Singleton row that holds the current cash balance.
-- id is always 1 — the CHECK constraint enforces this.
CREATE TABLE IF NOT EXISTS portfolio_state (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    cash_usd    REAL    NOT NULL,
    updated_at  TEXT    NOT NULL
);

-- Performance indexes (performance-discipline rule 4)
CREATE INDEX IF NOT EXISTS idx_feed_events_ts        ON feed_events(ts);
CREATE INDEX IF NOT EXISTS idx_feed_events_id        ON feed_events(id);
CREATE INDEX IF NOT EXISTS idx_trades_is_open        ON trades(is_open);
CREATE INDEX IF NOT EXISTS idx_trades_closed_at      ON trades(closed_at);
CREATE INDEX IF NOT EXISTS idx_trades_mint_open      ON trades(mint_address, is_open);
CREATE INDEX IF NOT EXISTS idx_trades_opened_at      ON trades(opened_at);
"""


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

async def init_db() -> None:
    """
    Create schema idempotently and seed the portfolio singleton if absent.
    Safe to call on every process startup.
    """
    async with aiosqlite.connect(config.DB_PATH) as conn:
        await conn.executescript(_SCHEMA_SQL)
        await conn.execute(
            """
            INSERT OR IGNORE INTO portfolio_state (id, cash_usd, updated_at)
            VALUES (1, ?, ?)
            """,
            (config.INITIAL_CASH_USD, _now_iso()),
        )
        await conn.commit()
    log.info("Database initialised: %s", config.DB_PATH)


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

@asynccontextmanager
async def get_db() -> AsyncIterator[aiosqlite.Connection]:
    """
    Async context manager that opens a WAL-mode connection and sets
    row_factory so rows are accessible by column name.

    Usage:
        async with get_db() as conn:
            rows = await conn.execute(...)
    """
    async with aiosqlite.connect(config.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        yield conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Feed events
# ---------------------------------------------------------------------------

async def insert_feed_event(conn: aiosqlite.Connection, event: FeedEvent) -> int:
    """Insert a feed event. Returns the auto-assigned integer id."""
    cursor = await conn.execute(
        """
        INSERT INTO feed_events
            (ts, symbol, mint_address, candidate_snapshot, verdict,
             confidence, risk_flags, entry_condition, invalidation_condition,
             thesis, led_to_trade_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event.ts,
            event.symbol,
            event.mint_address,
            json.dumps(event.candidate_snapshot),
            event.verdict,
            event.confidence,
            json.dumps(event.risk_flags),
            event.entry_condition,
            event.invalidation_condition,
            event.thesis,
            event.led_to_trade_id,
        ),
    )
    await conn.commit()
    row_id: int = cursor.lastrowid  # type: ignore[assignment]
    return row_id


async def get_feed_events(
    conn: aiosqlite.Connection,
    limit: int = 50,
    offset: int = 0,
) -> list[FeedEvent]:
    """Return paginated feed events, newest first."""
    cursor = await conn.execute(
        "SELECT * FROM feed_events ORDER BY id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    rows = await cursor.fetchall()
    return [_row_to_feed_event(r) for r in rows]


async def get_feed_events_since(
    conn: aiosqlite.Connection,
    since_id: int,
) -> list[FeedEvent]:
    """
    Return all events with id > since_id, ascending.
    Used by the WS broadcaster to poll for new events efficiently.
    """
    cursor = await conn.execute(
        "SELECT * FROM feed_events WHERE id > ? ORDER BY id ASC",
        (since_id,),
    )
    rows = await cursor.fetchall()
    return [_row_to_feed_event(r) for r in rows]


async def get_max_feed_event_id(conn: aiosqlite.Connection) -> int:
    """Return the highest feed event id, or 0 if the table is empty."""
    cursor = await conn.execute("SELECT MAX(id) as max_id FROM feed_events")
    row = await cursor.fetchone()
    return int(row["max_id"]) if row and row["max_id"] is not None else 0


def _row_to_feed_event(row: aiosqlite.Row) -> FeedEvent:
    return FeedEvent(
        id=row["id"],
        ts=row["ts"],
        symbol=row["symbol"],
        mint_address=row["mint_address"],
        candidate_snapshot=json.loads(row["candidate_snapshot"]),
        verdict=row["verdict"],
        confidence=row["confidence"],
        risk_flags=json.loads(row["risk_flags"]) if row["risk_flags"] else [],
        entry_condition=row["entry_condition"],
        invalidation_condition=row["invalidation_condition"],
        thesis=row["thesis"],
        led_to_trade_id=row["led_to_trade_id"],
    )


# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------

async def insert_trade(conn: aiosqlite.Connection, trade: Trade) -> None:
    """Persist a newly opened trade."""
    await conn.execute(
        """
        INSERT INTO trades
            (trade_id, symbol, mint_address, opened_at, entry_price_usd,
             position_size_usd, quantity, candidate_snapshot, verdict_snapshot,
             invalidation_condition, closed_at, exit_price_usd, exit_reason,
             realized_pnl_usd, realized_pnl_pct, is_open, reflection_text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trade.trade_id,
            trade.symbol,
            trade.mint_address,
            trade.opened_at,
            trade.entry_price_usd,
            trade.position_size_usd,
            trade.quantity,
            json.dumps(trade.candidate_snapshot),
            json.dumps(trade.verdict_snapshot),
            trade.invalidation_condition,
            trade.closed_at,
            trade.exit_price_usd,
            trade.exit_reason,
            trade.realized_pnl_usd,
            trade.realized_pnl_pct,
            1 if trade.is_open else 0,
            trade.reflection_text,
        ),
    )
    await conn.commit()


async def close_trade_in_db(
    conn: aiosqlite.Connection,
    trade_id: str,
    closed_at: str,
    exit_price_usd: float,
    exit_reason: str,
    realized_pnl_usd: float,
    realized_pnl_pct: float,
) -> None:
    """
    Atomically close an open trade. The WHERE clause includes is_open=1
    to prevent double-closing (idempotency, defense-first rule 7).
    """
    await conn.execute(
        """
        UPDATE trades
        SET closed_at          = ?,
            exit_price_usd     = ?,
            exit_reason        = ?,
            realized_pnl_usd   = ?,
            realized_pnl_pct   = ?,
            is_open            = 0
        WHERE trade_id = ? AND is_open = 1
        """,
        (
            closed_at,
            exit_price_usd,
            exit_reason,
            realized_pnl_usd,
            realized_pnl_pct,
            trade_id,
        ),
    )
    await conn.commit()


async def update_trade_reflection(
    conn: aiosqlite.Connection,
    trade_id: str,
    reflection_text: str,
) -> None:
    """Set the post-trade reflection text. Called async after close (FR-26)."""
    await conn.execute(
        "UPDATE trades SET reflection_text = ? WHERE trade_id = ?",
        (reflection_text, trade_id),
    )
    await conn.commit()


async def get_open_trades(conn: aiosqlite.Connection) -> list[Trade]:
    cursor = await conn.execute(
        "SELECT * FROM trades WHERE is_open = 1 ORDER BY opened_at ASC"
    )
    rows = await cursor.fetchall()
    return [_row_to_trade(r) for r in rows]


async def get_closed_trades(
    conn: aiosqlite.Connection,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "date",
) -> list[Trade]:
    """
    Paginated closed trades. sort_by: 'date' (default) or 'pnl'.
    Sorting pushed to SQL — no Python-side sort (performance-discipline rule 4).
    """
    if sort_by not in ("date", "pnl"):
        sort_by = "date"
    order_col = "closed_at" if sort_by == "date" else "realized_pnl_usd"
    cursor = await conn.execute(
        f"SELECT * FROM trades WHERE is_open = 0 ORDER BY {order_col} DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    rows = await cursor.fetchall()
    return [_row_to_trade(r) for r in rows]


async def get_all_closed_trades(conn: aiosqlite.Connection) -> list[Trade]:
    """Return all closed trades in chronological order (for stats/promotion gate)."""
    cursor = await conn.execute(
        "SELECT * FROM trades WHERE is_open = 0 ORDER BY closed_at ASC"
    )
    rows = await cursor.fetchall()
    return [_row_to_trade(r) for r in rows]


async def get_trade_by_id(
    conn: aiosqlite.Connection, trade_id: str
) -> Optional[Trade]:
    cursor = await conn.execute(
        "SELECT * FROM trades WHERE trade_id = ?", (trade_id,)
    )
    row = await cursor.fetchone()
    return _row_to_trade(row) if row else None


async def is_position_open(conn: aiosqlite.Connection, mint_address: str) -> bool:
    """
    Return True if there is already an open position for this mint address.
    Used as an idempotency guard in open_position() (defense-first rule 7).
    """
    cursor = await conn.execute(
        "SELECT 1 FROM trades WHERE mint_address = ? AND is_open = 1 LIMIT 1",
        (mint_address,),
    )
    return await cursor.fetchone() is not None


async def get_first_trade_date(conn: aiosqlite.Connection) -> Optional[str]:
    cursor = await conn.execute("SELECT MIN(opened_at) AS d FROM trades")
    row = await cursor.fetchone()
    return row["d"] if row and row["d"] else None


def _row_to_trade(row: aiosqlite.Row) -> Trade:
    return Trade(
        trade_id=row["trade_id"],
        symbol=row["symbol"],
        mint_address=row["mint_address"],
        opened_at=row["opened_at"],
        entry_price_usd=float(row["entry_price_usd"]),
        position_size_usd=float(row["position_size_usd"]),
        quantity=float(row["quantity"]),
        candidate_snapshot=json.loads(row["candidate_snapshot"]),
        verdict_snapshot=json.loads(row["verdict_snapshot"]),
        invalidation_condition=row["invalidation_condition"] or "",
        closed_at=row["closed_at"],
        exit_price_usd=float(row["exit_price_usd"]) if row["exit_price_usd"] is not None else None,
        exit_reason=row["exit_reason"],
        realized_pnl_usd=float(row["realized_pnl_usd"]) if row["realized_pnl_usd"] is not None else None,
        realized_pnl_pct=float(row["realized_pnl_pct"]) if row["realized_pnl_pct"] is not None else None,
        is_open=bool(row["is_open"]),
        reflection_text=row["reflection_text"],
    )


# ---------------------------------------------------------------------------
# Portfolio state
# ---------------------------------------------------------------------------

async def get_cash_balance(conn: aiosqlite.Connection) -> float:
    cursor = await conn.execute(
        "SELECT cash_usd FROM portfolio_state WHERE id = 1"
    )
    row = await cursor.fetchone()
    if row is None:
        raise RuntimeError("Portfolio state not initialised — call init_db() first.")
    return float(row["cash_usd"])


async def update_cash_balance(conn: aiosqlite.Connection, new_cash: float) -> None:
    if new_cash < 0:
        raise ValueError(
            f"Cash balance cannot go negative: {new_cash:.6f}. "
            "Check position sizing logic."
        )
    await conn.execute(
        "UPDATE portfolio_state SET cash_usd = ?, updated_at = ? WHERE id = 1",
        (new_cash, _now_iso()),
    )
    await conn.commit()


# ---------------------------------------------------------------------------
# Daily stats
# ---------------------------------------------------------------------------

async def upsert_daily_stats(conn: aiosqlite.Connection, stats: DailyStats) -> None:
    await conn.execute(
        """
        INSERT INTO daily_stats (date, open_positions, closed_trades, recommendations)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            open_positions  = excluded.open_positions,
            closed_trades   = excluded.closed_trades,
            recommendations = excluded.recommendations
        """,
        (
            stats.date,
            stats.open_positions,
            stats.closed_trades,
            json.dumps(stats.recommendations),
        ),
    )
    await conn.commit()
