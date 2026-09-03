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
    # Single source of truth for the live state dir (config.LIVE_STATE_DIR);
    # resolved per call so tests never read the operator's live kill switch.
    ks_path = Path(str(getattr(
        config, "KILL_SWITCH_FILE",
        str(config.LIVE_STATE_DIR / "kill_switch.json"))))
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


async def _sizing_truths() -> tuple[dict, dict]:
    """
    REF-R8/R9 public sizing truths: the risk budget + calibration the sizing
    actually used, persisted by the tick into the daily-stats row for today.
    A fresh book (no tick yet) recomputes them from the DB at cost-basis
    equity (no external calls). Any failure degrades to the fail-closed
    minimums: minimum-ticket budget and FLAT calibration (factor 1.0).
    Never raises.
    """
    from calibration import FLAT_CALIBRATION, compute_calibration
    from sizing import compute_risk_budget

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    budget_block = None
    cal_block = None
    try:
        async with db.get_db() as conn:
            row = await db.get_daily_stats(conn, today)
        if row is not None:
            sj = row.get("stats_json") or {}
            budget_block = sj.get("risk_budget")
            cal_block = sj.get("calibration")
    except Exception:
        log.warning("disclosure: persisted sizing truths unreadable",
                    exc_info=True)

    if budget_block is None or cal_block is None:
        try:
            async with db.get_db() as conn:
                cash = await db.get_cash_balance(conn)
                open_trades = await db.get_open_trades(conn)
                closed = await db.get_all_closed_trades(conn)
            equity = float(cash) + sum(
                float(t.position_size_usd) for t in open_trades)
            if budget_block is None:
                budget_block = compute_risk_budget(equity, 0.0).to_dict()
            if cal_block is None:
                cal_block = compute_calibration(closed).to_dict()
        except Exception:
            log.warning("disclosure: sizing truths unavailable - fail-closed "
                        "minimums", exc_info=True)
            if budget_block is None:
                budget_block = compute_risk_budget(0.0, 0.0).to_dict()
            if cal_block is None:
                cal_block = FLAT_CALIBRATION.to_dict()
    return budget_block, cal_block


@router.get("/api/disclosure.json")
async def get_disclosure():
    """
    REF-R6: live machine state for public auditability.

    Fields:
      armed          — LIVE_TRADING_ENABLED read verbatim from live_execution
                       (fail-closed False if the package is absent); the feed
                       can never claim disarmed while the machine is armed
      paper_only     — PAPER_TRADING_ONLY (§52: hardcoded True remains as the
                       historical safety flag; a SEPARATE single_book field
                       reports the paper book's retirement honestly)
      kill_switch    — state from live_execution/state/kill_switch.json
      break          — state from live_execution/state/break_state.json
      config_truths  — numeric caps/floors (no keys, no wallet address)
      risk_budget    — REF-R8 derived sizing budget (equity, drawdown factor,
                       max order/day, formula) the sizing used
      calibration    — REF-R9 conviction factor + formula from closed outcomes
      thesis_restatement — A11 cadence truths for the write-up re-authoring
                       job (stale horizon, per-pass cap, scope)
      anti_churn     — §49 DONT-pattern killer truths: auto-block
                       threshold, PnL basis, re-entry cooldown window
      generated_at_utc
    """
    # Safety fields — surface them for auditability. `armed` reads the REAL
    # live_execution flag (fail-closed False when the package is not
    # importable) so the public feed can never claim disarmed while the
    # machine is armed. paper_only stays hardcoded True in backend/config.py
    # (the historical safety flag); the §52 single-book truth is surfaced
    # separately so no field ever lies about the book that is running.
    try:
        from live_execution import config as _le_config
        armed = bool(getattr(_le_config, "LIVE_TRADING_ENABLED", False))
    except ImportError:
        armed = False
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

    # REF-R8/R9: the computed risk budget + calibration the sizing used.
    # Persisted by the tick into the daily-stats row for today; a fresh book
    # recomputes them from the DB at cost-basis equity (no external calls).
    risk_budget, calibration = await _sizing_truths()

    # REF-R11: commit-memo truths (on-chain precommit, fail-closed). The
    # constants live in live_execution (the only package allowed to build
    # transactions); read them if importable, else surface the documented
    # values. Memo publishing only ever runs when the live path is armed.
    memo_program_id = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
    memo_prefix = "commit:v1:"
    try:
        from live_execution.memo import MEMO_PROGRAM_ID as _mp, MEMO_PREFIX as _mpx
        memo_program_id, memo_prefix = _mp, _mpx
    except ImportError:
        pass
    commit_memo = {
        "implemented": True,
        "memo_program_id": memo_program_id,
        "memo_prefix": memo_prefix,
        "scheme": "sha256(nonce|canonical_payload_json) — the SAME hash as "
                  "the local seal, written on-chain as a Solana memo BEFORE "
                  "the fill is broadcast",
        "fail_closed": "a fill is never broadcast unless its commit memo is "
                       "confirmed on-chain first; memo failure blocks the "
                       "order and is journaled",
        "reveal": "immediate — payload+nonce are public in /api/proof.json; "
                  "the ordering proof is the on-chain hash timestamp",
        "signer": "the configured trading wallet keypair (no separate memo "
                  "key at this book scale)",
        "fee_model": "one base-fee transaction (5000 lamports) per order on "
                     "top of the fill; no rent (memo writes no state); "
                     "token-account rent ~0.002 SOL per new mint is paid by "
                     "the fill, not the memo",
        "active": bool(armed),
    }

    return {
        "generated_at_utc": _now_iso(),
        "armed": armed,
        "paper_only": paper_only,
        "single_book": True,   # §52: the paper book is retired; live-only
        "kill_switch": kill,
        "break": brk,
        "config_truths": config_truths,
        "risk_budget": risk_budget,
        "calibration": calibration,
        "commit_memo": commit_memo,
        # A11 (omo audit §30): thesis re-authoring cadence truths. Published
        # like the sizing formulas — the job is narrative-only, so the only
        # numbers it has are its cadence knobs.
        "thesis_restatement": {
            "implemented": True,
            "stale_hours": getattr(config, "THESIS_RESTATE_STALE_HOURS", None),
            "per_pass": getattr(config, "THESIS_RESTATE_PER_PASS", None),
            "scope": "narrative only — rewrites open thesis text against the "
                     "position's current numbers; never touches size, exits, "
                     "or verdicts",
            "source": "A11 (omo audit §30), reference thesis-author.server.ts",
        },
        # §49: anti-churn truths. The DONT-pattern killer is now a public,
        # auditable guarantee like the sizing formulas: thresholds are
        # hardcoded (never env-settable), the basis is realized PnL, and
        # the state semantics are spelled out.
        "anti_churn": {
            "implemented": True,
            "auto_block_consecutive_losses": getattr(
                config, "AUTO_BLOCK_CONSECUTIVE_LOSSES", None),
            "auto_block_basis": "realized PnL < 0 on any exit rule, "
                                "recorded on BOTH books (paper + live)",
            "reentry_cooldown_hours": getattr(
                config, "REENTRY_COOLDOWN_HOURS", None),
            "state": "mint-keyed JSON sidecar (gitignored); auto blocks "
                     "are lifted only by a human; close history survives "
                     "unblocks",
        },
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
