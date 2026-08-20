"""
api/routes/journal.py — GET /api/journal

Paginated closed trade history with full lifecycle info.
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from api import db

router = APIRouter()


@router.get("/journal")
async def get_journal(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    sort: str = Query(default="date", pattern="^(date|pnl)$"),
):
    """
    FR-8: Paginated closed trades, sortable by date or P&L.

    Each entry includes full lifecycle: entry, exit, thesis, exit reason,
    realized P&L, and the LLM's post-trade reflection (FR-17/FR-26).
    """
    async with db.get_db() as conn:
        trades = await db.get_closed_trades(conn, limit=limit, offset=offset, sort_by=sort)

    return {
        "trades": [
            {
                "trade_id": t.trade_id,
                "symbol": t.symbol,
                "mint_address": t.mint_address,
                "opened_at": t.opened_at,
                "closed_at": t.closed_at,
                "entry_price_usd": t.entry_price_usd,
                "exit_price_usd": t.exit_price_usd,
                "position_size_usd": t.position_size_usd,
                "realized_pnl_usd": t.realized_pnl_usd,
                "realized_pnl_pct": t.realized_pnl_pct,
                "exit_reason": t.exit_reason,
                "thesis": t.verdict_snapshot.get("thesis", ""),
                "entry_condition": t.verdict_snapshot.get("entry_condition", ""),
                "invalidation_condition": t.invalidation_condition,
                "confidence": t.verdict_snapshot.get("confidence"),
                "risk_flags": t.verdict_snapshot.get("risk_flags", []),
                "reflection_text": t.reflection_text,
                "candidate_snapshot": t.candidate_snapshot,
            }
            for t in trades
        ],
        "limit": limit,
        "offset": offset,
        "sort": sort,
        "count": len(trades),
    }
