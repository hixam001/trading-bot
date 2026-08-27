"""
tests/test_thesis_restate.py — A11 thesis re-authoring (omo audit §30).

Reference: omotrades/omo src/lib/thesis-author.server.ts (restateTheses).

Properties verified (every expectation hand-computed):
   1. parse_ts: valid ISO (aware + naive) parses; garbage/None/non-str -> None
   2. is_due: stale model row due; fresh model row NOT due; non-model author
      due even when fresh; unparseable updated_at due (fail toward refreshing)
   3. select_due: caps at per_pass, oldest text first, broken timestamps first
   4. validate_restatement: rejects non-str/empty/short(<20)/oversized(>1000)
   5. position_numbers: unrealized P&L matches the house money-math
      (compute_unrealized_pnl, slippage 2% + fee 1%) to the cent
   6. build_brief: carries symbol/mint/size/mark/pnl/write-up; degrades to
      "(numbers unavailable this pass)" with no position row
   7. DB layer: get_open_theses returns only open rows oldest-first;
      update_thesis_text rewrites open rows (rowcount 1) and REFUSES rows
      retired mid-pass (rowcount 0, text untouched); db_pg surface parity
   8. Orchestration: mock mode is a no-op; a valid rewrite updates text +
      author, journals a "did" event and an llm_call_usage row (task
      thesis_restate); short output rejected (old text kept); provider
      failure fails closed (no write, no raise); deepseek peak window skips
      the pass with zero HTTP; per_pass caps LLM calls; a DB error never
      raises into the tick.

HTTP is mocked with httpx.MockTransport (built into httpx — no new deps).
DB tests run against a fresh tmp SQLite file (pytest forces SQLite).
"""
from __future__ import annotations

import os
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import aiosqlite
import httpx
import pytest

import config

# Force SQLite in tests — do not touch operator DB
os.environ.setdefault("DATA_BACKEND", "mock")

import thesis_restate
from llm.client import MainGroqClient
from models import Trade


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NOW = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _ago(**kw) -> str:
    """Real-now-relative ISO stamp (orchestration uses datetime.now())."""
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


def _row(symbol="ALPHA", mint="MintAlpha111111111111111111111111111111111",
         author="model:groq:test", thesis="original write-up text",
         updated_at=None, trade_id=None) -> dict:
    return {
        "trade_id": trade_id or f"t-{symbol.lower()}",
        "mint_address": mint,
        "symbol": symbol,
        "author": author,
        "thesis": thesis,
        "created_at": _iso(NOW - timedelta(days=1)),
        "updated_at": updated_at if updated_at is not None else _iso(NOW),
    }


