"""
decision_pipeline.py — the shared read→think→gate core (Item #6, 2026-08-29).

Both pipelines (paper `main.run_tick` and the live `run_live_cycle.run_cycle`)
used to duplicate the same conceptual stages with copy-paste code that drifted
apart (the live copy missing the fake-chart filter, mis-calling
liveness.set_break, having no template-thinker fallback, never journaling
sizing refusals). This module is the SINGLE source of truth for the stages
both books must run identically:

    filter (blocklist + fake-chart) → enrich → regime → think (with
    fallback) → gate (STAGED: cheap rules → crowd scrape → crowd rules) →
    entry_allowed → journal

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
from rule_engine.gate import decision_from_results, evaluate_gate
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
    Live-only enrichment chain (research/social/web), paper-tick order.
    Mock runs stay hermetic: nothing runs when DATA_BACKEND != live. Fail-soft
    per feed. Returns the social read's LLM usages (paper journals them;
    [] when not live or the read failed).

    §43/§44: the CROWD feed is deliberately NOT part of this chain. Its
    fomo.fun scrape is metered and used to run for every candidate on every
    tick, before any rule could rule the candidate out — so quota burned on
    names that were about to fail liquidity or volume anyway. It now runs
    INSIDE the staged gate (`gate_candidate_staged` below), per candidate,
    only after that candidate's cheap rules have ALL passed.
    """
    social_usages: list = []
    if not candidates or config.DATA_BACKEND != "live":
        return social_usages
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
    # §48: web research LEFT the unconditional read chain — the search is
    # spent inside the staged gate (stage 4), only for candidates whose
    # rules all passed, behind the two-tier TTL cache. Same §43/§44 move
    # the crowd feed already made.
    return social_usages


# ---------------------------------------------------------------------------
# §44 — STAGED gate: cheap rules → (only if they ALL pass) crowd scrape →
# crowd rules. One assembled GateDecision, original rule order preserved.
#
# Ordering contract (operator decision 2026-08-30):
#   1. every non-crowd rule is evaluated first, unconditionally, for EVERY
#      candidate — exactly as before, no short-circuiting among them;
#   2. the fomo.fun scrape happens ONLY if all of those passed. A candidate
#      that failed any cheap rule is never scraped — that is the whole point:
#      scrape quota is the cost being cut;
#   3. the crowd rule(s) are then evaluated against whatever the scrape
#      returned. For a candidate that was never scraped, `crowd_heat` reports
#      `evaluated=False` ("not evaluated"), which fails the gate closed
#      without pretending to be a measurement.
#
# The LLM is untouched: the brain still runs once per tick over the whole
# board and the per-candidate thinker still runs before the gate, with the
# same prompts. This layer only sequences rule evaluation against feed I/O.
#
# Why per-candidate rather than one batch pre-pass: `cash_available` and
# `already_held` change AS positions open during the tick. Evaluating at gate
# time means that once cash is spent or a slot is taken, later candidates fail
# a cheap rule and their scrape is skipped too — strictly fewer scrapes than a
# single snapshot pre-pass, and the cheap rules are evaluated once instead of
# twice.
# ---------------------------------------------------------------------------

# Rules whose ONLY input comes from the metered crowd feed. Matched by
# function name so the split works for `ACTIVE_RULES` and the live
# cash-swapped `LIVE_ACTIVE_RULES` alike.
CROWD_RULE_IDS = ("crowd_heat",)


def _rule_id(fn) -> str:
    return getattr(fn, "__name__", "")


def cheap_rules(rules: list) -> list:
    """The rules that need no external feed — evaluated first, always."""
    return [r for r in rules if _rule_id(r) not in CROWD_RULE_IDS]


def crowd_rules(rules: list) -> list:
    """The feed-backed rules — evaluated only after the cheap ones passed."""
    return [r for r in rules if _rule_id(r) in CROWD_RULE_IDS]


async def _default_crowd_fetch(candidates: list) -> None:
    """Real crowd fetch (function-local import keeps mock runs from touching
    the module at all)."""
    from data_providers.crowd import enrich_crowd_heat
    await enrich_crowd_heat(candidates)


