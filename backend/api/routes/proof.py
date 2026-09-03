"""
api/routes/proof.py — local reference-parity proof endpoints (read-only).

Five endpoints mirroring the reference bot' public API surface:

  /api/proof.json    full record: recent decisions (commits), fills, refusals,
                     unattributed fills (REF-R7)
  /api/exits.json    exit thresholds, stored HWM marks, every sell bound
                     to its seal commit
  /api/verify.json   recompute sha256(nonce|payload) for every decision
                     commit and report pass/fail per row; for rows with a
                     bound Solana signature also check RPC binding (REF-R1)
  /api/binding.json  REF-R1: committed mint vs mint actually touched,
                     matched/mismatched counts, unknown when RPC unavailable
  /api/refusals.json every gate/model refusal with full rule breakdown
  /api/theses.json   REF-R3 durable thesis book

All read-only; no endpoint can change trading state.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone

from typing import Optional

from fastapi import APIRouter
import config
from api import db
from rule_engine.exits import ExitDecision  # noqa: F401 — type docs only

router = APIRouter()
log = logging.getLogger(__name__)

# SEC-06: In-memory TTL caching for heavy cryptographic verification endpoints
_VERIFY_CACHE: dict = {"ts": 0.0, "data": None}
_BINDING_CACHE: dict = {"ts": 0.0, "data": None}
_PROOF_CACHE_TTL: float = 3.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()




@router.get("/api/proof.json")
async def get_proof():
    """Full decision record: commits, fills, refusals, unattributed fills."""
    async with db.get_db() as conn:
        commits = await db.get_recent_decision_commits(conn)
        fills = await db.get_recent_fills(conn)
        refusals = await db.get_refusal_events(conn, limit=100)

    return {
        "generated_at_utc": _now_iso(),
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


# ---------------------------------------------------------------------------
# REF-R11 — on-chain precommit memo verification
# ---------------------------------------------------------------------------

# The memo program echoes the memo text into the transaction's program logs;
# that echo is what anyone can read back from public RPC (reference parity —
# verify.server.ts uses the identical log-line shape).
_MEMO_LOG_RE = re.compile(r'Memo \(len \d+\): "(.*)"$')


def _memo_text_from_tx(tx: dict) -> Optional[str]:
    """Extract the memo text from a fetched transaction's program logs."""
    for line in (tx.get("meta") or {}).get("logMessages") or []:
        m = _MEMO_LOG_RE.search(line)
        if m:
            return m.group(1)
    return None


