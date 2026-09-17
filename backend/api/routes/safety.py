"""
api/routes/safety.py — GET /api/safety: the §64 safety-state surface.

§2.2 of the 2026-09-16 audit: the most consequential safety mechanisms in a
system trading real money were invisible in the UI. This endpoint is the
single read-only source for the SystemStatus safety section AND the
persistent alert strip — both render exactly what this reports, never
client-side re-derivation.

Surfaces (all display-only; NO toggles — state writes keep their explicit
gated paths):
  kill_switch          engaged/clear + reason (same fail-closed file read as
                       /api/disclosure.json; a corrupt file reads as ENGAGED)
  daily_loss_breaker   DAILY_LOSS_BREAKER_USD limit + today's realized P&L
                       from the execution ledger (the breaker trips the SAME
                       kill-switch file; its AUTO: reason marks the breach)
  blocklist            block count (manual/auto split) + most recent addition
  break                operator/engine break state (fail-closed like the gate)
  alerts               ONLY conditions that genuinely demand attention:
                       breaker/kill engaged · LLM provider down · crowd
                       scrape chain exhausted · no fills in N hours while
                       candidates are flowing. Alert-fatigue rule: an empty
                       list is the feature working, so the strip renders
                       nothing. The break state is deliberately NOT an alert
                       (it is routine, self-expiring, and shown in the panel).

Gate: LIVE_BOOK_PUBLIC or require_local_or_admin — the breaker's realized
P&L is live-book-class money data (same posture as /api/live/portfolio),
even though the kill-switch/break booleans are already public via
/api/disclosure.json. All live_execution access is function-local optional
import (the sanctioned pattern); failures degrade, never 500, never invent.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Request

import config
from api.auth import require_local_or_admin
from api.routes.disclosure import _break_state, _kill_switch_state

log = logging.getLogger(__name__)
router = APIRouter()

# Alert thresholds (§64): symptom-level, env-overridable like the other
# observability knobs (WALLET_SCAN_TTL_SECONDS et al) — these are NOT
# trading-risk numbers, so the no-env-bypass rule for risk constants holds.
ALERT_NO_FILLS_HOURS = float(getattr(config, "ALERT_NO_FILLS_HOURS", 6.0))
ALERT_NO_FILLS_MIN_CANDIDATES = int(
    getattr(config, "ALERT_NO_FILLS_MIN_CANDIDATES", 10))


def _row_epoch(ts) -> float:
    """Tolerant ISO-8601 -> epoch (DB timestamps are heterogeneous strings).
    Unparseable timestamps count as OLD — never fabricate recency."""
    if not ts:
        return 0.0
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _breaker_limit() -> Optional[float]:
    """DAILY_LOSS_BREAKER_USD as a positive USD figure — static config truth,
    so it stays reportable even when the ledger itself is unreadable."""
    try:
        from live_execution import config as _le_config
        return float(abs(_le_config.DAILY_LOSS_BREAKER_USD))
    except Exception:
        log.warning("safety: breaker limit unreadable", exc_info=True)
        return None


def _daily_realized_pnl() -> Optional[float]:
    """Today's realized P&L in USD, or None when it cannot be read.

    Missing ledger -> 0.0 (an empty book really has realized nothing);
    CORRUPT ledger -> None (unreadable, not empty — a fabricated 0.0 would
    read as "flat day" while real positions are unaccounted for). Never
    fabricates."""
    try:
        from live_execution import config as _le_config
        from live_execution.models import ExecutionLedger
        ledger = ExecutionLedger(_le_config.STATE_DIR / "executions.json")
        return float(ledger.realized_pnl_today())
    except Exception:
        log.warning("safety: daily realized P&L unreadable (fail-soft)",
                    exc_info=True)
        return None


def _armed_flag() -> bool:
    try:
        from live_execution import config as _le_config
        return bool(getattr(_le_config, "LIVE_TRADING_ENABLED", False))
    except ImportError:
        return False


@router.get("/api/safety")
async def get_safety(request: Request):
    # Same posture as /api/live/portfolio (SEC-02): the breaker's realized
    # P&L is live-book data. Direct loopback (local dashboard) is allowed.
    if not getattr(config, "LIVE_BOOK_PUBLIC", False):
        require_local_or_admin(request)

    kill = _kill_switch_state()
    brk = _break_state()
    pnl = _daily_realized_pnl()
    limit = _breaker_limit()

    # Read the engine's timestamped observation, not this API process's cache.
    from data_providers import crowd
    chain = crowd.observed_chain_status()

    # LLM health via the shared §60 TTL cache — ONE probe per window serves
    # both /api/system-status and /api/safety.
    llm_provider = str(getattr(config, "MAIN_LLM_PROVIDER", "unknown"))
    try:
        from api.routes import system_status
        llm_ok = await system_status._llm_health(request.app.state.narrator)
    except Exception:
        log.warning("safety: LLM health probe failed (fail-soft)", exc_info=True)
        llm_ok = False

    import blocklist as _blocklist
    bl = _blocklist.summary()

    breaker = {
        "limit_usd": limit,
        "realized_pnl_today_usd": pnl,
        # A manual kill is not evidence that the daily-loss breaker tripped.
        "engaged": bool(kill["active"] and kill["reason"].startswith("AUTO: realized daily loss")),
        # Margin-to-limit, computed server-side (DESIGN.md §5: no client
        # money math): how much further today's realized loss can go before
        # the breaker trips. None when the ledger is unreadable.
        "headroom_usd": (
            round(limit - abs(min(pnl, 0.0)), 4)
            if (pnl is not None and limit is not None) else None
        ),
    }

    # ---- Alerts: only symptoms that genuinely demand attention (§3.1b) ----
    alerts = []
    if kill["active"]:
        alerts.append({
            "id": "kill_switch",
            "severity": "fail",
            "message": (f"kill switch engaged — {kill['reason'] or 'no reason recorded'}. "
                        "A human must clear it before any trade."),
        })
    if not llm_ok:
        alerts.append({
            "id": "llm_down",
            "severity": "fail",
            "message": (f"LLM provider down ({llm_provider}) — decisions degrade "
                        "to template; the engine is not thinking."),
        })
    if chain.get("exhausted"):
        alerts.append({
            "id": "data_chain",
            "severity": "warn",
            "message": ("crowd scrape chain exhausted — every configured scraper "
                        f"({', '.join(chain['configured'])}) is benched."),
        })

    # "No fills in N hours while candidates are flowing" — over verbatim DB
    # rows: windowed feed events (candidates flowing) vs bound commits.
    try:
        from api import db
        async with db.get_db() as conn:
            feed = await db.get_feed_events(conn, limit=1000)
            commits = await db.get_recent_decision_commits(conn, limit=1000)
        cutoff = (datetime.now(timezone.utc).timestamp()
                  - ALERT_NO_FILLS_HOURS * 3600.0)
        flowed = [r for r in feed if _row_epoch(r.get("ts")) >= cutoff]
        filled = [c for c in commits
                  if c.get("signature") and _row_epoch(c.get("created_at")) >= cutoff]
        if len(flowed) >= ALERT_NO_FILLS_MIN_CANDIDATES and not filled:
            alerts.append({
                "id": "no_fills",
                "severity": "warn",
                "message": (f"no fills in {ALERT_NO_FILLS_HOURS:.0f}h while "
                            f"{len(flowed)} candidates flowed through the window"),
            })
    except Exception:
        log.warning("safety: no-fills check failed (fail-soft)", exc_info=True)

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "armed": _armed_flag(),
        "kill_switch": {
            "engaged": kill["active"],
            "reason": kill["reason"],
        },
        "daily_loss_breaker": breaker,
        "blocklist": bl,
        "break": {
            "on_break": brk["on_break"],
            "reason": brk["reason"],
            "break_until_epoch": brk["break_until_epoch"],
        },
        "llm": {
            "main_reachable": llm_ok,
            "provider": llm_provider,
        },
        "crowd_chain": chain,
        "alerts": alerts,
        "thresholds": {
            "no_fills_hours": ALERT_NO_FILLS_HOURS,
            "no_fills_min_candidates": ALERT_NO_FILLS_MIN_CANDIDATES,
        },
    }

