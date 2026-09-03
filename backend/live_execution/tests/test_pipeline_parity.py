"""
tests/test_pipeline_parity.py — Item #6 shared-core parity (2026-08-29).

The paper tick (backend/main.run_tick) and the live cycle
(run_live_cycle.run_cycle) must run the SAME read/think/gate stages through
backend/decision_pipeline.py. These tests pin the live side of that contract:

  * run_cycle's source delegates to the shared read/enrich/think/break
    helpers — the drifted duplicates are gone (no inline filter_candidates,
    no raw thinker.think, no mis-arity set_break).
  * The shared gate, fed LIVE_ACTIVE_RULES, produces the live cash-floor
    decision (micro-bootstrap parity, mirroring test_live_cash_rule.py).
  * The paper-vs-live decision difference is EXACTLY the cash rule — every
    other rule agrees on the same candidate.

Hermetic: source inspection + pure rule evaluation. No network, no DB.
"""
from __future__ import annotations

import inspect

import run_live_cycle as rlc
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
        mint_authority_revoked=True, freeze_authority_revoked=True,
        is_likely_honeypot=False,
    )


def _regime() -> MarketRegime:
    return MarketRegime(
        computed_at="2026-08-29T00:00:00+00:00",
        pct_candidates_green_1h=50.0, median_volume_1h_usd=20_000.0,
        avg_buy_sell_ratio=1.5, regime_ok=True, regime_detail="test regime",
    )


def test_run_cycle_delegates_to_shared_read_stage():
    src = inspect.getsource(rlc.run_cycle)
    assert "read_candidates(build_provider())" in src
    assert "enrich_candidates(candidates)" in src


def test_run_cycle_uses_the_staged_gate():
    """§44: the live cycle gates through the shared STAGED gate, so the
    fomo scrape happens only after that candidate's cheap rules all passed."""
    src = inspect.getsource(rlc.run_cycle)
    assert "gate_candidate_staged(c, portfolio, regime," in src
    assert "LIVE_ACTIVE_RULES)" in src
    # The unconditional pre-gate evaluation is gone.
    assert "evaluate_gate(c, portfolio, regime, LIVE_ACTIVE_RULES)" not in src


def test_run_cycle_gates_before_thinking():
    """§44 ordering contract in the live cycle: the STAGED gate runs BEFORE
    the think stage, and the thinker is only called when the gate passed.
    §52: the call now carries the §49 memory_line; the ordering contract
    is unchanged."""
    src = inspect.getsource(rlc.run_cycle)
    gate_at = src.index("gate_candidate_staged(c, portfolio, regime,")
    think_at = src.index("think_candidate(c, thinker, memory_line)")
    assert gate_at < think_at, "gate must be evaluated before the thinker call"
    assert "if gate.all_passed:" in src
    assert 'think.source = "template:rules-refused"' in src


def test_run_cycle_delegates_to_shared_think_and_break():
    src = inspect.getsource(rlc.run_cycle)
    assert "think_candidate(c, thinker, memory_line)" in src
    assert "apply_break(think)" in src
    # The drifted duplicates are gone:
    assert "filter_candidates(candidates)" not in src
    assert "await thinker.think(c)" not in src
    # The latent mis-arity set_break call is gone:
    assert "liveness.set_break(think.break_minutes" not in src


async def test_shared_gate_with_live_rules_passes_micro_bootstrap_cash():
    """The shared gate_candidate + LIVE_ACTIVE_RULES = the live decision.
    $5 book: paper rules would refuse, live rules pass the cash floor."""
    import sys
    from pathlib import Path
    # run_live_cycle lives inside backend/ now: its own dir IS the backend root.
    backend = str(Path(rlc.__file__).resolve().parent)
    if backend not in sys.path:
        sys.path.insert(0, backend)
    from decision_pipeline import gate_candidate

    c = _candidate()
    portfolio = PortfolioState(cash_usd=5.0)
    gate = await gate_candidate(c, portfolio, _regime(),
                                rlc.LIVE_ACTIVE_RULES)
    assert gate.all_passed

    paper_gate = await gate_candidate(c, portfolio, _regime(),
                                      rules_mod.ACTIVE_RULES)
    assert not paper_gate.all_passed


def test_paper_vs_live_rule_difference_is_exactly_the_cash_rule():
    """Same candidate, both rule lists: every rule EXCEPT cash_available
    must agree. This is the unification's core promise — one gate, one rule
    set, one deliberate variation."""
    c = _candidate()
    portfolio = PortfolioState(cash_usd=5.0)
    regime = _regime()
    paper_gate = evaluate_gate(c, portfolio, regime, rules_mod.ACTIVE_RULES)
    live_gate = evaluate_gate(c, portfolio, regime, rlc.LIVE_ACTIVE_RULES)
    assert len(paper_gate.rules) == len(live_gate.rules)
    by_id_paper = {r.rule_id: r for r in paper_gate.rules}
    by_id_live = {r.rule_id: r for r in live_gate.rules}
    assert set(by_id_paper) == set(by_id_live)
    for rid, p_res in by_id_paper.items():
        if rid == "cash_available":
            assert p_res.passed != by_id_live[rid].passed   # $5: live-only pass
        else:
            assert p_res.passed == by_id_live[rid].passed, rid