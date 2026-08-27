"""
main.py — the async tick loop.

Per tick (§3.2 order matters):
  1. Fetch the candidate batch from the selected provider stack.
  2. Compute MarketRegime ONCE from the full batch and log it once
     (market_regime table) — never once per candidate.
  3. Evaluate + act per candidate via decide_and_act(); narrate EVERY
     decision (pass or fail) and persist it as a feed event with the FULL
     rule breakdown.
  4. Check fixed numeric exit conditions against every open position;
     close via the atomic engine; schedule fire-and-forget reflections.

The LLM is never in the decision path here — only narration of decisions
already made by the rule engine and exits already flagged by numeric checks.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timezone

import config
from api import db
from blocklist import filter_candidates
from data_providers import build_provider
from rule_engine.fake_chart import is_fake_candidate
from llm.narrator import generate_reflection
from llm.reuse import REUSE_TICK_WINDOW, reused_if_stable, stats_signature
from llm.thinker import Thinker, ThinkResult
from llm.llm_brain import LLMBrain, LLMVerdict, LLMBrainResult
from models import FeedEvent
from calibration import FLAT_CALIBRATION, compute_calibration
from paper_trading_engine import (
    compute_risk_budget,
    compute_ticket,
    load_portfolio_state,
    open_position,
    portfolio_equity_and_unrealized,
    scan_and_execute_exits,
)
from rule_engine.gate import evaluate_gate
from rule_engine.regime import compute_market_regime
from rule_engine.rules import ACTIVE_RULES

log = logging.getLogger(__name__)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _rule_summary(gate) -> str:
    return "; ".join(f"{r.rule_id}:{'PASS' if r.passed else 'FAIL'}" for r in gate.rules)


def _think_from_llm(ov: "LLMVerdict", brain_result: "LLMBrainResult") -> ThinkResult:
    """Map a validated brain verdict onto our ThinkResult. The verdict is 'buy' only
    for a valid 'buying' call; the deterministic gate still must pass before any
    entry. llm_usage is None because the single brain call is recorded once up
    front (not per candidate). Thesis uses ONLY the model's own reason/checks."""
    verdict = "buy" if ov.wants_entry else "pass"
    thesis = ov.reason or (ov.checks[0] if ov.checks else "the reference brain verdict")
    return ThinkResult(
        thesis=thesis,
        invalidation=ov.invalidation or "",
        verdict=verdict,
        source=brain_result.source,
        break_taking=brain_result.break_taking,
        break_minutes=brain_result.break_minutes,
        break_reason=brain_result.break_reason,
        llm_usage=None,
    )


