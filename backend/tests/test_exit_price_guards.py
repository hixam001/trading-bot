"""
tests/test_exit_price_guards.py — §32 exit-price sanity guards.

2026-08-28 cash-corruption incident: a transient bad quote priced a $0.04
token at $119.0648 (~2960x). The exit scanner ratcheted high-water on it and
a take-profit trim credited $94,737.90 of phantom cash. Two hardcoded,
fail-closed guards now make that class of bug impossible:

  1. SCAN GUARD (config.EXIT_PRICE_JUMP_MAX = 50): a single-scan price this
     many multiples ABOVE the established peak is a bad quote — skip the
     position this scan and do NOT ratchet high-water. Upward-only: a genuine
     collapse must still exit.
  2. PROCEEDS BACKSTOP (config.MAX_EXIT_PROCEEDS_MULT = 200): a single
     close/trim crediting more than this multiple of cost basis is refused
     BEFORE any state write.

Every expectation hand-computed with SLIPPAGE_PCT=0.02, FEE_PCT=0.01
(net exit factor 0.9702). DB tests run on a fresh tmp SQLite file.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import config
from api import db
from models import Trade
from paper_trading_engine import (
    close_position,
    scan_and_execute_exits,
    trim_position,
)

NOW = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)

NET_FACTOR = (1.0 - config.SLIPPAGE_PCT) * (1.0 - config.FEE_PCT)  # 0.9702


class ContextProvider:
    """Rich-context provider (get_exit_context path)."""

    def __init__(self, ctx: dict):
        self.ctx = ctx

    async def get_exit_context(self, mint_address: str, decimals=None) -> dict:
        return self.ctx

    async def get_current_price(self, mint_address: str, decimals=None) -> float:
        return self.ctx.get("price_usd", 0.001)


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "guards.db")
    return tmp_path


async def _open(conn, trade: Trade) -> None:
    assert await db.try_insert_open_trade(conn, trade) == 1
    await db.adjust_cash(conn, -(trade.position_size_usd * 1.01 * 1.02))


# --- guard constants -----------------------------------------------------------

def test_guard_constants_hardcoded_reference_values():
    assert config.EXIT_PRICE_JUMP_MAX == 50.0
    assert config.MAX_EXIT_PROCEEDS_MULT == 200.0


# --- 1. scan guard: bad quote skipped, high-water NOT ratcheted ----------------

async def test_scan_skips_bad_quote_and_does_not_ratchet_hwm(db_env):
    await db.init_db()
    trade = Trade(symbol="BAD", mint_address="MINTBADQ",
                  entry_price_usd=0.04022, position_size_usd=67.0,
                  quantity=1665.8378915962207,
                  candidate_snapshot={"decimals": 6})
    async with db.get_db() as conn:
        await _open(conn, trade)
        cash_before = await db.get_cash_balance(conn)
        # The exact incident price: ~2960x entry, far beyond the 50x cap.
        actions = await scan_and_execute_exits(
            ContextProvider({"price_usd": 119.0648}), conn, now=NOW)
        assert actions == 0
        stored = await db.get_trade_by_id(conn, trade.trade_id)
        assert stored.is_open
        assert stored.tranches_taken == 0
        # Peak stays at entry — the poisoned quote never touched it.
        assert stored.high_water_usd == pytest.approx(0.04022)
        assert await db.get_cash_balance(conn) == pytest.approx(cash_before)


async def test_scan_allows_legitimate_move_and_ratchets(db_env):
    await db.init_db()
    trade = Trade(symbol="UP", mint_address="MINTUPPP",
                  entry_price_usd=0.001, position_size_usd=100.0,
                  quantity=100_000.0, candidate_snapshot={"decimals": 6})
    async with db.get_db() as conn:
        await _open(conn, trade)
        # +50% in one scan: real moves do this; below the 50x jump cap and
        # below the +100% TP rung, so it only ratchets high-water.
        actions = await scan_and_execute_exits(
            ContextProvider({"price_usd": 0.0015}), conn, now=NOW)
        assert actions == 0
        stored = await db.get_trade_by_id(conn, trade.trade_id)
        assert stored.high_water_usd == pytest.approx(0.0015)


async def test_scan_genuine_collapse_still_exits(db_env):
    """The jump guard is upward-only: a crash must still hit the stop."""
    await db.init_db()
    trade = Trade(symbol="DOWN", mint_address="MINTDOWN",
                  entry_price_usd=0.001, position_size_usd=100.0,
                  quantity=100_000.0, candidate_snapshot={"decimals": 6})
    async with db.get_db() as conn:
        await _open(conn, trade)
        await scan_and_execute_exits(
            ContextProvider({"price_usd": 0.0018}), conn, now=NOW)  # ratchet
        actions = await scan_and_execute_exits(
            ContextProvider({"price_usd": 0.0005}), conn, now=NOW)  # -72%
        assert actions == 1
        closed = await db.get_all_closed_trades(conn)
        assert closed[0].exit_reason == "exit_stop_loss"



# --- incident replay: the exact 2026-08-28 sequence can no longer corrupt ----

async def test_incident_replay_neet_sequence(db_env):
    """Open neet-like position, feed the incident bad quote, then the real
    price. Cash must be untouched by the bad quote; no phantom trim."""
    await db.init_db()
    trade = Trade(symbol="neet", mint_address="MINTNEET",
                  entry_price_usd=0.04022, position_size_usd=67.0,
                  quantity=1665.8378915962207,
                  candidate_snapshot={"decimals": 6})
    async with db.get_db() as conn:
        await _open(conn, trade)
        cash_after_open = await db.get_cash_balance(conn)

        # Scan 1: the poisoned quote.
        assert await scan_and_execute_exits(
            ContextProvider({"price_usd": 119.0648}), conn, now=NOW) == 0
        # Scan 2: the real price (+18.6% — no rule fires).
        assert await scan_and_execute_exits(
            ContextProvider({"price_usd": 0.0477}), conn, now=NOW) == 0

        stored = await db.get_trade_by_id(conn, trade.trade_id)
        assert stored.is_open and stored.tranches_taken == 0
        assert stored.high_water_usd == pytest.approx(0.0477)
        assert await db.get_cash_balance(conn) == pytest.approx(cash_after_open)


# --- 2. proceeds backstop: close_position -------------------------------------

async def test_close_refuses_implausible_proceeds(db_env):
    await db.init_db()
    trade = Trade(symbol="X", mint_address="MINTXXXX",
                  entry_price_usd=0.001, position_size_usd=100.0,
                  quantity=100_000.0, candidate_snapshot={"decimals": 6})
    async with db.get_db() as conn:
        await _open(conn, trade)
        cash_before = await db.get_cash_balance(conn)
        # 250x entry: net proceeds = 100000*0.25*0.9702 = $24,255 = 242.6x
        # cost basis — beyond the 200x cap.
        result = await close_position(conn, trade, 0.25, "exit_stop_loss")
        assert result.applied is False
        assert result.reason == "implausible_proceeds"
        stored = await db.get_trade_by_id(conn, trade.trade_id)
        assert stored.is_open                      # position untouched
        assert await db.get_cash_balance(conn) == pytest.approx(cash_before)


async def test_close_allows_plausible_proceeds(db_env):
    await db.init_db()
    trade = Trade(symbol="Y", mint_address="MINTYYYY",
                  entry_price_usd=0.001, position_size_usd=100.0,
                  quantity=100_000.0, candidate_snapshot={"decimals": 6})
    async with db.get_db() as conn:
        await _open(conn, trade)
        cash_before = await db.get_cash_balance(conn)
        # 150x entry: net proceeds = 100000*0.15*0.9702 = $14,553 = 145.5x
        # cost basis — a moonshot, but within the 200x cap.
        result = await close_position(conn, trade, 0.15, "exit_stop_loss")
        assert result.applied is True
        stored = await db.get_trade_by_id(conn, trade.trade_id)
        assert not stored.is_open
        expected_credit = 100_000.0 * 0.15 * NET_FACTOR
        assert await db.get_cash_balance(conn) == pytest.approx(
            cash_before + expected_credit, abs=1e-6)


# --- 2b. proceeds backstop: trim_position -------------------------------------

async def test_trim_refuses_implausible_proceeds(db_env):
    await db.init_db()
    trade = Trade(symbol="Z", mint_address="MINTZZZZ",
                  entry_price_usd=0.001, position_size_usd=100.0,
                  quantity=100_000.0, candidate_snapshot={"decimals": 6})
    async with db.get_db() as conn:
        await _open(conn, trade)
        cash_before = await db.get_cash_balance(conn)
        # 33% slice at 700x entry: slice proceeds =
        # 33000*0.7*0.9702 = $22,411.6 = 224.1x cost basis — beyond the cap.
        result = await trim_position(conn, trade, 0.33, 0.7)
        assert result.applied is False
        assert result.reason == "implausible_proceeds"
        stored = await db.get_trade_by_id(conn, trade.trade_id)
        assert stored.is_open and stored.tranches_taken == 0
        assert stored.quantity == pytest.approx(100_000.0)     # untouched
        assert stored.position_size_usd == pytest.approx(100.0)
        assert await db.get_cash_balance(conn) == pytest.approx(cash_before)


async def test_trim_allows_plausible_proceeds(db_env):
    await db.init_db()
    trade = Trade(symbol="W", mint_address="MINTWWWW",
                  entry_price_usd=0.001, position_size_usd=100.0,
                  quantity=100_000.0, candidate_snapshot={"decimals": 6})
    async with db.get_db() as conn:
        await _open(conn, trade)
        cash_before = await db.get_cash_balance(conn)
        # 33% slice at 3x entry: slice proceeds =
        # 33000*0.003*0.9702 = $96.05 — comfortably within the cap.
        result = await trim_position(conn, trade, 0.33, 0.003)
        assert result.applied is True
        stored = await db.get_trade_by_id(conn, trade.trade_id)
        assert stored.is_open and stored.tranches_taken == 1
        expected_credit = 33_000.0 * 0.003 * NET_FACTOR
        assert await db.get_cash_balance(conn) == pytest.approx(
            cash_before + expected_credit, abs=1e-6)
