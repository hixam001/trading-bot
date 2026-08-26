"""
learning_loop.py — daily aggregate stats + threshold review input (G1–G3).

Computes and PERSISTS daily stats and per-rule rejection breakdowns, and
LOGS human-review threshold recommendations. It never auto-applies any
threshold change — recommendations are advisory log lines only.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timezone

import config
from api import db
from models import DailyStats
from promotion_gate import _max_drawdown_pct, _profit_factor

log = logging.getLogger(__name__)


def compute_daily_stats(closed_trades: list) -> dict:
    """Win rate, profit factor, max drawdown, total P&L (G1)."""
    closed = [t for t in closed_trades if t.realized_pnl_usd is not None]
    n = len(closed)
    wins = sum(1 for t in closed if t.realized_pnl_usd > 0)
    total_pnl = sum(t.realized_pnl_usd for t in closed)
    pf = _profit_factor(closed)
    dd = _max_drawdown_pct(closed)
    return {
        "closed_trades": n,
        "win_rate": round(wins / n, 4) if n else None,
        "profit_factor": round(pf, 4) if pf is not None else None,
        "max_drawdown_pct": round(dd, 2),
        "total_pnl_usd": round(total_pnl, 4),
    }


async def run_daily_learning() -> dict:
    """
    Aggregate today's stats, persist them via upsert_daily_stats, log the
    per-rule rejection breakdown and advisory recommendations (G2/G3).
    Called once per UTC day by the tick loop; also callable manually.
    """
    async with db.get_db() as conn:
        closed = await db.get_all_closed_trades(conn)
        events = await db.get_feed_events(conn, limit=1000)
        usages = await db.get_llm_call_usage(conn, limit=1000)

    stats = compute_daily_stats(closed)
    
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily_cost = sum(
        u["estimated_cost_usd"] or 0.0 for u in usages
        if u["ts"].startswith(today)
    )
    stats["daily_llm_cost_usd"] = round(daily_cost, 4)

    # G2: which rules are responsible for the most rejections?
    reject_counter: Counter = Counter()
    for e in events:
        if e["verdict"] == "fail":
            for rid in e["failed_rule_ids"]:
                reject_counter[rid] += 1
    stats["rejection_breakdown"] = dict(reject_counter.most_common())

    # G3: advisory only — logged, never applied.
    top_rules = reject_counter.most_common(3)
    if top_rules:
        rec = ", ".join(f"{rid}: {n} rejections" for rid, n in top_rules)
        log.info(
            "[threshold review] Rules causing most rejections today: %s. "
            "Review their thresholds against realized outcomes; changes are "
            "manual edits to config.py, never automatic.", rec,
        )
        stats["recommendations"] = [rid for rid, _ in top_rules]

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ds = DailyStats(
        date=today,
        open_positions=0,   # recomputed on read; historical snapshot value
        closed_trades=len(closed),
        stats_json=stats,
    )
    async with db.get_db() as conn:
        await db.upsert_daily_stats(conn, ds)
    log.info("daily learning loop persisted %s: %s", today,
             json.dumps({k: v for k, v in stats.items() if k != "rejection_breakdown"}))
    return stats
