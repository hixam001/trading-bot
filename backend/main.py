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
import logging
import time
from datetime import datetime, timezone

import config
from api import db
from data_providers import build_provider
from llm.narrator import NarrationResult, Narrator, generate_reflection
from llm.reuse import REUSE_TICK_WINDOW, reused_if_stable, stats_signature
from models import FeedEvent
from paper_trading_engine import (
    decide_and_act,
    load_portfolio_state,
    scan_and_execute_exits,
)
from rule_engine.regime import compute_market_regime

log = logging.getLogger(__name__)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _rule_summary(gate) -> str:
    return "; ".join(f"{r.rule_id}:{'PASS' if r.passed else 'FAIL'}" for r in gate.rules)


async def run_tick(provider, narrator: Narrator, state: dict | None = None) -> dict:
    """
    state: optional dict persisted ACROSS ticks by the caller (main()):
      {"tick": int, "theses": {mint: {...}}} — enables short-term thesis
      reuse (Task A.5). A fresh empty state means no reuse (tests).
    """
    t0 = time.monotonic()
    candidates = await provider.get_candidates(config.MAX_CANDIDATES_PER_TICK)
    if state is not None:
        state["tick"] = state.get("tick", 0) + 1

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
            portfolio = await load_portfolio_state(conn)
            gate = await decide_and_act(c, portfolio, regime, conn)

            # --- Task A.5: reuse prior thesis if stats are unchanged ------
            narration = None
            if state is not None:
                prior = (state.get("theses") or {}).get(c.mint_address)
                within_window = (
                    prior is not None
                    and state["tick"] - prior["tick"] <= REUSE_TICK_WINDOW
                )
                if within_window and reused_if_stable(
                        prior["decision"], gate.all_passed,
                        gate.failed_rule_ids, stats_signature(c)):
                    narration = NarrationResult(prior["thesis"], "reused", [])
                    reused += 1
            if narration is None:
                narration = await narrator.narrate(gate)

            event = FeedEvent(
                symbol=c.symbol,
                mint_address=c.mint_address,
                candidate_snapshot=c.to_dict(),
                verdict="pass" if gate.all_passed else "fail",
                thesis=narration.thesis,
                rule_breakdown=[
                    {"rule_id": r.rule_id, "passed": r.passed,
                     "detail": r.detail, "value": r.value}
                    for r in gate.rules
                ],
                failed_rule_ids=gate.failed_rule_ids,
                regime_ok=regime.regime_ok,
                grounding_flags=narration.grounding_flags,
                narration_source=narration.source,
            )

            if gate.all_passed:
                trade = await db.get_open_trade_for_mint(conn, c.mint_address)
                if trade is not None:
                    event.led_to_trade_id = trade.trade_id
                    if not trade.thesis:
                        await conn.execute(
                            "UPDATE trades SET thesis = ? WHERE trade_id = ? AND is_open = 1",
                            (narration.thesis, trade.trade_id),
                        )
                        await conn.commit()
                    opened += 1

            if state is not None:
                state.setdefault("theses", {})[c.mint_address] = {
                    "tick": state["tick"],
                    "decision": {"all_passed": gate.all_passed,
                                 "failed_rule_ids": list(gate.failed_rule_ids)},
                    "thesis": narration.thesis,
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
    narrator = Narrator()
    # Cross-tick state: tick counter + per-mint thesis reuse cache (Task A.5).
    # REUSE_TICK_WINDOW bounds how old a reused thesis may be.
    state: dict = {"tick": 0, "theses": {}}
    last_learning_date: str | None = None
    exit_task: asyncio.Task | None = None

    try:
        exit_task = asyncio.create_task(_exit_loop(provider))
        while True:
            try:
                await run_tick(provider, narrator, state)
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
        await narrator.aclose()


if __name__ == "__main__":
    asyncio.run(main())

