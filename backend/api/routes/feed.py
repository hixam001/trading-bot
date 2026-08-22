"""
api/routes/feed.py — GET /api/feed (paginated decision feed).
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from api import db

router = APIRouter()


@router.get("/api/feed")
async def get_feed(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    async with db.get_db() as conn:
        events = await db.get_feed_events(conn, limit=limit, offset=offset)
        total = await db.count_feed_events(conn)
    return {"total": total, "limit": limit, "offset": offset, "events": events}
