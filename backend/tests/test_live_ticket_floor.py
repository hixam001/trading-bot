"""
tests/test_live_ticket_floor.py — REF-R11 micro-bootstrap sizing floor.

The live book starts from $3-5 USDC and must compound from there. The paper
$25 MIN_TICKET_USD floor (frozen for calibration comparability) would
permanently skip every live entry at this scale, so the live path threads its
own MIN_LIVE_TICKET_USD floor through compute_ticket / compute_risk_budget.

These tests prove two things:
  1. Passing min_ticket_usd lets a micro book size below $25.
  2. Omitting it (the paper path) is bit-identical to before — the floor
     defaults to config.MIN_TICKET_USD and every frozen expectation holds.

Every expectation is hand-computed from the reference formulas with
PER_ORDER_FRACTION=0.035, DAY_MULTIPLE=4, HARD_ORDER_CEILING_USD=3000.
"""
from __future__ import annotations

import config
from paper_trading_engine import compute_risk_budget, compute_ticket


# --- compute_risk_budget floor threading -------------------------------------

def test_live_floor_allows_micro_order():
    # equity 100, df 1.0 -> raw 100*0.035 = 3.5; floor 0.5 keeps 3.5;
    # round_half_up(3.5) = 4. Well under the paper $25 floor.
    b = compute_risk_budget(100.0, 0.0, min_ticket_usd=0.5)
    assert b.max_order_usd == 4.0
    assert b.derived is True


def test_paper_default_floor_unchanged():
    # Same equity, but NO floor override -> clamps 3.5 up to the $25 paper
    # floor exactly as before (frozen calibration baseline).
    b = compute_risk_budget(100.0, 0.0)
    assert b.max_order_usd == 25.0


def test_live_floor_fail_closed_budget_uses_live_floor():
    # equity <= 0 fails closed to the floor; with a live floor that is 0.5,
    # not the paper 25.
    b = compute_risk_budget(0.0, 0.0, min_ticket_usd=0.5)
    assert b.max_order_usd == 0.5
    assert b.derived is False


def test_live_floor_daily_multiple():
    # max_daily = max_order * 4 = 4 * 4 = 16 (floor 0.5 does not bind).
    b = compute_risk_budget(100.0, 0.0, min_ticket_usd=0.5)
    assert b.max_daily_usd == 16.0


# --- compute_ticket floor threading (risk_budget mode) -----------------------

def test_ticket_risk_budget_live_floor(monkeypatch):
    monkeypatch.setattr(config, "SIZING_MODE", "risk_budget")
    # equity 100 -> budget order 4.0; cf 1.0 -> ticket 4.0 (floor 0.5).
    t = compute_ticket(0.0, None, equity_usd=100.0, unrealized_usd=0.0,
                       conviction_factor=1.0, min_ticket_usd=0.5)
    assert t == 4.0


def test_ticket_risk_budget_paper_default_unchanged(monkeypatch):
    monkeypatch.setattr(config, "SIZING_MODE", "risk_budget")
    # No floor override -> the $25 paper floor still applies.
    t = compute_ticket(0.0, None, equity_usd=100.0, unrealized_usd=0.0,
                       conviction_factor=1.0)
    assert t == 25.0


def test_ticket_conviction_live_floor(monkeypatch):
    monkeypatch.setattr(config, "SIZING_MODE", "conviction")
    # cash 4 * 0.15 = 0.6 base; heat 50 -> conviction 0.8; 0.6*0.8 = 0.48
    # -> round() = 0 -> floored at the live 0.5.
    t = compute_ticket(4.0, 50, min_ticket_usd=0.5)
    assert t == 0.5
