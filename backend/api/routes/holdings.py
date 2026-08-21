"""
api/routes/holdings.py — GET /api/holdings

Returns current open positions with live unrealized P&L.
Price fetches are capped at PRICE_FETCH_TIMEOUT_S — if the price API is
unreachable or slow, holdings still load immediately with null P&L rather
than blocking the whole request for 30+ seconds.
"""
from __future__ import annotations

import asyncio
import logging

import data_ingestion
import paper_trading_engine as engine
from api import db
from fastapi import APIRouter

log = logging.getLogger(__name__)
router = APIRouter()

# Hard cap per-position price fetch: never let one slow DNS/network call
# block the whole holdings response. P&L shows as null instead.
PRICE_FETCH_TIMEOUT_S = 3.0


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

    async def fetch_price(trade):
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
            current_price = await asyncio.wait_for(
                data_ingestion.get_current_price(trade.mint_address),
                timeout=PRICE_FETCH_TIMEOUT_S,
            )
            pnl_usd, pnl_pct = engine.compute_unrealized_pnl(trade, current_price)
            holding["current_price_usd"] = current_price
            holding["unrealized_pnl_usd"] = round(pnl_usd, 4)
            holding["unrealized_pnl_pct"] = round(pnl_pct, 2)
        except asyncio.TimeoutError:
            log.warning("Price fetch timed out for %s (>%.0fs) — showing null P&L",
                        trade.symbol, PRICE_FETCH_TIMEOUT_S)
        except data_ingestion.PriceUnavailableError as exc:
            log.warning("Price unavailable for %s: %s", trade.symbol, exc)
        except ValueError as exc:
            log.error("P&L computation error for trade %s: %s", trade.trade_id, exc)
        return holding

    holdings = await asyncio.gather(*(fetch_price(t) for t in open_trades))

    return {
        "holdings": holdings,
        "open_count": len(holdings),
        "cash_balance_usd": round(cash, 4),
    }
