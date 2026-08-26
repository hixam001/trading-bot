"""
knowledge_base/loader.py — static + ingested knowledge, digests, context (F1–F8).

Digest-at-ingest-time: raw files are stored whole; prompts receive only
compact digests, bounded by config.KB_MAX_CONTEXT_CHARS. Truncation drops
WHOLE documents (newest-ingested first), never cuts a document mid-body (F7).

Digest generation uses Ollama in live mode when healthy; otherwise an
extractive fallback (leading sentences, capped) — clearly labeled, still a
faithful subset of the source, never invented text.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import config

log = logging.getLogger(__name__)

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_DIGEST_CHARS = 600


def load_static_knowledge() -> str:
    try:
        return config.STATIC_KNOWLEDGE_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.warning("static knowledge file missing: %s", config.STATIC_KNOWLEDGE_FILE)
        return ""


def sanitize_filename(name: str) -> str:
    """Path-traversal-safe filename (F2)."""
    name = _SAFE_NAME_RE.sub("_", Path(name).name).strip("._") or "untitled"
    return name[-120:]


def _extractive_digest(content: str) -> str:
    """Fallback digest: leading sentences, capped. A subset — never invented."""
    sentences = re.split(r"(?<=[.!?])\s+", content.strip())
    out: list[str] = []
    total = 0
    for s in sentences:
        if total + len(s) > _MAX_DIGEST_CHARS or not s.strip():
            break
        out.append(s.strip())
        total += len(s) + 1
    return "[extractive digest] " + " ".join(out)


async def _llm_digest(content: str) -> str | None:
    """Ollama summarization; returns None when unavailable."""
    if config.DATA_BACKEND != "live":
        return None
    from llm.narrator import Narrator
    n = Narrator()
    try:
        result = await n._deepseek.complete_json(
            task="kb_digest",
            system_prompt="Summarize the following trading notes in at most 5 concise sentences using ONLY the information given. Do not invent advice.",
            user_prompt=content[:6000],
            json_mode=False
        )
        if result and result.text:
            return result.text
        return None
    finally:
        await n.aclose()


async def ingest_file(filename: str, content: str) -> dict:
    """
    Ingest one document (F2/F5): sanitize name, reject empty content,
    generate + persist digest, archive the raw source under ingested/.
    """
    if not content or not content.strip():
        raise ValueError("refusing to ingest empty document")
    safe = sanitize_filename(filename)
    digest = (await _llm_digest(content)) or _extractive_digest(content)

    config.INGESTED_KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    (config.INGESTED_KNOWLEDGE_DIR / safe).write_text(content, encoding="utf-8")

    from api import db
    async with db.get_db() as conn:
        await db.upsert_kb_document(conn, safe, content, digest)
    log.info("ingested %s (%d chars, digest %d chars)", safe, len(content), len(digest))
    return {"filename": safe, "digest": digest}


async def get_context(budget: int | None = None) -> str:
    """
    Prompt context (F6/F7): static knowledge first, then digests. If the
    budget is exceeded, WHOLE documents are dropped (never cut mid-document).
    """
    budget = budget or config.KB_MAX_CONTEXT_CHARS
    static = load_static_knowledge().strip()

    from api import db
    async with db.get_db() as conn:
        docs = await db.get_kb_documents(conn)

    parts: list[str] = []
    used = len(static)
    if static:
        parts.append("## Static knowledge\n" + static)

    for doc in docs:   # oldest first: newest digests are the ones dropped
        block = f"## {doc['filename']}\n{doc['digest']}"
        if used + len(block) + 2 > budget:
            log.info("kb context budget %d reached — dropping whole document %s",
                     budget, doc["filename"])
            continue
        parts.append(block)
        used += len(block) + 2

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Dynamic stats (F8): win rate by bucket, computed from real trade history
# ---------------------------------------------------------------------------

_LIQ_LABELS = {"lo": "small (<30k)", "mid": "medium (30-100k)", "hi": "large (>100k)"}
_AGE_LABELS = {"lo": "fresh (<6h)", "mid": "young (6-24h)", "hi": "mature (>24h)"}


def _bucket(value, edges: tuple[float, float]) -> str | None:
    if value is None:
        return None
    if value < edges[0]:
        return "lo"
    if value > edges[1]:
        return "hi"
    return "mid"


def compute_bucket_stats(closed_trades: list) -> dict:
    liq = {label: [0, 0] for label in _LIQ_LABELS.values()}
    age = {label: [0, 0] for label in _AGE_LABELS.values()}

    for t in closed_trades:
        won = 1 if (t.realized_pnl_usd or 0) > 0 else 0
        snap = t.candidate_snapshot or {}
        b = _bucket(snap.get("liquidity_usd"), (30_000, 100_000))
        if b:
            liq[_LIQ_LABELS[b]][0] += won
            liq[_LIQ_LABELS[b]][1] += 1
        b = _bucket(snap.get("age_hours"), (6.0, 24.0))
        if b:
            age[_AGE_LABELS[b]][0] += won
            age[_AGE_LABELS[b]][1] += 1

    def rates(d: dict) -> dict:
        return {
            k: {"wins": v[0], "trades": v[1],
                "win_rate": round(v[0] / v[1], 3) if v[1] else None}
            for k, v in d.items()
        }

    return {"by_liquidity_bucket": rates(liq), "by_age_bucket": rates(age)}

