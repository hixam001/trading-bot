"""
tests/test_rules.py — every rule function, both branches (J1/B2–B12).

Hand-built fixtures; no live data, no LLM, no DB.
"""
from __future__ import annotations

import pytest

import config
from models import Candidate, PortfolioState, Trade
from rule_engine.gate import evaluate_gate
from rule_engine.regime import MarketRegime
from rule_engine.rules import ACTIVE_RULES
import rule_engine.rules as rules_mod


def make_regime(ok: bool = True) -> MarketRegime:
    return MarketRegime(
        computed_at="2026-08-22T00:00:00+00:00",
        pct_candidates_green_1h=0.5,
        median_volume_1h_usd=50_000.0,
        avg_buy_sell_ratio=1.2,
        regime_ok=ok,
        regime_detail="fixture regime",
    )


def make_candidate(**overrides) -> Candidate:
    """A candidate that passes every rule by default; override per test."""
    base = dict(
        symbol="TEST",
        mint_address="Mint1111111111111111111111111111111111111",
        price_usd=0.001,
        liquidity_usd=50_000.0,        # >= MIN_LIQUIDITY_USD (15k) -> pass
        volume_24h_usd=100_000.0,
        market_cap_usd=100_000.0,
        volume_1h_usd=20_000.0,        # >= MIN_VOLUME_1H_USD (8k)    -> pass
        buys_1h=300, sells_1h=200,     # buys > sells                 -> pass
        price_change_1h_pct=5.0,
        age_hours=48.0,                # not (<24h AND fading)        -> pass
        has_twitter=True, has_telegram=True, has_website=None,  # heat 36 >= band
        mint_authority_revoked=True,
        freeze_authority_revoked=True,
        is_likely_honeypot=False,
    )
    base.update(overrides)
    return Candidate(**base)


def make_portfolio(cash: float = 1_000.0, positions: list[Trade] | None = None) -> PortfolioState:
    return PortfolioState(cash_usd=cash, open_positions=positions or [])


REGIME = make_regime(ok=True)
MINT = "Mint1111111111111111111111111111111111111"


# --- liquidity_floor -------------------------------------------------------

def test_liquidity_floor_pass():
    r = rules_mod.liquidity_floor(make_candidate(), make_portfolio(), REGIME)
    assert r.passed and r.rule_id == "liquidity_floor"
    assert "50,000" in r.detail


def test_liquidity_floor_fail():
    r = rules_mod.liquidity_floor(
        make_candidate(liquidity_usd=config.MIN_LIQUIDITY_USD - 1), make_portfolio(), REGIME)
    assert not r.passed


def test_liquidity_floor_none_fails_closed():
    r = rules_mod.liquidity_floor(make_candidate(liquidity_usd=None), make_portfolio(), REGIME)
    assert not r.passed and "unavailable" in r.detail and r.value is None


# --- volume_alive ----------------------------------------------------------

def test_volume_alive_pass():
    assert rules_mod.volume_alive(make_candidate(), make_portfolio(), REGIME).passed


def test_volume_alive_fail():
    r = rules_mod.volume_alive(
        make_candidate(volume_1h_usd=config.MIN_VOLUME_1H_USD - 1), make_portfolio(), REGIME)
    assert not r.passed


def test_volume_alive_none_fails_closed():
    assert not rules_mod.volume_alive(
        make_candidate(volume_1h_usd=None), make_portfolio(), REGIME).passed


# --- buy_pressure ----------------------------------------------------------

def test_buy_pressure_pass():
    r = rules_mod.buy_pressure(make_candidate(buys_1h=150, sells_1h=149), make_portfolio(), REGIME)
    assert r.passed and r.value == 1


def test_buy_pressure_fail_on_tie():
    assert not rules_mod.buy_pressure(
        make_candidate(buys_1h=100, sells_1h=100), make_portfolio(), REGIME).passed


def test_buy_pressure_none_fails_closed():
    assert not rules_mod.buy_pressure(
        make_candidate(buys_1h=None, sells_1h=10), make_portfolio(), REGIME).passed


# --- not_newborn_fade: joint condition (B5) --------------------------------

