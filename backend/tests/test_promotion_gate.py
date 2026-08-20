"""
tests/test_promotion_gate.py — Tests for promotion gate evaluation logic.

Defense-first rule 5: treat the promotion gate as a security boundary.
Tests verify:
  1. Gate passes only when ALL 5 criteria are individually met.
  2. Failing any single criterion fails the whole gate.
  3. evaluate() never mutates state.
  4. The "note" field is always present and contains the expected copy.

Run: pytest tests/test_promotion_gate.py -v
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timezone, timedelta

import pytest
import config
from models import Trade
import promotion_gate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_winning_trade(days_ago: int = 5, pnl: float = 50.0) -> Trade:
    t = Trade()
    opened = (datetime.now(timezone.utc) - timedelta(days=days_ago, hours=1)).isoformat()
    closed = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    t.opened_at = opened
    t.closed_at = closed
    t.realized_pnl_usd = pnl
    t.realized_pnl_pct = (pnl / 100.0) * 100
    t.position_size_usd = 100.0
    t.is_open = False
    t.candidate_snapshot = {"liquidity_usd": 100_000}
    t.verdict_snapshot = {}
    return t


def _make_losing_trade(days_ago: int = 5, pnl: float = -20.0) -> Trade:
    t = _make_winning_trade(days_ago=days_ago, pnl=pnl)
    t.realized_pnl_pct = (pnl / 100.0) * 100
    return t


def _make_full_passing_set():
    """Create a trade set that meets all 5 promotion criteria."""
    # Min trades: >= 40, win rate >= 55%, profit factor >= 1.5, drawdown < 20%
    # We'll make 60 trades: 35 winners at +$50, 25 losers at -$20
    trades = []
    for i in range(35):
        trades.append(_make_winning_trade(days_ago=15 - (i % 12), pnl=50.0))
    for i in range(25):
        trades.append(_make_losing_trade(days_ago=15 - (i % 12), pnl=-20.0))
    # Win rate = 35/60 = 58.3% ≥ 55% ✓
    # Gross profit = 35*50 = $1750, Gross loss = 25*20 = $500
    # Profit factor = 1750/500 = 3.5 ≥ 1.5 ✓
    # Max drawdown: starting $1000, runs of losses + wins, well under 20% ✓
    return trades


def _first_date_11_days_ago() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=11)).isoformat()


# ---------------------------------------------------------------------------
# Test: all criteria met
# ---------------------------------------------------------------------------

class TestAllCriteriaMet:
    def test_all_pass_returns_true(self):
        trades = _make_full_passing_set()
        result = promotion_gate.evaluate(trades, _first_date_11_days_ago())
        assert result["all_criteria_met"] is True

    def test_all_criteria_individually_passed(self):
        trades = _make_full_passing_set()
        result = promotion_gate.evaluate(trades, _first_date_11_days_ago())
        for c in result["criteria"]:
            assert c["passed"] is True, f"Criterion '{c['name']}' should pass but failed: {c}"

    def test_note_always_present(self):
        """The human-review note must always be present in the response."""
        trades = _make_full_passing_set()
        result = promotion_gate.evaluate(trades, _first_date_11_days_ago())
        assert "note" in result
        assert len(result["note"]) > 10
        # Must not imply automatic activation
        note_lower = result["note"].lower()
        assert "human" in note_lower or "manual" in note_lower or "separate" in note_lower

    def test_summary_mentions_human_review_when_met(self):
        trades = _make_full_passing_set()
        result = promotion_gate.evaluate(trades, _first_date_11_days_ago())
        assert result["all_criteria_met"] is True
        # Summary should clarify this doesn't auto-enable anything
        assert "human" in result["summary"].lower() or "review" in result["summary"].lower()


# ---------------------------------------------------------------------------
# Test: individual criteria failure
# ---------------------------------------------------------------------------

class TestIndividualCriteriaFailure:
    def test_insufficient_trades_fails(self):
        """Fewer than PROMOTION_MIN_TRADES closed trades fails."""
        trades = [_make_winning_trade() for _ in range(5)]  # only 5
        result = promotion_gate.evaluate(trades, _first_date_11_days_ago())
        assert result["all_criteria_met"] is False
        criterion = next(c for c in result["criteria"] if "trade count" in c["name"].lower())
        assert criterion["passed"] is False

    def test_insufficient_days_fails(self):
        """Learning window not elapsed fails."""
        trades = _make_full_passing_set()
        # First trade only 5 days ago — not enough
        first_date = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        result = promotion_gate.evaluate(trades, first_date)
        assert result["all_criteria_met"] is False
        criterion = next(c for c in result["criteria"] if "window" in c["name"].lower())
        assert criterion["passed"] is False

    def test_low_win_rate_fails(self):
        """Win rate below threshold fails."""
        # 10 winners, 30 losers = 25% win rate
        trades = [_make_winning_trade(pnl=100.0) for _ in range(10)]
        trades += [_make_losing_trade(pnl=-10.0) for _ in range(30)]
        result = promotion_gate.evaluate(trades, _first_date_11_days_ago())
        criterion = next(c for c in result["criteria"] if "win rate" in c["name"].lower())
        assert criterion["passed"] is False

    def test_low_profit_factor_fails(self):
        """Profit factor below 1.5 fails."""
        # Equal wins and losses at same amount → profit factor = 1.0
        trades = [_make_winning_trade(pnl=20.0) for _ in range(25)]
        trades += [_make_losing_trade(pnl=-20.0) for _ in range(25)]
        result = promotion_gate.evaluate(trades, _first_date_11_days_ago())
        criterion = next(c for c in result["criteria"] if "profit factor" in c["name"].lower())
        assert criterion["passed"] is False

    def test_high_drawdown_fails(self):
        """Drawdown exceeding MAX_DRAWDOWN_PCT fails."""
        # Start with $1000. 5 consecutive large losses.
        trades = []
        base_day = 15
        for i in range(5):
            t = _make_losing_trade(days_ago=base_day - i, pnl=-150.0)  # -$150 each
            trades.append(t)
        # 5 * -$150 = -$750 from $1000 → equity $250, drawdown = 75%
        # Then some wins after
        for i in range(45):
            trades.append(_make_winning_trade(days_ago=5, pnl=10.0))
        result = promotion_gate.evaluate(trades, _first_date_11_days_ago())
        criterion = next(c for c in result["criteria"] if "drawdown" in c["name"].lower())
        assert criterion["passed"] is False

    def test_no_trades_fails_all(self):
        """Zero closed trades fails all applicable criteria."""
        result = promotion_gate.evaluate([], None)
        assert result["all_criteria_met"] is False
        failed = [c for c in result["criteria"] if not c["passed"]]
        # At least trade count + learning window + win rate should fail
        assert len(failed) >= 3


# ---------------------------------------------------------------------------
# Test: evaluate() is pure (no side effects)
# ---------------------------------------------------------------------------

class TestPurity:
    def test_evaluate_does_not_mutate_trades(self):
        """evaluate() must not modify the trade objects passed to it."""
        trades = _make_full_passing_set()
        original_pnls = [t.realized_pnl_usd for t in trades]
        promotion_gate.evaluate(trades, _first_date_11_days_ago())
        after_pnls = [t.realized_pnl_usd for t in trades]
        assert original_pnls == after_pnls

    def test_evaluate_called_twice_same_result(self):
        """evaluate() is deterministic — same input always produces same output."""
        trades = _make_full_passing_set()
        first_date = _first_date_11_days_ago()
        result1 = promotion_gate.evaluate(trades, first_date)
        result2 = promotion_gate.evaluate(trades, first_date)
        assert result1["all_criteria_met"] == result2["all_criteria_met"]
        for c1, c2 in zip(result1["criteria"], result2["criteria"]):
            assert c1["passed"] == c2["passed"]

    def test_result_contains_required_keys(self):
        """All required keys must be present in every evaluate() result."""
        result = promotion_gate.evaluate([], None)
        required = {"all_criteria_met", "criteria", "summary", "note"}
        assert required.issubset(set(result.keys()))
        assert isinstance(result["criteria"], list)
        assert len(result["criteria"]) == 5  # exactly 5 criteria
