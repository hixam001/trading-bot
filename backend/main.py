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
from llm.narrator import Narrator, generate_reflection
from models import FeedEvent
from paper_trading_engine import (
    check_exit_conditions,
    close_position,
    decide_and_act,
    load_portfolio_state,
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


async def run_tick(provider, narrator: Narrator) -> dict:
    t0 = time.monotonic()
    candidates = await provider.get_candidates(config.MAX_CANDIDATES_PER_TICK)

    # Regime computed ONCE per tick from the full batch (C2), logged ONCE.
    regime = compute_market_regime(candidates)

    opened = closed = 0
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

            event.id = await db.insert_feed_event(conn, event)

        # --- exits: fixed numeric conditions only (§5.2) --------------------
        for trade in await db.get_open_trades(conn):
            try:
                # Decimals from the entry snapshot are REQUIRED for a correct
                # execution-price quote (wrong decimals fabricate prices).
                decimals = (trade.candidate_snapshot or {}).get("decimals")
                price = await provider.get_current_price(trade.mint_address, decimals)
            except Exception as exc:
                log.warning("price unavailable for %s — skipping exit check: %s",
                            trade.symbol, exc)
                continue
            reason = check_exit_conditions(trade, price)
            if reason is None:
                continue
            result = await close_position(conn, trade, price, reason)
            if result.applied:
                closed += 1
                rule_summary = _rule_summary_text(trade)
                # Fire-and-forget reflection (D5): never blocks the loop.
                asyncio.create_task(_store_reflection(trade.trade_id, rule_summary))

    elapsed_ms = (time.monotonic() - t0) * 1000.0   # K4 latency instrumentation
    log.info("tick done in %.0f ms: %d candidates, %d entries/scale-ins, %d closes",
             elapsed_ms, len(candidates), opened, closed)
    return {"candidates": len(candidates), "opened": opened,
            "closed": closed, "elapsed_ms": elapsed_ms}


def _rule_summary_text(trade) -> str:
    snap = trade.candidate_snapshot or {}
    return f"entry at ${trade.entry_price_usd:.8f} ({snap.get('source', 'unknown')} data)"


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
    last_learning_date: str | None = None

    try:
        while True:
            try:
                await run_tick(provider, narrator)
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
        await narrator.aclose()


if __name__ == "__main__":
    asyncio.run(main())