async def run_tick(provider, thinker: Thinker, state: dict | None = None,
                   brain: "LLMBrain | None" = None) -> dict:
    """
    The reference-style cycle (operator decision 2026-08-23):

        manage → read → think → gate → journal

    state: optional dict persisted ACROSS ticks by the caller (main()):
      {"tick": int, "theses": {mint: {...}}} — enables short-term think/thesis
      reuse. A fresh empty state means no reuse (tests).

    brain: optional LLMBrain (2026-08-27). When provided AND DATA_BACKEND=live
      AND config.LLM_BRAIN, ONE role-routed reference-style reasoning call grades every
      candidate; each candidate then uses the brain's verdict if it produced a
      valid one, else falls back to the per-candidate thinker. The deterministic
      gate below still authorizes every entry (brain verdict is necessary, not
      sufficient). Mock mode and tests are unaffected (brain stays inert).
    """
    t0 = time.monotonic()
    tick_ts = datetime.now(timezone.utc).isoformat()
    candidates = await provider.get_candidates(config.MAX_CANDIDATES_PER_TICK)
    if state is not None:
        state["tick"] = state.get("tick", 0) + 1

    # --- BLOCKLIST: manual + auto-blocked mints never reach think/enrichment
    # (saves qwen + stealth-scrape credits, and is the DONT churn killer).
    candidates, blocked_now = filter_candidates(candidates)
    for sym, reason in blocked_now:
        log.info("BLOCKED %s skipped: %s", sym, reason)

    # --- FAKE-CHART filter (A7, omo isFakeChart parity): wash-traded / dead /
    # manufactured tapes never reach enrichment or think/gate, so they burn no
    # scrape or LLM credits and never skew the regime. Each rejection logs its
    # tripped threshold (defense-first rule 6).
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
    candidates = real

    # --- READ stage: crowd conviction (fomo.fun board).
    # Live feeds ONLY in live mode — mock runs stay hermetic and fast (a real
    # feed answering for mock mints once flipped every verdict to fail).
    # Fail-soft: a dead feed leaves the presence proxy in place.
    if config.DATA_BACKEND == "live":
        try:
            from data_providers.crowd import enrich_crowd_heat
            await enrich_crowd_heat(candidates)
        except Exception:
            log.warning("crowd enrichment failed — proxy heat in use (fail-soft)",
                        exc_info=True)

    # --- READ stage part 2: second-pass cross-pool research on the head of
    # the board (the reference researches the names it cares about). Live-only so mock
    # runs stay hermetic; fail-soft like every feed.
    if config.DATA_BACKEND == "live":
        try:
            from data_providers.research import enrich_with_research
            await enrich_with_research(candidates)
        except Exception:
            log.warning("token research failed - continuing without it",
                        exc_info=True)
        # --- READ stage part 3: realtime social read (evidence only, never a
        # verdict). Provider-agnostic (Groq/Grok/OpenRouter); disabled when no
        # SOCIAL_LLM_API_KEY is configured.
        social_usages = []
        try:
            from llm.social import enrich_social
            _, social_usages = await enrich_social(candidates)
        except Exception:
            log.warning("social read failed - continuing without it",
                        exc_info=True)
        try:
            from llm.web_research import enrich_web
            await enrich_web(candidates)
        except Exception:
            log.warning("web research failed - continuing without it,", exc_info=True)
    # Regime computed ONCE per tick from the full batch (C2), logged ONCE.
    regime = compute_market_regime(candidates)

    opened = closed = reused = 0
    async with db.get_db() as conn:
        await db.insert_market_regime(
            conn,
            computed_at=regime.computed_at,
            candidate_count=len(candidates),
            pct_green=regime.pct_candidates_green_1h,
            median_vol=regime.median_volume_1h_usd,
            avg_ratio=regime.avg_buy_sell_ratio,
            regime_ok=regime.regime_ok,
            detail=regime.regime_detail,
        )
        await db.insert_event(conn, "read", tick_ts,
                      payload={"candidate_count": len(candidates),
                           "regime_ok": regime.regime_ok})
        log.info("tick regime: %s (%s)", "OK" if regime.regime_ok else "BAD",
                 regime.regime_detail)

        # Record social LLM usages
        if config.DATA_BACKEND == "live":
            for su in social_usages:
                await db.insert_llm_call_usage(
                    conn,
                    ts=tick_ts,
                    task=su.task,
                    provider=su.provider,
                    model=su.model,
                    status="success" if not su.degradation_reason else "error",
                    tick_ts=tick_ts,
                    mint_address=su.mint_address,
                    latency_ms=int(su.latency_ms),
                    input_tokens=su.input_tokens,
                    cache_hit_tokens=su.cache_hit_tokens,
                    output_tokens=su.output_tokens,
                    total_tokens=su.total_tokens,
                    estimated_cost_usd=su.estimated_cost_usd,
                    is_peak_window=su.is_peak_window,
                    degradation_reason=su.degradation_reason,
                )

        # --- LLM BRAIN (live + LLM_BRAIN only): ONE role-routed reference-style call
        # grades every candidate. Fail-closed: an empty/invalid result leaves
        # brain_result with no verdicts, so each candidate below falls back to
        # the per-candidate thinker. The single call's usage is recorded once.
        use_brain = (brain is not None and config.DATA_BACKEND == "live"
                     and config.LLM_BRAIN)
        brain_result = None
        if use_brain:
            portfolio_now = await load_portfolio_state(conn)
            brain_result = await brain.tick(candidates, portfolio_now)
            if brain_result.llm_usage is not None:
                bu = brain_result.llm_usage
                await db.insert_llm_call_usage(
                    conn,
                    ts=tick_ts,
                    task=bu.task,
                    provider=bu.provider,
                    model=bu.model,
                    status="success" if not bu.degradation_reason else "error",
                    tick_ts=tick_ts,
                    mint_address=None,
                    latency_ms=int(bu.latency_ms),
                    input_tokens=bu.input_tokens,
                    cache_hit_tokens=bu.cache_hit_tokens,
                    output_tokens=bu.output_tokens,
                    total_tokens=bu.total_tokens,
                    estimated_cost_usd=bu.estimated_cost_usd,
                    is_peak_window=bu.is_peak_window,
                    degradation_reason=bu.degradation_reason,
                )
            if brain_result.break_taking:
                from rule_engine import liveness
                liveness.set_break(True, brain_result.break_minutes,
                                   brain_result.break_reason)
                log.warning("llm_brain self-regulating break: %d mins (%s)",
                            brain_result.break_minutes, brain_result.break_reason)
            log.info("llm_brain tick: %d verdict(s) | source=%s%s",
                     len(brain_result.verdicts), brain_result.source,
                     " (degraded)" if brain_result.degraded else "")

        # --- REF-R8/R9: risk budget + calibration for this tick --------------
        # equity = cash + open position value; unrealized = open pnl. Marks
        # that fail to price are held at cost (never fabricated). Fail-closed:
        # any error leaves (0, 0) -> the budget collapses to MIN_TICKET_USD and
        # conviction to 1.0. Sizing stays pure code; the model never sizes.
        price_map: dict = {}
        equity_usd = 0.0
        unrealized_usd = 0.0
        try:
            book = await load_portfolio_state(conn)
            for t in book.open_positions:
                try:
                    decimals = (t.candidate_snapshot or {}).get("decimals")
                    px = await provider.get_current_price(t.mint_address,
                                                          decimals)
                    if px is not None and px > 0:
                        price_map[t.mint_address] = px
                except Exception:
                    continue   # unpriced mark -> held at cost below
            equity_usd, unrealized_usd = portfolio_equity_and_unrealized(
                book, price_map)
        except Exception:
            log.warning("risk budget inputs unreadable - sizing fails closed "
                        "to minimum ticket", exc_info=True)
            equity_usd = 0.0
            unrealized_usd = 0.0
        risk_budget = compute_risk_budget(equity_usd, unrealized_usd)
        try:
            calibration = compute_calibration(
                await db.get_all_closed_trades(conn))
        except Exception:
            log.warning("calibration unreadable - failing closed to 1.0",
                        exc_info=True)
            calibration = FLAT_CALIBRATION
        log.info("risk budget: equity $%.2f, dd factor %.3f, max order $%.0f, "
                 "max daily $%.0f | calibration: %d sample(s), conviction %.3f",
                 risk_budget.equity_usd, risk_budget.drawdown_factor,
                 risk_budget.max_order_usd, risk_budget.max_daily_usd,
                 calibration.samples, calibration.conviction_factor)
        # Persist so the public surfaces (disclosure.json) read the same
        # numbers the sizing used. Fail-soft: persistence never kills a tick.
        try:
            await db.patch_daily_stats(
                conn, datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                {"risk_budget": risk_budget.to_dict(),
                 "calibration": calibration.to_dict()})
        except Exception:
            log.warning("risk budget/calibration persistence failed "
                        "(non-fatal)", exc_info=True)

        for c in candidates:
            # --- THINK (the reference order): the model writes its assessment BEFORE
            # any rule is evaluated. Its verdict is a necessary veto layer.
            memories = await db.recall_memories(conn, topic=c.symbol, limit=3)
            memory_line = ""
            if memories:
                memory_line = "Memory (context only): " + " | ".join(
                    f"{m['topic']}: {m['note']}" for m in memories)
            # the reference BRAIN verdict if the brain produced a valid one for this
            # candidate; otherwise the per-candidate thinker (fail-closed path).
            ov = (brain_result.verdict_for(c.symbol)
                  if (use_brain and brain_result is not None) else None)
            if ov is not None:
                think = _think_from_llm(ov, brain_result)
            else:
                try:
                    think = await thinker.think(c, memory_line)
                except Exception as e:
                    log.error("thinker error on %s: %s", c.symbol, e, exc_info=True)
                    from llm.thinker import template_think
                    think = template_think(c)

            # Record thinker LLM usage
            if getattr(think, "llm_usage", None):
                tu = think.llm_usage
                await db.insert_llm_call_usage(
                    conn,
                    ts=tick_ts,
                    task=tu.task,
                    provider=tu.provider,
                    model=tu.model,
                    status="success" if not tu.degradation_reason else "error",
                    tick_ts=tick_ts,
                    mint_address=c.mint_address,
                    latency_ms=int(tu.latency_ms),
                    input_tokens=tu.input_tokens,
                    cache_hit_tokens=tu.cache_hit_tokens,
                    output_tokens=tu.output_tokens,
                    total_tokens=tu.total_tokens,
                    estimated_cost_usd=tu.estimated_cost_usd,
                    is_peak_window=tu.is_peak_window,
                    degradation_reason=tu.degradation_reason,
                )

            if think.break_taking:
                from rule_engine import liveness
                liveness.set_break(True, think.break_minutes, think.break_reason)
                log.warning("self-regulating break triggered: %d mins (reason: %s)", think.break_minutes, think.break_reason)
                
            await db.insert_event(
                conn, "thought", tick_ts, c.symbol, c.mint_address,
                {"verdict": think.verdict, "source": think.source,
                 "invalidation": think.invalidation,
                 "break_taking": think.break_taking,
                 "break_reason": think.break_reason},
            )

            portfolio = await load_portfolio_state(conn)
            gate = evaluate_gate(c, portfolio, regime, ACTIVE_RULES)

            # --- GATE: think→gate intersection. Either side alone refuses.
            entry_allowed = gate.all_passed and think.wants_entry

            # --- SIZING + daily deploy cap -----------------------------------
            # REF-R8/R9: risk-budget mode sizes from live equity x drawdown
            # factor x conviction; the daily ceiling is the derived
            # max_daily_usd. Other modes keep the static cap (unchanged).
            cand_equity, cand_unrealized = portfolio_equity_and_unrealized(
                portfolio, price_map)
            ticket = compute_ticket(
                portfolio.cash_usd, c.fomo_heat,
                equity_usd=cand_equity, unrealized_usd=cand_unrealized,
                conviction_factor=calibration.conviction_factor)
            deployed = await db.deployed_today(conn)
            if config.SIZING_MODE == "risk_budget":
                daily_ceiling = compute_risk_budget(
                    cand_equity, cand_unrealized).max_daily_usd
            else:
                daily_ceiling = config.DAILY_DEPLOY_CAP_USD
            refusal_reasons: list[str] = []
            if ticket < config.MIN_TICKET_USD:
                refusal_reasons.append("[ticket below minimum]")
            if deployed + ticket > daily_ceiling:
                refusal_reasons.append("[daily deploy cap reached]")
            if entry_allowed and refusal_reasons:
                entry_allowed = False

            full_thesis = think.thesis + (
                f" | invalidates if: {think.invalidation}"
                if think.invalidation else ""
            )
            if refusal_reasons:
                full_thesis += " " + " ".join(refusal_reasons)

            # --- reuse prior thesis if stats AND verdict are unchanged ------
            reused_now = False
            if state is not None:
                prior = (state.get("theses") or {}).get(c.mint_address)
                within_window = (
                    prior is not None
                    and state["tick"] - prior["tick"] <= REUSE_TICK_WINDOW
                    and prior.get("think_verdict") == think.verdict
                )
                if within_window and reused_if_stable(
                        prior["decision"], gate.all_passed,
                        gate.failed_rule_ids, stats_signature(c)):
                    reused_now = True
                    reused += 1

            # Thesis shown verbatim — full_thesis already carries the model's
            # answer plus its invalidation sentence exactly once.
            thesis_text = full_thesis

            event = FeedEvent(
                symbol=c.symbol,
                mint_address=c.mint_address,
                candidate_snapshot=c.to_dict(),
                verdict="pass" if entry_allowed else "fail",
                thesis=thesis_text,
                rule_breakdown=[
                    {"rule_id": r.rule_id, "passed": r.passed,
                     "detail": r.detail, "value": r.value}
                    for r in gate.rules
                ],
                failed_rule_ids=gate.failed_rule_ids,
                regime_ok=regime.regime_ok,
                grounding_flags=think.grounding_flags,
                narration_source=think.source,
            )

            # --- SEAL (reference parity): tamper-evident audit commit BEFORE any
            # action. sha256(nonce|canonical payload) stored with plaintext.
            now_iso = datetime.now(timezone.utc).isoformat()
            nonce = uuid.uuid4().hex
            commit_payload = {
                "tick_ts": now_iso,
                "symbol": c.symbol,
                "mint": c.mint_address,
                "think_verdict": think.verdict,
                "think_source": think.source,
                "entry_allowed": entry_allowed,
                "failed_rule_ids": list(gate.failed_rule_ids),
                "stats": stats_signature(c),
                "invalidation": think.invalidation,
            }
            canonical = json.dumps(commit_payload, sort_keys=True,
                                   separators=(",", ":"))
            payload_hash = hashlib.sha256(
                (nonce + "|" + canonical).encode()).hexdigest()
            await db.insert_decision_commit(
                conn, now_iso, now_iso, c.symbol, c.mint_address,
                think.verdict, entry_allowed, nonce, canonical, payload_hash,
                model_version=think.llm_usage.model if getattr(think, "llm_usage", None) else None,
                prompt_version=think.llm_usage.pricing_snapshot_id if getattr(think, "llm_usage", None) else None,
            )

            # --- EXECUTE: open only when both layers agree ------------------
            if entry_allowed and not refusal_reasons:
                existing = await db.get_open_trade_for_mint(conn, c.mint_address)
                if existing is None:
                    result = await open_position(conn, c, gate,
                                                 ticket_usd=ticket)
                    if result.applied:
                        trade_text = full_thesis
                        await db.set_trade_thesis(
                            conn, result.trade.trade_id, trade_text)
                        await db.upsert_thesis(
                            conn,
                            trade_id=result.trade.trade_id,
                            mint_address=c.mint_address,
                            symbol=c.symbol,
                            author=f"model:{think.source}",
                            thesis=trade_text,
                            created_at=result.trade.opened_at,
                        )
                        event.led_to_trade_id = result.trade.trade_id
                        opened += 1
                        await db.insert_event(
                            conn, "trade", tick_ts, c.symbol, c.mint_address,
                            {"action": "open", "trade_id": result.trade.trade_id},
                        )
            elif refusal_reasons and entry_allowed:
                log.info("ENTRY REFUSED %s: %s", c.symbol,
                         " ".join(refusal_reasons))

            if state is not None:
                state.setdefault("theses", {})[c.mint_address] = {
                    "tick": state["tick"],
                    "decision": {"all_passed": gate.all_passed,
                                 "failed_rule_ids": list(gate.failed_rule_ids),
                                 "stats": stats_signature(c)},
                    "think_verdict": think.verdict,
                    "invalidation": think.invalidation,
                    "thesis": think.thesis,
                }

            event.narration_source = getattr(think, "source", "unknown")
            if getattr(think, "llm_usage", None):
                event.model_version = think.llm_usage.model
                event.prompt_version = think.llm_usage.pricing_snapshot_id
            event.id = await db.insert_feed_event(conn, event)
            await db.insert_event(
                conn, "did" if entry_allowed else "refused", tick_ts,
                c.symbol, c.mint_address,
                {"entry_allowed": entry_allowed,
                 "failed_rule_ids": list(gate.failed_rule_ids),
                 "model_verdict": think.verdict},
            )

        # --- manage: the reference bot-model exit engine (§5.2 rebuild) -------------
        # Full rule set + sell risk gate; runs here AND on the dedicated fast
        # loop (_exit_loop), because stops gap badly when checked once/minute.
        closed += await scan_and_execute_exits(
            provider, conn, on_close=_on_closed_trade
        )

        # --- REF-R7: retro audit-log signature matching (post-cycle) --------
        # Attributes out-of-pipeline fills to decision rows when a fill
        # bypasses the tick. Fail-soft; never blocks the tick.
        try:
            from retro_matcher import run_retro_match
            await run_retro_match(conn)
        except Exception:
            log.debug("retro_match failed (non-fatal)", exc_info=True)

    elapsed_ms = (time.monotonic() - t0) * 1000.0   # K4 latency instrumentation
    log.info("tick done in %.0f ms: %d candidates, %d entries/scale-ins, "
             "%d closes, %d theses reused",
             elapsed_ms, len(candidates), opened, closed, reused)
    return {"candidates": len(candidates), "opened": opened,
            "closed": closed, "elapsed_ms": elapsed_ms}


