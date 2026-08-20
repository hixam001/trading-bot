"""
api/routes/knowledge_base_route.py — GET /api/knowledge-base, POST /api/ingest
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException

import knowledge_base as kb
from api import db

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/knowledge-base")
async def get_knowledge_base():
    """
    FR-10: Static knowledge markdown content + dynamic trade history stats.
    """
    async with db.get_db() as conn:
        closed_trades = await db.get_all_closed_trades(conn)

    dynamic_stats = kb.get_filter_threshold_recommendations(closed_trades)
    ingested_files = kb.list_ingested_files()

    # Read static content directly for display (not length-limited here)
    import config
    static_content = ""
    if config.STATIC_KNOWLEDGE_FILE.exists():
        static_content = config.STATIC_KNOWLEDGE_FILE.read_text(encoding="utf-8")

    return {
        "static_knowledge": static_content,
        "ingested_files": ingested_files,
        "dynamic_stats": dynamic_stats,
    }



@router.post("/ingest")
async def ingest_knowledge(body: dict = Body(...)):
    """
    FR-23/24: Ingest operator-supplied material into knowledge_base/ingested/.

    Body: { "filename": str, "content": str }
    Accepts markdown or plain text content. Sanitizes the filename.
    Reloads the knowledge base after saving so the next tick already
    benefits from the new material.
    """
    filename = body.get("filename", "")
    content = body.get("content", "")

    if not isinstance(filename, str) or not filename.strip():
        raise HTTPException(status_code=400, detail="'filename' must be a non-empty string")
    if len(filename) > 100:
        raise HTTPException(status_code=400, detail="'filename' must be <= 100 characters")
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(status_code=400, detail="'content' must be a non-empty string")
    if len(content) > 500_000:
        raise HTTPException(status_code=400, detail="'content' must be <= 500,000 characters")

    try:
        saved_path = kb.ingest_file(filename, content)
        return {
            "success": True,
            "saved_as": saved_path.name,
            "chars": len(content.strip()),
            "ingested_files": kb.list_ingested_files(),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except OSError as exc:
        log.error("Failed to save ingested file: %s", exc)
        raise HTTPException(status_code=500, detail=f"File write failed: {exc}")

