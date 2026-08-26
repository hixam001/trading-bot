"""
api/db.py — SQLite repository layer (WAL mode).

One writer (tick loop), many readers (API), without blocking each other.
All queries parameterized; all writes explicit.

ATOMICITY PATTERN (§5.1 — the highest-stakes correctness property here):
Every function that changes cash AND trade state does so in this order:
  1. Conditional state write whose WHERE clause makes a retry a no-op,
     returning the affected ROW COUNT.
  2. If rowcount == 0 → the operation already happened (retry/race):
     log it, touch NOTHING, report applied=False to the caller.
  3. Only after rowcount == 1 is confirmed: touch cash (itself guarded).
The rowcount is the SOLE authority on whether cash moves.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

import aiosqlite

import config
from models import DailyStats, FeedEvent, Trade

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


_SCHEMA_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS feed_events (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                      TEXT    NOT NULL,
    symbol                  TEXT    NOT NULL,
    mint_address            TEXT    NOT NULL,
    candidate_snapshot      TEXT    NOT NULL,
    verdict                 TEXT    NOT NULL CHECK (verdict IN ('pass', 'fail')),
    thesis                  TEXT,
    rule_breakdown          TEXT    NOT NULL DEFAULT '[]',
    failed_rule_ids         TEXT    NOT NULL DEFAULT '[]',
    regime_ok               INTEGER NOT NULL DEFAULT 0,
    grounding_flags         TEXT    NOT NULL DEFAULT '[]',
    narration_source        TEXT    NOT NULL DEFAULT '',
    led_to_trade_id         TEXT,
    model_version           TEXT,
    prompt_version          TEXT
);

CREATE TABLE IF NOT EXISTS trades (
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
    is_open                 INTEGER NOT NULL DEFAULT 1,
    high_water_usd          REAL,
    tranches_taken          INTEGER NOT NULL DEFAULT 0,
    reflection_text         TEXT
);

-- HARD backstop for open-position idempotency: at most one OPEN position
-- per mint, enforced by SQLite itself, not just application checks.
CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_one_open_per_mint
    ON trades(mint_address) WHERE is_open = 1;

CREATE TABLE IF NOT EXISTS market_regime (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    computed_at              TEXT    NOT NULL,
    candidate_count          INTEGER NOT NULL,
    pct_candidates_green_1h  REAL    NOT NULL,
    median_volume_1h_usd     REAL    NOT NULL,
    avg_buy_sell_ratio       REAL    NOT NULL,
    regime_ok                INTEGER NOT NULL CHECK (regime_ok IN (0, 1)),
    regime_detail            TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_call_counters (
    provider                TEXT    NOT NULL,
    day                     TEXT    NOT NULL,
    call_count              INTEGER NOT NULL DEFAULT 0,
    error_count             INTEGER NOT NULL DEFAULT 0,
    rate_limit_429_count    INTEGER NOT NULL DEFAULT 0,
    last_call_at            TEXT,
    PRIMARY KEY (provider, day)
);

CREATE TABLE IF NOT EXISTS kb_documents (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    filename                TEXT    NOT NULL UNIQUE,
    content                 TEXT    NOT NULL,
    digest                  TEXT    NOT NULL DEFAULT '',
    ingested_at             TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_stats (
    date                    TEXT    PRIMARY KEY,
    open_positions          INTEGER NOT NULL DEFAULT 0,
    closed_trades           INTEGER NOT NULL DEFAULT 0,
    stats_json              TEXT    NOT NULL DEFAULT '{}'
);

-- Decision commits (omo 'seal' parity): sha256(nonce|canonical payload) of
-- every candidate decision, written BEFORE the trade acts on it. The
-- plaintext payload is stored alongside so anyone can recompute the hash.
CREATE TABLE IF NOT EXISTS decision_commits (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT    NOT NULL,
    tick_ts         TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    mint_address    TEXT    NOT NULL,
    verdict         TEXT    NOT NULL,   -- think verdict: buy | pass
    entry_allowed   INTEGER NOT NULL,   -- 1 = both layers agreed
    nonce           TEXT    NOT NULL,
    payload_json    TEXT    NOT NULL,   -- canonical reveal payload
    payload_hash    TEXT    NOT NULL UNIQUE,
    -- OMO-R1/R7: fill binding and retro attribution fields (nullable)
    signature       TEXT,               -- Solana tx sig when a fill is bound
    phase           TEXT,               -- 'filled' | null
    matched_by      TEXT,               -- 'exact' | 'retro' | null
    model_version   TEXT,
    prompt_version  TEXT
);

CREATE INDEX IF NOT EXISTS idx_decision_commits_created
    ON decision_commits(created_at);
CREATE INDEX IF NOT EXISTS idx_decision_commits_sig
    ON decision_commits(signature) WHERE signature IS NOT NULL;

CREATE TABLE IF NOT EXISTS llm_call_usage (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                      TEXT    NOT NULL,
    task                    TEXT    NOT NULL,
    provider                TEXT    NOT NULL,
    model                   TEXT    NOT NULL,
    tick_ts                 TEXT,
    mint_address            TEXT,
    status                  TEXT    NOT NULL,
    latency_ms              INTEGER,
    input_tokens            INTEGER,
    cache_hit_tokens        INTEGER,
    output_tokens           INTEGER,
    total_tokens            INTEGER,
    estimated_cost_usd      REAL,
    is_peak_window          INTEGER NOT NULL DEFAULT 0,
    degradation_reason      TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_call_usage_ts ON llm_call_usage(ts);

-- OMO-R5 durable event stream and weighted lessons recalled by the thinker.
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL,
    kind         TEXT    NOT NULL CHECK (kind IN ('thought', 'did', 'refused', 'read', 'trade')),
    symbol       TEXT    NOT NULL DEFAULT '',
    mint_address TEXT    NOT NULL DEFAULT '',
    payload_json TEXT    NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS memories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    topic      TEXT    NOT NULL,
    note       TEXT    NOT NULL,
    weight     REAL    NOT NULL DEFAULT 1.0,
    hits       INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
CREATE INDEX IF NOT EXISTS idx_memories_topic_weight ON memories(topic, weight DESC);

-- Singleton portfolio row; id is always 1 (CHECK-enforced).
CREATE TABLE IF NOT EXISTS portfolio_state (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    cash_usd    REAL    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_feed_events_ts     ON feed_events(ts);
CREATE INDEX IF NOT EXISTS idx_feed_events_id     ON feed_events(id);
CREATE INDEX IF NOT EXISTS idx_trades_is_open     ON trades(is_open);
CREATE INDEX IF NOT EXISTS idx_trades_closed_at   ON trades(closed_at);
CREATE INDEX IF NOT EXISTS idx_regime_computed_at ON market_regime(computed_at);

-- OMO-R3 Durable thesis book
CREATE TABLE IF NOT EXISTS theses (
    trade_id          TEXT PRIMARY KEY,
    mint_address      TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    author            TEXT NOT NULL,
    thesis            TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    closed_at         TEXT,
    realized_pnl_usd  REAL
);
CREATE INDEX IF NOT EXISTS idx_theses_mint ON theses(mint_address);
"""