def _rule_summary_text(trade) -> str:
    snap = trade.candidate_snapshot or {}
    return f"entry at ${trade.entry_price_usd:.8f} ({snap.get('source', 'unknown')} data)"


async def _on_closed_trade(closed_trade) -> None:
    """Hook for the exit scanner: schedule a fire-and-forget reflection."""
    if closed_trade is None:
        return
    rule_summary = _rule_summary_text(closed_trade)
    asyncio.create_task(_store_reflection(closed_trade.trade_id, rule_summary))


async def _exit_loop(provider) -> None:
    """
    Dedicated fast exit scanner (the reference bot 'manage' cadence). Memecoins move
    faster than the 60s tick; risk checks run every EXIT_SCAN_INTERVAL_SECONDS
    with price-only HTTP and zero LLM in the path. Failures are logged and the
    loop continues (fail-closed: missing a scan never opens risk, it only
    delays a reaction).
    """
    while True:
        try:
            async with db.get_db() as conn:
                actions = await scan_and_execute_exits(
                    provider, conn, on_close=_on_closed_trade
                )
            if actions:
                log.info("exit scan: %d action(s) executed", actions)
        except Exception:
            log.exception("exit scan failed — continuing (fail-closed)")
        await asyncio.sleep(config.EXIT_SCAN_INTERVAL_SECONDS)


