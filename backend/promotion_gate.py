"""
promotion_gate.py — Read-only go/no-go checklist for live-trading readiness.

CRITICAL INVARIANTS (treat these as a security boundary, defense-first rule 5):
  1. This module is ENTIRELY READ-ONLY. It never writes to the database,
     never modifies config, never changes any flag.
  2. evaluate() returns a status report. It never "triggers" anything.
  3. The frontend's promotion gate panel is a STATUS DISPLAY, not a button.
  4. This module can confirm readiness, but it can NEVER initiate live trading.
     That decision is a human's, made via a separate, manual process.
  5. No future change to this file should ever add a "promote" or "activate"
     function. If you see such a function being added, stop — it is out of scope.

The 5 promotion criteria (from config):
  1. Minimum trade count: >= PROMOTION_MIN_TRADES
  2. Learning window elapsed: >= LEARNING_WINDOW_DAYS calendar days
  3. Minimum win rate: >= PROMOTION_MIN_WIN_RATE
  4. Minimum profit factor: >= PROMOTION_MIN_PROFIT_FACTOR
  5. Maximum drawdown: <= PROMOTION_MAX_DRAWDOWN_PCT

All thresholds are from config — hardcoded in this file only for documentation.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import config
from models import Trade

log = logging.getLogger(__name__)


def _compute_profit_factor(closed_trades: list[Trade]) -> Optional[float]:
    """Gross profit / gross loss. Returns None if no losses (undefined)."""
    gross_profit = sum(t.realized_pnl_usd for t in closed_trades
                       if (t.realized_pnl_usd or 0) > 0)
    gross_loss = abs(sum(t.realized_pnl_usd for t in closed_trades
                        if (t.realized_pnl_usd or 0) < 0))
    if gross_loss == 0:
        return None  # undefined (could mean no losses yet, not necessarily good)
    return gross_profit / gross_loss


def _compute_max_drawdown_pct(closed_trades: list[Trade]) -> float:
    """Peak-to-trough drawdown as a percentage of peak equity."""
    equity = config.INITIAL_CASH_USD
    peak = equity
    max_dd = 0.0
    for t in closed_trades:
        equity += t.realized_pnl_usd or 0.0
        if equity > peak:
            peak = equity
        if peak > 0:
            dd = ((peak - equity) / peak) * 100.0
            if dd > max_dd:
                max_dd = dd
    return max_dd


def _compute_days_elapsed(first_trade_date: Optional[str]) -> Optional[float]:
    """Return calendar days since first trade, or None if no trades yet."""
    if first_trade_date is None:
        return None
    try:
        first_dt = datetime.fromisoformat(first_trade_date)
        now_dt = datetime.now(timezone.utc)
        return (now_dt - first_dt).total_seconds() / 86400
    except (ValueError, TypeError) as exc:
        log.error("Could not parse first_trade_date %r: %s", first_trade_date, exc)
        return None


# ---------------------------------------------------------------------------
# Public API — READ ONLY
# ---------------------------------------------------------------------------

def evaluate(
    closed_trades: list[Trade],
    first_trade_date: Optional[str],
) -> dict:
    """
    Evaluate all promotion criteria against the current trade history.

    Args:
        closed_trades: All closed trades (from db.get_all_closed_trades).
        first_trade_date: ISO-8601 string of the earliest trade opening time.

    Returns:
        {
          "all_criteria_met": bool,
          "criteria": [
            {
              "name": str,
              "passed": bool,
              "actual": str | float | int | None,
              "required": str | float | int,
              "detail": str,
            },
            ...
          ],
          "summary": str,
          "note": str  -- always present, always says live trading requires human action
        }

    This function is PURE — no database access, no side effects.
    Accepts pre-fetched data so it can be tested without a running DB.
    """
    trade_count = len(closed_trades)
    win_count = sum(1 for t in closed_trades if (t.realized_pnl_usd or 0) > 0)
    win_rate = win_count / trade_count if trade_count > 0 else 0.0
    profit_factor = _compute_profit_factor(closed_trades)
    max_drawdown = _compute_max_drawdown_pct(closed_trades)
    days_elapsed = _compute_days_elapsed(first_trade_date)

    criteria = [
        # 1. Minimum trade count
        {
            "name": "Minimum trade count",
            "passed": trade_count >= config.PROMOTION_MIN_TRADES,
            "actual": trade_count,
            "required": config.PROMOTION_MIN_TRADES,
            "detail": (
                f"{trade_count} closed trades "
                f"({'✓' if trade_count >= config.PROMOTION_MIN_TRADES else f'need {config.PROMOTION_MIN_TRADES - trade_count} more'})"
            ),
        },
        # 2. Learning window elapsed
        {
            "name": "Learning window elapsed",
            "passed": days_elapsed is not None and days_elapsed >= config.LEARNING_WINDOW_DAYS,
            "actual": round(days_elapsed, 1) if days_elapsed is not None else None,
            "required": config.LEARNING_WINDOW_DAYS,
            "detail": (
                f"{days_elapsed:.1f} / {config.LEARNING_WINDOW_DAYS} days elapsed"
                if days_elapsed is not None
                else "No trades yet — window not started"
            ),
        },
        # 3. Minimum win rate
        {
            "name": "Minimum win rate",
            "passed": trade_count > 0 and win_rate >= config.PROMOTION_MIN_WIN_RATE,
            "actual": round(win_rate, 4) if trade_count > 0 else None,
            "required": config.PROMOTION_MIN_WIN_RATE,
            "detail": (
                f"{win_rate:.1%} win rate ({win_count}/{trade_count} winning trades)"
                if trade_count > 0
                else "No closed trades"
            ),
        },
        # 4. Minimum profit factor
        {
            "name": "Minimum profit factor",
            "passed": profit_factor is not None and profit_factor >= config.PROMOTION_MIN_PROFIT_FACTOR,
            "actual": round(profit_factor, 4) if profit_factor is not None else None,
            "required": config.PROMOTION_MIN_PROFIT_FACTOR,
            "detail": (
                f"Profit factor: {profit_factor:.2f}"
                if profit_factor is not None
                else "Undefined (no losing trades yet — collect more data)"
            ),
        },
        # 5. Maximum drawdown
        {
            "name": "Maximum drawdown",
            "passed": max_drawdown <= config.PROMOTION_MAX_DRAWDOWN_PCT,
            "actual": round(max_drawdown, 2),
            "required": config.PROMOTION_MAX_DRAWDOWN_PCT,
            "detail": (
                f"Max drawdown: {max_drawdown:.1f}% "
                f"({'✓' if max_drawdown <= config.PROMOTION_MAX_DRAWDOWN_PCT else f'exceeds {config.PROMOTION_MAX_DRAWDOWN_PCT}% limit'})"
            ),
        },
    ]

    all_met = all(c["passed"] for c in criteria)

    if all_met:
        summary = (
            "All promotion criteria are currently met. "
            "This does NOT automatically enable live trading — a human review is required."
        )
    else:
        failed_names = [c["name"] for c in criteria if not c["passed"]]
        summary = f"Not yet eligible. Failing criteria: {', '.join(failed_names)}."

    return {
        "all_criteria_met": all_met,
        "criteria": criteria,
        "summary": summary,
        "note": (
            "Meeting all criteria does not trigger anything automatically. "
            "Transitioning to live trading — if ever — is a separate, manual, "
            "human-reviewed process that is outside the scope of this system."
        ),
    }


if __name__ == "__main__":
    """Direct invocation: print current promotion gate status."""
    import asyncio
    import sys
    from api import db

    async def _main() -> None:
        await db.init_db()
        async with db.get_db() as conn:
            closed = await db.get_all_closed_trades(conn)
            first_date = await db.get_first_trade_date(conn)
        result = evaluate(closed, first_date)
        print(f"\n{'='*60}")
        print("PROMOTION GATE STATUS")
        print(f"{'='*60}")
        for c in result["criteria"]:
            status = "✓ PASS" if c["passed"] else "✗ FAIL"
            print(f"  [{status}] {c['name']}: {c['detail']}")
        print(f"\n{result['summary']}")
        print(f"\nNOTE: {result['note']}")
        print(f"{'='*60}\n")

    asyncio.run(_main())
