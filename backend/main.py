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
from llm.narrator import generate_reflection
from llm.reuse import REUSE_TICK_WINDOW, reused_if_stable, stats_signature
from llm.thinker import Thinker
from models import FeedEvent
from paper_trading_engine import (
    compute_ticket,
    load_portfolio_state,
    open_position,
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


async def run_tick(provider, thinker: Thinker, state: dict | None = None) -> dict:
    """
    The omo-style cycle (operator decision 2026-08-23):

        manage → read → think → gate → journal

    state: optional dict persisted ACROSS ticks by the caller (main()):
      {"tick": int, "theses": {mint: {...}}} — enables short-term think/thesis
      reuse. A fresh empty state means no reuse (tests).
    """
    t0 = time.monotonic()
    candidates = await provider.get_candidates(config.MAX_CANDIDATES_PER_TICK)
    if state is not None:
        state["tick"] = state.get("tick", 0) + 1

    # --- BLOCKLIST: manual + auto-blocked mints never reach think/enrichment
    # (saves qwen + stealth-scrape credits, and is the DONT churn killer).
    candidates, blocked_now = filter_candidates(candidates)
    for sym, reason in blocked_now:
        log.info("BLOCKED %s skipped: %s", sym, reason)

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
        log.info("tick regime: %s (%s)", "OK" if regime.regime_ok else "BAD",
                 regime.regime_detail)

        for c in candidates:
            # --- THINK (omo order): the model writes its assessment BEFORE
            # any rule is evaluated. Its verdict is a necessary veto layer.
            think = await thinker.think(c)

            portfolio = await load_portfolio_state(conn)
            gate = evaluate_gate(c, portfolio, regime, ACTIVE_RULES)

            # --- GATE: think→gate intersection. Either side alone refuses.
            entry_allowed = gate.all_passed and think.wants_entry

            # --- SIZING + daily deploy cap -----------------------------------
            ticket = compute_ticket(portfolio.cash_usd, c.fomo_heat)
            deployed = await db.deployed_today(conn)
            refusal_reasons: list[str] = []
            if ticket < config.MIN_TICKET_USD:
                refusal_reasons.append("[ticket below minimum]")
            if deployed + ticket > config.DAILY_DEPLOY_CAP_USD:
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

            # --- SEAL (omo parity): tamper-evident audit commit BEFORE any
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
                think.verdict, entry_allowed, nonce, canonical, payload_hash)

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
                        event.led_to_trade_id = result.trade.trade_id
                        opened += 1
            elif refusal_reasons and entry_allowed:
                log.info("ENTRY REFUSED %s: %s", c.symbol,
                         " ".join(refusal_reasons))

            if state is not None:
                state.setdefault("theses", {})[c.mint_address] = {
                    "tick": state["tick"],
                    "decision": {"all_passed": gate.all_passed,
                                 "failed_rule_ids": list(gate.failed_rule_ids)},
                    "think_verdict": think.verdict,
                    "invalidation": think.invalidation,
                    "thesis": think.thesis,
                }

            event.id = await db.insert_feed_event(conn, event)

        # --- manage: omotrades-model exit engine (§5.2 rebuild) -------------
        # Full rule set + sell risk gate; runs here AND on the dedicated fast
        # loop (_exit_loop), because stops gap badly when checked once/minute.
        closed += await scan_and_execute_exits(
            provider, conn, on_close=_on_closed_trade
        )

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
    Dedicated fast exit scanner (omotrades 'manage' cadence). Memecoins move
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
    # Cross-tick state: tick counter + per-mint think/thesis reuse cache.
    # REUSE_TICK_WINDOW bounds how old a reused thesis may be.
    state: dict = {"tick": 0, "theses": {}}
    last_learning_date: str | None = None
    exit_task: asyncio.Task | None = None

    try:
        exit_task = asyncio.create_task(_exit_loop(provider))
        while True:
            try:
                await run_tick(provider, thinker, state)
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

