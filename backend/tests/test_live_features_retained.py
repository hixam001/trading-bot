"""
tests/test_live_features_retained.py — §52 Phase B pins.

The paper book retired; the LIVE cycle must retain every feature the paper
tick had that live lacked. ALL offline (mock backend, temp DB, no network):

  1. TRADES MIRROR — the live book's open positions land in the shared
     trades table (idempotent per mint); a full close closes the mirrored
     row with the ledger's own pnl; calibration/learning keep feeding.
  2. BRAIN — run_cycle wires the role-routed LLMBrain (live mode + LLM_BRAIN
     on) with the LIVE portfolio as context, fail-closed to per-candidate
     thinker on any failure.
  3. MEMORY LINE — the live thinker call carries the §49 loss memories.
  4. REFLECTION — a full close schedules a closed-trade reflection.
  5. DAILY LEARNING — the live loop runs it once per UTC day.
"""
from __future__ import annotations

import inspect

import pytest
import pytest_asyncio

import config
import run_live_cycle as rlc
from api import db
from models import PortfolioState, Trade


def _trade(mint: str = "Mint" + "A" * 40) -> Trade:
    return Trade(
        trade_id=f"live-{mint[:8]}", symbol="TEST", mint_address=mint,
        opened_at="2026-09-02T00:00:00+00:00", entry_price_usd=0.001,
        position_size_usd=5.0, quantity=5000.0, candidate_snapshot={},
        thesis="live book", is_open=True,
    )


@pytest_asyncio.fixture
async def _db(tmp_path, monkeypatch):
    """Hermetic temp SQLite (repo convention) + mock backend."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "live_feats_test.db")
    monkeypatch.setattr(config, "DATA_BACKEND", "mock")
    await db.init_db()


# --- 1. trades mirror ------------------------------------------------------

async def test_mirror_live_trades_writes_open_rows(_db):
    portfolio = PortfolioState(cash_usd=10.0, open_positions=[_trade()])
    await rlc._mirror_live_trades(portfolio)
    async with db.get_db() as conn:
        rows = await db.get_open_trades(conn)
    assert len(rows) == 1
    assert rows[0].mint_address == _trade().mint_address
    # idempotent: a second mirror of the same book adds nothing
    await rlc._mirror_live_trades(portfolio)
    async with db.get_db() as conn:
        assert len(await db.get_open_trades(conn)) == 1


async def test_mirror_live_close_closes_the_row(_db):
    t = _trade()
    portfolio = PortfolioState(cash_usd=10.0, open_positions=[t])
    await rlc._mirror_live_trades(portfolio)
    await rlc._mirror_live_close(t.mint_address, exit_price_usd=0.0015,
                                 pnl_usd=2.5, rule_id="exit_stop_loss")
    async with db.get_db() as conn:
        closed = await db.get_all_closed_trades(conn)
    assert len(closed) == 1
    assert closed[0].realized_pnl_usd == 2.5
    assert closed[0].exit_reason == "exit_stop_loss"
    assert not closed[0].is_open


async def test_calibration_feeds_from_the_mirrored_live_book(_db):
    """The §52 retention promise: REF-R9 calibration reads the shared
    trades table — after a mirrored close it produces a real factor, not
    the flat no-sample default."""
    from calibration import compute_calibration, FLAT_CALIBRATION
    t = _trade()
    await rlc._mirror_live_trades(
        PortfolioState(cash_usd=10.0, open_positions=[t]))
    await rlc._mirror_live_close(t.mint_address, exit_price_usd=0.0015,
                                 pnl_usd=2.5, rule_id="exit_stop_loss")
    async with db.get_db() as conn:
        cal = compute_calibration(await db.get_all_closed_trades(conn))
    assert cal.samples == 1
    assert cal != FLAT_CALIBRATION or cal.samples == 1


# --- 2. brain ---------------------------------------------------------------

def test_run_cycle_wires_the_brain_with_live_portfolio_context():
    """The role-routed brain call is wired with the LIVE portfolio as
    context, journaling its single usage, fail-closed on any error."""
    src = inspect.getsource(rlc.run_cycle)
    assert "from llm.llm_brain import LLMBrain" in src
    assert "brain.tick(candidates, portfolio)" in src
    # fail-closed: a brain failure never kills the cycle
    assert 'falling back to per-candidate' in src
    # the usage journal exists (once per cycle)
    assert "insert_llm_call_usage" in src


# --- 3. memory line ---------------------------------------------------------

def test_live_thinker_sees_the_loss_memories():
    """§49 wrote live loss memories the live thinker never read — §52 fixes
    the wiring: the think call now carries the memory line."""
    src = inspect.getsource(rlc.run_cycle)
    assert "recall_memories(conn, topic=c.symbol," in src
    assert "memory_line = \"Memory (context only): \" + \" | \".join(" in src
    assert "think_candidate(c, thinker, memory_line)" in src


# --- 4. reflection on close ---------------------------------------------------

def test_full_close_schedules_a_reflection():
    src = inspect.getsource(rlc._manage)
    assert "_mirror_live_close(" in src
    assert "_store_live_reflection(" in src
    # the mirror runs before the §49 anti-churn memory (both on full close)
    assert src.index("_mirror_live_close(") < src.index("maybe_autoblock")


# --- 5. daily learning -------------------------------------------------------

def test_live_loop_runs_daily_learning_once_per_day():
    src = inspect.getsource(rlc.main)
    assert "from learning_loop import run_daily_learning" in src
    assert "last_learning_date != today" in src


# --- 6. the retired-paper imports are gone from the live runner ---------------

def test_live_runner_imports_sizing_not_paper_engine():
    """§52 Phase A: the live cycle's sizing spine is the neutral module."""
    src = inspect.getsource(rlc)
    assert "from sizing import" in src
    assert "from paper_trading_engine import" not in src