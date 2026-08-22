"""
api/routes/journal.py — GET /api/journal: closed trades with full lifecycle.
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from api import db

router = APIRouter()


@router.get("/api/journal")
async def get_journal(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    async with db.get_db() as conn:
        trades = await db.get_closed_trades_paginated(conn, limit=limit, offset=offset)
        cursor = await conn.execute("SELECT COUNT(*) FROM trades WHERE is_open = 0")
        total = int((await cursor.fetchone())[0])
    return {"total": total, "limit": limit, "offset": offset, "trades": trades}