def test_not_newborn_fade_pass_old_and_fading():
    # 30h old: outside the the reference bot 24h newborn window even while dumping.
    r = rules_mod.not_newborn_fade(
        make_candidate(age_hours=30.0, price_change_1h_pct=-50.0), make_portfolio(), REGIME)
    assert r.passed


def test_not_newborn_fade_pass_young_and_rising():
    r = rules_mod.not_newborn_fade(
        make_candidate(age_hours=0.5, price_change_1h_pct=+80.0), make_portfolio(), REGIME)
    assert r.passed


def test_not_newborn_fade_fail_young_and_fading():
    r = rules_mod.not_newborn_fade(
        make_candidate(age_hours=1.0, price_change_1h_pct=-35.0), make_portfolio(), REGIME)
    assert not r.passed


def test_not_newborn_fade_boundary_is_inclusive():
    r = rules_mod.not_newborn_fade(
        make_candidate(age_hours=config.NEWBORN_AGE_HOURS - 0.01,
                       price_change_1h_pct=-config.NEWBORN_FADE_PCT),
        make_portfolio(), REGIME)
    assert not r.passed


def test_not_newborn_fade_none_fails_closed():
    assert not rules_mod.not_newborn_fade(
        make_candidate(age_hours=None), make_portfolio(), REGIME).passed


# --- public_presence ---------------------------------------------------------

def test_public_presence_pass_any_single_channel():
    for kw in ("has_twitter", "has_telegram", "has_website"):
        r = rules_mod.public_presence(make_candidate(**{kw: True}), make_portfolio(), REGIME)
        assert r.passed, kw


def test_public_presence_fail_all_false():
    r = rules_mod.public_presence(
        make_candidate(has_twitter=False, has_telegram=False, has_website=False),
        make_portfolio(), REGIME)
    assert not r.passed


def test_public_presence_all_unknown_fails():
    r = rules_mod.public_presence(
        make_candidate(has_twitter=None, has_telegram=None, has_website=None),
        make_portfolio(), REGIME)
    assert not r.passed
    assert "none confirmed" in r.detail


# --- market_regime_ok -------------------------------------------------------

def test_market_regime_ok_pass():
    assert rules_mod.market_regime_ok(make_candidate(), make_portfolio(), make_regime(True)).passed


def test_market_regime_ok_fail():
    assert not rules_mod.market_regime_ok(make_candidate(), make_portfolio(), make_regime(False)).passed


# --- cash_available ---------------------------------------------------------

def test_cash_available_pass():
    assert rules_mod.cash_available(make_candidate(), make_portfolio(cash=1_000.0), REGIME).passed


def test_cash_available_fail():
    r = rules_mod.cash_available(
        make_candidate(), make_portfolio(cash=config.INTENDED_POSITION_SIZE_USD - 0.01), REGIME)
    assert not r.passed


# --- already_held (the reference bot parity: strictly ONE position per name) ----------

def test_already_held_pass_no_size():
    r = rules_mod.already_held(make_candidate(), make_portfolio(), REGIME)
    assert r.passed and r.detail == "no size on"


def test_already_held_fails_with_any_size_on_same_mint():
    pos = Trade(mint_address=MINT, position_size_usd=10.0)
    assert not rules_mod.already_held(
        make_candidate(), make_portfolio(positions=[pos]), REGIME).passed


def test_already_held_ignores_other_mints():
    pos = Trade(mint_address="OtherMint22222222222222222222222222222222222",
                position_size_usd=10_000.0)
    assert rules_mod.already_held(
        make_candidate(), make_portfolio(positions=[pos]), REGIME).passed


# --- crowd_heat (proxy source; act band from config) ---------------------------

def test_crowd_heat_pass_inside_band():
    # 2 signals -> heat 36 == CROWD_HEAT_MIN -> pass
    r = rules_mod.crowd_heat(make_candidate(has_twitter=True, has_telegram=True),
                             make_portfolio(), REGIME)
    assert r.passed and r.value == 36


def test_crowd_heat_fail_below_band():
    r = rules_mod.crowd_heat(
        make_candidate(has_twitter=True, has_telegram=False, has_website=False),
        make_portfolio(), REGIME)
    assert not r.passed and r.value == 28


