"""
main.py — Async tick loop for trading-bot.

This is the standalone entry point for the trading engine process.
Run as: python main.py

The tick loop runs independently from the FastAPI server. Both processes
share the SQLite database (WAL mode enables concurrent reads while the tick
loop writes). The API reads data; the tick loop writes data.

Per-tick behavior:
  1. Fetch candidates from the configured data backend.
  2. Apply deterministic pre-filters.
  3. Score passing candidates with the local LLM (concurrent where applicable).
  4. For verdict=pass: open a simulated position if criteria met.
  5. Check open positions for exit conditions (take-profit, stop-loss, timeout).
  6. Persist feed events for every decision (pass AND fail).
  7. Fire-and-forget reflection task for any positions just closed (FR-26/27).

Error isolation (FR-1):
  - Individual candidate failures (LLM errors, price lookup failures) are
    caught and logged. They do not kill the tick loop.
  - Tick-level errors (DB failure, data fetch failure) are caught and logged.
    The loop sleeps and tries again on the next tick.

Performance notes:
  - LLM calls for independent candidates could be concurrent, but the local
    Qwen3-8B model on a single GPU is the bottleneck — true concurrency would
    just queue behind the GPU anyway. We score serially to avoid OOM risk and
    keep timing measurements clean. If hardware changes, this is the place to
    add asyncio.gather().
  - Tick timing is logged so the actual critical path is measurable
    (performance-discipline rule 7).
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import config
import data_ingestion
import deterministic_filter
import knowledge_base
import llm_scorer
import paper_trading_engine as engine
from api import db
from models import FeedEvent, Trade

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("trading_bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("tick_loop")


# ---------------------------------------------------------------------------
# Per-trade reflection (FR-26/27): fire-and-forget async task
# ---------------------------------------------------------------------------

async def _reflect_on_trade(trade: Trade) -> None:
    """
    Generate and persist a post-trade reflection. Called via
    asyncio.create_task() — never awaited by the tick loop.

    Failures are logged but do not affect the trade record (FR-27).
    """
    log.info("Reflection task started for trade %s (%s)", trade.trade_id, trade.symbol)
    try:
        reflection = await llm_scorer.generate_reflection(trade)
        if reflection:
            async with db.get_db() as conn:
                await db.update_trade_reflection(conn, trade.trade_id, reflection)
            log.info(
                "Reflection saved for trade %s: %.80s...",
                trade.trade_id,
                reflection.replace("\n", " "),
            )
        else:
            log.info("No reflection generated for trade %s", trade.trade_id)
    except Exception as exc:
        log.warning("Reflection failed for trade %s: %s", trade.trade_id, exc)
    finally:
        # Update knowledge base similar-trade index so next scoring
        # benefits from this outcome (FR-26b)
        try:
            knowledge_base.reload_knowledge()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main tick logic
# ---------------------------------------------------------------------------

async def _run_single_tick(tick_num: int) -> None:
    """
    Execute one complete tick cycle.

    All exceptions from individual candidates are caught here and logged.
    The tick itself may raise, which the caller (run_loop) catches and
    logs at the tick level.
    """
    t_tick_start = time.monotonic()
    log.info("=== Tick #%d ===", tick_num)

    # ── 1. Fetch candidates ──────────────────────────────────────────────────
    t_fetch = time.monotonic()
    try:
        candidates = await data_ingestion.get_candidates()
    except Exception as exc:
        log.error("Tick #%d: data fetch failed: %s", tick_num, exc)
        return
    log.info("Fetch: %d candidates in %.2fs", len(candidates), time.monotonic() - t_fetch)

    if not candidates:
        log.info("Tick #%d: no candidates returned — sleeping", tick_num)
        return

    # ── 2. Check open positions for exit conditions ──────────────────────────
    t_exit = time.monotonic()
    async with db.get_db() as conn:
        open_trades = await db.get_open_trades(conn)

    newly_closed: list[Trade] = []
    if open_trades:
        async with db.get_db() as conn:
            for trade in open_trades:
                try:
                    current_price = await data_ingestion.get_current_price(trade.mint_address)
                    exit_result = engine.check_exit_conditions(trade, current_price)
                    if exit_result is not None:
                        exit_reason, exit_price = exit_result
                        closed_trade = await engine.close_position(
                            conn, trade, exit_price, exit_reason
                        )
                        newly_closed.append(closed_trade)
                except data_ingestion.PriceUnavailableError as exc:
                    log.warning(
                        "Cannot get price for open position %s (%s): %s — skipping exit check",
                        trade.symbol, trade.trade_id, exc,
                    )
                except Exception as exc:
                    log.error(
                        "Unexpected error checking exit for trade %s: %s",
                        trade.trade_id, exc, exc_info=True,
                    )

        log.info(
            "Exit check: %d open, %d closed in %.2fs",
            len(open_trades) - len(newly_closed),
            len(newly_closed),
            time.monotonic() - t_exit,
        )

    # Fire-and-forget reflection tasks (FR-26/27) — tick loop does NOT await
    for closed_trade in newly_closed:
        asyncio.create_task(
            _reflect_on_trade(closed_trade),
            name=f"reflect_{closed_trade.trade_id[:8]}",
        )

    # ── 3. Deterministic filter ──────────────────────────────────────────────
    t_filter = time.monotonic()
    passed_candidates = []
    filtered_events: list[FeedEvent] = []

    for c in candidates:
        passed, flags = deterministic_filter.apply_filters(c)
        if not passed:
            filtered_events.append(FeedEvent(
                symbol=c.symbol,
                mint_address=c.mint_address,
                candidate_snapshot=c.to_dict(),
                verdict="fail",
                risk_flags=flags,
                thesis=f"Pre-filter rejection: {'; '.join(flags)}",
            ))
        else:
            passed_candidates.append(c)

    log.info(
        "Filter: %d/%d passed in %.2fs",
        len(passed_candidates),
        len(candidates),
        time.monotonic() - t_filter,
    )

    # Persist filter-rejection events (FR-3: log both pass and fail)
    if filtered_events:
        async with db.get_db() as conn:
            for event in filtered_events:
                event.id = await db.insert_feed_event(conn, event)

    # ── 4. LLM scoring (serial — GPU is the bottleneck) ─────────────────────
    t_score = time.monotonic()
    kb_context = knowledge_base.get_context()
    score_events: list[FeedEvent] = []
    new_positions_to_open: list[tuple] = []  # (candidate, verdict)

    for candidate in passed_candidates[:config.MAX_CANDIDATES_PER_TICK]:
        try:
            verdict = await llm_scorer.score_candidate(candidate, kb_context)
        except Exception as exc:
            log.error("LLM scoring failed for %s: %s", candidate.symbol, exc, exc_info=True)
            continue

        if verdict is None:
            # LLM failed — fail closed, skip this candidate
            log.warning("Null verdict for %s — skipping (fail closed)", candidate.symbol)
            score_events.append(FeedEvent(
                symbol=candidate.symbol,
                mint_address=candidate.mint_address,
                candidate_snapshot=candidate.to_dict(),
                verdict="fail",
                risk_flags=["llm_error"],
                thesis="LLM scoring failed — candidate skipped (fail closed).",
            ))
            continue

        event = FeedEvent(
            symbol=candidate.symbol,
            mint_address=candidate.mint_address,
            candidate_snapshot=candidate.to_dict(),
            verdict=verdict.verdict,
            confidence=verdict.confidence,
            risk_flags=verdict.risk_flags,
            entry_condition=verdict.entry_condition,
            invalidation_condition=verdict.invalidation_condition,
            thesis=verdict.thesis,
        )

        if verdict.verdict == "pass":
            new_positions_to_open.append((candidate, verdict, event))
        else:
            score_events.append(event)

    log.info(
        "LLM: %d scored in %.2fs | %d pass, %d fail",
        len(passed_candidates),
        time.monotonic() - t_score,
        len(new_positions_to_open),
        len(score_events),
    )

    # ── 5. Open new positions ────────────────────────────────────────────────
    async with db.get_db() as conn:
        for candidate, verdict, event in new_positions_to_open:
            try:
                trade = await engine.open_position(conn, candidate, verdict)
                if trade is not None:
                    event.led_to_trade_id = trade.trade_id
                    score_events.append(event)
                else:
                    # open_position returned None (max positions, no cash, etc.)
                    event.verdict = "fail"
                    event.risk_flags = event.risk_flags + ["position_not_opened"]
                    event.thesis += " [Position not opened: max positions or insufficient cash]"
                    score_events.append(event)
            except Exception as exc:
                log.error(
                    "Failed to open position for %s: %s",
                    candidate.symbol, exc, exc_info=True,
                )
                event.verdict = "fail"
                event.risk_flags = event.risk_flags + ["open_position_error"]
                score_events.append(event)

    # Persist all LLM-scored feed events
    if score_events:
        async with db.get_db() as conn:
            for event in score_events:
                event.id = await db.insert_feed_event(conn, event)

    total_time = time.monotonic() - t_tick_start
    log.info("=== Tick #%d done in %.2fs ===", tick_num, total_time)


# ---------------------------------------------------------------------------
# Loop runner
# ---------------------------------------------------------------------------

async def run_loop() -> None:
    """
    Main async tick loop. Runs indefinitely until interrupted.
    Per-tick errors are caught and logged; the loop always continues.
    """
    log.info(
        "Tick loop starting | backend=%s | model=%s | interval=%ds | paper_only=%s",
        config.DATA_BACKEND,
        config.MODEL_NAME,
        config.TICK_INTERVAL_SECONDS,
        config.PAPER_TRADING_ONLY,
    )

    # Initialise database
    await db.init_db()
    log.info("Database ready")

    tick_num = 0
    while True:
        tick_num += 1
        try:
            await _run_single_tick(tick_num)
        except asyncio.CancelledError:
            log.info("Tick loop cancelled — shutting down")
            break
        except Exception as exc:
            log.error(
                "Tick #%d unhandled error (loop continues): %s",
                tick_num, exc, exc_info=True,
            )

        try:
            await asyncio.sleep(config.TICK_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            log.info("Tick sleep cancelled — shutting down")
            break


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _handle_shutdown(loop: asyncio.AbstractEventLoop) -> None:
    log.info("Shutdown signal received")
    for task in asyncio.all_tasks(loop):
        task.cancel()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_shutdown, loop)
        except NotImplementedError:
            pass  # Windows doesn't support add_signal_handler

    try:
        loop.run_until_complete(run_loop())
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Tick loop stopped.")
    finally:
        pending = asyncio.all_tasks(loop)
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(llm_scorer.close_client())
        loop.run_until_complete(data_ingestion.close_http_client())
        loop.close()
        log.info("Clean shutdown complete.")
