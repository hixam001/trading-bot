"""
tests/test_zero_as_unknown.py — §68/§69 zero-as-unknown regression tests.

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


def test_research_aggregate_pairs_cases():
    """aggregate_pairs handles all four required cases for buys_6h and sells_6h:
    (a) all pairs report buys=0 -> 0
    (b) no pair has a txns field -> None
    (c) mixed pairs (one reports, one doesn't) -> sum of the reporting ones
    (d) h6 txns present but buys key missing -> None
    """
    # (a) all pairs report 0
    pairs_a = [
        {"chainId": "solana", "liquidity": {"usd": 10_000}, "volume": {"h6": 100}, "txns": {"h6": {"buys": 0, "sells": 0}}},
        {"chainId": "solana", "liquidity": {"usd": 5_000}, "volume": {"h6": 50}, "txns": {"h6": {"buys": 0, "sells": 0}}},
    ]
    agg_a = aggregate_pairs(pairs_a)
    assert agg_a is not None
    assert agg_a["buys_6h"] == 0
    assert agg_a["sells_6h"] == 0

    # (b) no pair has a txns field
    pairs_b = [
        {"chainId": "solana", "liquidity": {"usd": 10_000}, "volume": {"h6": 100}},
        {"chainId": "solana", "liquidity": {"usd": 5_000}, "volume": {"h6": 50}},
    ]
    agg_b = aggregate_pairs(pairs_b)
    assert agg_b is not None
    assert agg_b["buys_6h"] is None
    assert agg_b["sells_6h"] is None

    # (c) mixed pairs (one reports, one doesn't)
    pairs_c = [
        {"chainId": "solana", "liquidity": {"usd": 10_000}, "volume": {"h6": 100}, "txns": {"h6": {"buys": 7, "sells": 3}}},
        {"chainId": "solana", "liquidity": {"usd": 5_000}, "volume": {"h6": 50}},
    ]
    agg_c = aggregate_pairs(pairs_c)
    assert agg_c is not None
    assert agg_c["buys_6h"] == 7
    assert agg_c["sells_6h"] == 3

    # (d) h6 txns present but buys key missing
    pairs_d = [
        {"chainId": "solana", "liquidity": {"usd": 10_000}, "volume": {"h6": 100}, "txns": {"h6": {"unrelated": 1}}},
    ]
    agg_d = aggregate_pairs(pairs_d)
    assert agg_d is not None
    assert agg_d["buys_6h"] is None
    assert agg_d["sells_6h"] is None


def _stub_client(pairs_by_mint: dict[str, list]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        mint = request.url.path.rsplit("/", 1)[-1]
        pairs = pairs_by_mint.get(mint, [])
        return httpx.Response(200, json={"pairs": pairs})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_research_enrich_preserves_zero_buys_and_sells():
    """enrich_with_research preserves explicit 0 counts (case a) and sums mixed pairs (case c)."""
    cand_zero = Candidate(
        symbol="ZERO",
        mint_address=MINT_A,
        price_usd=1.0,
        liquidity_usd=10_000,
        volume_24h_usd=20_000,
        market_cap_usd=50_000,
    )
    cand_mixed = Candidate(
        symbol="MIXED",
        mint_address=MINT_B,
        price_usd=1.0,
        liquidity_usd=10_000,
        volume_24h_usd=20_000,
        market_cap_usd=50_000,
    )

    # Case (a): all pairs report buys=0, sells=0
    pairs_zero = [
        {"chainId": "solana", "liquidity": {"usd": 10_000}, "volume": {"h6": 5_000}, "txns": {"h6": {"buys": 0, "sells": 0}}},
        {"chainId": "solana", "liquidity": {"usd": 5_000}, "volume": {"h6": 1_000}, "txns": {"h6": {"buys": 0, "sells": 0}}},
    ]
    # Case (c): mixed pairs (one reports, one doesn't)
    pairs_mixed = [
        {"chainId": "solana", "liquidity": {"usd": 10_000}, "volume": {"h6": 5_000}, "txns": {"h6": {"buys": 6, "sells": 2}}},
        {"chainId": "solana", "liquidity": {"usd": 5_000}, "volume": {"h6": 1_000}},
    ]

    async with _stub_client({MINT_A: pairs_zero, MINT_B: pairs_mixed}) as client:
        applied = await enrich_with_research([cand_zero, cand_mixed], client=client)

    assert applied == 2
    # Case (a): 0 must stay 0, not None
    assert cand_zero.buys_6h == 0
    assert cand_zero.sells_6h == 0
    # Case (c): sum of reporting pairs
    assert cand_mixed.buys_6h == 6
    assert cand_mixed.sells_6h == 2


@pytest.mark.asyncio
async def test_research_enrich_missing_stays_none():
    """enrich_with_research leaves buys_6h/sells_6h as None for missing txns (case b) or missing keys (case d)."""
    cand_notxns = Candidate(
        symbol="NOTXNS",
        mint_address=MINT_A,
        price_usd=1.0,
        liquidity_usd=10_000,
        volume_24h_usd=20_000,
        market_cap_usd=50_000,
    )
    cand_missing_keys = Candidate(
        symbol="NOKEYS",
        mint_address=MINT_B,
        price_usd=1.0,
        liquidity_usd=10_000,
        volume_24h_usd=20_000,
        market_cap_usd=50_000,
    )

    # Case (b): no pair has a txns field
    pairs_notxns = [
        {"chainId": "solana", "liquidity": {"usd": 10_000}, "volume": {"h6": 5_000}},
        {"chainId": "solana", "liquidity": {"usd": 5_000}, "volume": {"h6": 1_000}},
    ]
    # Case (d): h6 txns present but buys/sells keys missing
    pairs_missing_keys = [
        {"chainId": "solana", "liquidity": {"usd": 10_000}, "volume": {"h6": 5_000}, "txns": {"h6": {"volume": 100}}},
    ]

    async with _stub_client({MINT_A: pairs_notxns, MINT_B: pairs_missing_keys}) as client:
        applied = await enrich_with_research([cand_notxns, cand_missing_keys], client=client)

    assert applied == 2
    # Case (b): missing txns stays None
    assert cand_notxns.buys_6h is None
    assert cand_notxns.sells_6h is None
    # Case (d): missing buys/sells key in h6 stays None
    assert cand_missing_keys.buys_6h is None
    assert cand_missing_keys.sells_6h is None
