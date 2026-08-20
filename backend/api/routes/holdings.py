"""
api/routes/holdings.py — GET /api/holdings

Returns current open positions with live unrealized P&L.
"""
from __future__ import annotations

import logging

import data_ingestion
import paper_trading_engine as engine
from api import db
from fastapi import APIRouter

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/holdings")
async def get_holdings():
    """
    FR-7: Current open positions with live unrealized P&L.

    For each open position, fetches the current price and computes
    unrealized P&L. Price fetch failures are handled gracefully —
    the position is still returned but with pnl_usd/pnl_pct = null.
    """
    async with db.get_db() as conn:
        open_trades = await db.get_open_trades(conn)
        cash = await db.get_cash_balance(conn)

    holdings = []
    for trade in open_trades:
        holding: dict = {
            "trade_id": trade.trade_id,
            "symbol": trade.symbol,
            "mint_address": trade.mint_address,
            "opened_at": trade.opened_at,
            "entry_price_usd": trade.entry_price_usd,
            "position_size_usd": trade.position_size_usd,
            "quantity": trade.quantity,
            "invalidation_condition": trade.invalidation_condition,
            "thesis": trade.verdict_snapshot.get("thesis", ""),
            "current_price_usd": None,
            "unrealized_pnl_usd": None,
            "unrealized_pnl_pct": None,
        }
        try:
            current_price = await data_ingestion.get_current_price(trade.mint_address)
            pnl_usd, pnl_pct = engine.compute_unrealized_pnl(trade, current_price)
            holding["current_price_usd"] = current_price
            holding["unrealized_pnl_usd"] = round(pnl_usd, 4)
            holding["unrealized_pnl_pct"] = round(pnl_pct, 2)
        except data_ingestion.PriceUnavailableError as exc:
            log.warning("Price unavailable for %s: %s", trade.symbol, exc)
        except ValueError as exc:
            log.error("P&L computation error for trade %s: %s", trade.trade_id, exc)

        holdings.append(holding)

    return {
        "holdings": holdings,
        "open_count": len(holdings),
        "cash_balance_usd": round(cash, 4),
    }
