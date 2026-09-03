"""
api/routes/holdings.py — GET /api/holdings: open positions with live price
and unrealized P&L computed per request.

§52: single-book world — the trades table mirrors the LIVE book (ledger is
the money authority). Cash = the wallet's real on-chain USDC (chain truth),
same source as /api/live/portfolio.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from api import db
from sizing import compute_unrealized_pnl

log = logging.getLogger(__name__)
router = APIRouter()


async def _chain_cash() -> float:
    try:
        from live_execution import solana, wallet
        payer = wallet.load_keypair()
        bal = await solana.get_usdc_balance(wallet.pubkey_string(payer))
        return max(bal, 0.0) if bal is not None else 0.0
    except Exception:
        return 0.0


@router.get("/api/holdings")
async def get_holdings(request: Request):
    provider = request.app.state.provider
    async with db.get_db() as conn:
        trades = await db.get_open_trades(conn)
    cash = await _chain_cash()

    holdings = []
    for t in trades:
        try:
            # Decimals from the entry snapshot are REQUIRED for a correct
            # execution-price quote (wrong decimals fabricate prices).
            decimals = (t.candidate_snapshot or {}).get("decimals")
            price = await provider.get_current_price(t.mint_address, decimals)
        except Exception as exc:
            log.warning("holdings: price unavailable for %s: %s", t.symbol, exc)
            price = None
        if price is not None:
            try:
                pnl_usd, pnl_pct = compute_unrealized_pnl(t, price)
            except ValueError:
                pnl_usd = pnl_pct = None
        else:
            pnl_usd = pnl_pct = None
        holdings.append({
            "trade_id": t.trade_id,
            "symbol": t.symbol,
            "mint_address": t.mint_address,
            "opened_at": t.opened_at,
            "entry_price_usd": t.entry_price_usd,
            "position_size_usd": t.position_size_usd,
            "quantity": t.quantity,
            "thesis": t.thesis,
            "current_price_usd": price,
            "unrealized_pnl_usd": round(pnl_usd, 4) if pnl_usd is not None else None,
            "unrealized_pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
        })
    return {"cash_usd": cash, "open_positions": holdings,
            "count": len(holdings),
            "paper_trading_only": False}
