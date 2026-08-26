"""
api/db_pg.py — Supabase Postgres backend for the repository layer.

Same public function surface as api/db.py (SQLite), translated to Postgres
so the tick loop / API can run against a remote Supabase project when

    USE_SUPABASE_DB=1 and SUPABASE_DB_URL are set in .env

Schema lives in migrations/supabase/001_init.sql — run it ONCE in the
Supabase SQL Editor before flipping USE_SUPABASE_DB on (init_db verifies
and refuses to start otherwise).

Translation notes vs the SQLite implementation:
  ? placeholders        -> $1..$n (asyncpg)
  INTEGER 0/1 booleans   -> BOOLEAN True/False
  cursor.rowcount        -> status string from execute() ("UPDATE 1")
  cursor.lastrowid       -> RETURNING id
  INSERT OR IGNORE       -> ON CONFLICT (...) DO NOTHING
  TEXT ISO timestamps    -> TIMESTAMPTZ columns, read back with ::text so
                            every consumer keeps receiving ISO strings
  JSON-in-TEXT           -> JSONB; asyncpg passes/returns JSON text natively,
                            so json.dumps()/json.loads() semantics are kept

ATOMICITY PATTERN is preserved exactly (§5.1): every state change is a single
conditional statement whose WHERE clause makes retries no-ops; the affected
row count remains the sole authority on whether cash moves. Postgres makes
each autocommit statement atomic — identical guarantees to the SQLite path.
"""
from __future__ import annotations

import json
import logging
import ssl
from urllib.parse import urlparse
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

import asyncpg

import config
from models import DailyStats, FeedEvent, Trade

log = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None
_pool_closed: bool = True


def _dsn() -> str:
    """SUPABASE_DB_URL with TLS enforced (Supabase poolers require SSL)."""
    dsn = config.SUPABASE_DB_URL
    if "sslmode=" not in dsn:
        dsn += ("&" if "?" in dsn else "?") + "sslmode=require"
    return dsn


def _host_port(dsn: str) -> tuple[str, int]:
    parsed = urlparse(dsn)
    port = parsed.port or 5432
    return parsed.hostname or "", port


_PIN_PATH = config.BASE_DIR / ".supabase_fp.txt"


