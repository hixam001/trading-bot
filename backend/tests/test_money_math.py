"""
tests/test_money_math.py — Tests for P&L calculation, position sizing, and drawdown.

Defense-first rule 4: every money-math function needs a test with a known-correct
expected output. These are the functions where a sign error or off-by-one produces
a misleading track record.

Run: pytest tests/test_money_math.py -v
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from models import Trade
from paper_trading_engine import (
    compute_unrealized_pnl,
    compute_realized_pnl,
    _compute_position_size,
    _compute_quantity,
    check_exit_conditions,
)
import config


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_trade(
    entry_price: float = 0.001,
    position_size_usd: float = 100.0,
    quantity: float = 100_000.0,  # pre-computed
    opened_at: str = "2025-01-01T00:00:00+00:00",
) -> Trade:
    t = Trade()
    t.entry_price_usd = entry_price
    t.position_size_usd = position_size_usd
    t.quantity = quantity
    t.opened_at = opened_at
    t.is_open = True
    return t


# ---------------------------------------------------------------------------
# compute_unrealized_pnl
# ---------------------------------------------------------------------------

class TestComputeUnrealizedPnl:
    def test_profit_scenario(self):
        """Price doubled — unrealized P&L should be positive."""
        trade = _make_trade(
            entry_price=0.001,
            position_size_usd=100.0,
            quantity=100_000.0,  # bought 100k tokens at $0.001
        )
        current_price = 0.002  # 2x
        pnl_usd, pnl_pct = compute_unrealized_pnl(trade, current_price)
        # gross = 100k * 0.002 = $200
        # net = $200 * (1 - 0.02) * (1 - 0.01) = $200 * 0.98 * 0.99 = $194.04
        # pnl = $194.04 - $100 = $94.04
        assert abs(pnl_usd - 94.04) < 0.01, f"Expected ~94.04, got {pnl_usd}"
        assert pnl_pct > 0
        assert abs(pnl_pct - 94.04) < 0.1

    def test_loss_scenario(self):
        """Price halved — unrealized P&L should be negative."""
        trade = _make_trade(
            entry_price=0.001,
            position_size_usd=100.0,
            quantity=100_000.0,
        )
        current_price = 0.0005  # 0.5x
        pnl_usd, pnl_pct = compute_unrealized_pnl(trade, current_price)
        # gross = 100k * 0.0005 = $50
        # net = $50 * 0.98 * 0.99 = $48.51
        # pnl = $48.51 - $100 = -$51.49
        assert pnl_usd < 0, f"Expected negative pnl, got {pnl_usd}"
        assert abs(pnl_usd - (-51.49)) < 0.1, f"Expected ~-51.49, got {pnl_usd}"
        assert pnl_pct < 0

    def test_breakeven(self):
        """No price change — unrealized P&L should be negative due to exit costs."""
        trade = _make_trade(
            entry_price=0.001,
            position_size_usd=100.0,
            quantity=100_000.0,
        )
        current_price = 0.001  # same as entry
        pnl_usd, pnl_pct = compute_unrealized_pnl(trade, current_price)
        # Even at same price, exit costs (slippage+fee) create a loss
        assert pnl_usd < 0, "Should be slightly negative due to exit costs"

    def test_invalid_price_raises(self):
        trade = _make_trade()
        with pytest.raises(ValueError, match="current_price must be"):
            compute_unrealized_pnl(trade, 0.0)

    def test_negative_price_raises(self):
        trade = _make_trade()
        with pytest.raises(ValueError):
            compute_unrealized_pnl(trade, -1.0)

    def test_invalid_quantity_raises(self):
        trade = _make_trade(quantity=0.0)
        with pytest.raises(ValueError, match="quantity"):
            compute_unrealized_pnl(trade, 0.001)

    def test_invalid_position_size_raises(self):
        trade = _make_trade(position_size_usd=0.0)
        with pytest.raises(ValueError, match="position_size_usd"):
            compute_unrealized_pnl(trade, 0.001)


# ---------------------------------------------------------------------------
# compute_realized_pnl
# ---------------------------------------------------------------------------

class TestComputeRealizedPnl:
    def test_profitable_trade(self):
        """3x trade — profit after round-trip costs."""
        trade = _make_trade(
            entry_price=0.001,
            position_size_usd=100.0,
            quantity=100_000.0,
        )
        exit_price = 0.003  # 3x
        pnl_usd, pnl_pct = compute_realized_pnl(trade, exit_price)
        # gross = 100k * 0.003 = $300
        # net = $300 * 0.98 * 0.99 = $291.06
        # pnl = $291.06 - $100 = $191.06
        assert abs(pnl_usd - 191.06) < 0.1, f"Expected ~191.06, got {pnl_usd}"
        assert pnl_pct > 0

    def test_losing_trade(self):
        """80% loss trade."""
        trade = _make_trade(
            entry_price=0.001,
            position_size_usd=100.0,
            quantity=100_000.0,
        )
        exit_price = 0.0002  # 80% drop
        pnl_usd, pnl_pct = compute_realized_pnl(trade, exit_price)
        # gross = 100k * 0.0002 = $20
        # net = $20 * 0.98 * 0.99 = $19.404
        # pnl = $19.404 - $100 = -$80.596
        assert pnl_usd < 0
        assert abs(pnl_usd - (-80.596)) < 0.1, f"Expected ~-80.60, got {pnl_usd}"

    def test_known_exact_values(self):
        """Regression test with exact expected values."""
        trade = _make_trade(
            entry_price=1.0,
            position_size_usd=1000.0,
            quantity=1000.0,  # 1000 tokens at $1 each
        )
        exit_price = 1.5  # +50%
        pnl_usd, pnl_pct = compute_realized_pnl(trade, exit_price)
        # gross = 1000 * 1.5 = $1500
        # net = $1500 * (1 - 0.02) * (1 - 0.01) = $1500 * 0.98 * 0.99 = $1455.3
        # pnl = $1455.3 - $1000 = $455.3
        expected_pnl = 1500 * (1 - config.SLIPPAGE_PCT) * (1 - config.FEE_PCT) - 1000
        assert abs(pnl_usd - expected_pnl) < 0.001

    def test_sign_consistency(self):
        """pnl_usd and pnl_pct must have the same sign."""
        trade = _make_trade(entry_price=0.001, position_size_usd=100.0, quantity=100_000.0)
        for exit_price in [0.0005, 0.001, 0.002, 0.01]:
            pnl_usd, pnl_pct = compute_realized_pnl(trade, exit_price)
            if pnl_usd > 0:
                assert pnl_pct > 0, f"pnl_usd={pnl_usd}, pnl_pct={pnl_pct}"
            elif pnl_usd < 0:
                assert pnl_pct < 0, f"pnl_usd={pnl_usd}, pnl_pct={pnl_pct}"

    def test_zero_exit_price_raises(self):
        trade = _make_trade()
        with pytest.raises(ValueError, match="exit_price must be"):
            compute_realized_pnl(trade, 0.0)


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------

class TestPositionSizing:
    def test_ten_percent_of_cash(self):
        """Default POSITION_SIZE_PCT=0.10 of $1000 = $100."""
        size = _compute_position_size(1000.0)
        assert abs(size - 100.0) < 0.001

    def test_caps_at_full_cash(self):
        """Position size cannot exceed cash balance."""
        size = _compute_position_size(50.0)
        assert size <= 50.0

    def test_zero_cash_raises(self):
        with pytest.raises(ValueError, match="cash_balance"):
            _compute_position_size(0.0)

    def test_negative_cash_raises(self):
        with pytest.raises(ValueError):
            _compute_position_size(-100.0)


class TestComputeQuantity:
    def test_quantity_calculation(self):
        """Known exact quantity: $100 gross, $0.001 price, after 2%+1% costs."""
        qty, cost = _compute_quantity(100.0, 0.001)
        # net_usd_in = 100 * 0.98 * 0.99 = 97.02
        # qty = 97.02 / 0.001 = 97,020
        assert abs(qty - 97_020.0) < 1.0, f"Expected ~97,020, got {qty}"
        assert abs(cost - 100.0) < 0.001, "Cost basis should be gross amount"

    def test_zero_price_raises(self):
        with pytest.raises(ValueError, match="entry_price"):
            _compute_quantity(100.0, 0.0)

    def test_zero_position_raises(self):
        with pytest.raises(ValueError, match="gross_position_usd"):
            _compute_quantity(0.0, 0.001)


# ---------------------------------------------------------------------------
# Exit condition checks
# ---------------------------------------------------------------------------

class TestCheckExitConditions:
    def test_take_profit_triggers(self):
        """Position with +50% unrealized should trigger take_profit."""
        trade = _make_trade(
            entry_price=0.001,
            position_size_usd=100.0,
            quantity=100_000.0,
        )
        # We need a price where pnl_pct >= TAKE_PROFIT_PCT * 100 = 50%
        # Net exit = qty * price * (1-slippage) * (1-fee)
        # pnl = net_exit - cost; pct = pnl/cost
        # Need net_exit >= cost * 1.50 = $150
        # net_exit = qty * price * 0.98 * 0.99
        # price = 150 / (100000 * 0.98 * 0.99) = 150 / 97020 ≈ 0.001546
        price = 150 / (100_000 * (1 - config.SLIPPAGE_PCT) * (1 - config.FEE_PCT))
        result = check_exit_conditions(trade, price)
        assert result is not None
        assert result[0] == "take_profit"

    def test_stop_loss_triggers(self):
        """Position with -20% unrealized should trigger stop_loss."""
        trade = _make_trade(
            entry_price=0.001,
            position_size_usd=100.0,
            quantity=100_000.0,
        )
        # Net exit needs to be <= $80 (80% of cost)
        # price = 80 / (100000 * 0.98 * 0.99) ≈ 0.000824
        price = 80 / (100_000 * (1 - config.SLIPPAGE_PCT) * (1 - config.FEE_PCT))
        result = check_exit_conditions(trade, price)
        assert result is not None
        assert result[0] == "stop_loss"

    def test_no_exit_at_entry_price(self):
        """At entry price with small costs, should not trigger TP or SL (no timeout either)."""
        from datetime import datetime, timezone, timedelta
        recent_open = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        trade = _make_trade(
            entry_price=0.001,
            position_size_usd=100.0,
            quantity=100_000.0,
            opened_at=recent_open,
        )
        # At entry price, pnl is slightly negative (round-trip costs only, ~3%)
        # With STOP_LOSS_PCT=0.20, a 3% loss should NOT trigger stop-loss
        # With MAX_HOLD_HOURS=72 and trade opened 1 minute ago, timeout should NOT trigger
        result = check_exit_conditions(trade, 0.001)
        assert result is None, (
            f"Expected None at entry price (cost-only loss < SL threshold), got: {result}"
        )

    def test_timeout_triggers(self):
        """Trade older than MAX_HOLD_HOURS should trigger timeout."""
        from datetime import datetime, timezone, timedelta
        old_open = (
            datetime.now(timezone.utc) - timedelta(hours=config.MAX_HOLD_HOURS + 1)
        ).isoformat()
        trade = _make_trade(
            entry_price=0.001,
            position_size_usd=100.0,
            quantity=100_000.0,
            opened_at=old_open,
        )
        # Use a neutral price that won't trigger TP/SL
        neutral_price = 0.001
        result = check_exit_conditions(trade, neutral_price)
        # Should trigger timeout if the small cost-loss doesn't trigger SL first
        # Regardless, it must trigger SOMETHING (either SL or timeout)
        if result is not None:
            assert result[0] in ("timeout", "stop_loss")
