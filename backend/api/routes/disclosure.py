"""
api/routes/disclosure.py — REF-R6 public machine-truth feeds (read-only).

Two endpoints:
  /api/disclosure.json  live machine state: armed/disarmed, kill-switch,
                        break state, last cycle timestamp, config truths
                        (caps/floors). NO secrets (no API keys, no wallet).
  /api/reasoning.json   per-decision provenance: model that produced the
                        thesis, stage timings, inputs snapshot hash,
                        linked commit hash. Read from recent decision_commits.

reference: src/lib/disclosure.server.ts, /api/public/disclosure.json,
               /api/public/reasoning.json.
Full web UI terminal is explicitly OUT OF SCOPE — JSON endpoints first.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import config
from fastapi import APIRouter
from api import db

router = APIRouter()
log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _kill_switch_state() -> dict:
    """Read kill switch file. Fail-closed: if file unreadable, treat as KILL."""
    ks_path = Path(config.BASE_DIR).parent / "live_execution" / "state" / "kill_switch.json"
    try:
        if ks_path.is_file():
            data = json.loads(ks_path.read_text())
            return {"active": bool(data.get("active", False)),
                    "reason": str(data.get("reason", ""))}
        return {"active": False, "reason": "no state file"}
    except Exception as exc:
        return {"active": True, "reason": f"unreadable — treated as KILL: {exc}"}


def _break_state() -> dict:
    """Read break state file. Fail-closed: corrupt = on_break=True."""
    try:
        from rule_engine.liveness import is_on_break, break_reason, break_until
        on_break = is_on_break()
        return {
            "on_break": on_break,
            "reason": break_reason() if on_break else "",
            "break_until_epoch": break_until() if on_break else 0.0,
        }
    except RuntimeError as exc:
        # corrupt state file — liveness raises RuntimeError
        return {"on_break": True, "reason": f"state corrupt: {exc}", "break_until_epoch": 0.0}
    except Exception as exc:
        return {"on_break": False, "reason": f"state unavailable: {exc}", "break_until_epoch": 0.0}


@router.get("/api/disclosure.json")
async def get_disclosure():
    """
    REF-R6: live machine state for public auditability.

    Fields:
      armed          — LIVE_TRADING_ENABLED (always False; hardcoded disarmed)
      paper_only     — PAPER_TRADING_ONLY (always True; hardcoded)
      kill_switch    — state from live_execution/state/kill_switch.json
      break          — state from live_execution/state/break_state.json
      config_truths  — numeric caps/floors (no keys, no wallet address)
      generated_at_utc
    """
    # Safety fields — both hardcoded constants; surface them for auditability.
    armed = getattr(config, "LIVE_TRADING_ENABLED", False)
    paper_only = getattr(config, "PAPER_TRADING_ONLY", True)

    kill = _kill_switch_state()
    brk = _break_state()

    # Config truths — numeric thresholds only; no API keys, no wallet address
    config_truths = {
        "min_liquidity_usd": getattr(config, "MIN_LIQUIDITY_USD", None),
        "min_volume_1h_usd": getattr(config, "MIN_VOLUME_1H_USD", None),
        "min_ticket_usd": getattr(config, "MIN_TICKET_USD", None),
        "daily_deploy_cap_usd": getattr(config, "DAILY_DEPLOY_CAP_USD", None),
        "stop_loss_pct": getattr(config, "STOP_LOSS_PCT", None),
        "trail_activation_pct": getattr(config, "EXIT_TRAIL_ACTIVATION_PCT", None),
        "trail_give_back_pp": getattr(config, "EXIT_TRAIL_GIVE_BACK_PP", None),
        "max_candidates_per_tick": getattr(config, "MAX_CANDIDATES_PER_TICK", None),
        "tick_interval_seconds": getattr(config, "TICK_INTERVAL_SECONDS", None),
        "tp_ladder": [
            {"gain": g, "trim": t}
            for g, t in (getattr(config, "EXIT_TP_LADDER", None) or [])
        ],
        "crowd_heat_act_band": [
            getattr(config, "CROWD_HEAT_ACT_MIN", None),
            getattr(config, "CROWD_HEAT_ACT_MAX", None),
        ],
        "regime_green_band": [
            getattr(config, "REGIME_GREEN_MIN_PCT", None),
            getattr(config, "REGIME_GREEN_MAX_PCT", None),
        ],
        "regime_min_median_vol_usd": getattr(config, "REGIME_MIN_MEDIAN_VOL_USD", None),
    }

    return {
        "generated_at_utc": _now_iso(),
        "armed": armed,
        "paper_only": paper_only,
        "kill_switch": kill,
        "break": brk,
        "config_truths": config_truths,
    }


@router.get("/api/reasoning.json")
async def get_reasoning(limit: int = 50):
    """
    REF-R6: per-decision provenance — model, inputs hash, commit hash.

    For each recent decision commit, surfaces:
      - which model/source produced the thesis (from payload.think_source)
      - sha256 of the canonical inputs (the stored payload_json itself)
      - the stored payload_hash (the tamper-evident commit hash)
      - whether the commit allowed entry

    Note: stage timings are not yet instrumented per-row; the field is
    included as null for future instrumentation.
    """
    async with db.get_db() as conn:
        rows = await db.get_recent_decision_commits(conn, min(max(limit, 1), 200))

    reasoning = []
    for row in rows:
        payload = row.get("payload") or {}
        think_source = payload.get("think_source", "unknown")
        # Inputs snapshot hash: sha256 of the canonical payload_json (the
        # same bytes that the seal was computed over)
        payload_str = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        inputs_hash = hashlib.sha256(payload_str.encode()).hexdigest()

        reasoning.append({
            "id": row["id"],
            "created_at": row.get("created_at"),
            "symbol": row["symbol"],
            "mint": row.get("mint"),
            "think_source": think_source,
            "entry_allowed": row.get("entry_allowed"),
            "inputs_snapshot_hash": inputs_hash,
            "commit_hash": row.get("payload_hash"),
            "stage_timings_ms": None,  # reserved for future instrumentation
        })

    return {
        "generated_at_utc": _now_iso(),
        "count": len(reasoning),
        "reasoning": reasoning,
    }