async def init_db() -> None:
    conn = await aiosqlite.connect(config.DB_PATH)
    try:
        await conn.executescript(_SCHEMA_SQL)
        # Idempotent column migrations for DBs created before a column existed
        # (CREATE TABLE IF NOT EXISTS never alters an existing table).
        for stmt in (
            "ALTER TABLE trades ADD COLUMN high_water_usd REAL",
            "ALTER TABLE trades ADD COLUMN tranches_taken INTEGER NOT NULL DEFAULT 0",
            # OMO-R1/R7: binding + retro attribution columns on decision_commits
            "ALTER TABLE decision_commits ADD COLUMN signature TEXT",
            "ALTER TABLE decision_commits ADD COLUMN phase TEXT",
            "ALTER TABLE decision_commits ADD COLUMN matched_by TEXT",
            "ALTER TABLE decision_commits ADD COLUMN model_version TEXT",
            "ALTER TABLE decision_commits ADD COLUMN prompt_version TEXT",
            "ALTER TABLE feed_events ADD COLUMN model_version TEXT",
            "ALTER TABLE feed_events ADD COLUMN prompt_version TEXT",
        ):
            try:
                await conn.execute(stmt)
                await conn.commit()
            except Exception:
                pass  # column already exists
        await conn.execute(
            "INSERT OR IGNORE INTO portfolio_state (id, cash_usd, updated_at) "
            "VALUES (1, ?, ?)",
            (config.INITIAL_CASH_USD, _now_iso()),
        )
        await conn.commit()
    finally:
        await conn.close()


@asynccontextmanager
async def get_db() -> AsyncIterator[aiosqlite.Connection]:
    conn = await aiosqlite.connect(config.DB_PATH)
    conn.row_factory = aiosqlite.Row
    try:
        yield conn
    finally:
        await conn.close()


# ===========================================================================
# Feed events — every GateDecision, pass or fail
# ===========================================================================

