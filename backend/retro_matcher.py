"""
retro_matcher.py — REF-R7 retro audit-log signature matching.

reference: linkAuditToFills() in src/lib/audit.server.ts.

PURPOSE: attribute fills (opened trades) to decision rows when a fill
BYPASSES the pipeline (e.g. a hand-placed trade against the live wallet
once armed). The exact bind-at-execute (CommitLog.bind) stays the PRIMARY
binding and is NEVER overwritten by this layer — retro matching only ever
touches rows whose signature is still null.

ALGORITHM (the reference's, kept intact):
  1. pending = decision rows with entry_allowed=1 AND signature IS NULL
     (newest 60)
  2. candidates = opened trades (newest 120) not already bound
  3. match on: same symbol (case-insensitive, $ stripped) + same side
     (always 'buy' for now) + fill_at >= decision_at + within 12h window
  4. earliest unmatched fill wins; a `taken` signatures set grows during
     the run so nothing is claimed twice
  5. write back signature=trade_id, phase='filled', matched_by='retro'

SAFEGUARDS (beyond the reference):
  - Exact-bind rows (signature IS NOT NULL) are never overwritten; the
    WHERE clause in bind_commit_signature enforces this atomically.
  - Matched rows carry matched_by='retro' so exact vs retro bindings are
    distinguishable in /api/proof.json.
  - Unattributed fills are surfaced explicitly, never silently dropped.
  - Returns a result dict for logging; never raises (fail-soft).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from api import db

log = logging.getLogger(__name__)


_RETRO_WINDOW_HOURS = 12


def _strip_symbol(s: str) -> str:
    """Normalize symbol for case-insensitive, $-stripped comparison."""
    return s.strip().lstrip("$").upper()


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


async def run_retro_match(conn: Any) -> dict:
    """
    Run one retro-attribution pass. `conn` is a live db connection
    (SQLite or asyncpg — same interface via db layer).

    Returns:
      {matched: int, unmatched_decisions: [...], unmatched_fills: [...]}
    """
    try:
        pending = await db.get_pending_unsigned_commits(conn, limit=60)
        fills = await db.get_recent_fills_for_retro(conn, limit=120)
    except Exception as exc:
        log.warning("retro_match: db read failed (%s) — skipping", exc)
        return {"matched": 0, "unmatched_decisions": [], "unmatched_fills": [],
                "error": str(exc)}

    taken: set[str] = set()   # fill trade_ids already claimed this pass
    matched = 0

    for decision in pending:
        d_sym = _strip_symbol(decision.get("symbol", ""))
        d_at = _parse_iso(decision.get("created_at"))
        if not d_at:
            continue

        window_end = d_at + timedelta(hours=_RETRO_WINDOW_HOURS)
        best_fill = None
        best_fill_at: datetime | None = None

        for fill in fills:
            if fill["trade_id"] in taken:
                continue
            f_sym = _strip_symbol(fill.get("symbol", ""))
            if f_sym != d_sym:
                continue
            # side check: both are 'buy' in current paper mode
            if fill.get("side", "buy") != "buy":
                continue
            f_at = _parse_iso(fill.get("opened_at"))
            if not f_at:
                continue
            if f_at < d_at:
                continue  # fill must be at or after decision
            if f_at > window_end:
                continue  # outside 12h window
            # Earliest unmatched fill wins
            if best_fill is None or f_at < best_fill_at:
                best_fill = fill
                best_fill_at = f_at

        if best_fill is not None:
            try:
                rows = await db.bind_commit_signature(
                    conn,
                    commit_id=decision["id"],
                    signature=best_fill["trade_id"],
                    phase="filled",
                    matched_by="retro",
                )
            except Exception as exc:
                log.warning("retro_match: bind failed for commit %s: %s",
                            decision["id"], exc)
                rows = 0
            if rows:
                taken.add(best_fill["trade_id"])
                matched += 1
                log.info(
                    "retro_match: commit %d (%s) ← fill %s (retro)",
                    decision["id"], decision.get("symbol"),
                    best_fill["trade_id"][:12],
                )

    # Surface unattributed fills — fills not claimed by any commit
    claimed_trade_ids = {f["trade_id"] for f in fills if f["trade_id"] in taken}
    unmatched_fills = [
        f for f in fills
        if f["trade_id"] not in claimed_trade_ids
    ]
    unmatched_decisions = [
        d for d in pending
        if not any(True for _ in [])  # refreshed in next pass
    ]

    if matched:
        log.info("retro_match: %d fill(s) attributed to decisions", matched)

    return {
        "matched": matched,
        "unmatched_decisions_count": len(pending) - matched,
        "unmatched_fills": [
            {"trade_id": f["trade_id"], "symbol": f["symbol"],
             "opened_at": f["opened_at"]}
            for f in unmatched_fills
        ],
    }
