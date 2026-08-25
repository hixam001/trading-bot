"""
api/routes/stats.py — GET /api/stats: portfolio summary numbers.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

import config
from api import db
from paper_trading_engine import compute_unrealized_pnl
from promotion_gate import _max_drawdown_pct, _profit_factor

router = APIRouter()


@router.get("/api/stats")
async def get_stats(request: Request):
    provider = request.app.state.provider
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

    # Realized P&L: everything actually booked on closed trades.
    realized_pnl_usd = sum(t.realized_pnl_usd or 0.0 for t in chronological)

    # Unrealized P&L: live marks net of simulated exit costs, same math as
    # /api/holdings. Positions whose price is unavailable contribute nothing
    # (never fabricated as zero); with no marks at all the field stays None.
    unrealized_marks: list[float] = []
    total_spend_usd = 0.0
    for t in open_trades:
        # Entry cost incl. simulated fees/slippage — what was deducted from
        # cash when the position opened (compute_position_size cost basis).
        total_spend_usd += t.position_size_usd * (1.0 + config.FEE_PCT) * (
            1.0 + config.SLIPPAGE_PCT
        )
        try:
            decimals = (t.candidate_snapshot or {}).get("decimals")
            price = await provider.get_current_price(t.mint_address, decimals)
            pnl_usd, _pnl_pct = compute_unrealized_pnl(t, price)
            unrealized_marks.append(pnl_usd)
        except Exception:
            # Price unavailable / invalid inputs -> skip this mark (fail-soft).
            continue

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
        "realized_pnl_usd": round(realized_pnl_usd, 2),
        "unrealized_pnl_usd": (
            round(sum(unrealized_marks), 4) if unrealized_marks else None
        ),
        "total_spend_usd": round(total_spend_usd, 2),
        "equity_curve": equity_curve,
        "paper_trading_only": True,
    }

