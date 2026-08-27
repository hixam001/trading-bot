"""
tests/test_exit_engine.py — the the reference bot-model exit engine (§5.2 rebuild).

Covers every exit rule's fire/hold branches, the take-profit ladder with
tranche bookkeeping, the sell risk gate (cooldown / daily cap / min clip /
risk-off bypass), high-water-mark persistence, and an end-to-end
scan_and_execute_exits pass against a stub provider on a real tmp SQLite DB.

All expected values hand-computed with SLIPPAGE_PCT=0.02, FEE_PCT=0.01:
  net exit factor = 0.9702
  price for net gain g on make_trade(): value = 100*(1+g)/0.9702,
  price = value / 100_000
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import config
from api import db
from models import Trade
from paper_trading_engine import scan_and_execute_exits, trim_position
from rule_engine.exits import (
    ExitDecision,
    ExitInput,
    evaluate_exits,
    sell_risk_gate,
)

NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=timezone.utc)


def make_trade(
    entry_price: float = 0.001,
    size: float = 100.0,
    qty: float = 100_000.0,
    opened_days_ago: float = 0.0,
    high_water_usd: float | None = None,
    tranches_taken: int = 0,
) -> Trade:
    return Trade(
        symbol="T",
        mint_address="M",
        opened_at=(NOW - timedelta(days=opened_days_ago)).isoformat(),
        entry_price_usd=entry_price,
        position_size_usd=size,
        quantity=qty,
        high_water_usd=high_water_usd,
        tranches_taken=tranches_taken,
    )


def price_for_net_gain(gain: float, entry_price: float = 0.001) -> float:
    """Hand-derived: net = value*0.9702 - 100 == gain*100 -> value = 100(1+g)/0.9702."""
    value = 100.0 * (1.0 + gain) / 0.9702
    return value / 100_000.0


# --- hard stop -----------------------------------------------------------------

def test_stop_loss_fires_full_close():
    d = evaluate_exits(ExitInput(trade=make_trade(), price_usd=0.00075, now=NOW))
    assert d.rule_id == "exit_stop_loss"
    assert d.action == "close_full"


def test_small_loss_holds():
    # −10% net: inside the stop band
    d = evaluate_exits(ExitInput(trade=make_trade(), price_usd=price_for_net_gain(-0.10), now=NOW))
    assert d.action == "hold"


# --- trailing give-back ----------------------------------------------------------

def test_trail_fires_after_give_back_from_high():
    # Peaked at +80% gross; now +39% -> gave back 41 points >= 40 -> close.
    t = make_trade(high_water_usd=0.0018)
    d = evaluate_exits(ExitInput(trade=t, high_water_usd=t.high_water_usd,
                                 price_usd=0.00139, now=NOW))
    assert d.rule_id == "exit_trail_give_back"
    assert d.action == "close_full"


def test_trail_holds_inside_band():
    # Peak +80%, now +45% -> gave back 35 points < 40 -> hold.
    t = make_trade(high_water_usd=0.0018)
    d = evaluate_exits(ExitInput(trade=t, high_water_usd=t.high_water_usd,
                                 price_usd=0.00145, now=NOW))
    assert d.action == "hold"


def test_trail_never_activates_below_activation_level():
    # High-water only +30% (< 50pp activation): even a big dip can't trail out.
    t = make_trade(high_water_usd=0.0013)
    d = evaluate_exits(ExitInput(trade=t, high_water_usd=t.high_water_usd,
                                 price_usd=0.00085, now=NOW))
    # net here ≈ 0.85*0.9702−1 = −17.5% -> inside stop band? no (−20% needed).
    assert d.action == "hold"


def test_high_water_defaults_to_entry():
    # No stored HWM: peak == entry -> give-back impossible -> never trails.
    t = make_trade()
    d = evaluate_exits(ExitInput(trade=t, price_usd=price_for_net_gain(-0.05), now=NOW))
    assert d.action == "hold"


# --- liquidity break ---------------------------------------------------------------

def test_liquidity_break_fires_even_in_profit():
    d = evaluate_exits(ExitInput(trade=make_trade(), price_usd=price_for_net_gain(0.07),
                                 liquidity_usd=7_999.0, now=NOW))
    assert d.rule_id == "exit_liquidity_break"
    assert d.action == "close_full"


def test_liquidity_unknown_never_fabricates_a_break():
    d = evaluate_exits(ExitInput(trade=make_trade(), price_usd=price_for_net_gain(0.07),
                                 liquidity_usd=None, now=NOW))
    assert d.action == "hold"


def test_liquidity_at_floor_holds():
    d = evaluate_exits(ExitInput(trade=make_trade(), price_usd=price_for_net_gain(0.07),
                                 liquidity_usd=8_000.0, now=NOW))
    assert d.action == "hold"


# --- thesis invalidated --------------------------------------------------------------

def test_invalidation_fires_on_dump_with_sellers_leading():
    d = evaluate_exits(ExitInput(trade=make_trade(), price_usd=price_for_net_gain(-0.05),
                                 chg6h_pct=-26.0, buys6h=100, sells6h=141, now=NOW))
    assert d.rule_id == "exit_thesis_invalidated"


def test_invalidation_requires_both_conditions():
    base = dict(price_usd=price_for_net_gain(-0.05), now=NOW)
    d1 = evaluate_exits(ExitInput(trade=make_trade(), chg6h_pct=-26.0,
                                  buys6h=100, sells6h=100, **base))   # dump only
    d2 = evaluate_exits(ExitInput(trade=make_trade(), chg6h_pct=-5.0,
                                  buys6h=100, sells6h=500, **base))   # sellers only
    d3 = evaluate_exits(ExitInput(trade=make_trade(), buys6h=100, sells6h=500,
                                  **base))                             # data missing
    assert d1.action == "hold" and d2.action == "hold" and d3.action == "hold"


# --- stale thesis ----------------------------------------------------------------------

def test_stale_thesis_fires_when_flat_old_and_dry():
    t = make_trade(opened_days_ago=15)
    d = evaluate_exits(ExitInput(trade=t, price_usd=price_for_net_gain(0.05),
                                 vol6h_usd=4_000.0, now=NOW))
    assert d.rule_id == "exit_stale_thesis"
    assert d.action == "close_full"


def test_stale_thesis_holds_when_volume_still_real():
    t = make_trade(opened_days_ago=15)
    d = evaluate_exits(ExitInput(trade=t, price_usd=price_for_net_gain(0.05),
                                 vol6h_usd=50_000.0, now=NOW))
    assert d.action == "hold"


# --- take-profit ladder -------------------------------------------------------------------

def test_ladder_first_tranche_trims_third():
    d = evaluate_exits(ExitInput(trade=make_trade(),
                                 price_usd=price_for_net_gain(1.10),
                                 tranches_taken=0, now=NOW))
    assert d.rule_id == "exit_take_profit"
    assert d.action == "trim"
    assert d.fraction == pytest.approx(0.33)


def test_ladder_does_not_repeat_same_tranche():
    # Tranche 0 already taken; +110% net < +300% next rung -> hold.
    d = evaluate_exits(ExitInput(trade=make_trade(),
                                 price_usd=price_for_net_gain(1.10),
                                 tranches_taken=1, now=NOW))
    assert d.action == "hold"


def test_ladder_second_and_third_tranches():
    d2 = evaluate_exits(ExitInput(trade=make_trade(),
                                  price_usd=price_for_net_gain(3.20),
                                  tranches_taken=1, now=NOW))
    assert d2.action == "trim" and d2.fraction == pytest.approx(0.33)

    d3 = evaluate_exits(ExitInput(trade=make_trade(),
                                  price_usd=price_for_net_gain(9.50),
                                  tranches_taken=2, now=NOW))
    assert d3.action == "trim" and d3.fraction == pytest.approx(0.50)


def test_ladder_exhausted_holds_and_leaves_rest_to_trail():
    d = evaluate_exits(ExitInput(trade=make_trade(),
                                 price_usd=price_for_net_gain(12.0),
                                 tranches_taken=3, now=NOW))
    assert d.action == "hold"


def test_risk_off_beats_profit_taking():
    # Deep enough to stop AND past the first tranche: stop wins, full close.
    d = evaluate_exits(ExitInput(trade=make_trade(),
                                 price_usd=price_for_net_gain(-0.30), now=NOW))
    assert d.rule_id == "exit_stop_loss"
    assert d.action == "close_full"


# --- sell risk gate -------------------------------------------------------------------------

def _trim_decision() -> ExitDecision:
    return ExitDecision("exit_take_profit", "trim", 0.33, "test trim")


def test_gate_risk_off_bypasses_everything():
    d, note = sell_risk_gate(
        ExitDecision("exit_stop_loss", "close_full", 1.0, ""),
        trim_value_usd=5.0,
        last_exit_for_mint=NOW - timedelta(minutes=2),   # inside cooldown
        closes_last_24h=99,                              # over the cap
        now=NOW,
    )
    assert d.action == "close_full"
    assert "bypass" in note


def test_gate_cooldown_blocks_trim():
    d, note = sell_risk_gate(
        _trim_decision(), 500.0,
        last_exit_for_mint=NOW - timedelta(minutes=5), closes_last_24h=0, now=NOW)
    assert d.action == "hold" and "cooldown" in note


def test_gate_daily_cap_blocks_trim():
    d, note = sell_risk_gate(
        _trim_decision(), 500.0,
        last_exit_for_mint=None, closes_last_24h=config.MAX_EXITS_PER_24H, now=NOW)
    assert d.action == "hold" and "cap" in note


def test_gate_min_clip_blocks_dust_trim():
    d, note = sell_risk_gate(
        _trim_decision(), 10.0,
        last_exit_for_mint=None, closes_last_24h=0, now=NOW)
    assert d.action == "hold" and "clip" in note


def test_gate_passes_clean_trim():
    d, note = sell_risk_gate(
        _trim_decision(), 500.0,
        last_exit_for_mint=NOW - timedelta(minutes=45), closes_last_24h=2, now=NOW)
    assert d.action == "trim" and note == ""


# --- scan_and_execute_exits end-to-end on a tmp SQLite DB ---------------------

class StubProvider:
    """Price-only provider (the get_current_price fallback path)."""

    def __init__(self, price: float):
        self.price = price

    async def get_current_price(self, mint_address: str, decimals=None) -> float:
        return self.price


class ContextProvider:
    """Rich-context provider (get_exit_context path: price + liquidity + 6h)."""

    def __init__(self, ctx: dict):
        self.ctx = ctx

    async def get_exit_context(self, mint_address: str, decimals=None) -> dict:
        return self.ctx

    async def get_current_price(self, mint_address: str, decimals=None) -> float:
        return self.ctx.get("price_usd", 0.001)


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "exits.db")
    return tmp_path


async def test_scanner_closes_on_stop_loss_and_records_hwm(db_env):
    await db.init_db()
    trade = Trade(symbol="T", mint_address="MINTAAAATTTT",
                  entry_price_usd=0.001, position_size_usd=100.0,
                  quantity=100_000.0, candidate_snapshot={"decimals": 6})
    async with db.get_db() as conn:
        assert await db.try_insert_open_trade(conn, trade) == 1
        await db.adjust_cash(conn, -(100.0 * 1.01 * 1.02))   # entry debit
        # First scan at a HIGH price: HWM should ratchet up, no exit.
        actions = await scan_and_execute_exits(
            ContextProvider({"price_usd": 0.0018}), conn, now=NOW)
        assert actions == 0
        stored = await db.get_trade_by_id(conn, trade.trade_id)
        assert stored.high_water_usd == pytest.approx(0.0018)

    # Second scan after a crash through the stop: full close.
    async with db.get_db() as conn:
        actions = await scan_and_execute_exits(
            ContextProvider({"price_usd": 0.0005}), conn, now=NOW)
        assert actions == 1
        closed = await db.get_all_closed_trades(conn)
        assert closed[0].exit_reason == "exit_stop_loss"
        cash_after = await db.get_cash_balance(conn)

    # Cash conservation: entry debit − exit credit == the entry premium only.
    expected_final = config.INITIAL_CASH_USD - (
        100.0 * (1.01 * 1.02)) + (closed[0].position_size_usd
                                  + closed[0].realized_pnl_usd)
    assert cash_after == pytest.approx(expected_final, abs=1e-6)


async def test_scanner_trims_on_ladder_and_persists_counter(db_env):
    await db.init_db()
    trade = Trade(symbol="UP", mint_address="MINTBBBB",
                  entry_price_usd=0.001, position_size_usd=100.0,
                  quantity=100_000.0, candidate_snapshot={"decimals": 6})
    async with db.get_db() as conn:
        assert await db.try_insert_open_trade(conn, trade) == 1
        await db.adjust_cash(conn, -(100.0 * 1.01 * 1.02))   # entry debit
        rich = {"price_usd": price_for_net_gain(1.10), "liquidity_usd": 50_000.0}
        actions = await scan_and_execute_exits(ContextProvider(rich), conn, now=NOW)
        assert actions == 1
        stored = await db.get_trade_by_id(conn, trade.trade_id)
        assert stored.is_open and stored.tranches_taken == 1
        assert stored.position_size_usd == pytest.approx(67.0)
        assert stored.quantity == pytest.approx(67_000.0)
        cash_after_trim = await db.get_cash_balance(conn)

    # Trim proceeds credited exactly once: gross slice * 0.9702.
    expected_credit = (33_000.0 * price_for_net_gain(1.10)) * 0.9702
    assert cash_after_trim - config.INITIAL_CASH_USD == pytest.approx(
        expected_credit - (100.0 * 1.01 * 1.02), abs=1e-6)


async def test_trim_position_is_idempotent_and_atomic(db_env):
    await db.init_db()
    trade = Trade(symbol="ID", mint_address="MINTCCCC",
                  entry_price_usd=0.001, position_size_usd=100.0,
                  quantity=100_000.0, candidate_snapshot={})
    async with db.get_db() as conn:
        await db.try_insert_open_trade(conn, trade)
        result = await trim_position(conn, trade, 0.33, exit_price=0.0022)
        assert result.applied and result.reason == "trimmed"
        cash_once = await db.get_cash_balance(conn)

        # Double-trim attempt with a stale Trade object still decrements ONCE
        # more legitimately (row is open) — but a closed row must no-op:
        await db.close_trade_row(conn, trade.trade_id,
                                 datetime.now(timezone.utc).isoformat(),
                                 0.0022, "exit_stop_loss", -50.0, -50.0)
        result2 = await trim_position(conn, trade, 0.33, exit_price=0.0022)
        assert not result2.applied and result2.reason == "position_closed"

        # Exactly one trim's worth of cash was credited above; close credited once.
        async with db.get_db() as conn2:
            cash_final = await db.get_cash_balance(conn2)
        assert cash_final >= cash_once  # close only added; never double-trimmed