async def insert_feed_event(conn: aiosqlite.Connection, event: FeedEvent) -> int:
    cursor = await conn.execute(
        """
        INSERT INTO feed_events (
            ts, symbol, mint_address, candidate_snapshot, verdict, thesis,
            rule_breakdown, failed_rule_ids, regime_ok, grounding_flags,
            narration_source, led_to_trade_id, model_version, prompt_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event.ts, event.symbol, event.mint_address,
            json.dumps(event.candidate_snapshot), event.verdict, event.thesis,
            json.dumps(event.rule_breakdown), json.dumps(event.failed_rule_ids),
            int(event.regime_ok), json.dumps(event.grounding_flags),
            event.narration_source, event.led_to_trade_id,
            event.model_version, event.prompt_version,
        ),
    )
    await conn.commit()
    return int(cursor.lastrowid)


def _row_to_feed_dict(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "ts": row["ts"],
        "symbol": row["symbol"],
        "mint_address": row["mint_address"],
        "candidate_snapshot": json.loads(row["candidate_snapshot"]),
        "verdict": row["verdict"],
        "thesis": row["thesis"],
        "rule_breakdown": json.loads(row["rule_breakdown"]),
        "failed_rule_ids": json.loads(row["failed_rule_ids"]),
        "regime_ok": bool(row["regime_ok"]),
        "grounding_flags": json.loads(row["grounding_flags"]),
        "narration_source": row["narration_source"],
        "led_to_trade_id": row["led_to_trade_id"],
        "model_version": row["model_version"] if "model_version" in row.keys() else None,
        "prompt_version": row["prompt_version"] if "prompt_version" in row.keys() else None,
    }


async def get_feed_events(
    conn: aiosqlite.Connection, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    cursor = await conn.execute(
        "SELECT * FROM feed_events ORDER BY id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    rows = await cursor.fetchall()
    return [_row_to_feed_dict(r) for r in rows]


async def count_feed_events(conn: aiosqlite.Connection) -> int:
    cursor = await conn.execute("SELECT COUNT(*) FROM feed_events")
    return int((await cursor.fetchone())[0])


async def get_refusal_events(
    conn: aiosqlite.Connection, limit: int = 100
) -> list[dict[str, Any]]:
    """Every rejection at full detail - refusals are a first-class public
    artifact (omo parity). verdict=fail means think or gate refused.
    """
    cursor = await conn.execute(
        "SELECT * FROM feed_events WHERE verdict = ? ORDER BY id DESC LIMIT ?",
        ("fail", limit),
    )
    rows = await cursor.fetchall()
    return [_row_to_feed_dict(r) for r in rows]


# ===========================================================================
# OMO-R5 memory/events — append-only observations and weighted lessons
# ===========================================================================

async def insert_event(
    conn: aiosqlite.Connection, kind: str, ts: str, symbol: str = "",
    mint_address: str = "", payload: Optional[dict[str, Any]] = None,
) -> int:
    if kind not in {"thought", "did", "refused", "read", "trade"}:
        raise ValueError(f"invalid event kind: {kind}")
    cursor = await conn.execute(
        "INSERT INTO events (ts, kind, symbol, mint_address, payload_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (ts, kind, symbol, mint_address, json.dumps(payload or {}, sort_keys=True)),
    )
    await conn.commit()
    return int(cursor.lastrowid)


async def get_recent_events(
    conn: aiosqlite.Connection, limit: int = 100,
) -> list[dict[str, Any]]:
    cursor = await conn.execute(
        "SELECT id, ts, kind, symbol, mint_address, payload_json "
        "FROM events ORDER BY id DESC LIMIT ?", (limit,)
    )
    return [
        {"id": row["id"], "ts": row["ts"], "kind": row["kind"],
         "symbol": row["symbol"], "mint_address": row["mint_address"],
         "payload": json.loads(row["payload_json"])}
        for row in await cursor.fetchall()
    ]


async def upsert_memory(
    conn: aiosqlite.Connection, topic: str, note: str, weight: float = 1.0,
) -> int:
    if not topic.strip() or not note.strip() or weight <= 0:
        raise ValueError("memory topic, note, and positive weight are required")
    cursor = await conn.execute(
        "SELECT id FROM memories WHERE topic = ? AND note = ?", (topic, note)
    )
    row = await cursor.fetchone()
    if row:
        await conn.execute(
            "UPDATE memories SET weight = ?, updated_at = ? WHERE id = ?",
            (weight, _now_iso(), row["id"]),
        )
        await conn.commit()
        return int(row["id"])
    cursor = await conn.execute(
        "INSERT INTO memories (topic, note, weight, updated_at) VALUES (?, ?, ?, ?)",
        (topic, note, weight, _now_iso()),
    )
    await conn.commit()
    return int(cursor.lastrowid)


async def recall_memories(
    conn: aiosqlite.Connection, topic: str = "", limit: int = 3,
) -> list[dict[str, Any]]:
    """Return strongest lessons and count each returned lesson as recalled."""
    cursor = await conn.execute(
        "SELECT id, topic, note, weight, hits FROM memories "
        "WHERE ? = '' OR topic = ? ORDER BY weight DESC, hits DESC, id DESC LIMIT ?",
        (topic, topic, limit),
    )
    rows = await cursor.fetchall()
    for row in rows:
        await conn.execute("UPDATE memories SET hits = hits + 1 WHERE id = ?", (row["id"],))
    if rows:
        await conn.commit()
    return [
        {"id": row["id"], "topic": row["topic"], "note": row["note"],
         "weight": row["weight"], "hits": row["hits"] + 1}
        for row in rows
    ]


# ===========================================================================
# Trades — atomic, idempotent state changes (§5.1)
# ===========================================================================

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
        thesis=row["thesis"],
        closed_at=row["closed_at"],
        exit_price_usd=float(row["exit_price_usd"]) if row["exit_price_usd"] is not None else None,
        exit_reason=row["exit_reason"],
        realized_pnl_usd=float(row["realized_pnl_usd"]) if row["realized_pnl_usd"] is not None else None,
        realized_pnl_pct=float(row["realized_pnl_pct"]) if row["realized_pnl_pct"] is not None else None,
        is_open=bool(row["is_open"]),
        high_water_usd=float(row["high_water_usd"]) if row["high_water_usd"] is not None else None,
        tranches_taken=int(row["tranches_taken"] or 0),
        reflection_text=row["reflection_text"],
    )


async def try_insert_open_trade(conn: aiosqlite.Connection, trade: Trade) -> int:
    """
    ATOMIC OPEN (§5.1). Conditional insert: succeeds (rowcount 1) only if no
    open position exists for this mint; a concurrent/duplicate open attempt
    affects zero rows. Backed by the partial UNIQUE index as a hard backstop.
    Returns affected row count — caller must NOT touch cash unless it is 1.
    """
    cursor = await conn.execute(
        """
        INSERT INTO trades (
            trade_id, symbol, mint_address, opened_at, entry_price_usd,
            position_size_usd, quantity, candidate_snapshot, thesis, is_open,
            high_water_usd
        )
        SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?
        WHERE NOT EXISTS (
            SELECT 1 FROM trades WHERE mint_address = ? AND is_open = 1
        )
        """,
        (
            trade.trade_id, trade.symbol, trade.mint_address, trade.opened_at,
            trade.entry_price_usd, trade.position_size_usd, trade.quantity,
            json.dumps(trade.candidate_snapshot), trade.thesis,
            # Trail memory starts at the entry price itself.
            trade.entry_price_usd,
            trade.mint_address,
        ),
    )
    await conn.commit()
    return max(cursor.rowcount, 0)


async def close_trade_row(
    conn: aiosqlite.Connection,
    trade_id: str,
    closed_at: str,
    exit_price_usd: float,
    exit_reason: str,
    realized_pnl_usd: float,
    realized_pnl_pct: float,
) -> int:
    """
    ATOMIC CLOSE (§5.1). Updates WHERE trade_id = ? AND is_open = 1 so a
    repeated attempt affects ZERO rows and returns 0 — the caller then skips
    the cash credit entirely.
    """
    cursor = await conn.execute(
        """
        UPDATE trades
        SET closed_at = ?, exit_price_usd = ?, exit_reason = ?,
            realized_pnl_usd = ?, realized_pnl_pct = ?, is_open = 0
        WHERE trade_id = ? AND is_open = 1
        """,
        (closed_at, exit_price_usd, exit_reason,
         realized_pnl_usd, realized_pnl_pct, trade_id),
    )
    await conn.commit()
    return max(cursor.rowcount, 0)


async def trim_position_row(
    conn: aiosqlite.Connection,
    trade_id: str,
    qty_out: float,
    size_out_usd: float,
) -> int:
    """
    ATOMIC PARTIAL CLOSE (E8/E9 — omotrades-style TP tranches). Reduces an
    open position by the given fraction's quantity/cost basis and bumps the
    tranche counter, only while the row is still open and would keep a
    positive remainder. rowcount 0 = already closed or degenerate trim;
    caller must NOT touch cash unless it is 1.
    """
    cursor = await conn.execute(
        """
        UPDATE trades
        SET quantity = quantity - ?,
            position_size_usd = position_size_usd - ?,
            tranches_taken = tranches_taken + 1
        WHERE trade_id = ? AND is_open = 1
          AND quantity - ? > 0
          AND position_size_usd - ? >= 0
        """,
        (qty_out, size_out_usd, trade_id, qty_out, size_out_usd),
    )
    await conn.commit()
    return max(cursor.rowcount, 0)


async def update_high_water(
    conn: aiosqlite.Connection,
    trade_id: str,
    high_water_usd: float,
) -> None:
    """Trail memory: raise the peak price seen since entry (never lower it)."""
    await conn.execute(
        """
        UPDATE trades SET high_water_usd = ?
        WHERE trade_id = ? AND is_open = 1
          AND (high_water_usd IS NULL OR high_water_usd < ?)
        """,
        (high_water_usd, trade_id, high_water_usd),
    )
    await conn.commit()


async def get_last_closed_at_for_mint(
    conn: aiosqlite.Connection, mint_address: str
) -> Optional[str]:
    """Timestamp of this mint's most recent CLOSED position (sell cooldown)."""
    async with conn.execute(
        """
        SELECT closed_at FROM trades
        WHERE mint_address = ? AND closed_at IS NOT NULL
        ORDER BY closed_at DESC LIMIT 1
        """,
        (mint_address,),
    ) as cur:
        row = await cur.fetchone()
    return row["closed_at"] if row else None


async def count_closes_since(
    conn: aiosqlite.Connection, since_iso: str
) -> int:
    """Closed positions since a timestamp (rolling 24h exit ceiling)."""
    async with conn.execute(
        "SELECT COUNT(*) AS n FROM trades WHERE closed_at >= ?",
        (since_iso,),
    ) as cur:
        row = await cur.fetchone()
    return int(row["n"]) if row else 0


async def get_recent_closed_reasons(
    conn: aiosqlite.Connection, mint_address: str, limit: int = 2
) -> list[str]:
    """Newest-first exit reasons for a mint (auto-block consecutive check)."""
    async with conn.execute(
        """
        SELECT exit_reason FROM trades
        WHERE mint_address = ? AND closed_at IS NOT NULL
        ORDER BY closed_at DESC LIMIT ?
        """,
        (mint_address, limit),
    ) as cur:
        rows = await cur.fetchall()
    return [r["exit_reason"] for r in rows if r["exit_reason"]]


async def deployed_today(
    conn: aiosqlite.Connection, now_utc: Optional[datetime] = None
) -> float:
    """Sum of cost basis deployed today (UTC) — daily deploy cap input."""
    now = now_utc or datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0,
                            microsecond=0).isoformat()
    async with conn.execute(
        "SELECT COALESCE(SUM(position_size_usd), 0) AS s "
        "FROM trades WHERE opened_at >= ?",
        (day_start,),
    ) as cur:
        row = await cur.fetchone()
    return float(row["s"] or 0.0)


