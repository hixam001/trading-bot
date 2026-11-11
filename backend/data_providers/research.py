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
import math
from typing import Optional

import httpx
from bounded_http import bounded_request

import config
from models import Candidate

log = logging.getLogger(__name__)

RESEARCH_URL = config.DEXSCREENER_BASE_URL + "/latest/dex/tokens/"


def _f(value) -> Optional[float]:
    """A finite provider number, or unavailable; zero is a real observation."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _parse_count(value) -> Optional[int]:
    number = _f(value)
    if number is None or number < 0 or not number.is_integer():
        return None
    return int(number)


def _nested(pair: dict, key: str) -> dict:
    value = pair.get(key)
    return value if isinstance(value, dict) else {}


def _liq_of(pair: dict) -> Optional[float]:
    value = _f(_nested(pair, "liquidity").get("usd"))
    return value if value is not None and value >= 0 else None


def _complete_sum(values) -> Optional[float]:
    if any(value is None for value in values):
        return None
    try:
        total = math.fsum(values)
    except (OverflowError, ValueError):
        return None
    return total if math.isfinite(total) else None


def aggregate_pairs(pairs: list) -> Optional[dict]:
    """Aggregate Solana pools without fabricating unreadable facts.

    Total liquidity/volume are available only with complete finite coverage.
    Counts retain the existing sum-of-reporting-pools policy; missing and bad
    records contribute no alleged zero. No useful sibling is discarded.
    """
    if not isinstance(pairs, list):
        return None
    solana = [p for p in pairs if isinstance(p, dict) and p.get("chainId") == "solana"]
    if not solana:
        return None
    liquidities = [_liq_of(p) for p in solana]
    total_liq = _complete_sum(liquidities)
    known_pools = [(p, liq) for p, liq in zip(solana, liquidities) if liq is not None]
    deepest, deepest_liq = max(known_pools, key=lambda item: item[1]) if known_pools else (None, None)
    volumes = [_f(_nested(p, "volume").get("h6")) for p in solana]
    vol6h = _complete_sum([value if value is not None and value >= 0 else None for value in volumes])
    buys_reported: list[int] = []
    sells_reported: list[int] = []
    for pair in solana:
        h6 = _nested(pair, "txns").get("h6")
        if not isinstance(h6, dict):
            continue
        buys = _parse_count(h6.get("buys"))
        sells = _parse_count(h6.get("sells"))
        if buys is not None:
            buys_reported.append(buys)
        if sells is not None:
            sells_reported.append(sells)
    # Unknown liquidity cannot establish which pool is deepest.
    change = (_f(_nested(deepest, "priceChange").get("h6"))
              if deepest is not None and total_liq is not None else None)
    return {
        "pool_count": len(solana),
        "total_liquidity_usd": total_liq,
        "top_pool_share": (deepest_liq / total_liq) if total_liq is not None and total_liq > 0 else None,
        "volume_6h_usd": vol6h,
        "buys_6h": sum(buys_reported) if buys_reported else None,
        "sells_6h": sum(sells_reported) if sells_reported else None,
        "price_change_6h_pct": change,
    }


async def _fetch_pairs(client: httpx.AsyncClient, mint: str) -> Optional[list]:
    try:
        resp = await bounded_request(client, "GET", RESEARCH_URL + mint, headers={"accept": "application/json"})
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
        cand.total_liquidity_usd = agg["total_liquidity_usd"]
        cand.top_pool_share = agg["top_pool_share"]
        cand.volume_6h_usd = agg["volume_6h_usd"]
        cand.buys_6h = None if agg["buys_6h"] is None else int(agg["buys_6h"])
        cand.sells_6h = None if agg["sells_6h"] is None else int(agg["sells_6h"])
        if cand.price_change_6h_pct is None:
            cand.price_change_6h_pct = agg["price_change_6h_pct"]
        applied += 1
    return applied
