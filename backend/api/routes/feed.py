"""
api/routes/feed.py — GET /api/feed (paginated decision feed) and
GET /api/mint/{mint}/history (§65 mint-history drill-down).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

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


@router.get("/api/mint/{mint}/history")
async def get_mint_history(
    mint: str,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """§65 drill-down: EVERY feed event ever recorded for one mint, newest
    first — not just the window the live tape holds. Read-only, reuses the
    exact feed row shape so the frontend renders it with the same row
    components. An unknown mint is an EMPTY result (total 0), never an error;
    a malformed mint is a 422 (the SEC-07 base58 validator, same one
    discovery uses)."""
    # Function-local import (sanctioned pattern): backend must not hard-depend
    # on the provider layer for a read-only route.
    from data_providers.discovery import is_valid_solana_address

    if not is_valid_solana_address(mint):
        raise HTTPException(status_code=422, detail="not a valid Solana mint address")
    async with db.get_db() as conn:
        events = await db.get_feed_events_for_mint(conn, mint, limit=limit, offset=offset)
        total = await db.count_feed_events_for_mint(conn, mint)
    return {"mint": mint, "total": total, "limit": limit, "offset": offset, "events": events}


@router.get("/api/events.json")
async def get_events(limit: int = Query(100, ge=1, le=500)):
    async with db.get_db() as conn:
        events = await db.get_recent_events(conn, limit=limit)
    return {"events": events}
