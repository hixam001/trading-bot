"""Finite provider facts, nested-shape isolation and deterministic gate guards."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import math

import pytest

from data_providers import research
from models import Candidate, PortfolioState
from rule_engine.gate import evaluate_gate
from rule_engine import rules


def candidate(**changes):
    fields = dict(symbol="DUMMY", mint_address="dummy-mint", price_usd=1.,
                  liquidity_usd=100_000., volume_24h_usd=100_000.,
                  market_cap_usd=100_000., volume_1h_usd=100_000.,
                  buys_1h=20, sells_1h=10, age_hours=48.,
                  price_change_1h_pct=0., fomo_heat=36)
    fields.update(changes)
    return Candidate(**fields)


def pair(**changes):
    fields = dict(chainId="solana", liquidity={"usd": 100.}, volume={"h6": 10.},
                  txns={"h6": {"buys": 2, "sells": 1}}, priceChange={"h6": 0.})
    fields.update(changes)
    return fields


@pytest.mark.parametrize("value", [True, False, float("nan"), float("inf"), float("-inf"), "NaN", "Infinity", {}, [], 10**1000])
def test_nonfinite_or_malformed_provider_numbers_unavailable(value):
    assert research._f(value) is None
    assert research._parse_count(value) is None


@pytest.mark.parametrize("value", [0, 0., "0", "0.0"])
def test_provider_zero_is_a_fact(value):
    assert research._f(value) == 0.
    assert research._parse_count(value) == 0


@pytest.mark.parametrize("value", [1.5, "1.5", -1, "-1"])
def test_counts_are_nonnegative_integral(value):
    assert research._parse_count(value) is None


@pytest.mark.parametrize("nested", [None, [], "not-an-object", True, 4])
def test_malformed_nested_pools_preserve_valid_siblings(nested):
    malformed = pair(liquidity=nested, volume=nested, txns=nested, priceChange=nested)
    aggregate = research.aggregate_pairs([None, [], "listing", malformed, pair()])
    assert aggregate["buys_6h"] == 2
    assert aggregate["sells_6h"] == 1
    assert aggregate["pool_count"] == 2
    assert aggregate["total_liquidity_usd"] is None
    assert aggregate["volume_6h_usd"] is None
    assert aggregate["top_pool_share"] is None
    assert aggregate["price_change_6h_pct"] is None


def test_overflowing_total_does_not_become_infinity():
    aggregate = research.aggregate_pairs([pair(liquidity={"usd": 1e308}, volume={"h6": 1e308})] * 2)
    assert aggregate["total_liquidity_usd"] is None
    assert aggregate["volume_6h_usd"] is None
    assert aggregate["top_pool_share"] is None


@pytest.mark.parametrize("value", [None, {}, "pairs", 12, True])
def test_malformed_collection_is_unavailable(value):
    assert research.aggregate_pairs(value) is None


async def test_enrichment_preserves_zero_and_isolates_bad_candidate(monkeypatch):
    malformed = candidate(mint_address="bad")
    valid = candidate(mint_address="good", price_change_6h_pct=None)
    async def fetch(client, mint):
        return {"not": "a collection"} if mint == "bad" else [pair(
            liquidity={"usd": 0}, volume={"h6": 0},
            txns={"h6": {"buys": 0, "sells": 0}})]
    monkeypatch.setattr(research, "_fetch_pairs", fetch)
    assert await research.enrich_with_research([malformed, valid], client=object(), limit=2) == 1
    assert malformed.pool_count is None
    assert valid.total_liquidity_usd == 0
    assert valid.volume_6h_usd == 0
    assert valid.buys_6h == 0 and valid.sells_6h == 0
    assert valid.top_pool_share is None  # 0/0 is not a measured concentration
    assert valid.price_change_6h_pct == 0


@pytest.mark.parametrize("field,rule", [
    ("liquidity_usd", rules.liquidity_floor),
    ("volume_1h_usd", rules.volume_alive),
    ("buys_1h", rules.buy_pressure),
    ("sells_1h", rules.buy_pressure),
    ("age_hours", rules.not_newborn_fade),
    ("price_change_1h_pct", rules.not_newborn_fade),
    ("fomo_heat", rules.crowd_heat),
])
@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), float("-inf"), 10**1000])
def test_gate_cannot_authorize_invalid_numeric_fact(field, rule, value):
    decision = evaluate_gate(candidate(**{field: value}), PortfolioState(cash_usd=100.),
                             SimpleNamespace(), rules=[rule])
    assert decision.all_passed is False
    assert decision.rules[0].value is None
    assert "unavailable" in decision.rules[0].detail


@pytest.mark.parametrize("cash", [True, float("nan"), float("inf"), 10**1000])
def test_invalid_cash_cannot_authorize_entry(cash):
    result = rules.cash_available(candidate(), PortfolioState(cash_usd=cash), SimpleNamespace())
    assert not result.passed and result.value is None


def test_unknown_security_policy_and_legitimate_zero_momentum_unchanged():
    c = candidate(age_hours=48., price_change_1h_pct=0.,
                  mint_authority_revoked=None, freeze_authority_revoked=None,
                  is_likely_honeypot=None)
    assert rules.security_clear(c, PortfolioState(cash_usd=100.), SimpleNamespace()).passed
    assert rules.not_newborn_fade(c, PortfolioState(cash_usd=100.), SimpleNamespace()).passed
