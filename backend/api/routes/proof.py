"""
api/routes/proof.py — local omo-parity proof endpoints (read-only).

Three endpoints mirroring omotrades' public API surface:

  /api/proof.json    full record: recent decisions (commits), fills, refusals
  /api/exits.json    exit thresholds, stored HWM marks, every sell bound
                     to its seal commit
  /api/verify.json   recompute sha256(nonce|payload) for every decision
                     commit and report pass/fail per row

All read-only; no endpoint can change trading state.
"""
from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter
from api import db
from rule_engine.exits import ExitDecision  # noqa: F401 — type docs only

router = APIRouter()


@router.get("/api/proof.json")
async def get_proof():
    """Full decision record: commits, fills, refusals."""
    async with db.get_db() as conn:
        commits = await db.get_recent_decision_commits(conn)
        fills = await db.get_recent_fills(conn)
        refusals = await db.get_refusal_events(conn, limit=100)

    return {
        "generated_at_utc": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat(),
        "commits": commits,
        "fills": fills,
        "refusals": refusals,
        "counts": {
            "commits": len(commits),
            "fills": len(fills),
            "refusals": len(refusals),
        },
    }


@router.get("/api/exits.json")
async def get_exits():
    """Exit thresholds, stored HWM marks and open positions with their
    trailing context."""
    import config as cfg

    thresholds = {
        "stop_loss_pct": cfg.STOP_LOSS_PCT,
        "trail_activation_pct": cfg.EXIT_TRAIL_ACTIVATION_PCT,
        "trail_give_back_pp": cfg.EXIT_TRAIL_GIVE_BACK_PP,
        "liquidity_break_floor_usd": cfg.EXIT_LIQUIDITY_FLOOR_USD,
        "invalidation_chg6h_pct": cfg.EXIT_INVALIDATION_CHG6H_PCT,
        "invalidation_sell_mult": cfg.EXIT_INVALIDATION_SELL_MULT,
        "stale_days": cfg.EXIT_STALE_DAYS,
        "stale_band_pct": cfg.EXIT_STALE_BAND_PCT,
        "stale_vol6h_usd": cfg.EXIT_STALE_VOL6H_USD,
        "tp_ladder": [{"gain": g, "trim": t} for g, t in cfg.EXIT_TP_LADDER],
        "sell_risk_gate": {
            "min_clip_usd": cfg.MIN_TICKET_USD,
            "cooldown_minutes_per_mint": cfg.SELL_COOLDOWN_MINUTES * 60
            if hasattr(cfg, 'SELL_COOLDOWN_MINUTES') else 30,
            "max_exits_per_24h": getattr(cfg, 'MAX_EXITS_PER_24H', 8),
        },
    }

    marks = []
    async with db.get_db() as conn:
        marks = await db.get_open_position_marks(conn)

    return {"thresholds": thresholds, "open_position_marks": marks}


@router.get("/api/verify.json")
async def get_verify():
    """Recompute sha256(nonce|canonical_payload) for every decision commit
    and report pass/fail per row."""
    results = []
    verified = failed = 0

    async with db.get_db() as conn:
        rows = await db.get_verify_commits(conn)

    for row in rows:
        recomputed = hashlib.sha256(
            (row["nonce"] + "|" + row["payload_json"]).encode()
        ).hexdigest()
        ok = recomputed == row["payload_hash"]
        if ok:
            verified += 1
        else:
            failed += 1
        results.append({
            "id": row["id"],
            "symbol": row["symbol"],
            "verdict": row["verdict"],
            "stored_hash": row["payload_hash"],
            "recomputed_hash": recomputed,
            "match": ok,
        })

    return {
        "algorithm": "sha256(nonce|canonical_payload_json)",
        "totals": {"checked": len(results), "verified": verified,
                   "failed": failed},
        "rows": results,
    }

@router.get("/api/refusals.json")
async def get_refusals(limit: int = 100):
    """Every refusal with its full rule breakdown, newest first.

    omo publishes refusals as loudly as fills: a person faking automation
    has no reason to invent hundreds of boring nos, so the refusals are the
    most telling part of the record. Read-only; verdict=fail covers both
    model vetoes and failed gate rules.
    """
    async with db.get_db() as conn:
        rows = await db.get_refusal_events(conn, min(max(limit, 1), 500))
    return {
        "generated_at_utc": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat(),
        "count": len(rows),
        "refusals": rows,
    }
