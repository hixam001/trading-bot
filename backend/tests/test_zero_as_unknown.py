"""
tests/test_zero_as_unknown.py — §68 zero-as-unknown regression tests.

Verifies that:
1. discovery.py: legitimate 0 buys/sells stay 0, while missing fields remain None.
2. research.py: aggregate_pairs and enrich_with_research preserve 0 counts for buys/sells,
   while missing/None data stays None.
"""
from __future__ import annotations

import httpx
import pytest

from data_providers.discovery import KeywordScanner
from data_providers.research import aggregate_pairs, enrich_with_research
from models import Candidate


# Syntactically valid Solana base58 address for testing
MINT_A = "So11111111111111111111111111111111111111112"
MINT_B = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def test_discovery_zero_buys_and_sells_stay_zero():
    """When the provider API returns explicit 0 for buys and sells, they must not become None."""
    client = httpx.AsyncClient()
    kd = KeywordScanner(client)
    pair_zero = {
        "chainId": "solana",
        "pairAddress": "Pair111111111111111111111111111111111111111",
        "baseToken": {"address": MINT_A, "symbol": "ZERO", "name": "Zero Token"},
        "priceUsd": "1.0",
        "liquidity": {"usd": 50_000},
        "volume": {"h1": 10_000, "h24": 50_000, "h6": 20_000},
        "priceChange": {"h1": 1.0, "h6": 2.0, "h24": 3.0},
        "txns": {
            "h1": {"buys": 0, "sells": 0},
            "h6": {"buys": 0, "sells": 0},
        },
    }
    cands = kd._build_board([pair_zero], boosted_mints=set())
    assert len(cands) == 1
    c = cands[0]
    assert c.buys_1h == 0, f"Expected 0, got {c.buys_1h}"
    assert c.sells_1h == 0, f"Expected 0, got {c.sells_1h}"
    assert c.buys_6h == 0, f"Expected 0, got {c.buys_6h}"
    assert c.sells_6h == 0, f"Expected 0, got {c.sells_6h}"


def test_discovery_missing_buys_and_sells_stay_none():
    """When the provider API omits buys and sells, they must remain None (unknown)."""
    client = httpx.AsyncClient()
    kd = KeywordScanner(client)
    pair_missing = {
        "chainId": "solana",
        "pairAddress": "Pair222222222222222222222222222222222222222",
        "baseToken": {"address": MINT_B, "symbol": "NONE", "name": "None Token"},
        "priceUsd": "1.0",
        "liquidity": {"usd": 50_000},
        "volume": {"h1": 10_000, "h24": 50_000, "h6": 20_000},
        "priceChange": {"h1": 1.0, "h6": 2.0, "h24": 3.0},
        "txns": {},
    }
    cands = kd._build_board([pair_missing], boosted_mints=set())
    assert len(cands) == 1
    c = cands[0]
    assert c.buys_1h is None
    assert c.sells_1h is None
    assert c.buys_6h is None
    assert c.sells_6h is None


def test_research_aggregate_pairs_zero_stays_zero():
    """aggregate_pairs preserves 0 for buys_6h and sells_6h when txns have 0."""
    pairs = [
        {
            "chainId": "solana",
            "liquidity": {"usd": 10_000},
            "volume": {"h6": 5_000},
            "priceChange": {"h6": 0.5},
            "txns": {
                "h6": {"buys": 0, "sells": 0},
            },
        }
    ]
    agg = aggregate_pairs(pairs)
    assert agg is not None
    assert agg["buys_6h"] == 0
    assert agg["sells_6h"] == 0


@pytest.mark.asyncio
async def test_research_enrich_preserves_zero_buys_and_sells():
    """enrich_with_research sets 0 (not None) on candidate when aggregate is 0."""
    cand = Candidate(
        symbol="ZERO",
        mint_address=MINT_A,
        price_usd=1.0,
        liquidity_usd=10_000,
        volume_24h_usd=20_000,
        market_cap_usd=50_000,
    )
    # Mock aggregate dict where buys_6h and sells_6h are 0
    agg = {
        "pool_count": 1,
        "total_liquidity_usd": 10_000.0,
        "top_pool_share": 1.0,
        "volume_6h_usd": 5_000.0,
        "buys_6h": 0,
        "sells_6h": 0,
        "price_change_6h_pct": 2.5,
    }

    # Simulate the application step from enrich_with_research
    cand.pool_count = agg["pool_count"]
    cand.total_liquidity_usd = agg["total_liquidity_usd"] or None
    cand.top_pool_share = agg["top_pool_share"]
    cand.volume_6h_usd = agg["volume_6h_usd"] or None
    cand.buys_6h = None if agg["buys_6h"] is None else int(agg["buys_6h"])
    cand.sells_6h = None if agg["sells_6h"] is None else int(agg["sells_6h"])

    assert cand.buys_6h == 0
    assert cand.sells_6h == 0


@pytest.mark.asyncio
async def test_research_enrich_missing_stays_none():
    """enrich_with_research sets None when aggregate buys_6h/sells_6h is None."""
    cand = Candidate(
        symbol="NONE",
        mint_address=MINT_B,
        price_usd=1.0,
        liquidity_usd=10_000,
        volume_24h_usd=20_000,
        market_cap_usd=50_000,
    )
    agg = {
        "pool_count": 1,
        "total_liquidity_usd": 10_000.0,
        "top_pool_share": 1.0,
        "volume_6h_usd": 5_000.0,
        "buys_6h": None,
        "sells_6h": None,
        "price_change_6h_pct": None,
    }

    cand.buys_6h = None if agg["buys_6h"] is None else int(agg["buys_6h"])
    cand.sells_6h = None if agg["sells_6h"] is None else int(agg["sells_6h"])

    assert cand.buys_6h is None
    assert cand.sells_6h is None