# ---------------------------------------------------------------------------
# Decision commits — tamper-evident local audit trail (omo 'seal' parity)
# ---------------------------------------------------------------------------

async def insert_decision_commit(
    conn: aiosqlite.Connection,
    created_at: str,
    tick_ts: str,
    symbol: str,
    mint_address: str,
    verdict: str,
    entry_allowed: bool,
    nonce: str,
    payload_json: str,
    payload_hash: str,
    model_version: Optional[str] = None,
    prompt_version: Optional[str] = None,
) -> int:
    cursor = await conn.execute(
        """
        INSERT OR IGNORE INTO decision_commits (
            created_at, tick_ts, symbol, mint_address, verdict,
            entry_allowed, nonce, payload_json, payload_hash,
            model_version, prompt_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (created_at, tick_ts, symbol, mint_address, verdict,
         int(entry_allowed), nonce, payload_json, payload_hash,
         model_version, prompt_version),
    )
    await conn.commit()
    return max(cursor.rowcount, 0)


async def insert_llm_call_usage(
    conn: aiosqlite.Connection,
    ts: str,
    task: str,
    provider: str,
    model: str,
    status: str,
    tick_ts: Optional[str] = None,
    mint_address: Optional[str] = None,
    latency_ms: Optional[int] = None,
    input_tokens: Optional[int] = None,
    cache_hit_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    total_tokens: Optional[int] = None,
    estimated_cost_usd: Optional[float] = None,
    is_peak_window: bool = False,
    degradation_reason: Optional[str] = None,
) -> int:
    cursor = await conn.execute(
        """
        INSERT INTO llm_call_usage (
            ts, task, provider, model, tick_ts, mint_address, status,
            latency_ms, input_tokens, cache_hit_tokens, output_tokens,
            total_tokens, estimated_cost_usd, is_peak_window, degradation_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ts, task, provider, model, tick_ts, mint_address, status,
         latency_ms, input_tokens, cache_hit_tokens, output_tokens,
         total_tokens, estimated_cost_usd, int(is_peak_window), degradation_reason),
    )
    await conn.commit()
    return int(cursor.lastrowid)


