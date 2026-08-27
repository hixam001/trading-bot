"""
api/routes/admin.py — Operator-only book maintenance endpoints.

POST /api/admin/reset
    Modes:
      mode=reset_book  (default) — wipe all operational tables and restore
                                   the starting $1,000 balance. Every trade,
                                   feed event, decision commit, event/memory,
                                   thesis, daily_stat, and LLM call row is
                                   deleted. portfolio_state.cash_usd is
                                   reset to config.INITIAL_CASH_USD.

      mode=wipe_paper            — scoped reset: clear ONLY the paper-display
                                   rows (feed_events + trades) and restore the
                                   starting cash. KEEPS the proof/observability
                                   record (decision_commits, events, memories,
                                   theses, daily_stats, llm_call_usage,
                                   market_regime).

      mode=prune_only            — trim old feed_events and market_regime
                                   rows, keeping only the newest
                                   config.FEED_PRUNE_KEEP and
                                   config.REGIME_PRUNE_KEEP rows
                                   respectively. Trades, cash, and all other
                                   tables are UNTOUCHED.

    Requires: ?confirm=yes query param.
    Returns: JSON summary of rows deleted / tables reset.

SAFETY:
  - Paper-only: no wallet code, no transaction construction, no call to
    live_execution. This touches only the local SQLite (or Supabase) book.
  - Explicit confirmation required — calling without ?confirm=yes returns 400.
  - Loudly logged at WARNING level.
  - The endpoint is intentionally NOT listed in the public API description
    (include_in_schema=False) to avoid accidental discovery.

Never call this from the tick loop, the LLM, or the frontend.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import config
from api import db
from fastapi import APIRouter, HTTPException, Query

router = APIRouter()
log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.post("/api/admin/reset", include_in_schema=False)
async def admin_reset(
    confirm: str = Query(default="", description="Must be 'yes' to proceed."),
    mode: str = Query(
        default="reset_book",
        description=(
            "'reset_book' (full wipe), 'wipe_paper' (feed+trades only), or "
            "'prune_only' (trim old rows)."
        ),
    ),
):
    """Operator-only book maintenance.

    ?confirm=yes is required. Without it the endpoint is a no-op (400).
    mode=reset_book (default) wipes everything and restores $1,000 cash.
    mode=wipe_paper clears only feed_events + trades and restores cash.
    mode=prune_only trims feed_events and market_regime to configured limits.
    """
    if confirm.strip().lower() != "yes":
        raise HTTPException(
            status_code=400,
            detail=(
                "Confirmation required. Add ?confirm=yes to proceed. "
                "This action cannot be undone."
            ),
        )

    if mode not in ("reset_book", "wipe_paper", "prune_only"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown mode '{mode}'. Use 'reset_book', 'wipe_paper', or "
                "'prune_only'."
            ),
        )

    started_at = _now_iso()
    log.warning(
        "ADMIN RESET requested | mode=%s | confirm=yes | started_at=%s",
        mode, started_at,
    )

    async with db.get_db() as conn:
        if mode == "reset_book":
            result = await db.reset_book(conn, config.INITIAL_CASH_USD)
            log.warning(
                "ADMIN RESET complete | mode=reset_book | deleted=%d rows total",
                result["total_deleted"],
            )
        elif mode == "wipe_paper":
            result = await db.wipe_paper_book(conn, config.INITIAL_CASH_USD)
            log.warning(
                "ADMIN WIPE_PAPER complete | feed_events+trades cleared, cash "
                "reset | deleted=%d rows total",
                result["total_deleted"],
            )
        else:  # prune_only
            feed_deleted = await db.prune_feed_events(
                conn, config.FEED_PRUNE_KEEP
            )
            regime_deleted = await db.prune_market_regime(
                conn, config.REGIME_PRUNE_KEEP
            )
            result = {
                "reset": False,
                "prune_only": True,
                "feed_events_deleted": feed_deleted,
                "market_regime_deleted": regime_deleted,
                "feed_events_kept": config.FEED_PRUNE_KEEP,
                "market_regime_kept": config.REGIME_PRUNE_KEEP,
            }
            log.warning(
                "ADMIN PRUNE complete | feed_deleted=%d | regime_deleted=%d",
                feed_deleted, regime_deleted,
            )

    return {
        "started_at": started_at,
        "completed_at": _now_iso(),
        "mode": mode,
        "paper_trading_only": config.PAPER_TRADING_ONLY,
        **result,
    }