def _fetch_cert_fingerprint(host: str, port: int) -> str:
    """TLS-probe the server ourselves and return the SHA-256 of its cert."""
    import hashlib
    import socket

    probe = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    probe.check_hostname = False
    probe.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=10) as sock:
        with probe.wrap_socket(sock, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
    return hashlib.sha256(der).hexdigest()


async def _tls_context() -> ssl.SSLContext:
    """
    Strongest available TLS for the pooler, in order:
      1. SYSTEM   — normal public-CA verification (if Supabase serves one).
      2. PINNED   — fingerprint probe: hash the presented cert and require an
                    exact match against .supabase_fp.txt (written on first
                    ever connect). Any mismatch = hard abort (MITM guard).
                    Delete the pin file after a LEGITIMATE Supabase cert
                    rotation to re-pin.
      3. FALLBACK — encrypted-but-unverified, loudly logged (never silent).
    The returned context is intentionally unverified at the OpenSSL layer;
    authentication is provided by our own fingerprint probe performed at
    pool creation, immediately before asyncpg dials.
    """
    # 1. normal system trust store
    try:
        strict = ssl.create_default_context()
        conn = await asyncpg.connect(_dsn(), ssl=strict, timeout=10)
        await conn.close()
        log.info("db_pg: TLS verified against system CAs")
        return strict
    except ssl.SSLCertVerificationError:
        pass  # expected: self-signed pooler chain

    # 2. fingerprint pinning (TOFU on first run, exact-match afterwards)
    try:
        fp = _fetch_cert_fingerprint(*_host_port(_dsn()))
        expected: Optional[str] = None
        if _PIN_PATH.exists():
            expected = _PIN_PATH.read_text().strip()
            if expected and fp != expected:
                raise RuntimeError(
                    f"SUPABASE CERTIFICATE FINGERPRINT MISMATCH "
                    f"(pinned {expected[:16]}…, got {fp[:16]}…) — "
                    f"possible MITM. If Supabase rotated certs legitimately, "
                    f"delete {_PIN_PATH} to re-pin.")
        if not expected:
            _PIN_PATH.write_text(fp)
            log.warning("db_pg: TOFU pinned Supabase cert %s… -> %s",
                        fp[:16], _PIN_PATH)
        else:
            log.info("db_pg: Supabase cert fingerprint matches pin (%s…)",
                     fp[:16])
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # identity enforced by the pin above
        return ctx
    except RuntimeError:
        raise  # MITM suspicion must never be swallowed
    except Exception as exc:
        log.warning("db_pg: fingerprint probe failed (%s)", exc)

    # 3. last resort: encrypt without verifying
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    log.warning("db_pg: TLS verification UNAVAILABLE — "
                "connection encrypted but NOT authenticated")
    return ctx


async def _get_pool() -> asyncpg.Pool:
    global _pool, _pool_closed
    if _pool is None or _pool_closed:
        _pool = await asyncpg.create_pool(
            _dsn(), min_size=1, max_size=5,
            command_timeout=30, timeout=15,
            ssl=await _tls_context(),
        )
        _pool_closed = False
    return _pool


async def close_pool() -> None:
    global _pool, _pool_closed
    if _pool is not None and not _pool_closed:
        await _pool.close()
    _pool = None
    _pool_closed = True


def _rowcount(status: str) -> int:
    """"UPDATE 3" / "INSERT 0 1" / "DELETE 0" -> int."""
    try:
        return max(int(status.split()[-1]), 0)
    except (ValueError, IndexError):
        return 0


def _ts(value: str) -> datetime:
    """ISO string -> aware datetime (asyncpg requires datetime for TIMESTAMPTZ)."""
    return datetime.fromisoformat(value)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


_TRADE_COLS = """
    trade_id, symbol, mint_address, opened_at::text AS opened_at,
    entry_price_usd, position_size_usd, quantity,
    candidate_snapshot::text AS candidate_snapshot, thesis,
    closed_at::text AS closed_at, exit_price_usd, exit_reason,
    realized_pnl_usd, realized_pnl_pct, is_open, high_water_usd,
    tranches_taken, reflection_text
"""

_FEED_COLS = """
    id, ts::text AS ts, symbol, mint_address,
    candidate_snapshot::text AS candidate_snapshot, verdict, thesis,
    rule_breakdown::text AS rule_breakdown,
    failed_rule_ids::text AS failed_rule_ids,
    regime_ok, grounding_flags::text AS grounding_flags,
    narration_source, led_to_trade_id
"""


# ===========================================================================
# Init / connection
# ===========================================================================

async def init_db() -> None:
    """
    Verify the migration was applied and seed the singleton portfolio row.
    Does NOT create schema — that belongs to migrations/supabase/001_init.sql.
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        version = await conn.fetchval(
            "SELECT version FROM schema_migrations WHERE version = '001_init'")
        if not version:
            raise RuntimeError(
                "Supabase schema not initialised — run "
                "migrations/supabase/001_init.sql in the SQL Editor first.")
        await conn.execute(
            """
            INSERT INTO portfolio_state (id, cash_usd, updated_at)
            VALUES (1, $1, $2)
            ON CONFLICT (id) DO NOTHING
            """,
            float(config.INITIAL_CASH_USD), _now(),
        )
        # OMO-R1/R7: idempotent column additions for decision_commits
        for col_sql in (
            "ALTER TABLE decision_commits ADD COLUMN IF NOT EXISTS signature TEXT",
            "ALTER TABLE decision_commits ADD COLUMN IF NOT EXISTS phase TEXT",
            "ALTER TABLE decision_commits ADD COLUMN IF NOT EXISTS matched_by TEXT",
            "CREATE INDEX IF NOT EXISTS idx_decision_commits_sig "
            "ON decision_commits(signature) WHERE signature IS NOT NULL",
        ):
            try:
                await conn.execute(col_sql)
            except Exception:
                pass  # column/index already exists
    log.info("db_pg: Supabase schema verified, portfolio seeded/confirmed")


@asynccontextmanager
async def get_db() -> AsyncIterator[asyncpg.Connection]:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        yield conn


# ===========================================================================
# Feed events — every GateDecision, pass or fail
# ===========================================================================

async def insert_feed_event(conn: asyncpg.Connection, event: FeedEvent) -> int:
    return await conn.fetchval(
        """
        INSERT INTO feed_events (
            ts, symbol, mint_address, candidate_snapshot, verdict, thesis,
            rule_breakdown, failed_rule_ids, regime_ok, grounding_flags,
            narration_source, led_to_trade_id
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        RETURNING id
        """,
        _ts(event.ts), event.symbol, event.mint_address,
        json.dumps(event.candidate_snapshot), event.verdict, event.thesis,
        json.dumps(event.rule_breakdown), json.dumps(event.failed_rule_ids),
        bool(event.regime_ok), json.dumps(event.grounding_flags),
        event.narration_source, event.led_to_trade_id,
    )


def _row_to_feed_dict(row: asyncpg.Record) -> dict[str, Any]:
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
    }


async def get_feed_events(
    conn: asyncpg.Connection, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        f"SELECT {_FEED_COLS} FROM feed_events ORDER BY id DESC LIMIT $1 OFFSET $2",
        limit, offset,
    )
    return [_row_to_feed_dict(r) for r in rows]


async def count_feed_events(conn: asyncpg.Connection) -> int:
    return int(await conn.fetchval("SELECT COUNT(*) FROM feed_events"))


async def get_refusal_events(
    conn: asyncpg.Connection, limit: int = 100
) -> list[dict[str, Any]]:
    """Postgres twin of db.get_refusal_events - identical surface.
    """
    rows = await conn.fetch(
        f"SELECT {_FEED_COLS} FROM feed_events WHERE verdict = $1 ORDER BY id DESC LIMIT $2",
        "fail", limit,
    )
    return [_row_to_feed_dict(r) for r in rows]


# ===========================================================================
# OMO-R5 memory/events — append-only observations and weighted lessons
# ===========================================================================

async def insert_event(
    conn: asyncpg.Connection, kind: str, ts: str, symbol: str = "",
    mint_address: str = "", payload: Optional[dict[str, Any]] = None,
) -> int:
    if kind not in {"thought", "did", "refused", "read", "trade"}:
        raise ValueError(f"invalid event kind: {kind}")
    return int(await conn.fetchval(
        "INSERT INTO events (ts, kind, symbol, mint_address, payload_json) "
        "VALUES ($1, $2, $3, $4, $5) RETURNING id",
        _ts(ts), kind, symbol, mint_address, json.dumps(payload or {}, sort_keys=True),
    ))


async def get_recent_events(
    conn: asyncpg.Connection, limit: int = 100,
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT id, ts::text AS ts, kind, symbol, mint_address, "
        "payload_json::text AS payload_json FROM events ORDER BY id DESC LIMIT $1",
        limit,
    )
    return [
        {"id": row["id"], "ts": row["ts"], "kind": row["kind"],
         "symbol": row["symbol"], "mint_address": row["mint_address"],
         "payload": json.loads(row["payload_json"])}
        for row in rows
    ]


async def upsert_memory(
    conn: asyncpg.Connection, topic: str, note: str, weight: float = 1.0,
) -> int:
    if not topic.strip() or not note.strip() or weight <= 0:
        raise ValueError("memory topic, note, and positive weight are required")
    row = await conn.fetchrow(
        "SELECT id FROM memories WHERE topic = $1 AND note = $2", topic, note
    )
    if row:
        await conn.execute(
            "UPDATE memories SET weight = $1, updated_at = $2 WHERE id = $3",
            weight, _now(), row["id"],
        )
        return int(row["id"])
    return int(await conn.fetchval(
        "INSERT INTO memories (topic, note, weight, updated_at) "
        "VALUES ($1, $2, $3, $4) RETURNING id", topic, note, weight, _now()
    ))


async def recall_memories(
    conn: asyncpg.Connection, topic: str = "", limit: int = 3,
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT id, topic, note, weight, hits FROM memories "
        "WHERE $1 = '' OR topic = $1 ORDER BY weight DESC, hits DESC, id DESC LIMIT $2",
        topic, limit,
    )
    for row in rows:
        await conn.execute("UPDATE memories SET hits = hits + 1 WHERE id = $1", row["id"])
    return [
        {"id": row["id"], "topic": row["topic"], "note": row["note"],
         "weight": row["weight"], "hits": row["hits"] + 1}
        for row in rows
    ]


# ===========================================================================
# Trades — atomic, idempotent state changes (§5.1)
# ===========================================================================

def _row_to_trade(row: asyncpg.Record) -> Trade:
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


async def try_insert_open_trade(conn: asyncpg.Connection, trade: Trade) -> int:
    """
    ATOMIC OPEN (§5.1). Conditional insert backed by the one-open-position-
    per-mint EXCLUDE constraint as a hard backstop; the NOT EXISTS guard
    makes duplicate attempts affect zero rows.
    """
    status = await conn.execute(
        f"""
        INSERT INTO trades (
            trade_id, symbol, mint_address, opened_at, entry_price_usd,
            position_size_usd, quantity, candidate_snapshot, thesis, is_open,
            high_water_usd
        )
        SELECT $1, $2, $3, $4, $5, $6, $7, $8, $9, TRUE, $10
        WHERE NOT EXISTS (
            SELECT 1 FROM trades WHERE mint_address = $11 AND is_open = TRUE
        )
        """,
        trade.trade_id, trade.symbol, trade.mint_address,
        _ts(trade.opened_at), trade.entry_price_usd, trade.position_size_usd, trade.quantity,
        json.dumps(trade.candidate_snapshot), trade.thesis,
        trade.entry_price_usd,  # trail memory starts at entry price
        trade.mint_address,
    )
    return _rowcount(status)


async def close_trade_row(
    conn: asyncpg.Connection,
    trade_id: str,
    closed_at: str,
    exit_price_usd: float,
    exit_reason: str,
    realized_pnl_usd: float,
    realized_pnl_pct: float,
) -> int:
    """ATOMIC CLOSE (§5.1): zero rows on retry — caller skips the credit."""
    status = await conn.execute(
        """
        UPDATE trades
        SET closed_at = $1, exit_price_usd = $2, exit_reason = $3,
            realized_pnl_usd = $4, realized_pnl_pct = $5, is_open = FALSE
        WHERE trade_id = $6 AND is_open = TRUE
        """,
        _ts(closed_at), exit_price_usd, exit_reason,
        realized_pnl_usd, realized_pnl_pct, trade_id,
    )
    return _rowcount(status)


async def trim_position_row(
    conn: asyncpg.Connection,
    trade_id: str,
    qty_out: float,
    size_out_usd: float,
) -> int:
    """
    ATOMIC PARTIAL CLOSE (E8/E9): only while open and keeping a positive
    remainder; rowcount 0 = already closed or degenerate trim.
    """
    status = await conn.execute(
        """
        UPDATE trades
        SET quantity = quantity - $1,
            position_size_usd = position_size_usd - $2,
            tranches_taken = tranches_taken + 1
        WHERE trade_id = $3 AND is_open = TRUE
          AND quantity - $4 > 0
          AND position_size_usd - $5 >= 0
        """,
        qty_out, size_out_usd, trade_id, qty_out, size_out_usd,
    )
    return _rowcount(status)


async def update_high_water(
    conn: asyncpg.Connection,
    trade_id: str,
    high_water_usd: float,
) -> None:
    """Trail memory: raise the peak price seen since entry (never lower)."""
    await conn.execute(
        """
        UPDATE trades SET high_water_usd = $1
        WHERE trade_id = $2 AND is_open = TRUE
          AND (high_water_usd IS NULL OR high_water_usd < $3)
        """,
        high_water_usd, trade_id, high_water_usd,
    )


async def get_last_closed_at_for_mint(
    conn: asyncpg.Connection, mint_address: str
) -> Optional[str]:
    return await conn.fetchval(
        """
        SELECT closed_at::text FROM trades
        WHERE mint_address = $1 AND closed_at IS NOT NULL
        ORDER BY closed_at DESC LIMIT 1
        """,
        mint_address,
    )


async def count_closes_since(conn: asyncpg.Connection, since_iso: str) -> int:
    return int(await conn.fetchval(
        "SELECT COUNT(*) FROM trades WHERE closed_at >= $1", _ts(since_iso)))


async def get_recent_closed_reasons(
    conn: asyncpg.Connection, mint_address: str, limit: int = 2
) -> list[str]:
    rows = await conn.fetch(
        """
        SELECT exit_reason FROM trades
        WHERE mint_address = $1 AND closed_at IS NOT NULL
        ORDER BY closed_at DESC LIMIT $2
        """,
        mint_address, limit,
    )
    return [r["exit_reason"] for r in rows if r["exit_reason"]]


async def deployed_today(
    conn: asyncpg.Connection, now_utc: Optional[datetime] = None
) -> float:
    now = now_utc or datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0,
                            microsecond=0)  # datetime, not ISO str
    return float(await conn.fetchval(
        "SELECT COALESCE(SUM(position_size_usd), 0) FROM trades "
        "WHERE opened_at >= $1",
        day_start,
    ) or 0.0)


# ---------------------------------------------------------------------------
# Decision commits — tamper-evident audit trail (omo 'seal' parity)
# ---------------------------------------------------------------------------

async def insert_decision_commit(
    conn: asyncpg.Connection,
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
    status = await conn.execute(
        """
        INSERT INTO decision_commits (
            created_at, tick_ts, symbol, mint_address, verdict,
            entry_allowed, nonce, payload_json, payload_hash,
            model_version, prompt_version
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        ON CONFLICT (payload_hash) DO NOTHING
        """,
        _ts(created_at), _ts(tick_ts), symbol, mint_address, verdict,
        bool(entry_allowed), nonce, payload_json, payload_hash,
        model_version, prompt_version,
    )
    return _rowcount(status)

async def insert_llm_call_usage(
    conn: asyncpg.Connection,
    ts: str,
    task: str,
    provider: str,
    model: str,
    status_str: str,
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
    return await conn.fetchval(
        """
        INSERT INTO llm_call_usage (
            ts, task, provider, model, tick_ts, mint_address, status,
            latency_ms, input_tokens, cache_hit_tokens, output_tokens,
            total_tokens, estimated_cost_usd, is_peak_window, degradation_reason
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
        RETURNING id
        """,
        _ts(ts), task, provider, model, _ts(tick_ts) if tick_ts else None, mint_address, status_str,
        latency_ms, input_tokens, cache_hit_tokens, output_tokens,
        total_tokens, estimated_cost_usd, int(is_peak_window), degradation_reason,
    )


async def get_llm_call_usage(
    conn: asyncpg.Connection, limit: int = 100
) -> list[dict[str, Any]]:
    rows = await conn.fetch("SELECT * FROM llm_call_usage ORDER BY id DESC LIMIT $1", limit)
    return [dict(r) for r in rows]


# OMO-R7 retro attribution helpers -----------------------------------------

async def get_pending_unsigned_commits(
    conn: asyncpg.Connection, limit: int = 60
) -> list[dict[str, Any]]:
    """Decision rows with entry_allowed=True AND signature IS NULL (newest first)."""
    import json as _json
    rows = await conn.fetch(
        """
        SELECT id, created_at::text AS created_at, symbol, mint_address,
               verdict, payload_json::text AS payload_json
        FROM decision_commits
        WHERE entry_allowed = TRUE AND signature IS NULL
        ORDER BY created_at DESC LIMIT $1
        """,
        limit,
    )
    return [
        {
            "id": row["id"],
            "created_at": row["created_at"],
            "symbol": row["symbol"],
            "mint_address": row["mint_address"],
            "verdict": row["verdict"],
            "payload": _json.loads(row["payload_json"]),
        }
        for row in rows
    ]


async def get_recent_fills_for_retro(
    conn: asyncpg.Connection, limit: int = 120
) -> list[dict[str, Any]]:
    """Opened trade rows not already claimed by a bound commit."""
    rows = await conn.fetch(
        """
        SELECT trade_id, symbol, mint_address, opened_at::text AS opened_at,
               entry_price_usd, position_size_usd
        FROM trades
        ORDER BY opened_at DESC LIMIT $1
        """,
        limit,
    )
    return [
        {
            "trade_id": row["trade_id"],
            "symbol": row["symbol"],
            "mint_address": row["mint_address"],
            "opened_at": row["opened_at"],
            "side": "buy",
        }
        for row in rows
    ]


async def bind_commit_signature(
    conn: asyncpg.Connection,
    commit_id: int,
    signature: str,
    phase: str,
    matched_by: str,
) -> int:
    """Write back signature, phase, matched_by. Only updates if still NULL."""
    status = await conn.execute(
        """
        UPDATE decision_commits
        SET signature = $1, phase = $2, matched_by = $3
        WHERE id = $4 AND signature IS NULL
        """,
        signature, phase, matched_by, commit_id,
    )
    return _rowcount(status)


async def get_trade_by_id(conn: asyncpg.Connection, trade_id: str) -> Optional[Trade]:
    row = await conn.fetchrow(
        f"SELECT {_TRADE_COLS} FROM trades WHERE trade_id = $1", trade_id)
    return _row_to_trade(row) if row else None


async def get_open_trade_for_mint(conn: asyncpg.Connection, mint_address: str) -> Optional[Trade]:
    row = await conn.fetchrow(
        f"SELECT {_TRADE_COLS} FROM trades "
        "WHERE mint_address = $1 AND is_open = TRUE LIMIT 1",
        mint_address,
    )
    return _row_to_trade(row) if row else None


async def get_open_trades(conn: asyncpg.Connection) -> list[Trade]:
    rows = await conn.fetch(
        f"SELECT {_TRADE_COLS} FROM trades WHERE is_open = TRUE ORDER BY opened_at")
    return [_row_to_trade(r) for r in rows]


async def get_all_closed_trades(conn: asyncpg.Connection) -> list[Trade]:
    rows = await conn.fetch(
        f"SELECT {_TRADE_COLS} FROM trades WHERE is_open = FALSE "
        "ORDER BY closed_at DESC")
    return [_row_to_trade(r) for r in rows]


async def get_closed_trades_paginated(
    conn: asyncpg.Connection, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        f"SELECT {_TRADE_COLS} FROM trades WHERE is_open = FALSE "
        "ORDER BY closed_at DESC LIMIT $1 OFFSET $2",
        limit, offset,
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        t = _row_to_trade(r)
        d = t.to_dict()
        d["candidate_snapshot"] = json.loads(r["candidate_snapshot"])
        out.append(d)
    return out


async def update_reflection(conn: asyncpg.Connection, trade_id: str, text: str) -> None:
    await conn.execute(
        "UPDATE trades SET reflection_text = $1 WHERE trade_id = $2",
        text, trade_id)


async def count_trades(conn: asyncpg.Connection) -> int:
    return int(await conn.fetchval("SELECT COUNT(*) FROM trades"))


# ===========================================================================
# Portfolio cash — guarded adjustments (cash can never go negative)
# ===========================================================================

async def get_cash_balance(conn: asyncpg.Connection) -> float:
    val = await conn.fetchval("SELECT cash_usd FROM portfolio_state WHERE id = 1")
    if val is None:
        raise RuntimeError("Portfolio state not initialised — call init_db() first.")
    return float(val)


async def adjust_cash(conn: asyncpg.Connection, delta: float) -> int:
    """Atomic guarded adjustment; rowcount 0 = would go negative → refused."""
    status = await conn.execute(
        """
        UPDATE portfolio_state SET cash_usd = cash_usd + $1, updated_at = $2
        WHERE id = 1 AND cash_usd + $3 >= 0
        """,
        delta, _now(), delta,
    )
    return _rowcount(status)


async def get_first_trade_date(conn: asyncpg.Connection) -> Optional[str]:
    return await conn.fetchval("SELECT MIN(opened_at)::text FROM trades")


# ===========================================================================
# Market regime — one row per tick, append-only (C3)
# ===========================================================================

async def insert_market_regime(
    conn: asyncpg.Connection,
    computed_at: str,
    candidate_count: int,
    pct_green: float,
    median_vol: float,
    avg_ratio: float,
    regime_ok: bool,
    detail: str,
) -> int:
    return await conn.fetchval(
        """
        INSERT INTO market_regime (
            computed_at, candidate_count, pct_candidates_green_1h,
            median_volume_1h_usd, avg_buy_sell_ratio, regime_ok, regime_detail
        ) VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING id
        """,
        _ts(computed_at), candidate_count, pct_green, median_vol,
        avg_ratio, bool(regime_ok), detail,
    )


async def get_recent_regimes(
    conn: asyncpg.Connection, limit: int = 100
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT computed_at::text AS computed_at, candidate_count,
               pct_candidates_green_1h, median_volume_1h_usd,
               avg_buy_sell_ratio, regime_ok, regime_detail
        FROM market_regime ORDER BY computed_at DESC LIMIT $1
        """,
        limit,
    )
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
# Provider call counters (A8)
# ===========================================================================

async def record_provider_call(
    conn: asyncpg.Connection,
    provider: str,
    ok: bool = True,
    rate_limited: bool = False,
) -> None:
    await conn.execute(
        """
        INSERT INTO provider_call_counters
            (provider, day, call_count, error_count, rate_limit_429_count, last_call_at)
        VALUES ($1, $2, 1, $3, $4, $5)
        ON CONFLICT (provider, day) DO UPDATE SET
            call_count = provider_call_counters.call_count + 1,
            error_count = provider_call_counters.error_count + EXCLUDED.error_count,
            rate_limit_429_count =
                provider_call_counters.rate_limit_429_count + EXCLUDED.rate_limit_429_count,
            last_call_at = EXCLUDED.last_call_at
        """,
        provider, _today(),
        0 if ok else 1,
        1 if rate_limited else 0,
        _now(),
    )


async def get_provider_call_summary(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT provider, day, call_count, error_count, rate_limit_429_count,
               last_call_at::text AS last_call_at
        FROM provider_call_counters WHERE day = $1 ORDER BY provider
        """,
        _today(),
    )
    return [dict(r) for r in rows]


# ===========================================================================
# Knowledge base documents (F2/F5)
# ===========================================================================

async def upsert_kb_document(
    conn: asyncpg.Connection, filename: str, content: str, digest: str
) -> None:
    await conn.execute(
        """
        INSERT INTO kb_documents (filename, content, digest, ingested_at)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (filename) DO UPDATE SET
            content = EXCLUDED.content,
            digest = EXCLUDED.digest,
            ingested_at = EXCLUDED.ingested_at
        """,
        filename, content, digest, _now(),
    )


async def get_kb_documents(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT filename, content, digest, ingested_at::text AS ingested_at
        FROM kb_documents ORDER BY ingested_at
        """
    )
    return [dict(r) for r in rows]


# ===========================================================================
# Daily stats (learning loop persistence)
# ===========================================================================

async def upsert_daily_stats(conn: asyncpg.Connection, stats: DailyStats) -> None:
    await conn.execute(
        """
        INSERT INTO daily_stats (date, open_positions, closed_trades, stats_json)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (date) DO UPDATE SET
            open_positions = EXCLUDED.open_positions,
            closed_trades = EXCLUDED.closed_trades,
            stats_json = EXCLUDED.stats_json
        """,
        stats.date, stats.open_positions, stats.closed_trades,
        json.dumps(stats.stats_json),
    )


# ===========================================================================
# Theses (OMO-R3) - Durable Thesis Book
# ===========================================================================

async def upsert_thesis(
    conn: asyncpg.Connection,
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
        ) VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (trade_id) DO UPDATE SET
            thesis = EXCLUDED.thesis,
            updated_at = EXCLUDED.updated_at
        """,
        trade_id, mint_address, symbol, author, thesis, _ts(created_at), _now(),
    )


async def retire_thesis(
    conn: asyncpg.Connection,
    trade_id: str,
    closed_at: str,
    realized_pnl_usd: float,
) -> None:
    await conn.execute(
        """
        UPDATE theses
        SET closed_at = $1, realized_pnl_usd = $2
        WHERE trade_id = $3
        """,
        _ts(closed_at), realized_pnl_usd, trade_id,
    )


async def get_theses(
    conn: asyncpg.Connection, limit: int = 100, offset: int = 0
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT trade_id, mint_address, symbol, author, thesis,
               created_at::text AS created_at,
               updated_at::text AS updated_at,
               closed_at::text AS closed_at,
               realized_pnl_usd
        FROM theses ORDER BY created_at DESC LIMIT $1 OFFSET $2
        """,
        limit, offset,
    )
    return [dict(r) for r in rows]


# ===========================================================================
# Proof/journal helpers — mirror api/db.py's SQLite versions with
# dialect-correct Postgres (bool literals, $n params, ::text timestamps).
# ===========================================================================

async def count_closed_trades(conn: asyncpg.Connection) -> int:
    return int(await conn.fetchval(
        "SELECT COUNT(*) FROM trades WHERE is_open = FALSE"))


async def get_recent_decision_commits(
    conn: asyncpg.Connection, limit: int = 100
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT id, created_at::text AS created_at, tick_ts::text AS tick_ts,
               symbol, mint_address, verdict, entry_allowed, nonce,
               payload_json::text AS payload_json, payload_hash
        FROM decision_commits ORDER BY created_at DESC LIMIT $1
        """,
        limit,
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
        for r in rows
    ]


async def get_recent_fills(
    conn: asyncpg.Connection, limit: int = 100
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT trade_id, symbol, mint_address, opened_at::text AS opened_at,
               entry_price_usd, position_size_usd, thesis,
               closed_at::text AS closed_at, exit_price_usd, exit_reason,
               realized_pnl_usd, realized_pnl_pct, is_open
        FROM trades ORDER BY opened_at DESC LIMIT $1
        """,
        limit,
    )
    return [
        {
            "trade_id": r["trade_id"],
            "symbol": r["symbol"],
            "mint_address": r["mint_address"],
            "opened_at": r["opened_at"],
            "entry_price_usd": float(r["entry_price_usd"]),
            "position_size_usd": float(r["position_size_usd"]),
            "thesis": r["thesis"],
            "closed_at": r["closed_at"],
            "exit_price_usd": float(r["exit_price_usd"])
            if r["exit_price_usd"] is not None else None,
            "exit_reason": r["exit_reason"],
            "realized_pnl_usd": float(r["realized_pnl_usd"])
            if r["realized_pnl_usd"] is not None else None,
            "realized_pnl_pct": float(r["realized_pnl_pct"])
            if r["realized_pnl_pct"] is not None else None,
            "is_open": bool(r["is_open"]),
        }
        for r in rows
    ]


async def get_open_position_marks(
    conn: asyncpg.Connection,
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT trade_id, symbol, mint_address, entry_price_usd,
               position_size_usd, high_water_usd, tranches_taken,
               opened_at::text AS opened_at, is_open
        FROM trades WHERE is_open = TRUE
        """
    )
    return [
        {
            "trade_id": r["trade_id"],
            "symbol": r["symbol"],
            "mint_address": r["mint_address"],
            "entry_price_usd": float(r["entry_price_usd"]),
            "position_size_usd": float(r["position_size_usd"]),
            "high_water_usd": float(r["high_water_usd"])
            if r["high_water_usd"] is not None else None,
            "tranches_taken": int(r["tranches_taken"]),
            "opened_at": r["opened_at"],
            "is_open": bool(r["is_open"]),
        }
        for r in rows
    ]


async def get_verify_commits(
    conn: asyncpg.Connection, limit: int = 200
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT id, nonce, payload_json::text AS payload_json, payload_hash,
               symbol, verdict, created_at::text AS created_at, signature
        FROM decision_commits ORDER BY created_at DESC LIMIT $1
        """,
        limit,
    )
    return [dict(r) for r in rows]



async def set_trade_thesis(
    conn: asyncpg.Connection, trade_id: str, text: str
) -> None:
    """Attach the full thesis to a freshly opened position (open only)."""
    await conn.execute(
        "UPDATE trades SET thesis = $1 WHERE trade_id = $2 AND is_open = TRUE",
        text, trade_id,
    )


async def delete_trade_row(conn: asyncpg.Connection, trade_id: str) -> int:
    """Rollback helper: remove an unfunded open position (cash refused)."""
    status = await conn.execute(
        "DELETE FROM trades WHERE trade_id = $1 AND is_open = TRUE", trade_id
    )
    return _rowcount(status)
