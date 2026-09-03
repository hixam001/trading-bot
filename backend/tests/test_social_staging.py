"""
tests/test_social_staging.py — §51 social-read spend discipline: the
realtime attention read (llm/social.py via SOCIAL_LLM_*, Groq free tier)
left the unconditional read stage and became stage 5 of the staged gate.

ALL offline: the social reader is faked; no network, no LLM. Pins:
  1. STAGING — a candidate that fails a rule is never social-read; only an
     all-passed candidate gets the read (the same §44 discipline the fomo
     scrape and web search already follow);
  2. EVIDENCE ONLY — the read fills social_interest/social_note and never
     touches the decision (all_passed unchanged, verdicts untouched);
  3. REUSE — a candidate that already carries a social_interest costs
     nothing (cross-tick reuse);
  4. HERMETICITY — mock runs never social-read; empty SOCIAL_LLM_API_KEY =
     stage disabled (read_social_one returns immediately);
  5. FAIL-SOFT — a raising fetch is swallowed inside the gate; the decision
     still returns with social_interest None;
  6. USAGE QUEUE — a successful read queues exactly one LLMResult with the
     mint attached, and drain_social_usages hands it over exactly once
     (draining again yields [] — the queue cannot double-journal or grow).
"""
from __future__ import annotations

import pytest

import config
import decision_pipeline as dp
import llm.social as social_mod
from models import Candidate, PortfolioState
from rule_engine.regime import MarketRegime
from rule_engine.rules import ACTIVE_RULES


def make_regime(ok: bool = True) -> MarketRegime:
    return MarketRegime(
        computed_at="2026-09-02T00:00:00+00:00",
        pct_candidates_green_1h=0.5,
        median_volume_1h_usd=50_000.0,
        avg_buy_sell_ratio=1.2,
        regime_ok=ok,
        regime_detail="fixture regime",
    )


def make_candidate(**overrides) -> Candidate:
    base = dict(
        symbol="TEST", mint_address="Mint" + "1" * 40,
        price_usd=0.001, liquidity_usd=50_000.0, volume_24h_usd=100_000.0,
        market_cap_usd=100_000.0, volume_1h_usd=20_000.0,
        buys_1h=300, sells_1h=200, price_change_1h_pct=5.0,
        age_hours=48.0, has_twitter=True, has_telegram=True,
        mint_authority_revoked=True, freeze_authority_revoked=True,
        is_likely_honeypot=False,
    )
    base.update(overrides)
    return Candidate(**base)


@pytest.fixture(autouse=True)
def fresh_social_state(monkeypatch):
    """Mock backend, disabled stage key, empty queues per test."""
    monkeypatch.setattr(config, "DATA_BACKEND", "mock")
    monkeypatch.setattr(config, "SOCIAL_LLM_API_KEY", "")
    social_mod.take_queued_usages()      # drain any residue
    dp.drain_social_usages()
    yield
    social_mod.take_queued_usages()
    dp.drain_social_usages()


async def _fake_crowd(cands):
    """Make the crowd rule pass for the fetched candidates (heat 60)."""
    for c in cands:
        c.fomo_heat = 60
        c.crowd_heat_source = "fomo"


# --- contract 1 + 2: staging + evidence-only ---------------------------------

async def test_failed_candidate_is_never_social_read(monkeypatch):
    """A liquidity-failed candidate never costs a social read; the
    all-passed candidate does. The read never flips the decision."""
    monkeypatch.setattr(config, "DATA_BACKEND", "live")
    read: list = []

    async def fake_social(c):
        read.append(c.symbol)
        c.social_interest = "organic"
        c.social_note = "1h +5% with buys 300 vs sells 200."

    good = make_candidate(symbol="GOOD")
    thin = make_candidate(symbol="THIN", liquidity_usd=100.0)

    portfolio = PortfolioState(cash_usd=1_000.0)
    regime = make_regime(True)
    g = await dp.gate_candidate_staged(good, portfolio, regime, ACTIVE_RULES,
                                       crowd_fetch=_fake_crowd,
                                       social_fetch=fake_social)
    t = await dp.gate_candidate_staged(thin, portfolio, regime, ACTIVE_RULES,
                                       crowd_fetch=_fake_crowd,
                                       social_fetch=fake_social)

    assert read == ["GOOD"]               # the failed candidate was never read
    assert good.social_interest == "organic"
    assert thin.social_interest is None
    assert g.all_passed is True           # evidence only — decision untouched
    assert t.all_passed is False


async def test_social_read_runs_after_web_search_for_passed_candidates(
        monkeypatch):
    """Stage ordering: for an all-passed candidate the social read (stage 5)
    runs AFTER the web search (stage 4) — the thinker sees both lines."""
    monkeypatch.setattr(config, "DATA_BACKEND", "live")
    order: list = []

    async def fake_web(c):
        order.append("web")
        c.web_summary = "evidence line"

    async def fake_social(c):
        order.append("social")
        c.social_interest = "organic"
        c.social_note = "note."

    c = make_candidate()
    await dp.gate_candidate_staged(
        c, PortfolioState(cash_usd=1_000.0), make_regime(True), ACTIVE_RULES,
        crowd_fetch=_fake_crowd, web_fetch=fake_web, social_fetch=fake_social)
    assert order == ["web", "social"]


# --- contract 3: reuse --------------------------------------------------------

