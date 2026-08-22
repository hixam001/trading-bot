"""
api/routes/market_regime.py — GET /api/market-regime: recent regime history.
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from api import db

router = APIRouter()


@router.get("/api/market-regime")
async def get_market_regime(limit: int = Query(100, ge=1, le=1000)):
    async with db.get_db() as conn:
        regimes = await db.get_recent_regimes(conn, limit=limit)
    return {"regimes": regimes, "count": len(regimes)}
