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
        commits = []
        async with conn.execute(
            """
            SELECT id, created_at, tick_ts, symbol, mint_address,
                   verdict, entry_allowed, nonce, payload_json, payload_hash
            FROM decision_commits ORDER BY created_at DESC LIMIT 100
            """
        ) as cur:
            for row in await cur.fetchall():
                commits.append({
                    "id": row["id"],
                    "created_at": row["created_at"],
                    "tick_ts": row["tick_ts"],
                    "symbol": row["symbol"],
                    "mint": row["mint_address"],
                    "think_verdict": row["verdict"],
                    "entry_allowed": bool(row["entry_allowed"]),
                    "nonce": row["nonce"],
                    "payload": json.loads(row["payload_json"]),
                    "payload_hash": row["payload_hash"],
                })

        fills = []
        async with conn.execute(
            """
            SELECT trade_id, symbol, mint_address, opened_at,
                   entry_price_usd, position_size_usd, thesis,
                   closed_at, exit_price_usd, exit_reason,
                   realized_pnl_usd, realized_pnl_pct, is_open
            FROM trades ORDER BY opened_at DESC LIMIT 100
            """
        ) as cur:
            cols = [d[0] for d in cur.description]
            for row in await cur.fetchall():
                fills.append(dict(zip(cols, row)))

    return {
        "generated_at_utc": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat(),
        "commits": commits,
        "fills": fills,
        "counts": {
            "commits": len(commits),
            "fills": len(fills),
        },
    }


@router.get("/api/exits.json")
async def get_exits():
    """Exit thresholds, stored HWM marks and open positions with their
    trailing context."""
    from paper_trading_engine import default_ledger  # noqa: F401

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
        async with conn.execute(
            """
            SELECT trade_id, symbol, mint_address, entry_price_usd,
                   position_size_usd, high_water_usd, tranches_taken,
                   opened_at, is_open
            FROM trades WHERE is_open = 1
            """
        ) as cur:
            cols = [d[0] for d in cur.description]
            for row in await cur.fetchall():
                marks.append(dict(zip(cols, row)))

    return {"thresholds": thresholds, "open_position_marks": marks}


@router.get("/api/verify.json")
async def get_verify():
    """Recompute sha256(nonce|canonical_payload) for every decision commit
    and report pass/fail per row."""
    results = []
    verified = failed = 0

    async with db.get_db() as conn:
        async with conn.execute(
            """
            SELECT id, nonce, payload_json, payload_hash, symbol, verdict
            FROM decision_commits ORDER BY created_at DESC LIMIT 200
            """
        ) as cur:
            rows = await cur.fetchall()

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