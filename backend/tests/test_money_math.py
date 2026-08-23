"""
tests/test_money_math.py — known-correct expected outputs + edge cases (J2/E5).

All expected values hand-computed:
  SLIPPAGE_PCT = 0.02, FEE_PCT = 0.01
  net exit factor = (1 - 0.02) * (1 - 0.01) = 0.9702
"""
from __future__ import annotations

import pytest

import config
from models import Trade
from paper_trading_engine import (
    check_exit_conditions,
    compute_entry_cost,
    compute_position_size,
    compute_realized_pnl,
    compute_unrealized_pnl,
)


def make_trade(
    size: float = 100.0,
    qty: float = 100_000.0,
    price: float = 0.001,
) -> Trade:
    return Trade(
        symbol="T",
        mint_address="M",
        entry_price_usd=price,
        position_size_usd=size,
        quantity=qty,
        opened_at="2026-08-22T00:00:00+00:00",
    )


# --- compute_unrealized_pnl -------------------------------------------------

def test_unrealized_flat_price_is_negative_costs():
    # At the entry price, exit costs make P&L slightly negative.
    pnl_usd, pnl_pct = compute_unrealized_pnl(make_trade(), 0.001)
    assert pnl_usd == pytest.approx(100_000 * 0.001 * 0.9702 - 100.0)
    assert pnl_pct == pytest.approx(pnl_usd / 100.0 * 100.0)
    assert pnl_usd < 0


def test_unrealized_double_price():
    # Price doubles: gross 200k*0.002=200 -> wait: qty=100000, price=0.002 -> $200
    # net = 200 * 0.9702 = 194.04; pnl = +94.04 (+94.04%)
    pnl_usd, pnl_pct = compute_unrealized_pnl(make_trade(), 0.002)
    assert pnl_usd == pytest.approx(94.04)
    assert pnl_pct == pytest.approx(94.04)


def test_unrealized_invalid_inputs_raise():
    with pytest.raises(ValueError):
        compute_unrealized_pnl(make_trade(), 0.0)
    with pytest.raises(ValueError):
        compute_unrealized_pnl(make_trade(), -0.001)
    with pytest.raises(ValueError):
        compute_unrealized_pnl(make_trade(qty=0.0), 0.001)


# --- compute_realized_pnl ---------------------------------------------------

def test_realized_known_value():
    trade = make_trade()
    pnl_usd, pnl_pct = compute_realized_pnl(trade, 0.002)
    assert pnl_usd == pytest.approx(94.04)
    assert pnl_pct == pytest.approx(94.04)


def test_realized_loss():
    # Exit at half entry: gross $50, net 50*0.9702 = $48.51; pnl = 48.51-100 = -51.49
    pnl_usd, pnl_pct = compute_realized_pnl(make_trade(), 0.0005)
    assert pnl_usd == pytest.approx(-51.49)
    assert pnl_pct == pytest.approx(-51.49)


def test_realized_invalid_inputs_raise():
    with pytest.raises(ValueError):
        compute_realized_pnl(make_trade(), -1.0)
    with pytest.raises(ValueError):
        compute_realized_pnl(make_trade(size=0.0), 0.001)


# --- sizing ------------------------------------------------------------------

def test_position_sizing_fixed_size():
    size, qty = compute_position_size(0.005)
    assert size == config.INTENDED_POSITION_SIZE_USD
    assert qty == pytest.approx(config.INTENDED_POSITION_SIZE_USD / 0.005)


def test_entry_cost_includes_fees_and_slippage():
    # 100 * 1.01 * 1.02 = 103.02
    assert compute_entry_cost(100.0) == pytest.approx(103.02)


def test_sizing_invalid_price_raises():
    with pytest.raises(ValueError):
        compute_position_size(0.0)


# --- check_exit_conditions (E6 — omotrades-model engine, price-only probe) ---

def test_exit_stop_loss():
    trade = make_trade()
    price = trade.entry_price_usd * 0.75   # ~-26% net
    assert check_exit_conditions(trade, price) == "exit_stop_loss"


def test_exit_below_first_tranche_holds():
    """+60% net is BELOW the first ladder tranche (+100%): the old +50%
    take-profit is gone — winners now run on the trail + ladder (omotrades
    model). The single-price probe must hold here."""
    trade = make_trade()
    price = trade.entry_price_usd * 1.62   # ~+60% net
    assert check_exit_conditions(trade, price) is None


def test_exit_no_condition_holds():
    trade = make_trade()
    from datetime import datetime, timezone
    trade.opened_at = datetime.now(timezone.utc).isoformat()  # fresh position
    mid = trade.entry_price_usd * 1.10     # ~+7% net — inside both bands
    assert check_exit_conditions(trade, mid) is None
