"""
api/routes/knowledge_base.py — GET /api/knowledge-base + POST ingest (F4/F9).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from api import db
from api.auth import require_admin_token
from knowledge_base import loader

router = APIRouter()


class IngestRequest(BaseModel):
    documents: list[dict]   # [{"filename": str, "content": str}, ...]


@router.get("/api/knowledge-base")
async def get_knowledge_base():
    static = loader.load_static_knowledge()
    async with db.get_db() as conn:
        docs = await db.get_kb_documents(conn)
        closed = await db.get_all_closed_trades(conn)
    return {
        "static_knowledge": static,
        "ingested": [
            {"filename": d["filename"], "digest": d["digest"],
             "ingested_at": d["ingested_at"]}
            for d in docs
        ],
        "dynamic_stats": loader.compute_bucket_stats(closed),
    }


@router.post("/api/knowledge-base/ingest")
async def ingest_documents(req: IngestRequest, request: Request):
    # §38 F3: mutating endpoint — requires the operator token (fail closed).
    require_admin_token(request)
    if not req.documents:
        raise HTTPException(status_code=400, detail="no documents provided")
    results = []
    errors = []
    for doc in req.documents:
        try:
            r = await loader.ingest_file(doc.get("filename", "untitled"),
                                         doc.get("content", ""))
            results.append(r)
        except (ValueError, TypeError) as exc:
            errors.append({"filename": doc.get("filename"), "error": str(exc)})
    return {"ingested": results, "errors": errors}
