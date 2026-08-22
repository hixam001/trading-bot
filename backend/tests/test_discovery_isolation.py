"""
tests/test_discovery_isolation.py — Task A guard rails.

discovery_source is an OBSERVABILITY-ONLY field: the rule engine must never
read it, and gate outcomes must be identical regardless of its value.
"""
from __future__ import annotations

import inspect

from rule_engine.gate import evaluate_gate
from rule_engine.rules import ACTIVE_RULES
from tests.test_rules import make_candidate, make_portfolio, make_regime


def test_no_rule_references_discovery_source():
    """No rule function's source may mention discovery_source (A.4)."""
    for fn in ACTIVE_RULES:
        src = inspect.getsource(fn)
        assert "discovery_source" not in src, \
            f"rule {fn.__name__} reads discovery_source — decision coupling!"


async def test_gate_verdict_independent_of_discovery_source():
    """Same candidate, different discovery_source => identical verdict/rules."""
    cands = [make_candidate(discovery_source=s)
             for s in ("trending", "new_listing", "both", "unknown")]
    decisions = [
        evaluate_gate(c, make_portfolio(), make_regime(True), ACTIVE_RULES)
        for c in cands
    ]
    first = decisions[0]
    for d in decisions[1:]:
        assert d.all_passed == first.all_passed
        assert d.failed_rule_ids == first.failed_rule_ids
        assert [r.passed for r in d.rules] == [r.passed for r in first.rules]


def test_candidate_defaults_to_explicit_unknown():
    """Never silently default to 'trending' — unknown is explicit."""
    from models import Candidate
    c = Candidate(symbol="X", mint_address="M", price_usd=0.001,
                  liquidity_usd=1.0, volume_24h_usd=1.0, market_cap_usd=1.0)
    assert c.discovery_source == "unknown"


def test_mock_provider_tags_discovery_sources():
    from data_providers.mock import MockProvider
    import asyncio
    cands = asyncio.run(MockProvider().get_candidates(6))
    sources = {c.discovery_source for c in cands}
    assert {"trending", "new_listing", "both"} <= sources