async def get_llm_call_usage(
    conn: aiosqlite.Connection, limit: int = 100
) -> list[dict[str, Any]]:
    cursor = await conn.execute("SELECT * FROM llm_call_usage ORDER BY id DESC LIMIT ?", (limit,))
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]

# OMO-R7 retro attribution helpers -----------------------------------------

async def get_pending_unsigned_commits(
    conn: aiosqlite.Connection, limit: int = 60
) -> list[dict[str, Any]]:
    """Decision rows with entry_allowed=1 AND signature IS NULL (newest first).
    These are candidates for retro attribution when an out-of-pipeline fill
    is detected. Only rows that intended an entry are eligible."""
    cursor = await conn.execute(
        """
        SELECT id, created_at, symbol, mint_address, verdict, payload_json
        FROM decision_commits
        WHERE entry_allowed = 1 AND signature IS NULL
        ORDER BY created_at DESC LIMIT ?
        """,
        (limit,),
    )
    return [
        {
            "id": row["id"],
            "created_at": row["created_at"],
            "symbol": row["symbol"],
            "mint_address": row["mint_address"],
            "verdict": row["verdict"],
            "payload": json.loads(row["payload_json"]),
        }
        for row in await cursor.fetchall()
    ]


async def get_recent_fills_for_retro(
    conn: aiosqlite.Connection, limit: int = 120
) -> list[dict[str, Any]]:
    """Fills (opened trades) whose signature is not already claimed — candidates
    for retro attribution. A 'fill' here is any opened trade row. Side is
    always 'buy' for opens (the only side our paper engine currently writes)."""
    cursor = await conn.execute(
        """
        SELECT trade_id, symbol, mint_address, opened_at, entry_price_usd,
               position_size_usd
        FROM trades
        WHERE trade_id NOT IN (
            SELECT trade_id FROM trades WHERE trade_id IN (
                SELECT payload_json FROM decision_commits
                WHERE signature IS NOT NULL AND signature = trade_id
            )
        )
        ORDER BY opened_at DESC LIMIT ?
        """,
        (limit,),
    )
    return [
        {
            "trade_id": row["trade_id"],
            "symbol": row["symbol"],
            "mint_address": row["mint_address"],
            "opened_at": row["opened_at"],
            "side": "buy",
        }
        for row in await cursor.fetchall()
    ]


