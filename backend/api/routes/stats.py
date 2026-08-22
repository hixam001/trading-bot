"""
api/routes/stats.py — GET /api/stats: portfolio summary + equity curve.
"""
from __future__ import annotations

from fastapi import APIRouter

import config
from api import db
from promotion_gate import _max_drawdown_pct, _profit_factor

router = APIRouter()


@router.get("/api/stats")
async def get_stats():
    async with db.get_db() as conn:
        closed = await db.get_all_closed_trades(conn)
        open_trades = await db.get_open_trades(conn)
        cash = await db.get_cash_balance(conn)

    chronological = sorted(
        [t for t in closed if t.closed_at], key=lambda t: t.closed_at
    )
    equity_curve = []
    equity = config.INITIAL_CASH_USD
    for t in chronological:
        equity += t.realized_pnl_usd or 0.0
        equity_curve.append({"closed_at": t.closed_at, "equity_usd": round(equity, 2)})

    n = len(chronological)
    wins = sum(1 for t in chronological if (t.realized_pnl_usd or 0) > 0)
    return {
        "initial_cash_usd": config.INITIAL_CASH_USD,
        "cash_usd": round(cash, 2),
        "equity_usd": round(equity, 2),
        "open_positions": len(open_trades),
        "closed_trades": n,
        "win_rate": round(wins / n, 4) if n else None,
        "profit_factor": (
            round(_profit_factor(chronological), 4)
            if _profit_factor(chronological) is not None else None
        ),
        "max_drawdown_pct": round(_max_drawdown_pct(chronological), 2),
        "total_pnl_usd": round(equity - config.INITIAL_CASH_USD, 4),
        "equity_curve": equity_curve,
        "paper_trading_only": True,
    }
