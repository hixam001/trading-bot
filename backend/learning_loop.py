"""
learning_loop.py — Daily aggregate analysis and threshold recommendations.

This runs once per day (via APScheduler or direct invocation) and produces:
  1. Win-rate-by-bucket statistics from closed trade history.
  2. Human-readable threshold tuning suggestions (printed/logged — never
     auto-applied to config, per defense-first rule 2).
  3. A daily_stats row in the database for the frontend stats panel.

Per FR-28: this is complementary to per-trade reflection (FR-26), not a
replacement. Per-trade reflection gives immediate feedback after each trade.
This daily report gives aggregate-level trend analysis.

Run modes:
  - As a scheduled job: called from api/main.py via APScheduler.
  - Directly: python learning_loop.py
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone

import config
import knowledge_base
from api import db
from models import DailyStats, Trade

log = logging.getLogger(__name__)


def _compute_stats(closed_trades: list[Trade]) -> dict:
    """
    Compute aggregate portfolio statistics from all closed trades.
    Pure function — no side effects.
    """
    if not closed_trades:
        return {
            "total_closed": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": None,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "profit_factor": None,
            "total_realized_pnl": 0.0,
            "avg_pnl_usd": None,
            "avg_win_usd": None,
            "avg_loss_usd": None,
            "max_drawdown_pct": None,
        }

    total = len(closed_trades)
    pnl_values = [t.realized_pnl_usd or 0.0 for t in closed_trades]
    wins = sum(1 for p in pnl_values if p > 0)
    losses = sum(1 for p in pnl_values if p <= 0)
    gross_profit = sum(p for p in pnl_values if p > 0)
    gross_loss = abs(sum(p for p in pnl_values if p < 0))
    total_pnl = sum(pnl_values)

    win_rate = wins / total if total > 0 else None
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None
    avg_pnl = total_pnl / total if total > 0 else None
    avg_win = gross_profit / wins if wins > 0 else None
    avg_loss = -(gross_loss / losses) if losses > 0 else None

    # Max drawdown: running equity peak-to-trough
    running_equity = config.INITIAL_CASH_USD
    peak = running_equity
    max_dd_pct = 0.0
    for pnl in pnl_values:
        running_equity += pnl
        if running_equity > peak:
            peak = running_equity
        dd_pct = ((peak - running_equity) / peak) * 100 if peak > 0 else 0.0
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct

    return {
        "total_closed": total,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
        "gross_profit": round(gross_profit, 4),
        "gross_loss": round(gross_loss, 4),
        "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
        "total_realized_pnl": round(total_pnl, 4),
        "avg_pnl_usd": round(avg_pnl, 4) if avg_pnl is not None else None,
        "avg_win_usd": round(avg_win, 4) if avg_win is not None else None,
        "avg_loss_usd": round(avg_loss, 4) if avg_loss is not None else None,
        "max_drawdown_pct": round(max_dd_pct, 2),
    }


def _generate_recommendations(stats: dict, threshold_stats: dict) -> list[str]:
    """
    Generate human-readable threshold tuning suggestions.
    Returns a list of plain-text recommendation strings.

    These are NEVER auto-applied. They are for human review only.
    """
    recommendations = []

    if stats["total_closed"] < 10:
        recommendations.append(
            f"Insufficient data ({stats['total_closed']} trades) for meaningful recommendations. "
            f"Collect at least 10 closed trades."
        )
        return recommendations

    win_rate = stats["win_rate"]
    profit_factor = stats["profit_factor"]
    max_dd = stats["max_drawdown_pct"]

    if win_rate is not None and win_rate < 0.40:
        recommendations.append(
            f"Win rate is low ({win_rate:.1%}). Consider raising MIN_LIQUIDITY_USD or "
            f"MIN_HOLDER_COUNT to filter out weaker candidates."
        )

    if profit_factor is not None and profit_factor < 1.0:
        recommendations.append(
            f"Profit factor below 1.0 ({profit_factor:.2f}) — losses are outpacing wins. "
            f"Review TAKE_PROFIT_PCT and STOP_LOSS_PCT balance."
        )

    if max_dd is not None and max_dd > 25.0:
        recommendations.append(
            f"Max drawdown is high ({max_dd:.1f}%). Consider reducing POSITION_SIZE_PCT "
            f"or tightening STOP_LOSS_PCT."
        )

    # Per-bucket analysis
    by_liq = threshold_stats.get("win_rate_by_liquidity_bucket", {})
    for bucket, data in by_liq.items():
        if data["trades"] >= 5 and data["win_rate"] is not None and data["win_rate"] < 0.35:
            recommendations.append(
                f"Poor win rate in liquidity bucket {bucket} "
                f"({data['win_rate']:.1%} over {data['trades']} trades). "
                f"Consider excluding this bucket via MIN_LIQUIDITY_USD adjustment."
            )

    if not recommendations:
        recommendations.append(
            "Performance looks reasonable. No specific threshold changes suggested at this time."
        )

    return recommendations


async def run_daily_analysis() -> None:
    """
    Run the full daily aggregate analysis and persist daily_stats.
    Called by APScheduler or directly.
    """
    today = date.today().isoformat()
    log.info("Learning loop: running daily analysis for %s", today)

    async with db.get_db() as conn:
        closed_trades = await db.get_all_closed_trades(conn)
        open_trades = await db.get_open_trades(conn)

    stats = _compute_stats(closed_trades)
    threshold_stats = knowledge_base.get_filter_threshold_recommendations(closed_trades)
    recommendations = _generate_recommendations(stats, threshold_stats)

    # Log recommendations for human review
    log.info("Daily analysis for %s:", today)
    log.info("  Closed trades: %d | Win rate: %s | Profit factor: %s | Max DD: %s%%",
             stats["total_closed"],
             f"{stats['win_rate']:.1%}" if stats["win_rate"] is not None else "N/A",
             f"{stats['profit_factor']:.2f}" if stats["profit_factor"] is not None else "N/A",
             f"{stats['max_drawdown_pct']:.1f}" if stats["max_drawdown_pct"] is not None else "N/A")
    log.info("  Threshold recommendations (human review required):")
    for rec in recommendations:
        log.info("    - %s", rec)

    # Persist daily stats
    daily = DailyStats(
        date=today,
        open_positions=len(open_trades),
        closed_trades=len(closed_trades),
        recommendations={
            "stats": stats,
            "threshold_stats": threshold_stats,
            "text_recommendations": recommendations,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    async with db.get_db() as conn:
        await db.upsert_daily_stats(conn, daily)

    log.info("Daily analysis saved to database.")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(run_daily_analysis())