async def _verify_memo(row: dict, recomputed_hash: str, _get_tx,
                       memo_prefix: str) -> dict:
    """
    REF-R11 memo checks for one decision-commit row.

    Checks (all must pass for status='verified'):
      1. memo_confirmed          — memo tx exists, meta.err == null
      2. memo_hash_matches_chain — memo text after the prefix equals the
         recomputed sha256(nonce|canonical_payload)
      3. memo_before_fill        — memo slot strictly earlier than the bound
         fill's slot (no fill bound -> unknown)
    Any check that cannot run reports 'unknown', NEVER 'pass' (fail closed).
    Rows without a memo signature report status='not_published' — honestly,
    never dressed up as proof (the reference shows these "as such" too).
    """
    memo_sig = row.get("memo_signature")
    if not memo_sig:
        return {"published": False, "status": "not_published", "checks": []}

    base = {"published": True, "memo_signature": memo_sig,
            "memo_slot": row.get("memo_slot")}
    if _get_tx is None:
        return {**base, "status": "unknown", "checks": [
            {"name": "rpc_available", "status": "unknown",
             "detail": "live_execution not importable in paper mode"}]}

    try:
        memo_tx = await _get_tx(memo_sig)
    except Exception as exc:
        memo_tx = None
        log.debug("verify: memo fetch failed for %s: %s", memo_sig[:20], exc)
    if memo_tx is None:
        return {**base, "status": "unknown", "checks": [
            {"name": "memo_fetch", "status": "unknown",
             "detail": "RPC returned no data for memo signature"}]}

    checks = []
    meta = memo_tx.get("meta") or {}
    err = meta.get("err")
    checks.append({
        "name": "memo_confirmed",
        "status": "pass" if err is None else "fail",
        "detail": "no error" if err is None else f"err={err}",
    })

    memo_text = _memo_text_from_tx(memo_tx)
    on_chain_hash = None
    if memo_text is not None and memo_text.startswith(memo_prefix):
        on_chain_hash = memo_text[len(memo_prefix):]
    hash_ok = on_chain_hash is not None and on_chain_hash == recomputed_hash
    checks.append({
        "name": "memo_hash_matches_chain",
        "status": "pass" if hash_ok else "fail",
        "detail": f"on_chain={on_chain_hash!r} recomputed={recomputed_hash[:16]}",
    })

    memo_slot = memo_tx.get("slot")
    fill_sig = row.get("signature")
    if not fill_sig:
        checks.append({"name": "memo_before_fill", "status": "unknown",
                       "detail": "no fill bound to this commit"})
    else:
        try:
            fill_tx = await _get_tx(fill_sig)
        except Exception:
            fill_tx = None
        fill_slot = fill_tx.get("slot") if isinstance(fill_tx, dict) else None
        if memo_slot is None or fill_slot is None:
            checks.append({"name": "memo_before_fill", "status": "unknown",
                           "detail": f"slots unavailable (memo={memo_slot} fill={fill_slot})"})
        else:
            order_ok = int(memo_slot) < int(fill_slot)
            checks.append({
                "name": "memo_before_fill",
                "status": "pass" if order_ok else "fail",
                "detail": f"memo_slot={memo_slot} fill_slot={fill_slot}",
            })

    statuses = [c["status"] for c in checks]
    if "fail" in statuses:
        overall = "failed"
    elif "unknown" in statuses:
        overall = "unknown"
    else:
        overall = "verified"
    return {**base, "status": overall, "on_chain_hash": on_chain_hash,
            "memo_slot": memo_slot, "checks": checks}


@router.get("/api/verify.json")
async def get_verify():
    """Recompute sha256(nonce|canonical_payload) for every decision commit
    and report pass/fail per row. REF-R11: rows carrying an on-chain memo
    signature additionally get the memo verified against public RPC (memo
    confirmed, memo text matches the committed hash, memo slot precedes the
    bound fill). RPC unavailable -> 'unknown', never 'pass'."""
    now = time.time()
    if "pytest" not in sys.modules and _VERIFY_CACHE["data"] is not None and (now - _VERIFY_CACHE["ts"]) < _PROOF_CACHE_TTL:
        return _VERIFY_CACHE["data"]



    # Optional RPC read helper + memo prefix (live_execution is not
    # importable in pure-paper mode; the local recompute still runs).
    _get_tx = None
    memo_prefix = "commit:v1:"
    try:
        from live_execution.solana import get_transaction as _get_tx_fn
        from live_execution.memo import MEMO_PREFIX as _live_prefix
        _get_tx = _get_tx_fn
        memo_prefix = _live_prefix
    except ImportError:
        pass

    results = []
    verified = failed = 0
    memo_totals = {"published": 0, "verified": 0, "failed": 0, "unknown": 0}

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
        memo_block = await _verify_memo(row, recomputed, _get_tx, memo_prefix)
        if memo_block["published"]:
            memo_totals["published"] += 1
            if memo_block["status"] in memo_totals:
                memo_totals[memo_block["status"]] += 1
        results.append({
            "id": row["id"],
            "symbol": row["symbol"],
            "verdict": row["verdict"],
            "stored_hash": row["payload_hash"],
            "recomputed_hash": recomputed,
            "match": ok,
            "memo": memo_block,
        })

    payload = {
        "algorithm": "sha256(nonce|canonical_payload_json)",
        "memo_algorithm": f"memo text = '{memo_prefix}' + hash, written "
                          "on-chain BEFORE the fill; recomputed from the "
                          "revealed payload+nonce and matched against the "
                          "memo log line fetched from public RPC",
        "totals": {"checked": len(results), "verified": verified,
                   "failed": failed},
        "memo_totals": memo_totals,
        "rows": results,
    }
    _VERIFY_CACHE["ts"] = now
    _VERIFY_CACHE["data"] = payload
    return payload