def _chat_body(content: str, prompt_tokens=300, completion_tokens=60) -> dict:
    return {
        "id": "chatcmpl-test",
        "choices": [{
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": content},
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _mock_llm(handler) -> MainGroqClient:
    c = MainGroqClient(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    c.api_key = "test-key"   # house convention: mocked transport, dummy key
    return c


def _ok_llm(content: str) -> MainGroqClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_chat_body(content))
    return _mock_llm(handler)


async def _setup_db(db_path: str) -> None:
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


async def _seed_thesis(conn, row: dict) -> None:
    """Insert a thesis row, then backdate updated_at for staleness tests
    (test-only setup; the app never backdates)."""
    from api import db
    await db.upsert_thesis(
        conn, trade_id=row["trade_id"], mint_address=row["mint_address"],
        symbol=row["symbol"], author=row["author"], thesis=row["thesis"],
        created_at=row["created_at"],
    )
    await conn.execute(
        "UPDATE theses SET updated_at = ? WHERE trade_id = ?",
        (row["updated_at"], row["trade_id"]),
    )
    await conn.commit()


def _trade(mint="MintAlpha111111111111111111111111111111111") -> Trade:
    return Trade(
        trade_id="t-alpha", symbol="ALPHA", mint_address=mint,
        opened_at=_iso(NOW - timedelta(days=1)),
        entry_price_usd=0.001, position_size_usd=100.0, quantity=100_000.0,
        candidate_snapshot={}, thesis="x", is_open=True,
    )


@pytest.fixture
def live_mode(monkeypatch):
    """Deterministic live-mode config for orchestration tests."""
    monkeypatch.setattr(config, "DATA_BACKEND", "live")
    monkeypatch.setattr(config, "MAIN_LLM_PROVIDER", "groq")
    monkeypatch.setattr(thesis_restate, "_is_peak_window", lambda: False)
    return monkeypatch


# ---------------------------------------------------------------------------
# 1-2. parse_ts / is_due
# ---------------------------------------------------------------------------

def test_parse_ts_valid_and_invalid():
    assert thesis_restate.parse_ts(_iso(NOW)) == NOW
    # naive timestamps are assumed UTC
    assert thesis_restate.parse_ts("2026-08-27T12:00:00") == NOW
    assert thesis_restate.parse_ts("not-a-date") is None
    assert thesis_restate.parse_ts("") is None
    assert thesis_restate.parse_ts(None) is None
    assert thesis_restate.parse_ts(12345) is None


def test_is_due_stale_model_row():
    row = _row(updated_at=_iso(NOW - timedelta(hours=7)))
    assert thesis_restate.is_due(row, NOW, 6.0) is True


def test_is_due_fresh_model_row_not_due():
    row = _row(updated_at=_iso(NOW - timedelta(hours=1)))
    assert thesis_restate.is_due(row, NOW, 6.0) is False


def test_is_due_boundary_not_stale():
    # exactly at the horizon is NOT yet stale (strictly older is required)
    row = _row(updated_at=_iso(NOW - timedelta(hours=6)))
    assert thesis_restate.is_due(row, NOW, 6.0) is False


def test_is_due_non_model_author_due_even_when_fresh():
    row = _row(author="operator", updated_at=_iso(NOW - timedelta(minutes=5)))
    assert thesis_restate.is_due(row, NOW, 6.0) is True


def test_is_due_unparseable_timestamp_is_due():
    row = _row(updated_at="garbage")
    assert thesis_restate.is_due(row, NOW, 6.0) is True


# ---------------------------------------------------------------------------
# 3. select_due — cap + ordering
# ---------------------------------------------------------------------------

def test_select_due_caps_at_per_pass_oldest_first():
    rows = [
        _row(symbol="NEW", updated_at=_iso(NOW - timedelta(hours=7))),
        _row(symbol="OLD", updated_at=_iso(NOW - timedelta(hours=20))),
        _row(symbol="MID", updated_at=_iso(NOW - timedelta(hours=10))),
        _row(symbol="FRESH", updated_at=_iso(NOW - timedelta(minutes=10))),
    ]
    due = thesis_restate.select_due(rows, NOW, 6.0, 2)
    assert [r["symbol"] for r in due] == ["OLD", "MID"]   # FRESH not due


def test_select_due_broken_timestamps_sort_first():
    rows = [
        _row(symbol="OLD", updated_at=_iso(NOW - timedelta(hours=20))),
        _row(symbol="BROKEN", updated_at="garbage"),
    ]
    due = thesis_restate.select_due(rows, NOW, 6.0, 5)
    assert [r["symbol"] for r in due] == ["BROKEN", "OLD"]


def test_select_due_empty_when_nothing_due():
    rows = [_row(updated_at=_iso(NOW - timedelta(minutes=1)))]
    assert thesis_restate.select_due(rows, NOW, 6.0, 2) == []


# ---------------------------------------------------------------------------
# 4. validate_restatement
# ---------------------------------------------------------------------------

def test_validate_restatement_bounds():
    good = "base holds, buyers still lead; out if the hour flips red."
    assert thesis_restate.validate_restatement(good) == good
    assert thesis_restate.validate_restatement("  " + good + "  ") == good
    assert thesis_restate.validate_restatement("x" * 19) is None      # short
    assert thesis_restate.validate_restatement("x" * 1001) is None    # oversized
    assert thesis_restate.validate_restatement("") is None
    assert thesis_restate.validate_restatement(None) is None
    assert thesis_restate.validate_restatement(123) is None


# ---------------------------------------------------------------------------
# 5-6. position_numbers + build_brief
# ---------------------------------------------------------------------------

def test_position_numbers_reuses_house_pnl_math():
    # Hand-computed: gross = 100000 * 0.0012 = 120.00
    #   net = 120 * (1 - 0.02) * (1 - 0.01) = 120 * 0.98 * 0.99 = 116.424
    #   pnl_usd = 116.424 - 100 = 16.424; pnl_pct = 16.424%
    pos = thesis_restate.position_numbers(_trade(), 0.0012)
    assert pos["size_usd"] == 100.0
    assert pos["entry_price_usd"] == 0.001
    assert pos["current_price_usd"] == 0.0012
    assert pos["unrealized_usd"] == 16.424
    assert pos["unrealized_pct"] == 16.42


def test_position_numbers_no_trade_no_price():
    pos = thesis_restate.position_numbers(None, None)
    assert pos["size_usd"] is None
    assert pos["current_price_usd"] is None
    assert pos["unrealized_usd"] is None


def test_build_brief_carries_numbers_and_text():
    row = _row(thesis="entered on buyers leading the hour")
    pos = thesis_restate.position_numbers(_trade(), 0.0012)
    brief = thesis_restate.build_brief(row, pos)
    assert "name: ALPHA (MintAlpha111111111111111111111111111111111)" in brief
    assert "$100.00 open" in brief
    assert "current mark $0.00120000" in brief
    assert "unrealized $+16.42 (+16.4%)" in brief
    assert "current write-up: entered on buyers leading the hour" in brief


def test_build_brief_degrades_without_position():
    brief = thesis_restate.build_brief(_row(),
                                       thesis_restate.position_numbers(None, None))
    assert "(numbers unavailable this pass)" in brief
    assert "current write-up: original write-up text" in brief


# ---------------------------------------------------------------------------
# 7. DB layer (fresh tmp SQLite) + PG surface parity
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_open_theses_excludes_retired_orders_oldest_first():
    from api import db
    async with _test_db() as conn:
        await _seed_thesis(conn, _row(symbol="OLD", trade_id="t-old",
                                      updated_at=_iso(NOW - timedelta(hours=20))))
        await _seed_thesis(conn, _row(symbol="NEW", trade_id="t-new",
                                      updated_at=_iso(NOW - timedelta(hours=1))))
        await _seed_thesis(conn, _row(symbol="GONE", trade_id="t-gone",
                                      updated_at=_iso(NOW - timedelta(hours=30))))
        await db.retire_thesis(conn, "t-gone", _iso(NOW), -12.5)

        rows = await db.get_open_theses(conn)
        assert [r["symbol"] for r in rows] == ["OLD", "NEW"]   # GONE retired


@pytest.mark.asyncio
async def test_update_thesis_text_rewrites_open_row():
    from api import db
    async with _test_db() as conn:
        await _seed_thesis(conn, _row(trade_id="t-alpha",
                                      updated_at=_iso(NOW - timedelta(hours=9))))
        written = await db.update_thesis_text(
            conn, "t-alpha", "advanced write-up, base still holds",
            "model:groq:test-model")
        assert written == 1
        rows = await db.get_theses(conn)
        row = next(r for r in rows if r["trade_id"] == "t-alpha")
        assert row["thesis"] == "advanced write-up, base still holds"
        assert row["author"] == "model:groq:test-model"
        # updated_at moved forward (bumped by the write itself)
        assert thesis_restate.parse_ts(row["updated_at"]) > NOW - timedelta(hours=9)
        assert row["closed_at"] is None


@pytest.mark.asyncio
async def test_update_thesis_text_refuses_retired_row():
    from api import db
    async with _test_db() as conn:
        await _seed_thesis(conn, _row(trade_id="t-alpha"))
        await db.retire_thesis(conn, "t-alpha", _iso(NOW), 5.0)
        written = await db.update_thesis_text(
            conn, "t-alpha", "must never be written", "model:groq:x")
        assert written == 0
        rows = await db.get_theses(conn)
        row = next(r for r in rows if r["trade_id"] == "t-alpha")
        assert row["thesis"] == "original write-up text"      # untouched
        assert row["realized_pnl_usd"] == 5.0                 # retirement intact


def test_db_pg_surface_parity():
    """Both backends expose the A11 surface (lockstep rule)."""
    from api import db_pg
    assert callable(getattr(db_pg, "get_open_theses", None))
    assert callable(getattr(db_pg, "update_thesis_text", None))


# ---------------------------------------------------------------------------
# 8. Orchestration (mocked LLM, fresh tmp DB)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_restate_noop_in_mock_mode(monkeypatch):
    async with _test_db() as conn:
        await _seed_thesis(conn, _row(updated_at=_ago(hours=9)))

        def _explode():
            raise AssertionError("no LLM client may be built in mock mode")
        monkeypatch.setattr(thesis_restate, "build_main_client", _explode)

        out = await thesis_restate.restate_theses(conn, [_trade()], {})
        assert out == []


@pytest.mark.asyncio
async def test_restate_writes_valid_restatement(live_mode):
    from api import db
    good = "base still holds with buyers leading; out if the hour flips red."
    async with _test_db() as conn:
        await _seed_thesis(conn, _row(trade_id="t-alpha",
                                      updated_at=_ago(hours=9)))
        live_mode.setattr(thesis_restate, "build_main_client",
                          lambda: _ok_llm(good))

        out = await thesis_restate.restate_theses(
            conn, [_trade()],
            {"MintAlpha111111111111111111111111111111111": 0.0012})

        assert len(out) == 1
        assert out[0]["trade_id"] == "t-alpha"
        assert out[0]["before"] == "original write-up text"
        assert out[0]["after"] == good
        rows = await db.get_theses(conn)
        row = next(r for r in rows if r["trade_id"] == "t-alpha")
        assert row["thesis"] == good
        assert row["author"] == f"model:groq:{config.GROQ_MODEL}"
        # journaled loudly: a "did" event + a usage row (task thesis_restate)
        events = await db.get_recent_events(conn, limit=50)
        assert any(e["kind"] == "did"
                   and e["payload"].get("action") == "thesis_restate"
                   and e["payload"].get("trade_id") == "t-alpha"
                   for e in events)
        usages = await db.get_llm_call_usage(conn, limit=50)
        assert any(u["task"] == "thesis_restate" and u["status"] == "success"
                   for u in usages)


@pytest.mark.asyncio
async def test_restate_rejects_short_output(live_mode):
    from api import db
    async with _test_db() as conn:
        await _seed_thesis(conn, _row(trade_id="t-alpha",
                                      updated_at=_ago(hours=9)))
        live_mode.setattr(thesis_restate, "build_main_client",
                          lambda: _ok_llm("too short"))

        out = await thesis_restate.restate_theses(conn, [_trade()], {})
        assert out == []
        rows = await db.get_theses(conn)
        row = next(r for r in rows if r["trade_id"] == "t-alpha")
        assert row["thesis"] == "original write-up text"      # old text kept
        events = await db.get_recent_events(conn, limit=50)
        assert not any(e["payload"].get("action") == "thesis_restate"
                       for e in events)


@pytest.mark.asyncio
async def test_restate_fails_closed_on_provider_error(live_mode):
    from api import db

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    async with _test_db() as conn:
        await _seed_thesis(conn, _row(trade_id="t-alpha",
                                      updated_at=_ago(hours=9)))
        live_mode.setattr(thesis_restate, "build_main_client",
                          lambda: _mock_llm(handler))

        out = await thesis_restate.restate_theses(conn, [_trade()], {})
        assert out == []                                       # no raise
        rows = await db.get_theses(conn)
        row = next(r for r in rows if r["trade_id"] == "t-alpha")
        assert row["thesis"] == "original write-up text"
        usages = await db.get_llm_call_usage(conn, limit=50)
        assert any(u["task"] == "thesis_restate" and u["status"] == "error"
                   for u in usages)                            # degradation seen


@pytest.mark.asyncio
async def test_restate_skips_deepseek_peak_window(monkeypatch):
    async with _test_db() as conn:
        await _seed_thesis(conn, _row(updated_at=_ago(hours=9)))
        monkeypatch.setattr(config, "DATA_BACKEND", "live")
        monkeypatch.setattr(config, "MAIN_LLM_PROVIDER", "deepseek")
        monkeypatch.setattr(thesis_restate, "_is_peak_window", lambda: True)

        def _explode():
            raise AssertionError("no LLM call during deepseek peak window")
        monkeypatch.setattr(thesis_restate, "build_main_client", _explode)

        out = await thesis_restate.restate_theses(conn, [_trade()], {})
        assert out == []


@pytest.mark.asyncio
async def test_restate_caps_llm_calls_at_per_pass(live_mode):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_chat_body(
            f"advanced write-up number {calls['n']}, base still holds"))

    async with _test_db() as conn:
        for i, sym in enumerate(["AAA", "BBB", "CCC"]):
            await _seed_thesis(conn, _row(
                symbol=sym, trade_id=f"t-{i}",
                mint=f"Mint{i}xxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
                updated_at=_ago(hours=9 + i)))
        live_mode.setattr(thesis_restate, "build_main_client",
                          lambda: _mock_llm(handler))

        out = await thesis_restate.restate_theses(conn, [], {})
        assert calls["n"] == config.THESIS_RESTATE_PER_PASS == len(out)


async def _zero() -> int:
    return 0


@pytest.mark.asyncio
async def test_restate_row_retired_mid_pass(live_mode, monkeypatch):
    from api import db
    good = "advanced write-up; base still holds, buyers lead the hour."
    async with _test_db() as conn:
        await _seed_thesis(conn, _row(trade_id="t-alpha",
                                      updated_at=_ago(hours=9)))
        live_mode.setattr(thesis_restate, "build_main_client",
                          lambda: _ok_llm(good))
        # Simulate the close race: the write guard reports the row retired.
        monkeypatch.setattr(db, "update_thesis_text", lambda *a, **kw: _zero())

        out = await thesis_restate.restate_theses(conn, [_trade()], {})
        assert out == []
        events = await db.get_recent_events(conn, limit=50)
        assert not any(e["payload"].get("action") == "thesis_restate"
                       for e in events)


@pytest.mark.asyncio
async def test_restate_never_raises_on_db_error(live_mode):
    from api import db

    async def _boom(conn):
        raise RuntimeError("db unreadable")
    live_mode.setattr(db, "get_open_theses", _boom)

    out = await thesis_restate.restate_theses(None, [_trade()], {})
    assert out == []                                            # fail-soft
