"""
tests/test_calibration.py - REF-R9 closed-loop conviction factor.

Reference parity (computeCalibration() from omotrades/omo, ported verbatim):

    win_rate    = winners / usable          (win: pnl_pct > 0, loss: <= 0)
    expectancy  = win_rate * avg_win + (1 - win_rate) * avg_loss
    raw         = 1 + min(expectancy/50, 0.2)      if expectancy >= 0
                  1 + max(expectancy/25, -0.4)     otherwise
    confidence  = min(usable/12, 1.0)
    factor      = clamp(1 + (raw - 1) * confidence, 0.6, 1.2)

Every expectation below is hand-computed. Fail-closed: no usable outcomes
or any malformed input -> FLAT (factor 1.0). The factor is advisory
arithmetic only - never an automatic threshold change.
"""
from __future__ import annotations

from types import SimpleNamespace

from calibration import FLAT_CALIBRATION, _round, compute_calibration


def _t(pct):
    """A closed-trade stand-in carrying only the field calibration reads."""
    return SimpleNamespace(realized_pnl_pct=pct)


def test_no_trades_is_flat():
    cal = compute_calibration([])
    assert cal is FLAT_CALIBRATION
    assert cal.conviction_factor == 1.0
    assert cal.samples == 0
    assert compute_calibration(None) is FLAT_CALIBRATION


def test_twelve_wins_caps_at_1_2():
    # wr 1.0, avg_win 10, exp = 10 -> raw = 1 + min(10/50, 0.2) = 1.2
    # confidence = 12/12 = 1 -> factor 1.2 (the hard cap)
    cal = compute_calibration([_t(10.0)] * 12)
    assert cal.samples == 12
    assert cal.wins == 12
    assert cal.win_rate == 1.0
    assert cal.expectancy_pct == 10.0
    assert cal.conviction_factor == 1.2


def test_twelve_losses_floors_at_0_6():
    # exp = -10 -> raw = 1 + max(-10/25, -0.4) = 0.6 -> factor 0.6 (floor)
    cal = compute_calibration([_t(-10.0)] * 12)
    assert cal.wins == 0
    assert cal.expectancy_pct == -10.0
    assert cal.conviction_factor == 0.6


def test_small_sample_pulled_toward_neutral():
    # 3 wins of +10: raw 1.2 but confidence 3/12 = 0.25
    # factor = 1 + 0.2 * 0.25 = 1.05
    cal = compute_calibration([_t(10.0)] * 3)
    assert cal.samples == 3
    assert cal.conviction_factor == 1.05


def test_mixed_book_hand_computed():
    # 6 wins +10, 6 losses -5: wr 0.5, exp = 0.5*10 + 0.5*(-5) = 2.5
    # raw = 1 + 2.5/50 = 1.05, confidence 1 -> factor 1.05
    cal = compute_calibration([_t(10.0)] * 6 + [_t(-5.0)] * 6)
    assert cal.win_rate == 0.5
    assert cal.avg_win_pct == 10.0
    assert cal.avg_loss_pct == -5.0
    assert cal.expectancy_pct == 2.5
    assert cal.conviction_factor == 1.05


def test_extreme_wins_capped_by_raw_scale():
    # exp = 100 -> min(100/50, 0.2) = 0.2 -> still 1.2, never above
    cal = compute_calibration([_t(100.0)] * 12)
    assert cal.conviction_factor == 1.2


def test_extreme_losses_floored_by_raw_scale():
    # exp = -50 -> max(-50/25, -0.4) = -0.4 -> still 0.6, never below
    cal = compute_calibration([_t(-50.0)] * 12)
    assert cal.conviction_factor == 0.6


def test_non_finite_outcomes_skipped():
    trades = [_t(float("nan")), _t(float("inf"))] + [_t(10.0)] * 12
    cal = compute_calibration(trades)
    assert cal.samples == 12
    assert cal.conviction_factor == 1.2


def test_zero_pnl_counts_as_loss_with_zero_impact():
    # wr 0, avg_loss 0 -> exp 0 -> raw 1.0 -> factor 1.0
    cal = compute_calibration([_t(0.0)])
    assert cal.samples == 1
    assert cal.wins == 0
    assert cal.conviction_factor == 1.0


def test_malformed_input_never_raises():
    assert compute_calibration([1, 2, 3]) is FLAT_CALIBRATION
    assert compute_calibration("junk") is FLAT_CALIBRATION
    assert compute_calibration([object()]) is FLAT_CALIBRATION


def test_round_matches_js_math_round():
    # bankers rounding would give 2 for 2.5; Math.round gives 3
    assert _round(2.5, 0) == 3
    assert _round(3.5, 0) == 4
    assert _round(0.5, 0) == 1
    assert _round(0.125, 2) == 0.13
    assert _round(2.5) == 2.5      # default rounds to 3 decimal places


def test_to_dict_shape():
    d = compute_calibration([_t(10.0)] * 12).to_dict()
    assert set(d) == {"samples", "wins", "win_rate", "avg_win_pct",
                      "avg_loss_pct", "expectancy_pct", "conviction_factor",
                      "formula"}
    assert "expectancy" in d["formula"]