# ---------------------------------------------------------------------------
# REF-R1 — Independent binding report
# ---------------------------------------------------------------------------

def _verify_binding_checks(tx: dict, commit_payload: dict,
                            commit_created_at: str, wallet_address: str) -> dict:
    """
    Run REF-R1 binding checks against a fetched transaction.

    Checks (all must pass for a matched result):
      1. tx exists and confirmed (meta.err == null)
      2. time ordering: commit created_at < tx blockTime
      3. account key 0 == wallet address (fee payer)
      4. pre/postTokenBalances include the committed mint
    Returns {"status": "matched" | "mismatched", "checks": [...]}
    A check that cannot run reports status="unknown", not "pass".
    """
    checks = []
    mint = commit_payload.get("mint", "")
    committed_side = "buy" if commit_payload.get("entry_allowed") else "sell"

    # Check 1: confirmed
    meta = tx.get("meta") or {}
    err = meta.get("err")
    checks.append({
        "name": "tx_confirmed",
        "status": "pass" if err is None else "fail",
        "detail": "no error" if err is None else f"err={err}",
    })

    # Check 2: time ordering
    block_time = tx.get("blockTime")
    if block_time is not None and commit_created_at:
        try:
            from datetime import datetime, timezone
            commit_ts = datetime.fromisoformat(
                commit_created_at.replace("Z", "+00:00")).timestamp()
            ordering_ok = commit_ts < block_time
            checks.append({
                "name": "time_ordering",
                "status": "pass" if ordering_ok else "fail",
                "detail": f"commit_ts={commit_ts:.0f} block_time={block_time}",
            })
        except Exception:
            checks.append({"name": "time_ordering", "status": "unknown",
                           "detail": "could not parse timestamps"})
    else:
        checks.append({"name": "time_ordering", "status": "unknown",
                       "detail": "blockTime unavailable"})

    # Check 3: fee payer == wallet
    tx_inner = tx.get("transaction") or {}
    msg = tx_inner.get("message") or {}
    account_keys = msg.get("accountKeys") or []
    if account_keys and wallet_address:
        first_key = account_keys[0]
        if isinstance(first_key, dict):
            first_key = first_key.get("pubkey", "")
        fee_payer_ok = str(first_key) == wallet_address
        checks.append({
            "name": "fee_payer",
            "status": "pass" if fee_payer_ok else "fail",
            "detail": f"key0={first_key!r} wallet={wallet_address!r}",
        })
    else:
        checks.append({"name": "fee_payer", "status": "unknown",
                       "detail": "accountKeys or wallet not available"})

    # Check 4: mint in token balances
    pre = meta.get("preTokenBalances") or []
    post = meta.get("postTokenBalances") or []
    all_mints = {b.get("mint") for b in pre + post if b.get("mint")}
    if mint and all_mints:
        mint_present = mint in all_mints
        checks.append({
            "name": "mint_present",
            "status": "pass" if mint_present else "fail",
            "detail": f"committed={mint} found={mint_present}",
        })
    else:
        checks.append({"name": "mint_present", "status": "unknown",
                       "detail": f"mint={mint!r} or balances empty"})

    # Overall: fail if ANY check failed; unknown if no failures and any unknown
    statuses = [c["status"] for c in checks]
    if "fail" in statuses:
        overall = "mismatched"
    elif "unknown" in statuses:
        overall = "unknown"
    else:
        overall = "matched"

    return {"status": overall, "checks": checks, "committed_mint": mint,
            "committed_side": committed_side}


