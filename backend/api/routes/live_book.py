"""
api/routes/live_book.py — GET /api/live/portfolio: the LIVE book, read-only.

The dashboard's other panels show the PAPER book (backend DB). This endpoint
surfaces the REAL book the same way run_live_cycle sees it:

  * cash      — the wallet's actual on-chain USDC balance (never an
                internal accumulator; None when unreadable — fail-closed)
  * positions — the live ExecutionLedger's open buys, marked with the same
                Jupiter pricing the rest of the app uses
  * realized  — the ledger's close records
  * equity    — cash + open position value (unpriced marks held at cost)

READ-ONLY like every other endpoint: nothing here can move money. All
live_execution access is function-local optional imports (the sanctioned
pattern used by proof.py/disclosure.py) and every failure degrades to
{"enabled": false, "reason": ...} — the endpoint never 500s and never
fabricates a number.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Request

import config
from api import db
from api.auth import require_local_or_admin

log = logging.getLogger(__name__)
router = APIRouter()

# §60 wallet scan: TTL cache for the chain token-balances read. The
# dashboard polls /api/live/portfolio every 5s; the chain read must not be
# (public RPC rate-limits hard). Balances age at most
# config.WALLET_SCAN_TTL_SECONDS (default 300s), so a position the operator
# closed manually from their wallet leaves the dashboard within that window.
# Fail-soft: during an RPC outage the last good read is reused up to 3x TTL
# (flagged stale), then the scan reports unchecked. An EMPTY wallet ({} is
# an answered read) is a good cached value; None is never cached.
_scan_cache: dict = {"balances": None, "at": 0.0, "good_at": 0.0}


async def _wallet_balances(pubkey: str) -> tuple[Optional[dict], bool]:
    """(balances, stale) — None means unknown this request (no cached truth)."""
    from live_execution import solana
    now = time.monotonic()
    ttl = float(getattr(config, "WALLET_SCAN_TTL_SECONDS", 300.0))
    if _scan_cache["balances"] is not None and now - _scan_cache["at"] < ttl:
        return _scan_cache["balances"], False
    try:
        balances = await solana.get_token_balances(pubkey)
    except Exception:
        log.warning("wallet scan: chain read failed", exc_info=True)
        balances = None
    if balances is None:
        if _scan_cache["balances"] is not None and now - _scan_cache["good_at"] < 3 * ttl:
            return _scan_cache["balances"], True
        return None, False
    _scan_cache.update(balances=balances, at=now, good_at=now)
    return balances, False


def _check_access(request: Request) -> None:
    """Operator access control for live book surfaces (SEC-02).

    When LIVE_BOOK_PUBLIC is False, access is restricted to DIRECT loopback
    connections (local operator / dashboard) or a valid X-Admin-Token.
    §55: loopback is only trusted when the connection is NOT proxied —
    behind the deploy guide's Caddy reverse_proxy every internet visitor
    arrives with client.host == 127.0.0.1, so any forwarding header disables
    the loopback shortcut (auth.require_local_or_admin).
    """
    if getattr(config, "LIVE_BOOK_PUBLIC", False):
        return
    require_local_or_admin(request)


def _disabled(reason: str) -> dict:
    return {"enabled": False, "reason": reason}


@router.get("/api/live/portfolio")
async def get_live_portfolio(request: Request):
    _check_access(request)
    try:
        return await _build(request)
    except Exception as exc:
        # The live book must never take the dashboard down.
        log.warning("live portfolio unreadable: %s", exc, exc_info=True)
        return _disabled(f"unreadable: {exc}")



async def _build(request: Request) -> dict:
    # Sanctioned function-local optional imports (backend never hard-depends
    # on live_execution; a paper-only checkout simply has no live book).
    try:
        from live_execution import config as le_config
        from live_execution.models import ExecutionLedger
    except ImportError:
        return _disabled("live_execution package not importable")

    if not getattr(le_config, "LIVE_TRADING_ENABLED", False):
        return _disabled("disarmed (LIVE_TRADING_ENABLED=False)")

    try:
        from live_execution import solana, wallet
        payer = wallet.load_keypair()
        pubkey = wallet.pubkey_string(payer)
    except Exception as exc:
        return _disabled(f"wallet unreadable: {exc}")

    ledger = ExecutionLedger(le_config.STATE_DIR / "executions.json")
    try:
        records = ledger._load()   # same-package read pattern as run_live_cycle
    except RuntimeError as exc:
        return _disabled(f"ledger corrupt: {exc}")

    # --- cash: chain truth, never accumulated -----------------------------
    cash_usd = None
    sol_balance = None
    try:
        cash_usd = await solana.get_usdc_balance(pubkey)
    except Exception:
        log.warning("live portfolio: USDC balance unreadable", exc_info=True)
    try:
        sol_balance = await solana.get_sol_balance(pubkey)
    except Exception:
        pass

    # --- open positions from the ledger (aggregate buys per mint) ---------
    meta: dict[str, dict] = {}
    for r in records:
        if r.get("kind") != "buy" or r.get("status") not in ledger._OPEN:
            continue
        mint = r["mint"]
        tokens = float(r.get("tokens_out") or 0.0)
        cost = float(r.get("usd_size") or 0.0)
        if mint in meta:
            meta[mint]["tokens"] += tokens
            meta[mint]["cost"] += cost
            continue
        meta[mint] = {
            "tokens": tokens,
            "cost": cost,
            "opened_ts": float(r.get("ts") or 0.0),
            "entry_price_usd": float(r.get("price_usd") or 0.0),
        }

    # --- §60 wallet scan: chain truth on quantities, TTL-cached -------------
    # The journal stays the authority on cost; the chain is the authority on
    # HOW MANY tokens the wallet holds right now. A position the operator
    # sold out-of-band (chain balance 0) is EXCLUDED from this response so
    # the dashboard never keeps showing tokens that are gone. The ledger is
    # NEVER mutated by a read (A2 invariant): vanished mints are reported in
    # chain_excluded for the operator, resolved via repair_vanished.py.
    balances, stale = await _wallet_balances(pubkey)
    scan = {"checked": False, "stale": False, "at_utc": None}
    if balances is not None:
        from live_execution.reconcile import reconcile
        scan["checked"] = True
        scan["stale"] = stale
        scan["at_utc"] = datetime.now(timezone.utc).isoformat()
        reconcile(meta, balances)

    # symbols: the live cycle journals a thesis row per live entry
    theses: dict[str, str] = {}
    try:
        async with db.get_db() as conn:
            for row in await db.get_theses(conn, limit=200):
                theses[row.get("mint_address", "")] = row.get("symbol") or ""
    except Exception:
        pass

    provider = request.app.state.provider
    positions = []
    open_value_usd = 0.0
    unrealized_usd = 0.0
    have_unrealized = False
    for mint, m in meta.items():
        # §60 wallet scan: a vanished position (sold on-chain) never renders;
        # a short one clamps to chain truth (never show tokens the wallet
        # does not hold — the operator cannot sell what is not there).
        if m.get("chain_excluded"):
            continue
        mark = None
        try:
            decimals = await solana.get_mint_decimals(mint)
            if decimals is not None:
                mark = await provider.get_current_price(mint, decimals)
        except Exception:
            mark = None
        tokens = float(m.get("chain_tokens") or m["tokens"])
        value = (tokens * mark) if (mark is not None and mark > 0) else m["cost"]
        open_value_usd += value
        pos = {
            "mint_address": mint,
            "symbol": theses.get(mint) or mint[:6],
            "cost_usd": round(m["cost"], 4),
            "tokens": tokens,
            "entry_price_usd": m["entry_price_usd"] or None,
            "current_price_usd": mark,
            "value_usd": round(value, 4),
            "unrealized_pnl_usd": None,
            "opened_at": (
                datetime.fromtimestamp(m["opened_ts"], tz=timezone.utc).isoformat()
                if m["opened_ts"] else None
            ),
        }
        if mark is not None and mark > 0:
            pos["unrealized_pnl_usd"] = round(value - m["cost"], 4)
            unrealized_usd += value - m["cost"]
            have_unrealized = True
        positions.append(pos)

    # --- realized P&L from close records -----------------------------------
    realized_usd = sum(
        float(r.get("pnl_usd") or 0.0)
        for r in records
        if r.get("kind") == "close" and r.get("pnl_usd") is not None
    )
    closed_count = sum(1 for r in records if r.get("kind") == "close")

    equity_usd = None
    if cash_usd is not None:
        equity_usd = round(max(cash_usd, 0.0) + open_value_usd, 4)

    return {
        "enabled": True,
        "armed": True,
        "manual_confirmation": bool(
            getattr(le_config, "REQUIRE_MANUAL_CONFIRMATION", True)),
        "wallet": pubkey,
        "cash_usd": round(cash_usd, 4) if cash_usd is not None else None,
        "sol_balance": round(sol_balance, 6) if sol_balance is not None else None,
        "equity_usd": equity_usd,
        "open_value_usd": round(open_value_usd, 4),
        "unrealized_pnl_usd": round(unrealized_usd, 4) if have_unrealized else None,
        "realized_pnl_usd": round(realized_usd, 4),
        "deployed_today_usd": round(ledger.deployed_today_usd(), 4),
        "closed_trades": closed_count,
        "positions": positions,
        # §60 wallet scan surfacing: which journal positions the chain says
        # are gone (operator review / repair_vanished.py), and how fresh the
        # scan itself is (stale = reused during an RPC outage).
        "chain_scan": scan,
        "chain_excluded": [
            {
                "mint": mint,
                "tokens": float(m["tokens"]),
                "cost_usd": round(m["cost"], 4),
            }
            for mint, m in meta.items()
            if m.get("chain_excluded")
        ],
        "count": len(positions),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/api/live/executions")
async def get_live_executions(request: Request):
    """The LIVE order journal — why an enter did or did not become a fill.

    Two read-only views of the live_execution state dir:
      * commits — every sealed order decision with its full lifecycle
                  (sealed -> published memo -> bound fill, or failed + reason)
      * records — the execution ledger's money movements (buys and closes)

    Same fail-soft contract as /api/live/portfolio: never 500s, never
    fabricates a number, degrades to {"enabled": false, "reason": ...}.
    """
    _check_access(request)
    try:
        return _build_executions()
    except Exception as exc:
        log.warning("live executions unreadable: %s", exc, exc_info=True)
        return _disabled(f"unreadable: {exc}")



def _build_executions() -> dict:
    try:
        from live_execution import config as le_config
        from live_execution.commit_log import CommitLog
        from live_execution.models import ExecutionLedger
    except ImportError:
        return _disabled("live_execution package not importable")

    if not getattr(le_config, "LIVE_TRADING_ENABLED", False):
        return _disabled("disarmed (LIVE_TRADING_ENABLED=False)")

    commits = CommitLog(le_config.STATE_DIR / "commits.json").recent(limit=100)
    ledger = ExecutionLedger(le_config.STATE_DIR / "executions.json")
    try:
        records = list(reversed(ledger._load()[-100:]))   # newest first
    except RuntimeError as exc:
        return _disabled(f"ledger corrupt: {exc}")

    return {
        "enabled": True,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "commits": commits,
        "records": records,
        "totals": {
            "commits": len(commits),
            "bound": sum(1 for c in commits if c.get("status") == "bound"),
            "failed": sum(1 for c in commits if c.get("status") == "failed"),
            "published_unfilled": sum(
                1 for c in commits if c.get("status") == "published"),
            "buys": sum(1 for r in records if r.get("kind") == "buy"),
            "closes": sum(1 for r in records if r.get("kind") == "close"),
        },
    }