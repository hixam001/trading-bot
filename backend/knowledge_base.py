"""
knowledge_base.py — Knowledge base loader and context builder.

Loads two sources of knowledge:
  1. static_knowledge.md — hand-curated by the operator
  2. knowledge_base/ingested/*.md|txt — operator-supplied external material (FR-23/24)

Both are loaded at module startup (and reloaded on demand after ingestion).
Context injected into LLM prompts is bounded by config.KB_MAX_CONTEXT_CHARS
(performance-discipline rule 8: prompt length matters for LLM latency).

Dynamic stats (win rate by liquidity bucket etc.) are computed from the
trade database at query time — these are a separate function called by
the /api/knowledge-base endpoint, not injected into scoring prompts.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import config
from models import Candidate

log = logging.getLogger(__name__)

# Module-level cache — updated by reload_knowledge()
_static_text: str = ""
_ingested_texts: list[tuple[str, str]] = []  # (filename, content)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def reload_knowledge() -> None:
    """
    Load (or reload) all knowledge files from disk.
    Called at startup and after any new file is ingested (FR-23/24).
    Thread-safe enough for our single-writer model.
    """
    global _static_text, _ingested_texts

    # Static knowledge
    if config.STATIC_KNOWLEDGE_FILE.exists():
        _static_text = config.STATIC_KNOWLEDGE_FILE.read_text(encoding="utf-8")
        log.info("Loaded static knowledge: %d chars", len(_static_text))
    else:
        _static_text = ""
        log.warning("Static knowledge file not found: %s", config.STATIC_KNOWLEDGE_FILE)

    # Ingested material (FR-24: separate from static so provenance is clear)
    _ingested_texts = []
    ingested_dir = config.INGESTED_KNOWLEDGE_DIR
    if ingested_dir.is_dir():
        for fpath in sorted(ingested_dir.glob("*")):
            if fpath.suffix.lower() in (".md", ".txt") and fpath.is_file():
                try:
                    content = fpath.read_text(encoding="utf-8")
                    _ingested_texts.append((fpath.name, content))
                    log.info("Loaded ingested: %s (%d chars)", fpath.name, len(content))
                except OSError as exc:
                    log.error("Failed to read ingested file %s: %s", fpath.name, exc)

    log.info(
        "Knowledge base ready: 1 static + %d ingested files",
        len(_ingested_texts),
    )


def get_context(candidate: Optional[Candidate] = None) -> str:
    """
    Build the knowledge base context string to inject into a scoring prompt.

    Concatenates static + ingested content, then truncates to
    config.KB_MAX_CONTEXT_CHARS (performance-discipline rule 8: longer
    prompts cost real tok/s on a local 8B model).

    The candidate argument is reserved for future similar-trade retrieval
    (e.g. semantic search by liquidity bucket) — currently not used.
    """
    parts: list[str] = []

    if _static_text:
        parts.append("### Curated Knowledge\n" + _static_text.strip())

    for fname, content in _ingested_texts:
        parts.append(f"### Ingested: {fname}\n" + content.strip())

    combined = "\n\n---\n\n".join(parts)

    if len(combined) > config.KB_MAX_CONTEXT_CHARS:
        combined = combined[: config.KB_MAX_CONTEXT_CHARS]
        # Don't cut mid-sentence
        last_newline = combined.rfind("\n")
        if last_newline > config.KB_MAX_CONTEXT_CHARS // 2:
            combined = combined[:last_newline]
        combined += "\n\n[...context truncated to fit prompt budget...]"
        log.debug("KB context truncated to %d chars", len(combined))

    return combined


# ---------------------------------------------------------------------------
# Dynamic stats (used by /api/knowledge-base, NOT injected into prompts)
# ---------------------------------------------------------------------------

def get_filter_threshold_recommendations(closed_trades: list) -> dict:
    """
    Compute win-rate statistics by liquidity bucket and other segments
    from closed trade history.

    Returns a dict suitable for the /api/knowledge-base response.
    This runs in Python — no DB query inside (caller passes the trades).
    """
    if not closed_trades:
        return {
            "total_closed": 0,
            "win_rate_overall": None,
            "win_rate_by_liquidity_bucket": {},
            "win_rate_by_age_bucket": {},
            "note": "No closed trades yet — stats will appear after the first trade closes.",
        }

    total = len(closed_trades)
    wins = sum(1 for t in closed_trades if (t.realized_pnl_usd or 0) > 0)
    win_rate = wins / total if total > 0 else None

    # Liquidity buckets (matches the table in static_knowledge.md)
    buckets_liq: dict[str, dict] = {
        "<25k":        {"trades": 0, "wins": 0},
        "25k–100k":    {"trades": 0, "wins": 0},
        "100k–500k":   {"trades": 0, "wins": 0},
        ">500k":       {"trades": 0, "wins": 0},
    }
    for t in closed_trades:
        liq = t.candidate_snapshot.get("liquidity_usd", 0)
        win = (t.realized_pnl_usd or 0) > 0
        if liq < 25_000:
            b = "<25k"
        elif liq < 100_000:
            b = "25k–100k"
        elif liq < 500_000:
            b = "100k–500k"
        else:
            b = ">500k"
        buckets_liq[b]["trades"] += 1
        if win:
            buckets_liq[b]["wins"] += 1

    win_rate_by_liq = {
        b: {
            "trades": v["trades"],
            "win_rate": round(v["wins"] / v["trades"], 3) if v["trades"] > 0 else None,
        }
        for b, v in buckets_liq.items()
    }

    # Age buckets
    buckets_age: dict[str, dict] = {
        "1–6h":   {"trades": 0, "wins": 0},
        "6–48h":  {"trades": 0, "wins": 0},
        "2–7d":   {"trades": 0, "wins": 0},
    }
    for t in closed_trades:
        age = t.candidate_snapshot.get("age_hours", 0)
        win = (t.realized_pnl_usd or 0) > 0
        if age < 6:
            b = "1–6h"
        elif age < 48:
            b = "6–48h"
        else:
            b = "2–7d"
        buckets_age[b]["trades"] += 1
        if win:
            buckets_age[b]["wins"] += 1

    win_rate_by_age = {
        b: {
            "trades": v["trades"],
            "win_rate": round(v["wins"] / v["trades"], 3) if v["trades"] > 0 else None,
        }
        for b, v in buckets_age.items()
    }

    return {
        "total_closed": total,
        "win_rate_overall": round(win_rate, 3) if win_rate is not None else None,
        "win_rate_by_liquidity_bucket": win_rate_by_liq,
        "win_rate_by_age_bucket": win_rate_by_age,
    }


def ingest_file(filename: str, content: str) -> Path:
    """
    Write content to the ingested/ directory and reload knowledge.
    Returns the path of the saved file.

    Filename is sanitized — only alphanumeric, hyphens, underscores, dots.
    Content that results in an empty file is rejected (fail-closed).
    """
    # Sanitize filename
    safe_chars = set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
    )
    sanitized = "".join(c if c in safe_chars else "_" for c in filename)
    if not sanitized.endswith((".md", ".txt")):
        sanitized += ".md"

    content_stripped = content.strip()
    if not content_stripped:
        raise ValueError("Ingested content is empty after stripping whitespace.")

    dest = config.INGESTED_KNOWLEDGE_DIR / sanitized
    dest.write_text(content_stripped, encoding="utf-8")
    log.info("Ingested new file: %s (%d chars)", dest.name, len(content_stripped))

    reload_knowledge()
    return dest


def list_ingested_files() -> list[dict]:
    """Return metadata about all currently ingested files."""
    result = []
    if config.INGESTED_KNOWLEDGE_DIR.is_dir():
        for fpath in sorted(config.INGESTED_KNOWLEDGE_DIR.glob("*")):
            if fpath.suffix.lower() in (".md", ".txt") and fpath.is_file():
                result.append({
                    "filename": fpath.name,
                    "size_bytes": fpath.stat().st_size,
                    "chars": len(fpath.read_text(encoding="utf-8")),
                })
    return result


# Load on import
reload_knowledge()