async def bind_commit_signature(
    conn: aiosqlite.Connection,
    commit_id: int,
    signature: str,
    phase: str,
    matched_by: str,
) -> int:
    """Write back signature, phase, and matched_by to a decision commit row.
    Only updates rows that still have signature IS NULL (exact-bind rows are
    never overwritten). Returns affected rowcount."""
    cursor = await conn.execute(
        """
        UPDATE decision_commits
        SET signature = ?, phase = ?, matched_by = ?
        WHERE id = ? AND signature IS NULL
        """,
        (signature, phase, matched_by, commit_id),
    )
    await conn.commit()
    return max(cursor.rowcount, 0)


async def get_trade_by_id(conn: aiosqlite.Connection, trade_id: str) -> Optional[Trade]:
    cursor = await conn.execute("SELECT * FROM trades WHERE trade_id = ?", (trade_id,))
    row = await cursor.fetchone()
    return _row_to_trade(row) if row else None


async def get_open_trade_for_mint(conn: aiosqlite.Connection, mint_address: str) -> Optional[Trade]:
    cursor = await conn.execute(
        "SELECT * FROM trades WHERE mint_address = ? AND is_open = 1 LIMIT 1",
        (mint_address,),
    )
    row = await cursor.fetchone()
    return _row_to_trade(row) if row else None


async def get_open_trades(conn: aiosqlite.Connection) -> list[Trade]:
    cursor = await conn.execute("SELECT * FROM trades WHERE is_open = 1 ORDER BY opened_at")
    return [_row_to_trade(r) for r in await cursor.fetchall()]


async def get_all_closed_trades(conn: aiosqlite.Connection) -> list[Trade]:
    cursor = await conn.execute(
        "SELECT * FROM trades WHERE is_open = 0 ORDER BY closed_at DESC"
    )
    return [_row_to_trade(r) for r in await cursor.fetchall()]


