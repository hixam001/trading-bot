"""
api/routes/promotion_gate_route.py — GET /api/promotion-gate

Read-only promotion gate status display.
"""
from __future__ import annotations

from fastapi import APIRouter

import promotion_gate
from api import db

router = APIRouter()


@router.get("/promotion-gate")
async def get_promotion_gate():
    """
    FR-11: Current status of every promotion criterion.

    This endpoint is read-only. It calls promotion_gate.evaluate() which
    is a pure function — no writes, no side effects, no flags changed.
    The frontend displays this as a status checklist, not a control panel.
    """
    async with db.get_db() as conn:
        closed_trades = await db.get_all_closed_trades(conn)
        first_date = await db.get_first_trade_date(conn)

    result = promotion_gate.evaluate(closed_trades, first_date)
    return result
