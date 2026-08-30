"""
tests/test_live_ticket_floor.py — REF-R11 micro-bootstrap sizing floor,
now §45 EQUITY-PROPORTIONAL (operator decision 2026-08-30).

The live book starts from $3-5 USDC and must compound from there. The paper
$25 MIN_TICKET_USD floor (frozen for calibration comparability) would
permanently skip every live entry at this scale, so the live path threads its
own floor through compute_ticket / compute_risk_budget — but the floor itself
is no longer a fixed $0.50: a fixed floor froze every entry the moment cash
dipped under ~$3.33 (the "site says ENTER but nothing executes" incident,
ticket $0.496 < $0.50). The §45 formula scales with the book:

    min_live_ticket(equity) = max($0.10 dust floor, equity * 0.10)

These tests prove three things:
  1. Passing min_ticket_usd lets a micro book size below $25.
  2. The §45 formula itself: growth with equity, dust clamp, fail-closed.
  3. Omitting the floor (the paper path) is bit-identical to before — the
     floor defaults to config.MIN_TICKET_USD and every frozen expectation
     holds, including the historical $0.50-threading cases.

Every expectation is hand-computed with PER_ORDER_FRACTION=0.035,
DAY_MULTIPLE=4, HARD_ORDER_CEILING_USD=3000.
"""
from __future__ import annotations

import config
from paper_trading_engine import compute_risk_budget, compute_ticket


# --- the §45 formula itself ----------------------------------------------------

def test_min_live_ticket_scales_with_equity():
    from live_execution.config import min_live_ticket_usd
    # $100 book -> 10% = $10 floor
    assert min_live_ticket_usd(100.0) == 10.0
    # $1000 book -> $100 floor
    assert min_live_ticket_usd(1000.0) == 100.0
    # $5 micro book -> $0.50 floor
    assert min_live_ticket_usd(5.0) == 0.5


def test_min_live_ticket_dust_clamp():
    from live_execution.config import (MIN_LIVE_TICKET_ABS_FLOOR_USD,
                                      min_live_ticket_usd)
    # $0.50 book -> 10% = $0.05 -> clamped up to the dust floor
    assert min_live_ticket_usd(0.5) == MIN_LIVE_TICKET_ABS_FLOOR_USD
    # $1.00 book -> 10% = $0.10 = the dust floor exactly
    assert min_live_ticket_usd(1.0) == 0.10


def test_min_live_ticket_fails_closed_on_bad_input():
    from live_execution.config import (MIN_LIVE_TICKET_ABS_FLOOR_USD,
                                      min_live_ticket_usd)
    assert min_live_ticket_usd(0.0) == MIN_LIVE_TICKET_ABS_FLOOR_USD
    assert min_live_ticket_usd(-5.0) == MIN_LIVE_TICKET_ABS_FLOOR_USD
    assert min_live_ticket_usd(None) == MIN_LIVE_TICKET_ABS_FLOOR_USD
    assert min_live_ticket_usd(float("nan")) == MIN_LIVE_TICKET_ABS_FLOOR_USD
    assert min_live_ticket_usd(float("inf")) == MIN_LIVE_TICKET_ABS_FLOOR_USD
    assert min_live_ticket_usd("not-a-number") == MIN_LIVE_TICKET_ABS_FLOOR_USD


def test_min_live_ticket_unfreezes_the_incident_book():
    """THE regression: equity $4.59 -> floor $0.459; the $0.4962 ticket that
    the fixed $0.50 floor refused now clears."""
    from live_execution.config import min_live_ticket_usd
    floor = min_live_ticket_usd(4.5864)      # 3.3078 cash + 1.2786 deployed
    assert floor == 0.4586
    assert 0.4962 >= floor                    # the TREE ticket places


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
