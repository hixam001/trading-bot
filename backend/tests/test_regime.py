"""
tests/test_regime.py — MarketRegime computed from candidate batches (C1).
"""
from __future__ import annotations

from rule_engine.regime import MarketRegime, compute_market_regime
from tests.test_rules import make_candidate


def test_regime_empty_batch_is_bad_regime():
    r = compute_market_regime([])
    assert r.regime_ok is False
    assert "no candidates" in r.regime_detail


def test_regime_healthy_batch():
    # Mixed batch: 50% green, healthy median volume — within both bounds.
    batch = [make_candidate(price_change_1h_pct=5.0 if i % 2 == 0 else -2.0) for i in range(10)]
    r = compute_market_regime(batch)
    assert r.pct_candidates_green_1h == 0.5
    assert r.median_volume_1h_usd == 20_000.0
    assert r.avg_buy_sell_ratio == 1.5  # 300/200
    assert r.regime_ok is True


def test_regime_too_broadly_green_fails():
    # > REGIME_MAX_PCT_GREEN fraction green — broad pump smell
    batch = [make_candidate(price_change_1h_pct=10.0 if i < 9 else -10.0) for i in range(10)]
    r = compute_market_regime(batch)
    assert r.pct_candidates_green_1h == 0.9
    assert r.regime_ok is False


def test_regime_thin_median_volume_fails():
    batch = [make_candidate(volume_1h_usd=1_000.0) for _ in range(5)]
    r = compute_market_regime(batch)
    assert r.median_volume_1h_usd == 1_000.0
    assert r.regime_ok is False


def test_regime_is_frozen_dataclass():
    r = compute_market_regime([make_candidate()])
    try:
        r.regime_ok = True  # type: ignore[misc]
        raised = False
    except Exception:
        raised = True
    assert raised
