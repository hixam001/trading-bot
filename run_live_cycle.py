"""
run_live_cycle.py - ONE autonomous omo-style decision cycle (root bridge).

Option-A arming: after a human edits live_execution/config.py to flip
LIVE_TRADING_ENABLED (and, for fully autonomous flow,
REQUIRE_MANUAL_CONFIRMATION=False), this runner drives the full pipeline:

    manage -> read -> think -> gate -> execute

The isolation contract stays intact: backend/ never imports live_execution.
This ROOT script imports the paper side READ-ONLY (providers, thinker, pure
rule functions are the shared brain) and routes entries/exits through
live_execution.executor.place_order into the REAL book. The paper tick loop
keeps running its own simulated book independently - two books, one brain.

Usage:
    .venv/bin/python run_live_cycle.py --once   # one cycle, then exit
    .venv/bin/python run_live_cycle.py          # loop at TICK_INTERVAL_SECONDS

DISARMED by default: while LIVE_TRADING_ENABLED is False every order comes
back "unarmed" and only reads happen. Devnet first. Always.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
for p in (str(BACKEND), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import config as paper_config                     # noqa: E402
from blocklist import filter_candidates           # noqa: E402
from data_providers import build_provider         # noqa: E402
from data_providers.jupiter import JupiterProvider  # noqa: E402
from llm.thinker import Thinker                   # noqa: E402
from models import PortfolioState, Trade          # noqa: E402
from rule_engine.exits import ExitInput, evaluate_exits  # noqa: E402
from rule_engine.gate import evaluate_gate        # noqa: E402
from rule_engine.regime import compute_market_regime  # noqa: E402
from rule_engine.rules import ACTIVE_RULES        # noqa: E402
from api import db                                # noqa: E402

from live_execution import config as live_config  # noqa: E402
from live_execution import solana                 # noqa: E402
from live_execution.executor import place_order   # noqa: E402
from live_execution.models import ExecutionLedger  # noqa: E402

log = logging.getLogger("run_live_cycle")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _live_portfolio(ledger: ExecutionLedger) -> tuple[PortfolioState, dict]:
    """PortfolioState for the gate built from the LIVE ledger (read-only).
    Returns (portfolio, {mint: {price_usd, tokens, opened_ts}} metadata).
    """
    positions: list[Trade] = []
    meta: dict = {}
    for r in ledger._load():   # same package - internal read is acceptable
        if r.get("kind") != "buy" or r.get("status") not in ledger._OPEN:
            continue
        mint = r["mint"]
        price = float(r.get("price_usd") or 0)
        tokens = float(r.get("tokens_out") or 0)
        if mint in meta:
            m = meta[mint]
            m["tokens"] += tokens
            m["cost"] += float(r.get("usd_size") or 0)
            continue
        meta[mint] = {
            "price_usd": price,
            "tokens": tokens,
            "cost": float(r.get("usd_size") or 0),
            "opened_ts": float(r.get("ts") or 0),
        }
    for mint, m in meta.items():
        qty = m["tokens"] or 1.0
        positions.append(Trade(
            trade_id=f"live-{mint[:8]}",
            symbol=mint[:6],
            mint_address=mint,
            opened_at=_iso(m["opened_ts"]), 
            entry_price_usd=m["price_usd"],
            position_size_usd=m["cost"],
            quantity=qty,
            candidate_snapshot={}, 
            thesis="live book", 
            is_open=True,
        ))
    exposure = sum(p.position_size_usd for p in positions)
    cash = max(live_config.MAX_TOTAL_EXPOSURE_USD - exposure, 0.0)
    return PortfolioState(cash_usd=cash, open_positions=positions), meta


async def _manage(jupiter: JupiterProvider, ledger: ExecutionLedger, hwm: dict, meta: dict) -> None:
    """Re-price every open position, run the omo exit rule set, route sells."""
    for mint, m in meta.items():
        try:
            dec = await solana.get_mint_decimals(mint)
            if dec is None:
                log.warning("manage %s: decimals unknown - skipped this pass", mint[:8])
                continue
            price = await jupiter.get_current_price(mint, decimals=dec)
        except Exception as exc:
            log.warning("manage %s: pricing failed (%s)", mint[:8], exc)
            continue
        if price <= 0:
            continue
        prev = hwm.get(mint, m["price_usd"])
        hwm[mint] = max(prev, price)
        trade = Trade(
            trade_id=f"live-{mint[:8]}", symbol=mint[:6], mint_address=mint,
            opened_at=_iso(m["opened_ts"]), entry_price_usd=m["price_usd"],
            position_size_usd=m["cost"], quantity=m["tokens"] or 1.0,
            candidate_snapshot={}, thesis="live book", is_open=True,
            high_water_usd=hwm[mint],
        )
        decision = evaluate_exits(ExitInput(trade=trade, price_usd=price, high_water_usd=hwm[mint]))
        if decision.action == "hold":
            continue
        log.info("EXIT %s %s (%s)", decision.action, mint[:8], decision.detail)
        result = await place_order(
            side="sell", mint=mint, symbol=mint[:6],
            fraction=decision.fraction,
        )
        log.info("sell -> %s %s", result.status, result.reason)
        if result.status == "filled" and decision.action == "close_full":
            hwm.pop(mint, None)
            async with db.get_db() as conn:
                pnl = result.usd_value - m["cost"]
                await db.retire_thesis(
                    conn,
                    trade_id=f"live-{mint[:8]}",
                    closed_at=datetime.now(timezone.utc).isoformat(),
                    realized_pnl_usd=pnl,
                )


async def run_cycle(once: bool = False) -> dict:
    """One full cycle. Returns a step-by-step outcome record, refusals included."""
    ledger = ExecutionLedger(live_config.STATE_DIR / "executions.json")
    portfolio, meta = _live_portfolio(ledger)
    hwm: dict = getattr(run_cycle, "_hwm", {})
    jupiter = JupiterProvider()

    await _manage(jupiter, ledger, hwm, meta)
    run_cycle._hwm = hwm

    candidates = await build_provider().get_candidates(paper_config.MAX_CANDIDATES_PER_TICK)
    candidates, blocked_now = filter_candidates(candidates)
    if paper_config.DATA_BACKEND == "live":
        try:
            from data_providers.crowd import enrich_crowd_heat
            await enrich_crowd_heat(candidates)
        except Exception:
            log.warning("crowd enrichment failed - proxy heat in use", exc_info=True)
        try:
            from data_providers.research import enrich_with_research
            await enrich_with_research(candidates)
        except Exception:
            log.warning("research failed - continuing", exc_info=True)
        try:
            from llm.web_research import enrich_web
            await enrich_web(candidates)
        except Exception:
            log.warning("web research failed - continuing without it",
                        exc_info=True)
        try:
            from llm.social import enrich_social
            await enrich_social(candidates)
        except Exception:
            log.warning("social read failed - continuing without it", exc_info=True)

    regime = compute_market_regime(candidates)
    thinker = Thinker()
    outcome = {"entries": [], "exits": [], "regime_ok": regime.regime_ok,
               "candidates": len(candidates)}
    for c in candidates:
        think = await thinker.think(c)
        
        if think.break_taking:
            from rule_engine import liveness
            liveness.set_break(think.break_minutes, think.break_reason)
            log.warning("self-regulating break triggered: %d mins (reason: %s)", think.break_minutes, think.break_reason)
            
        gate = evaluate_gate(c, portfolio, regime, ACTIVE_RULES)
        entry_allowed = gate.all_passed and think.wants_entry
        failed = [r.rule_id for r in gate.rules if not r.passed]
        log.info("%s think=%s gate=%s%s", c.symbol, think.verdict,
                 "PASS" if gate.all_passed else "FAIL:" + ",".join(failed),
                 "" if entry_allowed else " -> refused")
        if not entry_allowed:
            continue
        cash = portfolio.cash_usd
        usd = min(cash * paper_config.TICKET_CASH_FRACTION, paper_config.TICKET_MAX_USD)
        if usd < paper_config.MIN_TICKET_USD:
            continue
        result = await place_order(
            side="buy", mint=c.mint_address, symbol=c.symbol, usd=usd,
            output_decimals=c.decimals,
            idempotency_key=f"{c.mint_address}-{datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")}",
        )
        log.info("buy %s $%.2f -> %s %s", c.symbol, usd, result.status, result.reason)
        if result.status == "filled":
            async with db.get_db() as conn:
                await db.upsert_thesis(
                    conn,
                    trade_id=f"live-{c.mint_address[:8]}",
                    mint_address=c.mint_address,
                    symbol=c.symbol,
                    author=f"model:{think.source}",
                    thesis=think.thesis + (f" | invalidates if: {think.invalidation}" if think.invalidation else ""),
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
        outcome["entries"].append({"symbol": c.symbol, "status": result.status,
                                   "reason": result.reason})
        break   # one decision per cycle (omo cadence parity)

    # --- OMO-R7: retro audit-log signature matching (post-cycle) ----------
    # Only runs from the paper-side DB; the live book has its own CommitLog.
    try:
        from retro_matcher import run_retro_match
        async with db.get_db() as retro_conn:
            await run_retro_match(retro_conn)
    except Exception:
        log.debug("retro_match post-cycle failed (non-fatal)", exc_info=True)

    return outcome



def main() -> None:
    parser = argparse.ArgumentParser(description="omo-style live decision cycle")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument("--drill", action="store_true", help="DEVNET drill: exercise sign/send/confirm without Jupiter or tokens")
    args = parser.parse_args()
    if args.drill:
        from live_execution.drill import run_drill
        steps = asyncio.run(run_drill())
        failed = [s for s in steps if not s["ok"]]
        sys.exit(1 if failed else 0)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")

    if paper_config.DATA_BACKEND != "live":
        log.error("DATA_BACKEND=%s - the live cycle runs on the live stack only.", paper_config.DATA_BACKEND)
        log.error("Set DATA_BACKEND=live in .env, then re-run.")
        sys.exit(2)

    # omo disclosure parity: report the signing state unedited.
    if live_config.LIVE_TRADING_ENABLED:
        log.info("ARMED (manual confirmation %s)", "ON" if live_config.REQUIRE_MANUAL_CONFIRMATION else "OFF - fully autonomous")
    else:
        log.warning("UNARMED: LIVE_TRADING_ENABLED=False - reads/think/gate run; every order returns unarmed")

    async def loop() -> None:
        while True:
            try:
                await run_cycle(once=args.once)
            except Exception:
                log.exception("cycle crashed - continuing")
            if args.once:
                return
            await asyncio.sleep(paper_config.TICK_INTERVAL_SECONDS)

    asyncio.run(loop())


if __name__ == "__main__":
    main()