async def test_candidate_with_existing_social_interest_costs_nothing(
        monkeypatch):
    """A reused social_interest (set on a previous tick) skips the read."""
    monkeypatch.setattr(config, "DATA_BACKEND", "live")

    async def must_not_run(c):
        raise AssertionError("social read re-ran although interest existed")

    c = make_candidate(social_interest="peaked", social_note="already read")
    await dp.gate_candidate_staged(
        c, PortfolioState(cash_usd=1_000.0), make_regime(True), ACTIVE_RULES,
        crowd_fetch=_fake_crowd, social_fetch=must_not_run)
    assert c.social_interest == "peaked"


# --- contract 4: hermeticity + disabled -------------------------------------

async def test_mock_mode_never_social_reads(monkeypatch):
    """Mock runs: stage 5 never runs — hermeticity preserved."""
    monkeypatch.setattr(config, "DATA_BACKEND", "mock")

    async def must_not_run(c):
        raise AssertionError("social fetch ran in mock mode")

    c = make_candidate()
    await dp.gate_candidate_staged(
        c, PortfolioState(cash_usd=1_000.0), make_regime(True), ACTIVE_RULES,
        crowd_fetch=_fake_crowd, social_fetch=must_not_run)
    assert c.social_interest is None


async def test_read_social_one_disabled_without_key(monkeypatch):
    """Empty SOCIAL_LLM_API_KEY: read_social_one returns immediately — no
    reader is even constructed (enabled is False)."""
    class BoomReader:
        def __init__(self, client=None):
            raise AssertionError("reader constructed although stage disabled")

    monkeypatch.setattr(social_mod, "SocialReader", BoomReader)
    c = make_candidate()
    await social_mod.read_social_one(c)     # must not raise
    assert c.social_interest is None


# --- contract 5: fail-soft ------------------------------------------------------

async def test_raising_social_fetch_never_blocks_the_decision(monkeypatch):
    """A raising social fetch is swallowed inside the gate: the decision
    still returns, social_interest stays None."""
    monkeypatch.setattr(config, "DATA_BACKEND", "live")

    async def boom(c):
        raise RuntimeError("social endpoint down")

    c = make_candidate()
    decision = await dp.gate_candidate_staged(
        c, PortfolioState(cash_usd=1_000.0), make_regime(True), ACTIVE_RULES,
        crowd_fetch=_fake_crowd, social_fetch=boom)
    assert decision.all_passed is True
    assert c.social_interest is None


# --- contract 6: the usage queue -------------------------------------------------

async def test_successful_read_queues_usage_drained_exactly_once(monkeypatch):
    """A successful read queues ONE usage with the mint attached; the drain
    hands it over once and the second drain is empty (no double journal, no
    unbounded growth)."""
    from llm.client import LLMResult

    monkeypatch.setattr(config, "DATA_BACKEND", "live")
    monkeypatch.setattr(config, "SOCIAL_LLM_API_KEY", "groq-free")

    class FakeReader:
        def __init__(self, client=None):
            pass

        @property
        def enabled(self):
            return True

        async def read(self, c):
            usage = LLMResult(text="{}", provider="groq",
                              model="qwen/qwen3.8-27b", task="social_read")
            return "organic", "buys 300 vs sells 200.", usage

    monkeypatch.setattr(social_mod, "SocialReader", FakeReader)
    c = make_candidate()
    await social_mod.read_social_one(c)

    queued = social_mod.take_queued_usages()
    assert len(queued) == 1
    assert queued[0].task == "social_read"
    assert queued[0].mint_address == c.mint_address
    assert c.social_interest == "organic"
    # Second take: empty — the queue was cleared.
    assert social_mod.take_queued_usages() == []


async def test_stage_social_read_collects_usages_into_dp_queue(monkeypatch):
    """stage_social_read (the gate's default fetch) funnels the reader's
    usage into decision_pipeline's queue; drain_social_usages hands ALL of
    it over once."""
    from llm.client import LLMResult

    monkeypatch.setattr(config, "DATA_BACKEND", "live")
    monkeypatch.setattr(config, "SOCIAL_LLM_API_KEY", "groq-free")

    class FakeReader:
        def __init__(self, client=None):
            pass

        @property
        def enabled(self):
            return True

        async def read(self, c):
            usage = LLMResult(text="{}", provider="groq",
                              model="qwen/qwen3.8-27b", task="social_read")
            return "unclear", "tape thin.", usage

    monkeypatch.setattr(social_mod, "SocialReader", FakeReader)
    c = make_candidate()
    await dp.stage_social_read(c)
    drained = dp.drain_social_usages()
    assert len(drained) == 1
    assert drained[0].task == "social_read"
    assert dp.drain_social_usages() == []      # exactly-once drain


async def test_stage_social_read_swallows_reader_errors(monkeypatch):
    """A raising reader inside stage_social_read never escapes into the
    gate (fail-soft contract) and queues nothing."""
    monkeypatch.setattr(config, "DATA_BACKEND", "live")
    monkeypatch.setattr(config, "SOCIAL_LLM_API_KEY", "groq-free")

    class BoomReader:
        def __init__(self, client=None):
            pass

        @property
        def enabled(self):
            return True

        async def read(self, c):
            raise RuntimeError("groq down")

    monkeypatch.setattr(social_mod, "SocialReader", BoomReader)
    c = make_candidate()
    await dp.stage_social_read(c)            # must not raise
    assert c.social_interest is None
    assert dp.drain_social_usages() == []