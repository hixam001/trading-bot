"""
rule_engine/gate.py — evaluate_gate(): the sole entry decision-maker (§2.2).
"""
from __future__ import annotations

from typing import Callable

from models import Candidate, GateDecision, PortfolioState, RuleResult
from rule_engine.regime import MarketRegime

RuleFn = Callable[[Candidate, PortfolioState, MarketRegime], RuleResult]


def decision_from_results(
    candidate: Candidate,
    results: list[RuleResult],
) -> GateDecision:
    """
    Assemble the verdict from already-computed rule results. Shared by
    `evaluate_gate` (which computes them itself) and the sequenced gate in
    `decision_pipeline` (which computes the cheap rules first, then the
    feed-backed ones) so there is exactly ONE place that decides what
    `all_passed` / `failed_rule_ids` / `not_evaluated_rule_ids` mean.
    """
    failed = [r.rule_id for r in results if not r.passed and r.evaluated]
    skipped = [r.rule_id for r in results if not r.evaluated]
    return GateDecision(
        candidate=candidate,
        rules=results,
        # Fail closed: an unevaluated rule can never be part of a pass.
        all_passed=all(r.passed and r.evaluated for r in results),
        failed_rule_ids=failed,
        not_evaluated_rule_ids=skipped,
    )


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

    ONE documented exception (§43/§44, operator decision 2026-08-30): a rule
    whose only input comes from a metered external feed may report
    `evaluated=False` when the pipeline deliberately did not spend the call
    (today: `crowd_heat`, whose fomo.fun lookup happens only AFTER the cheap
    rules have all passed — see `decision_pipeline.gate_candidate_staged`).
    The rule is STILL in `rules` with an explicit "not evaluated" detail, so
    the audit trail never goes silent; it simply cannot claim a pass:

      - a non-evaluated rule NEVER satisfies all_passed (fail closed), and
      - it is NOT listed in failed_rule_ids (it was not a real rejection),
        it is listed in not_evaluated_rule_ids instead.

    This function itself never fetches anything: it evaluates whatever rules
    it is handed against the candidate as it stands. The staged variant lives
    in decision_pipeline because ordering feed I/O is a pipeline concern.
    """
    results = [rule(candidate, portfolio, regime) for rule in rules]
    return decision_from_results(candidate, results)
