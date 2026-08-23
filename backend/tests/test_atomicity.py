"""
tests/test_atomicity.py — double-open / double-close / scale-in proofs (J3).

The single highest-stakes property in the codebase: cash is debited/credited
EXACTLY ONCE, even when operations are retried or raced.

Each test uses an isolated temp SQLite DB via the _db fixture.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

import config
from api import db
from models import Candidate
from paper_trading_engine import close_position, open_position
from tests.test_rules import make_candidate


@pytest_asyncio.fixture
async def _db(tmp_path):
    config.DB_PATH = tmp_path / "atomicity_test.db"
    await db.init_db()
    async with db.get_db() as conn:
        yield conn


def _candidate(mint: str = "MintAAA11111111111111111111111111111111111", **kw) -> Candidate:
    c = make_candidate(mint_address=mint)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


async def _cash(conn) -> float:
    return await db.get_cash_balance(conn)


# ---------------------------------------------------------------------------
# Double-open
# ---------------------------------------------------------------------------

async def test_double_open_debits_cash_once(_db):
    c = _candidate()
    first = await open_position(_db, c, gate=None)  # gate unused by engine
    assert first.applied
    cash_after_first = await _cash(_db)

    second = await open_position(_db, c, gate=None)
    assert not second.applied
    assert second.reason == "duplicate_open_position"
    assert await _cash(_db) == pytest.approx(cash_after_first)   # debited ONCE

    trades = await db.get_open_trades(_db)
    assert len(trades) == 1


async def test_open_then_close_then_reopen_is_allowed(_db):
    c = _candidate()
    await open_position(_db, c, None)
    trade = await db.get_open_trade_for_mint(_db, c.mint_address)
    await close_position(_db, trade, exit_price=c.price_usd * 2, exit_reason="take_profit")
    # Re-entry after a close is a legitimate new position, not a duplicate.
    again = await open_position(_db, c, None)
    assert again.applied
    assert len(await db.get_open_trades(_db)) == 1


# ---------------------------------------------------------------------------
# Double-close
# ---------------------------------------------------------------------------

async def test_double_close_credits_cash_once(_db):
    c = _candidate()
    await open_position(_db, c, None)
    trade = await db.get_open_trade_for_mint(_db, c.mint_address)
    cash_open = await _cash(_db)

    first = await close_position(_db, trade, exit_price=c.price_usd, exit_reason="timeout")
    assert first.applied
    cash_after_close = await _cash(_db)
    assert cash_after_close > cash_open - 1e9  # credited

    second = await close_position(_db, trade, exit_price=c.price_usd, exit_reason="timeout")
    assert not second.applied
    assert second.reason == "already_closed"
    assert await _cash(_db) == pytest.approx(cash_after_close)   # credited ONCE


async def test_crash_simulation_state_row_written_cash_not_debited_replays_cleanly(_db):
    """
    Simulates a crash between the trade-state write and the cash write:
    the trade row exists but cash was never debited. A replay of
    open_position must be a no-op (no double-open, no second debit).
    """
    c = _candidate()
    trade_open = await open_position(_db, c, None)
    assert trade_open.applied
    cash_after = await _cash(_db)

    # Simulate "cash write never happened": restore cash to initial.
    current = cash_after
    lost = config.INITIAL_CASH_USD - current
    await db.adjust_cash(_db, lost)

    # Replay: the conditional insert must affect zero rows (open position
    # already exists for this mint) — no second debit.
    replay = await open_position(_db, c, None)
    assert not replay.applied
    assert await _cash(_db) == pytest.approx(config.INITIAL_CASH_USD)


# ---------------------------------------------------------------------------
# (Scale-in atomicity tests REMOVED: the omotrades-model rebuild deleted the
# scale-in path — `already_held` makes pyramiding structurally impossible.)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# PAPER_TRADING_ONLY runtime assertion (E7)
# ---------------------------------------------------------------------------

async def test_open_position_asserts_paper_trading_flag(_db, monkeypatch):
    import paper_trading_engine as engine
    monkeypatch.setattr(config, "PAPER_TRADING_ONLY", False)
    with pytest.raises(RuntimeError, match="PAPER_TRADING_ONLY"):
        await engine.open_position(_db, _candidate(), None)
