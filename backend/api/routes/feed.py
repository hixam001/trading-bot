"""
api/routes/feed.py — GET /api/feed

Returns paginated feed events (both pass and fail decisions), newest first.
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from api import db

router = APIRouter()


@router.get("/feed")
async def get_feed(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """
    Paginated list of all feed events (pass and fail), newest first.

    FR-6: Each event includes timestamp, symbol, candidate stats snapshot,
    verdict, confidence, risk flags, thesis, entry/invalidation conditions.
    """
    async with db.get_db() as conn:
        events = await db.get_feed_events(conn, limit=limit, offset=offset)
    return {
        "events": [e.to_dict() for e in events],
        "limit": limit,
        "offset": offset,
        "count": len(events),
    }