def test_crowd_heat_max_never_exceeded_by_proxy():
    r = rules_mod.crowd_heat(
        make_candidate(has_twitter=True, has_telegram=True, has_website=True),
        make_portfolio(), REGIME)
    assert r.passed and r.value == 44


def test_compute_crowd_heat_matches_reference_formula_shape():
    # the reference bot: heat = 20 + 8 x conviction sources, clamped 0..100.
    assert rules_mod.compute_crowd_heat(make_candidate()) == 36
    assert rules_mod.compute_crowd_heat(
        make_candidate(has_website=True)) == 44


# --- not_on_break -----------------------------------------------------------------

def test_not_on_break_default_awake():
    r = rules_mod.not_on_break(make_candidate(), make_portfolio(), REGIME)
    assert r.passed and r.detail == "awake"


def test_not_on_break_blocks_entries_while_set():
    from rule_engine import liveness
    liveness.set_break(True)
    try:
        r = rules_mod.not_on_break(make_candidate(), make_portfolio(), REGIME)
        assert not r.passed and "break" in r.detail
    finally:
        liveness.set_break(False)


# --- security_clear: None always passes (B10) --------------------------------

def test_security_clear_pass_all_unknown():
    r = rules_mod.security_clear(
        make_candidate(mint_authority_revoked=None, freeze_authority_revoked=None,
                       is_likely_honeypot=None),
        make_portfolio(), REGIME)
    assert r.passed and "unknown" in r.detail


def test_security_clear_pass_revoked_and_clean():
    assert rules_mod.security_clear(make_candidate(), make_portfolio(), REGIME).passed


def test_security_clear_fail_mint_authority_not_revoked():
    assert not rules_mod.security_clear(
        make_candidate(mint_authority_revoked=False), make_portfolio(), REGIME).passed


def test_security_clear_fail_honeypot():
    assert not rules_mod.security_clear(
        make_candidate(is_likely_honeypot=True), make_portfolio(), REGIME).passed


# --- Gate evaluation: no short-circuiting (B12) + full sample ---------------

def test_gate_no_short_circuit_all_rules_present_on_failure():
    bad = make_candidate(
        liquidity_usd=100.0,            # fails
        volume_1h_usd=10.0,             # fails
        buys_1h=1, sells_1h=99,         # fails
        has_twitter=False, has_telegram=False, has_website=False,  # presence + heat fail
        mint_authority_revoked=False,   # fails security_clear
    )
    decision = evaluate_gate(bad, make_portfolio(cash=0.0), make_regime(ok=False), ACTIVE_RULES)
    assert not decision.all_passed
    assert len(decision.rules) == len(ACTIVE_RULES)          # every rule evaluated
    assert len({r.rule_id for r in decision.rules}) == len(ACTIVE_RULES)
    for expected in ("liquidity_floor", "volume_alive", "buy_pressure",
                     "public_presence", "crowd_heat", "cash_available"):
        assert expected in decision.failed_rule_ids
    # rules AFTER the first failure still carry real results
    assert decision.rules[-1].rule_id == "not_on_break"


def test_gate_full_decision_sample_all_pass(capsys):
    decision = evaluate_gate(make_candidate(), make_portfolio(), make_regime(True), ACTIVE_RULES)
    assert decision.all_passed and decision.failed_rule_ids == []
    assert len(decision.rules) == 9
    print("\n--- Sample full GateDecision (every rule shown) ---")
    for r in decision.rules:
        print(f"  {r.rule_id:24s} {'PASS' if r.passed else 'FAIL'}  {r.detail}")


def test_gate_already_held_blocks_pyramiding():
    """the reference bot parity: any size on the name fails the gate (no scale-ins)."""
    pos = Trade(mint_address=MINT, position_size_usd=10.0)
    decision = evaluate_gate(make_candidate(), make_portfolio(positions=[pos]),
                             REGIME, ACTIVE_RULES)
    assert not decision.all_passed
    assert "already_held" in decision.failed_rule_ids





def test_active_rules_are_exactly_reference_nine():
    from rule_engine.rules import ACTIVE_RULES
    expected = ["liquidity_floor", "volume_alive", "buy_pressure", "not_newborn_fade", "public_presence", "crowd_heat", "cash_available", "already_held", "not_on_break"]
    assert [r.__name__ for r in ACTIVE_RULES] == expected