"""
tests/live_decimals.py — mirror of backend/tests/test_jupiter_decimals.py for
live_execution's quote/pricing path.

Same fail-closed structure as the paper-side decimals regression tests, plus
IDENTITY assertions proving live_execution imports (never re-implements) the
shared unit math from data_providers.jupiter — so the two sides cannot drift.
"""
from __future__ import annotations

import asyncio

import pytest

from live_execution import jupiter_executor as je


# --- identity parity: one implementation codebase-wide -------------------------

def test_unit_math_is_imported_not_reimplemented():
    import sys
    backend_mod = sys.modules.get("data_providers.jupiter")
    if backend_mod is None:                      # direct import fallback
        import data_providers.jupiter as backend_mod
    assert je.raw_units_for_one_token is backend_mod.raw_units_for_one_token


def test_quote_url_imported_from_backend_config():
    import config as backend_config
    assert je._BACKEND_QUOTE_URL is backend_config.JUPITER_QUOTE_URL
    assert backend_config.JUPITER_QUOTE_URL == \
        "https://lite-api.jup.ag/swap/v1/quote"


# --- raw units (same assertions as the paper-side tests) -----------------------

def test_raw_units_six_decimals_is_one_million_not_a_billion():
    assert je.raw_units_for_one_token(6) == 1_000_000


def test_raw_units_nine_decimals_is_a_billion():
    assert je.raw_units_for_one_token(9) == 1_000_000_000


def test_raw_units_negative_raises():
    with pytest.raises(ValueError):
        je.raw_units_for_one_token(-1)


def test_raw_units_zero_is_valid():
    assert je.raw_units_for_one_token(0) == 1


# --- quote math: decimals-aware, fail-closed ------------------------------------

def test_quote_six_decimal_token_math(env_quote):
    # BARRON-like: $10 of a 6-decimal token priced at $14.47/token.
    env_quote.response = {"outAmount": str(int(0.691 * 1e6))}   # 691000 raw
    q = asyncio.run(je.get_jupiter_quote("MINT", 6, 10.0))
    assert q["tokens_out"] == pytest.approx(0.691)
    assert q["price_usd"] == pytest.approx(14.4718, rel=1e-3)


def test_quote_nine_decimal_token_math(env_quote):
    # SOL-like: $47.22 buys exactly 0.5 tokens at $94.44 -> out = 5e8 raw.
    env_quote.response = {"outAmount": "500000000"}
    q = asyncio.run(je.get_jupiter_quote("MINT", 9, 47.22))     # in-cap size
    assert q["tokens_out"] == pytest.approx(0.5)
    assert q["price_usd"] == pytest.approx(94.44)


async def test_get_quote_fails_closed_without_decimals(env_quote, monkeypatch):
    """Unknown decimals must refuse to quote — never guess 9."""
    def _boom(*a, **k):
        raise AssertionError("network call attempted after refusal!")
    monkeypatch.setattr(je, "_get_json", _boom)
    with pytest.raises(je.Refusal, match="UNKNOWN decimals"):
        await je.get_jupiter_quote(
            "SomeMint1111111111111111111111111111111111", None, 10.0)


@pytest.fixture
def env_quote(monkeypatch, tmp_path):
    """
    Minimal armed environment + captured-quote hook. The fixture object gets
    a mutable `response` attribute that fake _get_json returns for quotes.
    """
    import live_execution.config as le_config

    monkeypatch.setattr(le_config, "LIVE_TRADING_ENABLED", True)
    monkeypatch.setattr(le_config, "REQUIRE_MANUAL_CONFIRMATION", False)
    monkeypatch.setattr(le_config, "STATE_DIR", tmp_path)

    class Holder:
        response: dict = {}

    holder = Holder()

    async def fake(url, params):
        if url.endswith("/quote"):
            return holder.response
        raise AssertionError(f"unexpected call to {url}")

    monkeypatch.setattr(je, "_get_json", fake)
    return holder