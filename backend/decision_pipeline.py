"""
decision_pipeline.py — the shared read→think→gate core (Item #6, 2026-08-29).

Both pipelines (paper `main.run_tick` and the live `run_live_cycle.run_cycle`)
used to duplicate the same conceptual stages with copy-paste code that drifted
apart (the live copy missing the fake-chart filter, mis-calling
liveness.set_break, having no template-thinker fallback, never journaling
sizing refusals). This module is the SINGLE source of truth for the stages
both books must run identically:

    filter (blocklist + fake-chart) → enrich → regime → think (with
    fallback) → gate (same rule set) → entry_allowed → journal

Isolation contract preserved: this module imports ONLY backend/ modules —
never live_execution (backend must stay importable without the live stack).

Variation points (injected, never imported):
  - rules:             which gate rule list to evaluate (ACTIVE_RULES or the
                       live cash-swapped LIVE_ACTIVE_RULES)
  - portfolio_loader:  async () -> PortfolioState for the GATE (paper reads
                       the paper DB; live builds it from the real ledger)
  - thinker:           the Thinker instance to call per candidate
  - journaler:         async callback for per-candidate decision rows; each
                       pipeline journals in its own shape (live adds book:"live")
  - executor:          async callback receiving (candidate, think, gate,
                       ticket_usd, entry_allowed, refusal_reasons) — paper
                       opens a paper position; live places a guarded order.

Sizing/verification/cash handling stay with each pipeline (they differ
deliberately: paper has reuse state + INTENDED_POSITION_SIZE_USD; live has
ledger cash floors + CommitLog).
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

import config
from blocklist import filter_candidates
from models import Candidate, GateDecision, PortfolioState
from rule_engine.fake_chart import is_fake_candidate
from rule_engine.gate import evaluate_gate
from rule_engine.regime import MarketRegime, compute_market_regime

log = logging.getLogger(__name__)

Executor = Callable[[Candidate, object, GateDecision, float,
                     bool, list], Awaitable[Optional[object]]]
Journaler = Callable[[Candidate, object, GateDecision, bool],
                     Awaitable[None]]
PortfolioLoader = Callable[[], Awaitable[PortfolioState]]


async def read_candidates(provider) -> list:
    """
    Shared READ stage: fetch + blocklist + fake-chart filter, same order for
    both books. Extracted verbatim from paper main.run_tick. The live runner
    previously duplicated this WITHOUT the fake-chart filter — that gap is
    now closed (A7 parity restored).
    """
    candidates = await provider.get_candidates(config.MAX_CANDIDATES_PER_TICK)

    # --- BLOCKLIST: manual + auto-blocked mints never reach think/enrichment
    # (saves qwen + stealth-scrape credits, and is the DONT churn killer).
    candidates, blocked_now = filter_candidates(candidates)
    for sym, reason in blocked_now:
        log.info("BLOCKED %s skipped: %s", sym, reason)

    # --- FAKE-CHART filter (A7, omo isFakeChart parity): wash-traded / dead /
    # manufactured tapes never reach enrichment or think/gate, so they burn
    # no scrape or LLM credits and never skew the regime.
    real = []
    for c in candidates:
        fake, reason = is_fake_candidate(c)
        if fake:
            log.info("FAKE-CHART %s (%s) skipped: %s",
                     c.symbol, (c.mint_address or "")[:8], reason)
        else:
            real.append(c)
    if len(real) != len(candidates):
        log.info("fake-chart filter removed %d of %d candidates",
                 len(candidates) - len(real), len(candidates))
    return real


async def enrich_candidates(candidates: list) -> list:
    """
    Live-only enrichment chain (crowd/research/social/web), paper-tick order.
    Mock runs stay hermetic: nothing runs when DATA_BACKEND != live. Fail-soft
    per feed. Returns the social read's LLM usages (paper journals them;
    [] when not live or the read failed).
    """
    social_usages: list = []
    if not candidates or config.DATA_BACKEND != "live":
        return social_usages
    try:
        from data_providers.crowd import enrich_crowd_heat
        await enrich_crowd_heat(candidates)
    except Exception:
        log.warning("crowd enrichment failed - proxy heat in use (fail-soft)",
                    exc_info=True)
    try:
        from data_providers.research import enrich_with_research
        await enrich_with_research(candidates)
    except Exception:
        log.warning("token research failed - continuing without it",
                    exc_info=True)
    try:
        # Social read is EVIDENCE ONLY — never a verdict.
        from llm.social import enrich_social
        _, social_usages = await enrich_social(candidates)
    except Exception:
        log.warning("social read failed - continuing without it",
                    exc_info=True)
    try:
        from llm.web_research import enrich_web
        await enrich_web(candidates)
    except Exception:
        log.warning("web research failed - continuing without it",
                    exc_info=True)
    return social_usages


async def think_candidate(candidate, thinker, memory_line: str = ""):
    """
    THINK with the shared template fallback. The live runner previously let a
    thinker exception kill the whole cycle; now the same fail-closed template
    path the paper tick uses degrades a single candidate instead.
    """
    try:
        return await thinker.think(candidate, memory_line)
    except Exception as e:
        log.error("thinker error on %s: %s", candidate.symbol, e,
                  exc_info=True)
        from llm.thinker import template_think
        return template_think(candidate)


async def apply_break(think) -> bool:
    """
    Self-regulating break (REF-R4): correct arity for BOTH pipelines (the
    live copy previously called set_break(minutes, reason) — missing the
    leading `taking` positional — a latent TypeError).
    """
    if getattr(think, "break_taking", False):
        from rule_engine import liveness
        liveness.set_break(True, think.break_minutes, think.break_reason)
        log.warning("self-regulating break triggered: %d mins (reason: %s)",
                    think.break_minutes, think.break_reason)
        return True
    return False


async def gate_candidate(
    candidate: Candidate,
    portfolio: PortfolioState,
    regime: MarketRegime,
    rules: list,
) -> GateDecision:
    """GATE — identical evaluation for both books; only the rule list differs
    (paper ACTIVE_RULES; live swaps cash_available for the live floor)."""
    return evaluate_gate(candidate, portfolio, regime, rules)


def entry_decision(think, gate: GateDecision) -> bool:
    """think→gate intersection: either side alone refuses. Same for both."""
    return bool(gate.all_passed and think.wants_entry)