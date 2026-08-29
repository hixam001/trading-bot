"""
tests/test_decision_pipeline.py — Item #6 shared-core characterization.

The paper tick (main.run_tick) and the live cycle (run_live_cycle.run_cycle)
must run the SAME read/think/gate stages. These tests pin the unification
contract:

  1. read_candidates = blocklist + fake-chart filter, in that order,
     same MAX_CANDIDATES_PER_TICK — verbatim extraction from the paper tick.
  2. enrich_candidates is live-only (mock runs stay hermetic) and fail-soft.
  3. think_candidate degrades to the template thinker on thinker error —
     a live thinker exception no longer kills the cycle.
  4. apply_break uses the CORRECT set_break arity (the live copy previously
     called set_break(minutes, reason) — a latent TypeError).
  5. gate/entry semantics: think→gate intersection, same for both books.

Hermetic: no network, no DB, no LLM — the provider/thinker are fakes.
"""
from __future__ import annotations

import config
import decision_pipeline as dp
from models import Candidate, PortfolioState
from rule_engine.regime import MarketRegime


def make_regime(ok: bool = True) -> MarketRegime:
    return MarketRegime(
        computed_at="2026-08-29T00:00:00+00:00",
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


class FakeProvider:
    """Returns a fixed batch; counts reads."""
    def __init__(self, batch):
        self.batch = batch
        self.reads = 0

    async def get_candidates(self, limit):
        self.reads += 1
        self.limit_used = limit
        return list(self.batch)


class FakeThinker:
    """Optional raising thinker; records calls."""
    def __init__(self, result=None, raise_exc=None):
        self.result = result
        self.raise_exc = raise_exc
        self.calls = 0

    async def think(self, candidate, memory_line=""):
        self.calls += 1
        if self.raise_exc:
            raise self.raise_exc
        return self.result


class _FakeThink:
    """Minimal think object with the fields apply_break/gate look at."""
    def __init__(self, wants_entry=True, break_taking=False, minutes=5,
                 reason="test"):
        self.thesis = "t"
        self.invalidation = ""
        self.verdict = "buy" if wants_entry else "pass"
        self.wants_entry = wants_entry
        self.source = "template"
        self.break_taking = break_taking
        self.break_minutes = minutes
        self.break_reason = reason
        self.grounding_flags = []


# --- read stage ----------------------------------------------------------------

async def test_read_candidates_applies_blocklist_and_fake_chart():
    real = make_candidate()
    # Fake-chart threshold 3 (1h-vol-20x-depth): 1.1M 1h volume over 50k
    # depth — a tape that turns over its own pool 22x in an hour.
    fake = make_candidate(symbol="FAKE", volume_1h_usd=1_100_000.0)
    blocked = make_candidate(symbol="404")   # static symbol blocklist (A6)
    provider = FakeProvider([real, fake, blocked])
    out = await dp.read_candidates(provider)
    assert provider.limit_used == config.MAX_CANDIDATES_PER_TICK
    assert [c.symbol for c in out] == ["TEST"]   # fake + blocked dropped


async def test_read_candidates_passes_healthy_batch_through():
    """The paper tick's original semantics preserved: healthy candidates
    pass through untouched, same count, same order."""
    a = make_candidate(symbol="AA")
    b = make_candidate(symbol="BB")
    out = await dp.read_candidates(FakeProvider([a, b]))
    assert [c.symbol for c in out] == ["AA", "BB"]


async def test_enrich_candidates_mock_stays_hermetic(monkeypatch):
    """Mock mode: no enricher ever runs (the hermeticity leak lesson)."""
    monkeypatch.setattr(config, "DATA_BACKEND", "mock")

    async def _must_not_run(cands):
        raise AssertionError("live enricher ran in mock mode")

    import data_providers.crowd as crowd_mod
    monkeypatch.setattr(crowd_mod, "enrich_crowd_heat", _must_not_run)
    usages = await dp.enrich_candidates([make_candidate()])
    assert usages == []


async def test_enrich_candidates_fail_soft_on_every_feed(monkeypatch):
    """Every enricher raising still completes and returns [] usages."""
    monkeypatch.setattr(config, "DATA_BACKEND", "live")

    import data_providers.crowd as crowd_mod
    import data_providers.research as research_mod
    import llm.social as social_mod
    import llm.web_research as web_mod

    async def _boom(cands=None):
        raise RuntimeError("feed down")

    async def _boom2(cands, *a, **kw):
        raise RuntimeError("feed down")

    monkeypatch.setattr(crowd_mod, "enrich_crowd_heat", _boom)
    monkeypatch.setattr(research_mod, "enrich_with_research", _boom)
    monkeypatch.setattr(social_mod, "enrich_social", _boom2)
    monkeypatch.setattr(web_mod, "enrich_web", _boom)
    usages = await dp.enrich_candidates([make_candidate()])
    assert usages == []


# --- think stage ---------------------------------------------------------------

async def test_think_candidate_returns_thinker_result():
    think = _FakeThink()
    tk = FakeThinker(result=think)
    out = await dp.think_candidate(make_candidate(), tk)
    assert out is think
    assert tk.calls == 1


async def test_think_candidate_degrades_to_template_on_error():
    """The Item #6 fix: a thinker exception degrades this candidate to the
    template path instead of killing the cycle (live previously crashed)."""
    tk = FakeThinker(raise_exc=RuntimeError("LLM down"))
    out = await dp.think_candidate(make_candidate(), tk)
    assert out is not None
    assert out.wants_entry is not None     # template verdict present
    assert "degraded" in out.source or out.source == "template"


async def test_think_candidate_passes_memory_line():
    seen = {}

    class RecordingThinker:
        async def think(self, candidate, memory_line=""):
            seen["memory_line"] = memory_line
            return _FakeThink()

    await dp.think_candidate(make_candidate(), RecordingThinker(),
                            memory_line="Memory (context only): TEST: note")
    assert "TEST: note" in seen["memory_line"]


# --- break stage ---------------------------------------------------------------

async def test_apply_break_correct_arity(monkeypatch):
    """The Item #6 fix: apply_break(True, minutes, reason) — the live copy
    previously called set_break(minutes, reason), a latent TypeError."""
    calls = []

    from rule_engine import liveness
    monkeypatch.setattr(liveness, "set_break",
                        lambda taking, minutes=0, reason="": calls.append(
                            (taking, minutes, reason)))

    think = _FakeThink(break_taking=True, minutes=10, reason="vapor")
    took = await dp.apply_break(think)
    assert took is True
    assert calls == [(True, 10, "vapor")]


async def test_apply_break_noop_when_not_taking(monkeypatch):
    from rule_engine import liveness

    def _must_not(*a, **kw):
        raise AssertionError("set_break must not be called")

    monkeypatch.setattr(liveness, "set_break", _must_not)
    assert await dp.apply_break(_FakeThink(break_taking=False)) is False


# --- gate + entry ---------------------------------------------------------------

async def test_gate_candidate_and_entry_intersection():
    c = make_candidate()
    portfolio = PortfolioState(cash_usd=1_000.0)
    regime = make_regime(True)
    from rule_engine.rules import ACTIVE_RULES
    gate = await dp.gate_candidate(c, portfolio, regime, ACTIVE_RULES)
    assert gate.all_passed
    # think→gate intersection: either side alone refuses
    assert dp.entry_decision(_FakeThink(wants_entry=True), gate) is True
    assert dp.entry_decision(_FakeThink(wants_entry=False), gate) is False

    bad = make_candidate(liquidity_usd=100.0)
    gate_bad = await dp.gate_candidate(bad, portfolio, regime, ACTIVE_RULES)
    assert not gate_bad.all_passed
    assert dp.entry_decision(_FakeThink(wants_entry=True), gate_bad) is False


async def test_gate_uses_injected_rule_list():
    """The variation point: the rule list is injectable — the live cycle's
    swapped list (tested in live_execution/tests/test_pipeline_parity.py)
    differs ONLY in the cash rule; the shared gate is rule-list agnostic."""
    c = make_candidate()
    portfolio = PortfolioState(cash_usd=1_000.0)
    regime = make_regime(True)
    from rule_engine.rules import ACTIVE_RULES
    gate = await dp.gate_candidate(c, portfolio, regime, ACTIVE_RULES)
    assert gate.all_passed
    # Injecting a subset still evaluates exactly what was injected.
    from rule_engine.rules import liquidity_floor
    sub_gate = await dp.gate_candidate(c, portfolio, regime, [liquidity_floor])
    assert len(sub_gate.rules) == 1
    assert sub_gate.rules[0].rule_id == "liquidity_floor"


# --- both entry points delegate to the shared core ------------------------------

def test_main_run_tick_uses_shared_read_stage():
    """Parity contract: the paper tick runs the shared read/enrich stages."""
    import inspect
    import main as paper_main
    src = inspect.getsource(paper_main.run_tick)
    assert "read_candidates(provider)" in src
    assert "enrich_candidates(candidates)" in src


def test_isolation_backend_never_imports_live_execution():
    """The handoff §1 contract: backend/ never imports live_execution. The
    check scans import statements (prose may legitimately mention it)."""
    import decision_pipeline
    with open(decision_pipeline.__file__) as f:
        src = f.read()
    assert "from live_execution" not in src
    assert "import live_execution" not in src