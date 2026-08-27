"""data_providers/research.py - second-pass cross-pool token research.

Port of the reference bot researchToken: for the names the loop actually cares about,
one extra Dexscreener call aggregates EVERY Solana pool - pool count, total
liquidity, top-pool share (concentration risk) and 6h windows summed across
pools. Fills fields the deepest-pair snapshot missed; a failed research leaves
every field untouched (None semantics preserved, fail-soft like every feed).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

import config
from models import Candidate

log = logging.getLogger(__name__)

RESEARCH_URL = config.DEXSCREENER_BASE_URL + "/latest/dex/tokens/"


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _i(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _liq_of(pair: dict) -> float:
    return _f((pair.get("liquidity") or {}).get("usd"))


def aggregate_pairs(pairs: list) -> Optional[dict]:
    """Pure aggregation over Dexscreener pairs for ONE mint (unit-testable).

    Returns None when no Solana pair exists - never fabricates defaults."""
    solana = [p for p in pairs if isinstance(p, dict) and p.get("chainId") == "solana"]
    if not solana:
        return None
    total_liq = sum(_liq_of(p) for p in solana)
    deepest = max(solana, key=_liq_of)
    vol6h = sum(_f((p.get("volume") or {}).get("h6")) for p in solana)
    buys6h = sum(_i(((p.get("txns") or {}).get("h6") or {}).get("buys")) for p in solana)
    sells6h = sum(_i(((p.get("txns") or {}).get("h6") or {}).get("sells")) for p in solana)
    try:
        chg6h: Optional[float] = float(((deepest.get("priceChange") or {}).get("h6")))
    except (TypeError, ValueError):
        chg6h = None
    return {
        "pool_count": len(solana),
        "total_liquidity_usd": total_liq,
        "top_pool_share": (_liq_of(deepest) / total_liq) if total_liq > 0 else 1.0,
        "volume_6h_usd": vol6h,
        "buys_6h": buys6h,
        "sells_6h": sells6h,
        "price_change_6h_pct": chg6h,
    }


async def _fetch_pairs(client: httpx.AsyncClient, mint: str) -> Optional[list]:
    try:
        resp = await client.get(RESEARCH_URL + mint, headers={"accept": "application/json"})
        if resp.status_code != 200:
            return None
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    pairs = data.get("pairs") if isinstance(data, dict) else None
    return pairs if isinstance(pairs, list) else None


async def enrich_with_research(
    candidates: list[Candidate],
    client: Optional[httpx.AsyncClient] = None,
    limit: Optional[int] = None,
    max_concurrency: int = 4,
) -> int:
    """Research the head of the board (the reference researches the names it cares about).

    Always sets the cross-pool aggregates; fills a missing single-pair field
    from the aggregate without overwriting fresher values. Returns count.
    """
    picks = candidates[: (limit if limit is not None else config.RESEARCH_PER_TICK)]
    if not picks:
        return 0
    own_client = client is None
    client = client or httpx.AsyncClient()
    sem = asyncio.Semaphore(max_concurrency)

    async def one(c: Candidate) -> Optional[dict]:
        async with sem:
            pairs = await _fetch_pairs(client, c.mint_address)
        return aggregate_pairs(pairs) if pairs else None

    try:
        results = await asyncio.gather(*(one(c) for c in picks))
    finally:
        if own_client:
            await client.aclose()

    applied = 0
    for cand, agg in zip(picks, results):
        if not agg:
            continue
        cand.pool_count = agg["pool_count"]
        cand.total_liquidity_usd = agg["total_liquidity_usd"] or None
        cand.top_pool_share = agg["top_pool_share"]
        cand.volume_6h_usd = agg["volume_6h_usd"] or None
        cand.buys_6h = agg["buys_6h"] or None
        cand.sells_6h = agg["sells_6h"] or None
        if cand.price_change_6h_pct is None:
            cand.price_change_6h_pct = agg["price_change_6h_pct"]
        applied += 1
    return applied