async def _default_web_fetch(candidate) -> None:
    """§48: real staged web-search fetch (function-local import keeps mock
    runs from touching the module at all)."""
    from llm.web_research import search_for_candidate
    evidence = await search_for_candidate(candidate)
    if evidence:
        candidate.web_summary = evidence


async def gate_candidate_staged(
    candidate: Candidate,
    portfolio: PortfolioState,
    regime: MarketRegime,
    rules: list,
    crowd_fetch=None,
    web_fetch=None,
) -> GateDecision:
    """
    The staged gate (§44). Returns ONE GateDecision whose `rules` list is in
    the same order as the injected rule list, so the journal/UI see no
    reordering — only the EVALUATION order changed.

    Fail-soft: a crowd-feed exception leaves the candidate on the presence
    proxy (the pre-§43 degradation) and never propagates into the tick.
    Mock/`DATA_BACKEND != live`: no fetch at all, nothing deferred, so
    hermetic runs behave exactly as they always have.

    §48 (2026-08-30): a THIRD stage — the web-search evidence fetch — runs
    after the crowd rule(s), ONLY for a candidate whose merged decision is
    all_passed (i.e. the candidates the per-candidate thinker is about to
    evaluate). A candidate that failed any rule never costs a search;
    cache-fresh candidates cost nothing either (two-tier TTL inside
    web_research). The fetch is fail-soft and never blocks the decision.
    """
    cheap = cheap_rules(rules)
    crowd = crowd_rules(rules)

    # --- stage 1: every cheap rule, unconditionally (no short-circuiting).
    cheap_results = {_rule_id(fn): fn(candidate, portfolio, regime)
                     for fn in cheap}

    if not crowd:
        # No feed-backed rule in this list — nothing to stage.
        return decision_from_results(candidate,
                                     [cheap_results[_rule_id(fn)]
                                      for fn in rules])

    # --- stage 2: the scrape, ONLY when every cheap rule passed.
    # Staging applies to the LIVE feed only. In mock/tests there is no scrape
    # to save, so nothing is deferred and `crowd_heat` keeps its documented
    # presence-proxy behavior — hermetic runs are bit-for-bit unchanged.
    if config.DATA_BACKEND == "live":
        cheap_ok = all(r.passed and r.evaluated
                       for r in cheap_results.values())
        if cheap_ok:
            candidate.crowd_lookup_deferred = False
            try:
                await (crowd_fetch or _default_crowd_fetch)([candidate])
            except Exception:
                log.warning("crowd fetch failed for %s - proxy heat in use "
                            "(fail-soft)", candidate.symbol, exc_info=True)
        else:
            # Never scraped -> the crowd rule reports "not evaluated" instead
            # of scoring the presence proxy. This is the quota saving.
            candidate.crowd_lookup_deferred = True
            log.info("crowd scrape skipped for %s: failed %s (no quota spent)",
                     candidate.symbol,
                     ",".join(rid for rid, r in cheap_results.items()
                              if not (r.passed and r.evaluated)))

    # --- stage 3: the crowd rule(s), against whatever stage 2 produced.
    crowd_results = {_rule_id(fn): fn(candidate, portfolio, regime)
                     for fn in crowd}

    merged = {**cheap_results, **crowd_results}
    decision = decision_from_results(candidate,
                                     [merged[_rule_id(fn)] for fn in rules])

    # --- stage 4 (§48): the web-search evidence fetch — ONLY for a candidate
    # the per-candidate thinker is about to evaluate (all rules passed AND
    # evaluated). A candidate that failed anything above never costs a
    # search; a cache-fresh candidate costs zero network calls. Mock runs:
    # nothing fetched, nothing deferred — hermeticity preserved.
    if decision.all_passed and config.DATA_BACKEND == "live":
        try:
            if not getattr(candidate, "web_summary", None):
                await (web_fetch or _default_web_fetch)(candidate)
        except Exception:
            log.warning("web search failed for %s - thinker runs without "
                        "the web line (fail-soft)", candidate.symbol,
                        exc_info=True)
    elif config.DATA_BACKEND == "live":
        log.info("web search skipped for %s: failed %s (no quota spent)",
                 candidate.symbol,
                 ",".join(rid for rid, r in merged.items()
                          if not (r.passed and r.evaluated)))
    return decision


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