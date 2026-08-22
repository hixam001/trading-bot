"""
api/routes/promotion_gate.py — GET /api/promotion-gate.

Mirrors promotion_gate.evaluate() exactly and is equally READ-ONLY: this
endpoint reports status; it can never enable, trigger, or promote anything.
"""
from __future__ import annotations

from fastapi import APIRouter

from api import db
from promotion_gate import evaluate

router = APIRouter()


@router.get("/api/promotion-gate")
async def get_promotion_gate():
    async with db.get_db() as conn:
        closed = await db.get_all_closed_trades(conn)
        first_date = await db.get_first_trade_date(conn)
    return evaluate(closed, first_date)
