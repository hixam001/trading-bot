"""
tests/test_jupiter_decimals.py — regression tests for the BARRON 96k%-P&L bug.

Root cause: Jupiter was quoted with amount=10**9 raw units for EVERY mint
(assuming 9 decimals). A 6-decimal token like BARRON therefore sold 1000
tokens while the result was recorded as the price of ONE token — a 1000x
price fabrication that take-profit turned into ~+96,400% realized P&L.
These tests pin the decimals-aware quoting math and the fail-closed guard.
"""
from __future__ import annotations

import pytest

from data_providers.base import ProviderError
from data_providers.jupiter import price_from_quote, raw_units_for_one_token


def test_raw_units_six_decimals_is_one_million_not_a_billion():
    assert raw_units_for_one_token(6) == 1_000_000


def test_raw_units_nine_decimals_is_a_billion():
    assert raw_units_for_one_token(9) == 1_000_000_000


def test_raw_units_negative_raises():
    with pytest.raises(ValueError):
        raw_units_for_one_token(-1)


def test_price_from_quote_six_decimal_token():
    # BARRON-like: 6 decimals, real price $0.000691. One token -> 691 raw
    # USDC (USDC itself has 6 decimals).
    assert price_from_quote("691", 6) == pytest.approx(0.000691)


def test_price_from_quote_nine_decimal_token():
    # SOL: 9 decimals, $94.44 -> out = 94.44 * 1e6 raw USDC.
    assert price_from_quote("94440000", 9) == pytest.approx(94.44)


def test_price_from_quote_rejects_garbage():
    with pytest.raises(ProviderError):
        price_from_quote("not-a-number", 6)


async def test_get_current_price_fails_closed_without_decimals():
    """Unknown decimals must refuse to quote — never guess 9."""
    from data_providers.jupiter import JupiterProvider
    j = JupiterProvider()
    with pytest.raises(ProviderError, match="decimals unknown"):
        await j.get_current_price("SomeMint1111111111111111111111111111111111", None)


async def test_get_current_price_uses_in_memory_cache(monkeypatch):
    """Subsequent requests within cache TTL return cached price without network fetch."""
    from data_providers import jupiter as jup_mod
    j = jup_mod.JupiterProvider(cache_ttl_seconds=2.0)
    calls = []

    async def fake_fetch_json(*args, **kwargs):
        calls.append(len(calls) + 1)
        return {"outAmount": "1000000"}  # 1 USDC for 1 token

    monkeypatch.setattr(jup_mod, "fetch_json", fake_fetch_json)

    p1 = await j.get_current_price("TestMint1111111111111111111111111111111111", 6)
    assert p1 == 1.0
    assert len(calls) == 1

    # Second call immediately should hit cache
    p2 = await j.get_current_price("TestMint1111111111111111111111111111111111", 6)
    assert p2 == 1.0
    assert len(calls) == 1


async def test_get_current_price_cache_expires(monkeypatch):
    """Requests after TTL expires re-fetch from network."""
    from data_providers import jupiter as jup_mod
    j = jup_mod.JupiterProvider(cache_ttl_seconds=0.05)
    calls = []

    async def fake_fetch_json(*args, **kwargs):
        calls.append(len(calls) + 1)
        return {"outAmount": "1000000"}

    monkeypatch.setattr(jup_mod, "fetch_json", fake_fetch_json)

    p1 = await j.get_current_price("TestMint1111111111111111111111111111111111", 6)
    assert p1 == 1.0
    assert len(calls) == 1

    import asyncio
    await asyncio.sleep(0.06)

    p2 = await j.get_current_price("TestMint1111111111111111111111111111111111", 6)
    assert p2 == 1.0
    assert len(calls) == 2