async def get_closed_trades_paginated(
    conn: aiosqlite.Connection, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    cursor = await conn.execute(
        "SELECT * FROM trades WHERE is_open = 0 ORDER BY closed_at DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    return [t.to_dict() for t in (_row_to_trade(r) for r in await cursor.fetchall())]


async def update_reflection(conn: aiosqlite.Connection, trade_id: str, text: str) -> None:
    await conn.execute(
        "UPDATE trades SET reflection_text = ? WHERE trade_id = ?", (text, trade_id)
    )
    await conn.commit()


async def count_trades(conn: aiosqlite.Connection) -> int:
    cursor = await conn.execute("SELECT COUNT(*) FROM trades")
    return int((await cursor.fetchone())[0])


# ===========================================================================
# Portfolio cash — guarded adjustments (cash can never go negative)
# ===========================================================================

async def get_cash_balance(conn: aiosqlite.Connection) -> float:
    cursor = await conn.execute("SELECT cash_usd FROM portfolio_state WHERE id = 1")
    row = await cursor.fetchone()
    if row is None:
        raise RuntimeError("Portfolio state not initialised — call init_db() first.")
    return float(row["cash_usd"])


async def adjust_cash(conn: aiosqlite.Connection, delta: float) -> int:
    """
    Atomic, guarded cash adjustment. The WHERE clause makes the write a
    no-op if it would drive cash negative (rowcount 0 = refused). Returns
    affected row count.
    """
    cursor = await conn.execute(
        "UPDATE portfolio_state SET cash_usd = cash_usd + ?, updated_at = ? "
        "WHERE id = 1 AND cash_usd + ? >= 0",
        (delta, _now_iso(), delta),
    )
    await conn.commit()
    return max(cursor.rowcount, 0)


async def get_first_trade_date(conn: aiosqlite.Connection) -> Optional[str]:
    cursor = await conn.execute("SELECT MIN(opened_at) AS d FROM trades")
    row = await cursor.fetchone()
    return row["d"] if row and row["d"] else None


# ===========================================================================
# Market regime — one row per tick, append-only (C3)
# ===========================================================================

async def insert_market_regime(
    conn: aiosqlite.Connection,
    computed_at: str,
    candidate_count: int,
    pct_green: float,
    median_vol: float,
    avg_ratio: float,
    regime_ok: bool,
    detail: str,
) -> int:
    cursor = await conn.execute(
        """
        INSERT INTO market_regime (
            computed_at, candidate_count, pct_candidates_green_1h,
            median_volume_1h_usd, avg_buy_sell_ratio, regime_ok, regime_detail
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (computed_at, candidate_count, pct_green, median_vol,
         avg_ratio, int(regime_ok), detail),
    )
    await conn.commit()
    return int(cursor.lastrowid)


async def get_recent_regimes(
    conn: aiosqlite.Connection, limit: int = 100
) -> list[dict[str, Any]]:
    cursor = await conn.execute(
        "SELECT * FROM market_regime ORDER BY computed_at DESC LIMIT ?", (limit,)
    )
    rows = await cursor.fetchall()
    return [
        {
            "computed_at": r["computed_at"],
            "candidate_count": r["candidate_count"],
            "pct_candidates_green_1h": r["pct_candidates_green_1h"],
            "median_volume_1h_usd": r["median_volume_1h_usd"],
            "avg_buy_sell_ratio": r["avg_buy_sell_ratio"],
            "regime_ok": bool(r["regime_ok"]),
            "regime_detail": r["regime_detail"],
        }
        for r in rows
    ]


# ===========================================================================
# Provider call counters (A8) — per provider per UTC day
# ===========================================================================

async def record_provider_call(
    conn: aiosqlite.Connection,
    provider: str,
    ok: bool = True,
    rate_limited: bool = False,
) -> None:
    await conn.execute(
        """
        INSERT INTO provider_call_counters
            (provider, day, call_count, error_count, rate_limit_429_count, last_call_at)
        VALUES (?, ?, 1, ?, ?, ?)
        ON CONFLICT(provider, day) DO UPDATE SET
            call_count = call_count + 1,
            error_count = error_count + excluded.error_count,
            rate_limit_429_count = rate_limit_429_count + excluded.rate_limit_429_count,
            last_call_at = excluded.last_call_at
        """,
        (
            provider, _today(),
            0 if ok else 1,
            1 if rate_limited else 0,
            _now_iso(),
        ),
    )
    await conn.commit()


async def get_provider_call_summary(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    cursor = await conn.execute(
        "SELECT * FROM provider_call_counters WHERE day = ? ORDER BY provider",
        (_today(),),
    )
    rows = await cursor.fetchall()
    return [
        {
            "provider": r["provider"], "day": r["day"],
            "call_count": r["call_count"], "error_count": r["error_count"],
            "rate_limit_429_count": r["rate_limit_429_count"],
            "last_call_at": r["last_call_at"],
        }
        for r in rows
    ]


# ===========================================================================
# Knowledge base documents (F2/F5)
# ===========================================================================

async def upsert_kb_document(
    conn: aiosqlite.Connection, filename: str, content: str, digest: str
) -> None:
    await conn.execute(
        """
        INSERT INTO kb_documents (filename, content, digest, ingested_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(filename) DO UPDATE SET
            content = excluded.content,
            digest = excluded.digest,
            ingested_at = excluded.ingested_at
        """,
        (filename, content, digest, _now_iso()),
    )
    await conn.commit()


async def get_kb_documents(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    cursor = await conn.execute(
        "SELECT filename, content, digest, ingested_at FROM kb_documents ORDER BY ingested_at"
    )
    return [dict(r) for r in await cursor.fetchall()]


# ===========================================================================
# Daily stats (learning loop persistence)
# ===========================================================================

async def upsert_daily_stats(conn: aiosqlite.Connection, stats: DailyStats) -> None:
    await conn.execute(
        """
        INSERT INTO daily_stats (date, open_positions, closed_trades, stats_json)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            open_positions = excluded.open_positions,
            closed_trades = excluded.closed_trades,
            stats_json = excluded.stats_json
        """,
        (stats.date, stats.open_positions, stats.closed_trades,
         json.dumps(stats.stats_json)),
    )
    await conn.commit()


# ===========================================================================
# Proof/journal helpers — formerly raw SQL in routes; MUST live here so the
# Postgres backend (db_pg.py) can override them with dialect-correct SQL.
# ===========================================================================

async def count_closed_trades(conn: aiosqlite.Connection) -> int:
    cursor = await conn.execute("SELECT COUNT(*) FROM trades WHERE is_open = 0")
    return int((await cursor.fetchone())[0])


async def get_recent_decision_commits(
    conn: aiosqlite.Connection, limit: int = 100
) -> list[dict[str, Any]]:
    cursor = await conn.execute(
        """
        SELECT id, created_at, tick_ts, symbol, mint_address,
               verdict, entry_allowed, nonce, payload_json, payload_hash
        FROM decision_commits ORDER BY created_at DESC LIMIT ?
        """,
        (limit,),
    )
    return [
        {
            "id": r["id"],
            "created_at": r["created_at"],
            "tick_ts": r["tick_ts"],
            "symbol": r["symbol"],
            "mint": r["mint_address"],
            "think_verdict": r["verdict"],
            "entry_allowed": bool(r["entry_allowed"]),
            "nonce": r["nonce"],
            "payload": json.loads(r["payload_json"]),
            "payload_hash": r["payload_hash"],
        }
        for r in await cursor.fetchall()
    ]


async def get_recent_fills(
    conn: aiosqlite.Connection, limit: int = 100
) -> list[dict[str, Any]]:
    cursor = await conn.execute(
        """
        SELECT trade_id, symbol, mint_address, opened_at,
               entry_price_usd, position_size_usd, thesis,
               closed_at, exit_price_usd, exit_reason,
               realized_pnl_usd, realized_pnl_pct, is_open
        FROM trades ORDER BY opened_at DESC LIMIT ?
        """,
        (limit,),
    )
    return [
        {
            "trade_id": r["trade_id"],
            "symbol": r["symbol"],
            "mint_address": r["mint_address"],
            "opened_at": r["opened_at"],
            "entry_price_usd": r["entry_price_usd"],
            "position_size_usd": r["position_size_usd"],
            "thesis": r["thesis"],
            "closed_at": r["closed_at"],
            "exit_price_usd": r["exit_price_usd"],
            "exit_reason": r["exit_reason"],
            "realized_pnl_usd": r["realized_pnl_usd"],
            "realized_pnl_pct": r["realized_pnl_pct"],
            "is_open": bool(r["is_open"]),
        }
        for r in await cursor.fetchall()
    ]


async def get_open_position_marks(
    conn: aiosqlite.Connection,
) -> list[dict[str, Any]]:
    cursor = await conn.execute(
        """
        SELECT trade_id, symbol, mint_address, entry_price_usd,
               position_size_usd, high_water_usd, tranches_taken,
               opened_at, is_open
        FROM trades WHERE is_open = 1
        """
    )
    return [
        {
            "trade_id": r["trade_id"],
            "symbol": r["symbol"],
            "mint_address": r["mint_address"],
            "entry_price_usd": r["entry_price_usd"],
            "position_size_usd": r["position_size_usd"],
            "high_water_usd": r["high_water_usd"],
            "tranches_taken": r["tranches_taken"],
            "opened_at": r["opened_at"],
            "is_open": bool(r["is_open"]),
        }
        for r in await cursor.fetchall()
    ]


async def get_verify_commits(
    conn: aiosqlite.Connection, limit: int = 200
) -> list[dict[str, Any]]:
    cursor = await conn.execute(
        """
        SELECT id, nonce, payload_json, payload_hash, symbol, verdict,
               created_at, signature
        FROM decision_commits ORDER BY created_at DESC LIMIT ?
        """,
        (limit,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def set_trade_thesis(
    conn: aiosqlite.Connection, trade_id: str, text: str
) -> None:
    """Attach the full thesis to a freshly opened position (open only)."""
    await conn.execute(
        "UPDATE trades SET thesis = ? WHERE trade_id = ? AND is_open = 1",
        (text, trade_id),
    )
    await conn.commit()


async def delete_trade_row(conn: aiosqlite.Connection, trade_id: str) -> int:
    """Rollback helper: remove an unfunded open position (cash refused)."""
    cursor = await conn.execute(
        "DELETE FROM trades WHERE trade_id = ? AND is_open = 1", (trade_id,)
    )
    await conn.commit()
    return max(cursor.rowcount, 0)


# ===========================================================================
# Theses (OMO-R3) - Durable Thesis Book
# ===========================================================================

async def upsert_thesis(
    conn: aiosqlite.Connection,
    trade_id: str,
    mint_address: str,
    symbol: str,
    author: str,
    thesis: str,
    created_at: str,
) -> None:
    await conn.execute(
        """
        INSERT INTO theses (
            trade_id, mint_address, symbol, author, thesis, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(trade_id) DO UPDATE SET
            thesis = excluded.thesis,
            updated_at = excluded.updated_at
        """,
        (trade_id, mint_address, symbol, author, thesis, created_at, _now_iso()),
    )
    await conn.commit()


async def retire_thesis(
    conn: aiosqlite.Connection,
    trade_id: str,
    closed_at: str,
    realized_pnl_usd: float,
) -> None:
    await conn.execute(
        """
        UPDATE theses
        SET closed_at = ?, realized_pnl_usd = ?
        WHERE trade_id = ?
        """,
        (closed_at, realized_pnl_usd, trade_id),
    )
    await conn.commit()


async def get_theses(
    conn: aiosqlite.Connection, limit: int = 100, offset: int = 0
) -> list[dict[str, Any]]:
    cursor = await conn.execute(
        "SELECT * FROM theses ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (limit, offset)
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


# ===========================================================================
# Backend selection — when Supabase is configured (USE_SUPABASE_DB=1 +
# SUPABASE_DB_URL), the Postgres implementation in api/db_pg.py overrides
# every public repository function above; the surface is identical.
#
# Under pytest we FORCE SQLite regardless of .env: the test suite owns its
# own tmp-file databases and must never touch the live remote book.
# ===========================================================================
if config.USE_SUPABASE_DB and config.SUPABASE_DB_URL and "pytest" not in sys.modules:
    from api import db_pg as _pg_backend

    globals().update({
        _name: getattr(_pg_backend, _name)
        for _name in dir(_pg_backend)
        if not _name.startswith("_") and callable(getattr(_pg_backend, _name))
    })
    log.info("DB backend: Supabase Postgres")
else:
    log.info("DB backend: local SQLite at %s", config.DB_PATH)