async def _store_reflection(trade_id: str, rule_summary: str) -> None:
    from models import Trade
    try:
        async with db.get_db() as conn:
            trade = await db.get_trade_by_id(conn, trade_id)
            if trade is None or trade.is_open:
                return
            text = await generate_reflection(trade, rule_summary)
            await db.update_reflection(conn, trade_id, text)
    except Exception:
        log.warning("reflection for %s failed (non-fatal)", trade_id, exc_info=True)


async def main() -> None:
    setup_logging()
    log.info("trading-bot starting | PAPER_TRADING_ONLY=%s | backend=%s",
             config.PAPER_TRADING_ONLY, config.DATA_BACKEND)
    assert config.PAPER_TRADING_ONLY is True
    await db.init_db()
    provider = build_provider()
    thinker = Thinker()
    # Reference-style brain (2026-08-27): one role-routed reasoning call per tick in
    # live mode. Inert in mock/tests; the deterministic gate still authorizes.
    brain = LLMBrain()
    # Cross-tick state: tick counter + per-mint think/thesis reuse cache.
    # REUSE_TICK_WINDOW bounds how old a reused thesis may be.
    state: dict = {"tick": 0, "theses": {}}
    last_learning_date: str | None = None
    exit_task: asyncio.Task | None = None

    try:
        exit_task = asyncio.create_task(_exit_loop(provider))
        while True:
            try:
                await run_tick(provider, thinker, state, brain=brain)
            except Exception:
                log.exception("tick failed — continuing next interval (fail-closed)")
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if last_learning_date != today:
                try:
                    from learning_loop import run_daily_learning
                    await run_daily_learning()
                    last_learning_date = today
                except Exception:
                    log.exception("daily learning failed (non-fatal)")
            await asyncio.sleep(config.TICK_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        if exit_task is not None:
            exit_task.cancel()
        close_provider = getattr(provider, "aclose", None)
        if close_provider is not None:
            # LiveProviderStack closes the WS feed + shared HTTP client.
            try:
                await close_provider()
            except Exception:
                log.warning("provider shutdown raised (non-fatal)", exc_info=True)
        await thinker.aclose()


if __name__ == "__main__":
    asyncio.run(main())