"""
api/routes/stats.py — GET /api/stats, GET /api/learning-window

Portfolio-level stats: equity curve, win rate, profit factor, drawdown,
cash balance, and learning window progress.
"""
from __future__ import annotations

from fastapi import APIRouter

import config
from api import db
from models import Trade

router = APIRouter()


def _build_equity_curve(closed_trades: list[Trade]) -> list[dict]:
    """
    Build a time series of equity points from closed trade history.
    Starting point is INITIAL_CASH_USD at the time of the first trade.

    Each point: { timestamp: str, equity_usd: float, pct_return: float }
    """
    if not closed_trades:
        return []

    points = []
    equity = config.INITIAL_CASH_USD

    for t in closed_trades:
        equity += t.realized_pnl_usd or 0.0
        pct_return = ((equity - config.INITIAL_CASH_USD) / config.INITIAL_CASH_USD) * 100.0
        points.append({
            "timestamp": t.closed_at,
            "equity_usd": round(equity, 4),
            "pct_return": round(pct_return, 2),
        })

    return points


def _compute_stats_dict(closed_trades: list[Trade], cash: float, open_count: int) -> dict:
    total = len(closed_trades)
    if total == 0:
        return {
            "cash_balance_usd": round(cash, 4),
            "total_realized_pnl_usd": 0.0,
            "open_positions": open_count,
            "total_closed_trades": 0,
            "win_count": 0,
            "loss_count": 0,
            "win_rate": None,
            "profit_factor": None,
            "max_drawdown_pct": 0.0,
            "avg_pnl_usd": None,
            "avg_win_usd": None,
            "avg_loss_usd": None,
        }

    pnl_values = [t.realized_pnl_usd or 0.0 for t in closed_trades]
    wins = [p for p in pnl_values if p > 0]
    losses = [p for p in pnl_values if p < 0]

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    total_pnl = sum(pnl_values)

    win_rate = len(wins) / total if total > 0 else None
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None

    # Max drawdown
    equity = config.INITIAL_CASH_USD
    peak = equity
    max_dd = 0.0
    for pnl in pnl_values:
        equity += pnl
        if equity > peak:
            peak = equity
        if peak > 0:
            dd = ((peak - equity) / peak) * 100.0
            if dd > max_dd:
                max_dd = dd

    return {
        "cash_balance_usd": round(cash, 4),
        "total_realized_pnl_usd": round(total_pnl, 4),
        "open_positions": open_count,
        "total_closed_trades": total,
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
        "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
        "max_drawdown_pct": round(max_dd, 2),
        "avg_pnl_usd": round(total_pnl / total, 4) if total > 0 else None,
        "avg_win_usd": round(gross_profit / len(wins), 4) if wins else None,
        "avg_loss_usd": round(-gross_loss / len(losses), 4) if losses else None,
    }


@router.get("/stats")
async def get_stats():
    """
    FR-9: Portfolio summary with equity curve time series.
    """
    async with db.get_db() as conn:
        closed_trades = await db.get_all_closed_trades(conn)
        open_trades = await db.get_open_trades(conn)
        cash = await db.get_cash_balance(conn)

    stats = _compute_stats_dict(closed_trades, cash, len(open_trades))
    equity_curve = _build_equity_curve(closed_trades)

    return {
        **stats,
        "equity_curve": equity_curve,
        "initial_cash_usd": config.INITIAL_CASH_USD,
    }


@router.get("/learning-window")
async def get_learning_window():
    """
    FR-12: Learning window progress — days elapsed vs. target, trades vs. minimum.
    """
    async with db.get_db() as conn:
        closed_trades = await db.get_all_closed_trades(conn)
        first_date = await db.get_first_trade_date(conn)

    days_elapsed: float = 0.0
    if first_date:
        from datetime import datetime, timezone
        try:
            first_dt = datetime.fromisoformat(first_date)
            now_dt = datetime.now(timezone.utc)
            days_elapsed = (now_dt - first_dt).total_seconds() / 86400
        except (ValueError, TypeError):
            days_elapsed = 0.0

    return {
        "days_elapsed": round(days_elapsed, 1),
        "days_target": config.LEARNING_WINDOW_DAYS,
        "trades_closed": len(closed_trades),
        "trades_target": config.PROMOTION_MIN_TRADES,
        "window_started": first_date is not None,
        "window_complete": days_elapsed >= config.LEARNING_WINDOW_DAYS,
    }
