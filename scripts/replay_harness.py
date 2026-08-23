"""
scripts/replay_harness.py — backtest the current pipeline over historical
trade snapshots.

For each closed trade in the DB, rebuilds the Candidate from its stored
candidate_snapshot, re-evaluates the CURRENT entry gate + think template +
exit engine, and reports would-have-entered / would-have-exited outcomes
vs what actually happened. This lets you measure the effect of a rule
change BEFORE deploying it — no live capital at risk.

Usage:
    cd backend && ../.venv/bin/python ../scripts/replay_harness.py

Output: per-trade comparison table + summary (win rate, P&L delta,
model-veto count, cap-refusal count).
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from api import db
from llm.thinker import template_think
from models import Candidate, PortfolioState, Trade
from rule_engine.exits import ExitInput, evaluate_exits
from rule_engine.gate import evaluate_gate
from rule_engine.regime import compute_market_regime
from rule_engine.rules import ACTIVE_RULES


def _rebuild_trade(row) -> Trade:
    return Trade(
        trade_id=row["trade_id"], symbol=row["symbol"],
        mint_address=row["mint_address"], opened_at=row["opened_at"],
        entry_price_usd=row["entry_price_usd"] or 0.0,
        position_size_usd=row["position_size_usd"] or 100.0,
        quantity=100_000.0,
    )


async def replay() -> list[dict]:
    results = []
    async with db.get_db() as conn:
        rows = await conn.execute_fetchall(
            """
            SELECT trade_id, symbol, mint_address, opened_at, closed_at,
                   entry_price_usd, exit_price_usd, realized_pnl_pct,
                   realized_pnl_usd, exit_reason, candidate_snapshot
            FROM trades WHERE is_open = 0 ORDER BY opened_at
            """)
        for row in rows:
            snap_raw = row["candidate_snapshot"]
            try:
                snap = json.loads(snap_raw) if isinstance(snap_raw, str) \
                    else (snap_raw or {})
            except (TypeError, ValueError):
                snap = {}

            try:
                cand = Candidate(**{k: v for k, v in snap.items()
                                    if k in Candidate.__dataclass_fields__})
            except Exception as exc:
                results.append({"symbol": row["symbol"], "error": str(exc)})
                continue

            portfolio = PortfolioState(cash_usd=10_000.0)
            regime = compute_market_regime([cand])
            gate = evaluate_gate(cand, portfolio, regime, ACTIVE_RULES)
            think = template_think(cand)

            # Exit engine on the actual close price.
            exit_price = row["exit_price_usd"] or 0.0
            exit_dec = None
            if exit_price > 0 and row["entry_price_usd"]:
                fake = _rebuild_trade(row)
                exit_dec = evaluate_exits(ExitInput(
                    trade=fake, price_usd=max(exit_price, 1e-12)))

            pnl_pct = row["realized_pnl_pct"] or 0.0
            results.append({
                "symbol": row["symbol"],
                "actual_pnl_pct": round(pnl_pct, 2),
                "actual_exit_reason": row["exit_reason"],
                "gate_passes_now": gate.all_passed,
                "failed_rules_now": gate.failed_rule_ids,
                "think_verdict_template": think.verdict,
                "would_enter": gate.all_passed and think.wants_entry,
                "current_exit_rule": exit_dec.rule_id if exit_dec else "-",
                "current_exit_action": (exit_dec.action if exit_dec else "-"),
            })
    return results


def summarize(results: list[dict]) -> str:
    valid = [r for r in results if not r.get("error")]
    total = len(valid)
    if not total:
        return f"no replayable trades ({len(results)} rows total)"
    enter = sum(1 for r in valid if r["would_enter"])
    wins = sum(1 for r in valid
               if r["would_enter"] and r["actual_pnl_pct"] > 0)
    vetoed_by_gate = sum(1 for r in valid if not r["gate_passes_now"])
    vetoed_by_model = sum(
        1 for r in valid
        if r["gate_passes_now"] and not r["think_verdict_template"].startswith("buy"))
    replay_sum = sum(r["actual_pnl_pct"] for r in valid if r["would_enter"])
    actual_sum = sum(r["actual_pnl_pct"] for r in valid)
    errors = len(results) - len(valid)

    lines = [
        f"closed trades: {total} ({errors} skipped — bad/missing snapshot)",
        f"gate would-pass now: {total - vetoed_by_gate}/{total} "
        f"(blocked: {vetoed_by_gate})",
        f"template-think would-buy: {sum(1 for r in valid if r['think_verdict_template'] == 'buy')}/{total}",
        f"think-veto of gate-passing trades: {vetoed_by_model}",
        f"would-enter (both agree): {enter} → {wins}W/{enter - wins}L",
        "",
        f"P&L replay: would-enter subset = {replay_sum:+.1f}% | "
        f"actual-all-trades = {actual_sum:+.1f}% | delta "
        f"{replay_sum - actual_sum:+.1f}pp",
    ]
    return "\n".join(lines)


async def main():
    results = await replay()
    print("=== REPLAY HARNESS ===")
    print(summarize(results))
    print("\n=== PER-TRADE DETAIL ===")
    for r in results:
        if r.get("error"):
            print(f"  {r.get('symbol', '?'):>8} ERROR: {r['error']}")
            continue
        status = ("ENTER" if r["would_enter"]
                  else "skip-gate" if not r["gate_passes_now"]
                  else "skip-model")
        blocked = (",".join(r["failed_rules_now"]) or "-")[:40]
        print(f"  {r['symbol']:>8} pnl={r['actual_pnl_pct']:+6.2f}% "
              f"{status:<11} blocked-by={blocked:<40} "
              f"exit-rule-now={r['current_exit_rule']}")


if __name__ == "__main__":
    asyncio.run(main())

