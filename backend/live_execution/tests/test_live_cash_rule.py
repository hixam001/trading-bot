"""
tests/test_live_cash_rule.py — micro-bootstrap gate parity (2026-08-28),
floor now §45 equity-proportional (2026-08-30).

The paper `cash_available` rule checks cash against
INTENDED_POSITION_SIZE_USD ($100 — sized for the $1,000 paper book). The live
book starts from a few USDC (REF-R11 micro-bootstrap), so the paper threshold
refused EVERY live entry before sizing even ran (a $5 book could never buy
anything that passed the other ten rules — "not working as intended").

run_live_cycle swaps in `_live_cash_available` (checks the §45 floor:
max($0.10, 10% of at-cost equity)) via LIVE_ACTIVE_RULES; every other rule
stays verbatim and the paper ACTIVE_RULES + INTENDED_POSITION_SIZE_USD are
untouched (calibration-frozen). These tests pin that contract.

Hermetic: no network, no ledger, no DB — pure rule evaluation.
"""
from __future__ import annotations

import pytest

import config as paper_config
import run_live_cycle as rlc
from live_execution import config as live_config
from models import Candidate, PortfolioState
from rule_engine import rules as rules_mod
from rule_engine.gate import evaluate_gate
from rule_engine.regime import MarketRegime

MINT = "MINTAAA1111111111111111111111111111111111111"


def _candidate() -> Candidate:
    return Candidate(
        symbol="TEST", mint_address=MINT, price_usd=0.001,
        liquidity_usd=50_000.0, volume_24h_usd=100_000.0,
        market_cap_usd=100_000.0, volume_1h_usd=20_000.0,
        buys_1h=300, sells_1h=200, price_change_1h_pct=5.0,
        age_hours=48.0, has_twitter=True, has_telegram=True,
    )


def _regime() -> MarketRegime:
    return MarketRegime(
        computed_at="2026-08-28T00:00:00+00:00",
        pct_candidates_green_1h=50.0, median_volume_1h_usd=20_000.0,
        avg_buy_sell_ratio=1.5, regime_ok=True, regime_detail="test regime",
    )


def _cash_rule_result(rules, cash_usd: float):
    """Run the gate and return the cash_available RuleResult."""
    gate = evaluate_gate(_candidate(), PortfolioState(cash_usd=cash_usd),
                         _regime(), rules)
    matches = [r for r in gate.rules if r.rule_id == "cash_available"]
    assert len(matches) == 1
    return matches[0]


# --- structural contract -----------------------------------------------------

def test_live_rules_swap_only_the_cash_rule():
    """LIVE_ACTIVE_RULES is the same length and swaps exactly cash_available."""
    assert len(rlc.LIVE_ACTIVE_RULES) == len(rules_mod.ACTIVE_RULES)
    for paper_rule, live_rule in zip(rules_mod.ACTIVE_RULES,
                                     rlc.LIVE_ACTIVE_RULES):
        if paper_rule is rules_mod.cash_available:
            assert live_rule is rlc._live_cash_available
        else:
            assert live_rule is paper_rule   # every other rule verbatim


def test_paper_cash_rule_unchanged():
    """The paper rule still checks INTENDED_POSITION_SIZE_USD (frozen)."""
    res = rules_mod.cash_available(
        _candidate(), PortfolioState(cash_usd=5.0), _regime())
    assert res.rule_id == "cash_available"
    assert res.passed is False                 # $5 < $100 intended size
    assert paper_config.INTENDED_POSITION_SIZE_USD == 100.0


# --- the live cash rule ------------------------------------------------------

def test_live_cash_rule_passes_at_micro_bootstrap_floor():
    """A few USDC clears the §45 equity-proportional floor — the exact
    incident: $5 cash, no positions -> equity $5 -> floor $0.50 -> pass."""
    res = rlc._live_cash_available(
        _candidate(), PortfolioState(cash_usd=5.0), _regime())
    assert res.passed is True
    assert res.value == 5.0
    assert "live floor" in res.detail
    assert "at-cost equity" in res.detail


def test_live_cash_rule_passes_at_the_incident_book():
    """§45 regression: $3.31 cash + $1.28 deployed = $4.59 equity -> floor
    $0.459 -> the gate that used to pass at a fixed $0.50 still passes, and
    the detail shows the equity-derived floor for audit transparency."""
    res = rlc._live_cash_available(
        _candidate(),
        PortfolioState(cash_usd=3.3078,
                       open_positions=[_pos(1.2786)]),
        _regime())
    assert res.passed is True
    assert "vs live floor $0.46" in res.detail
    assert "at-cost equity $4.59" in res.detail


def test_live_cash_rule_fails_below_floor():
    # $0.30 cash, nothing deployed -> equity $0.30 -> floor $0.10 (dust);
    # $0.30 >= $0.10 passes. To FAIL the cash rule the book must be nearly
    # empty AND mostly deployed — e.g. $0.05 cash + $3.00 deployed ->
    # equity $3.05 -> floor $0.305 > $0.05 cash -> fail (book is over-deployed).
    res = rlc._live_cash_available(
        _candidate(),
        PortfolioState(cash_usd=0.05, open_positions=[_pos(3.00)]),
        _regime())
    assert res.passed is False
    assert "vs live floor $0.30" in res.detail


def _pos(cost_usd: float):
    from models import Trade
    return Trade(mint_address="MintBBBB22222222222222222222222222222222222",
                 position_size_usd=cost_usd)


# --- end-to-end through the gate --------------------------------------------

def test_gate_cash_outcome_flips_between_paper_and_live_rules():
    """A $5 book: paper rules fail cash_available, live rules pass it. This is
    the regression the fix targets — identical candidate, only the rule set
    differs."""
    paper_res = _cash_rule_result(rules_mod.ACTIVE_RULES, cash_usd=5.0)
    live_res = _cash_rule_result(rlc.LIVE_ACTIVE_RULES, cash_usd=5.0)
    assert paper_res.passed is False
    assert live_res.passed is True


def test_gate_uses_live_rules_in_run_cycle_path():
    """run_cycle evaluates with LIVE_ACTIVE_RULES (not the paper set)."""
    import inspect
    src = inspect.getsource(rlc.run_cycle)
    assert "LIVE_ACTIVE_RULES" in src
    assert "evaluate_gate(c, portfolio, regime, ACTIVE_RULES)" not in src
