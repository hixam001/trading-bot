"""
api/routes/knowledge_base.py — GET /api/knowledge-base + POST ingest (F4/F9).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

import config
from api import db
from api.auth import require_admin_token
from knowledge_base import loader

router = APIRouter()


class IngestDocumentItem(BaseModel):
    """Specific allowed fields for document ingestion.

    Mass assignment defense (Rule 14): any unexpected fields (role, is_admin,
    user_id, account_status, permissions, etc.) are stripped and ignored.
    """
    filename: str = Field(..., min_length=1, max_length=255, description="Document filename")
    content: str = Field(..., min_length=1, max_length=500_000, description="Document body content")

    model_config = ConfigDict(extra="ignore")

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, v: str) -> str:
        if "\x00" in v:
            raise ValueError("Null bytes are not allowed in filename")
        v = v.strip()
        if not v:
            raise ValueError("Filename cannot be empty")
        ext = Path(v).suffix.lower()
        if not ext or ext not in loader.ALLOWED_EXTENSIONS:
            raise ValueError(
                f"File extension '{ext}' is not permitted. Allowed extensions: "
                f"{', '.join(sorted(loader.ALLOWED_EXTENSIONS))}"
            )
        return v

    @field_validator("content")
    @classmethod
    def validate_content(cls, v: str) -> str:
        # 1. Executable binary magic bytes inspection
        raw_prefix_utf8 = v.encode("utf-8", errors="ignore")[:16]
        raw_prefix_latin = v.encode("latin-1", errors="ignore")[:16]
        for magic in loader.DISALLOWED_BINARY_MAGIC:
            if raw_prefix_utf8.startswith(magic) or raw_prefix_latin.startswith(magic):
                raise ValueError("Binary executable files are not permitted")

        # 2. Executable script shebang inspection
        if loader._SHEBANG_RE.search(v[:200]):
            raise ValueError("Executable scripts are not permitted")

        # 3. Null byte inspection
        if "\x00" in v:
            raise ValueError("Null bytes are not allowed in content")

        # 4. Length / size bounds inspection
        if len(v) > config.MAX_INGEST_CHARS:
            raise ValueError(
                f"Document content exceeds maximum allowed size of {config.MAX_INGEST_CHARS} characters"
            )
        return v


class IngestRequest(BaseModel):
    documents: list[IngestDocumentItem] = Field(
        default_factory=list,
        max_length=50,
        description="Documents to ingest (up to 50 items)",
    )

    model_config = ConfigDict(extra="ignore")


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
            r = await loader.ingest_file(doc.filename, doc.content)
            results.append(r)
        except (ValueError, TypeError) as exc:
            errors.append({"filename": doc.filename, "error": str(exc)})
    return {"ingested": results, "errors": errors}
