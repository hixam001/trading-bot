"""
promotion_gate.py — READ-ONLY go/no-go checklist for live-trading readiness
(G4–G6).

CRITICAL INVARIANTS (treat as a security boundary, defense-first rule 5):
  1. This module is ENTIRELY READ-ONLY: it never writes to the database,
     never modifies config, never triggers anything.
  2. evaluate() returns a status report. It never "promotes", "activates",
     or enables anything.
  3. Meeting every criterion still requires a separate, manual, human-
     reviewed decision made OUTSIDE this system.

No future change to this file should ever add a "promote" or "activate"
function. If you are about to add one, stop.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import config
from models import Trade

log = logging.getLogger(__name__)


def _profit_factor(closed_trades: list[Trade]) -> Optional[float]:
    gross_profit = sum(t.realized_pnl_usd or 0 for t in closed_trades
                       if (t.realized_pnl_usd or 0) > 0)
    gross_loss = abs(sum(t.realized_pnl_usd or 0 for t in closed_trades
                         if (t.realized_pnl_usd or 0) < 0))
    if gross_loss == 0:
        return None   # undefined (no losses yet is not automatically good)
    return gross_profit / gross_loss


def _max_drawdown_pct(closed_trades: list[Trade]) -> float:
    """Peak-to-trough drawdown over the equity curve of realized P&L."""
    equity = config.INITIAL_CASH_USD
    peak = equity
    max_dd = 0.0
    for t in sorted(closed_trades, key=lambda x: x.closed_at or ""):
        equity += t.realized_pnl_usd or 0.0
        peak = max(peak, equity)
        if peak > 0:
            dd = ((peak - equity) / peak) * 100.0
            max_dd = max(max_dd, dd)
    return max_dd


def _days_elapsed(first_trade_date: Optional[str]) -> Optional[float]:
    if not first_trade_date:
        return None
    try:
        first_dt = datetime.fromisoformat(first_trade_date)
        return (datetime.now(timezone.utc) - first_dt).total_seconds() / 86400.0
    except (ValueError, TypeError) as exc:
        log.error("could not parse first_trade_date %r: %s", first_trade_date, exc)
        return None


def evaluate(closed_trades: list[Trade], first_trade_date: Optional[str]) -> dict:
    """
    Evaluate the five promotion criteria against current history.
    PURE FUNCTION on its inputs; performs no I/O and no writes.
    """
    trade_count = len(closed_trades)
    wins = sum(1 for t in closed_trades if (t.realized_pnl_usd or 0) > 0)
    win_rate = wins / trade_count if trade_count else None
    profit_factor = _profit_factor(closed_trades)
    max_dd = _max_drawdown_pct(closed_trades)
    days = _days_elapsed(first_trade_date)

    criteria = [
        {
            "name": "Minimum trade count",
            "passed": trade_count >= config.PROMOTION_MIN_TRADES,
            "actual": trade_count,
            "required": config.PROMOTION_MIN_TRADES,
            "detail": f"{trade_count} / {config.PROMOTION_MIN_TRADES} closed trades",
        },
        {
            "name": "Learning window elapsed",
            "passed": days is not None and days >= config.LEARNING_WINDOW_DAYS,
            "actual": round(days, 2) if days is not None else None,
            "required": config.LEARNING_WINDOW_DAYS,
            "detail": (
                f"{days:.1f} / {config.LEARNING_WINDOW_DAYS} days elapsed"
                if days is not None else "No trades yet — window not started"
            ),
        },
        {
            "name": "Minimum win rate",
            "passed": win_rate is not None and win_rate >= config.PROMOTION_MIN_WIN_RATE,
            "actual": round(win_rate, 4) if win_rate is not None else None,
            "required": config.PROMOTION_MIN_WIN_RATE,
            "detail": (
                f"{win_rate:.1%} win rate ({wins}/{trade_count})"
                if win_rate is not None else "No closed trades"
            ),
        },
        {
            "name": "Minimum profit factor",
            "passed": profit_factor is not None
                      and profit_factor >= config.PROMOTION_MIN_PROFIT_FACTOR,
            "actual": round(profit_factor, 4) if profit_factor is not None else None,
            "required": config.PROMOTION_MIN_PROFIT_FACTOR,
            "detail": (
                f"Profit factor {profit_factor:.2f}" if profit_factor is not None
                else "Undefined (no losing trades yet)"
            ),
        },
        {
            "name": "Maximum drawdown",
            "passed": max_dd <= config.PROMOTION_MAX_DRAWDOWN_PCT,
            "actual": round(max_dd, 2),
            "required": config.PROMOTION_MAX_DRAWDOWN_PCT,
            "detail": f"Max drawdown {max_dd:.1f}% vs limit "
                      f"{config.PROMOTION_MAX_DRAWDOWN_PCT}%",
        },
    ]

    all_met = all(c["passed"] for c in criteria)
    failed_names = [c["name"] for c in criteria if not c["passed"]]
    summary = (
        ("All promotion criteria currently met. This does NOT enable live "
         "trading — a human review is required.")
        if all_met else
        f"Not yet eligible. Failing criteria: {', '.join(failed_names)}."
    )
    return {
        "all_criteria_met": all_met,
        "criteria": criteria,
        "summary": summary,
        "note": (
            "Meeting all criteria does not trigger anything automatically. "
            "Transitioning to live trading — if ever — is a separate, manual, "
            "human-reviewed process outside the scope of this system."
        ),
    }
