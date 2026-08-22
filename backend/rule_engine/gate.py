"""
rule_engine/gate.py — evaluate_gate(): the sole entry decision-maker (§2.2).
"""
from __future__ import annotations

from typing import Callable

from models import Candidate, GateDecision, PortfolioState, RuleResult
from rule_engine.regime import MarketRegime

RuleFn = Callable[[Candidate, PortfolioState, MarketRegime], RuleResult]


def evaluate_gate(
    candidate: Candidate,
    portfolio: PortfolioState,
    regime: MarketRegime,
    rules: list[RuleFn],
) -> GateDecision:
    """
    Every rule in `rules` is evaluated independently and unconditionally —
    there is NO short-circuiting (§2.2). Even when an early rule fails, every
    later rule's result is still computed and logged, so a rejected
    candidate's full profile is visible in the journal.
    """
    results = [rule(candidate, portfolio, regime) for rule in rules]
    failed = [r.rule_id for r in results if not r.passed]
    return GateDecision(
        candidate=candidate,
        rules=results,
        all_passed=(len(failed) == 0),
        failed_rule_ids=failed,
    )