@router.get("/api/binding.json")
async def get_binding():
    """
    REF-R1: pair committed mint vs mint actually touched in the fill tx.

    For each decision commit that has a bound signature, attempt to verify
    the on-chain transaction against the commit payload. Rows without a
    signature report status='unbound'. Rows where the RPC is unavailable
    report status='unknown' — never 'pass'. Only rows where all four checks
    pass report 'matched'.
    """
    now = time.time()
    if "pytest" not in sys.modules and _BINDING_CACHE["data"] is not None and (now - _BINDING_CACHE["ts"]) < _PROOF_CACHE_TTL:
        return _BINDING_CACHE["data"]



    import config as cfg

    wallet_address = getattr(cfg, "WALLET_ADDRESS", "")
    # Try to import get_transaction — only available in live_execution; in
    # pure-paper mode we still produce the report but all bound rows show
    # 'unknown' (RPC not reachable from paper side).
    _get_tx = None
    try:
        from live_execution.solana import get_transaction as _get_tx_fn
        _get_tx = _get_tx_fn
    except ImportError:
        pass


    async with db.get_db() as conn:
        rows = await db.get_verify_commits(conn)

    pairs = []
    matched = mismatched = unknown = unbound = 0

    for row in rows:
        sig = row.get("signature")
        if not sig:
            pairs.append({
                "id": row["id"],
                "symbol": row["symbol"],
                "status": "unbound",
                "signature": None,
                "venue": row.get("venue"),   # A3: null until a fill is bound
                "checks": [],
            })
            unbound += 1
            continue

        try:
            payload = json.loads(row["payload_json"])
        except Exception:
            payload = {}

        if _get_tx is None:
            result = {"status": "unknown", "checks": [
                {"name": "rpc_available", "status": "unknown",
                 "detail": "live_execution not importable in paper mode"}
            ], "committed_mint": payload.get("mint", ""), "committed_side": "unknown"}
        else:
            try:
                tx = await _get_tx(sig)
            except Exception as exc:
                tx = None
                log.debug("binding: get_transaction(%s) failed: %s", sig[:20], exc)

            if tx is None:
                result = {"status": "unknown", "checks": [
                    {"name": "tx_fetch", "status": "unknown",
                     "detail": "RPC returned no data for signature"}
                ], "committed_mint": payload.get("mint", ""), "committed_side": "unknown"}
            else:
                commit_created_at = row.get("created_at", "")
                result = _verify_binding_checks(tx, payload, commit_created_at,
                                                wallet_address)

        if result["status"] == "matched":
            matched += 1
        elif result["status"] == "mismatched":
            mismatched += 1
        else:
            unknown += 1

        pairs.append({
            "id": row["id"],
            "symbol": row["symbol"],
            "signature": sig,
            # A3: which program executed the fill (null on paper rows and on
            # rows journaled before venue attribution existed).
            "venue": row.get("venue"),
            **result,
        })

    payload = {
        "generated_at_utc": _now_iso(),
        "totals": {"matched": matched, "mismatched": mismatched,
                   "unknown": unknown, "unbound": unbound},
        "pairs": pairs,
    }
    _BINDING_CACHE["ts"] = now
    _BINDING_CACHE["data"] = payload
    return payload



@router.get("/api/refusals.json")
async def get_refusals(limit: int = 100):
    """Every refusal with its full rule breakdown, newest first.

    the reference publishes refusals as loudly as fills: a person faking automation
    has no reason to invent hundreds of boring nos, so the refusals are the
    most telling part of the record. Read-only; verdict=fail covers both
    model vetoes and failed gate rules.
    """
    async with db.get_db() as conn:
        rows = await db.get_refusal_events(conn, min(max(limit, 1), 500))
    return {
        "generated_at_utc": _now_iso(),
        "count": len(rows),
        "refusals": rows,
    }

@router.get("/api/theses.json")
async def get_theses(limit: int = 100):
    """The Durable Thesis Book.
    
    Every position's written thesis, updated over its lifecycle, and stamped
    with the realized PnL on exit.
    """
    async with db.get_db() as conn:
        rows = await db.get_theses(conn, min(max(limit, 1), 500))
    return {
        "generated_at_utc": _now_iso(),
        "count": len(rows),
        "theses": rows,
    }
