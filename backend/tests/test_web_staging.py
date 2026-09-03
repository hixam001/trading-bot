"""
tests/test_web_staging.py — §48 web-search spend discipline (staged-gate
stage 4 + two-tier cross-tick TTL cache) + §51 free-first transport chain.

ALL offline: the search transports (Brave / SearXNG / Firecrawl) are faked;
no network anywhere. Pins:
  1. STAGING — a candidate that fails a rule is never searched; only an
     all-passed candidate gets the web evidence fetch;
  2. CACHE — a hit serves evidence with zero network calls; a miss is
     re-searched after the SHORT window (not the long one);
  3. MINT KEYING — two candidates sharing a ticker but not a mint NEVER
     inherit each other's evidence;
  4. HERMETICITY — mock runs never search; no transport configured =
     stage disabled;
  5. FAIL-SOFT — a search error returns "" and never raises into the gate.
"""
from __future__ import annotations

import pytest

import config
import decision_pipeline as dp
import llm.web_research as wr
from models import Candidate, PortfolioState
from rule_engine.regime import MarketRegime
from rule_engine.rules import ACTIVE_RULES


def make_regime(ok: bool = True) -> MarketRegime:
    return MarketRegime(
        computed_at="2026-08-30T00:00:00+00:00",
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
def fresh_cache(monkeypatch):
    """Fresh cache + mock backend + NO search transport keys per test.
    §51: Brave/SearXNG must be cleared TOO — config loads the operator's
    real .env, so a host with BRAVE_SEARCH_API_KEY set would otherwise
    leak real-hop configuration into these hermetic tests (the same reason
    FIRECRAWL_API_KEY is force-cleared). Chain bench state resets per test."""
    monkeypatch.setattr(wr, "_evidence_cache",
                        wr._EvidenceCache(7200.0, 1800.0))
    monkeypatch.setattr(config, "DATA_BACKEND", "mock")
    monkeypatch.setattr(config, "FIRECRAWL_API_KEY", "")
    monkeypatch.setattr(config, "BRAVE_SEARCH_API_KEY", "")
    monkeypatch.setattr(config, "SEARXNG_URL", "")
    wr.reset_search_chain_state()
    yield
    wr.reset_search_chain_state()


def _live(monkeypatch, key="fc-x"):
    monkeypatch.setattr(config, "DATA_BACKEND", "live")
    monkeypatch.setattr(config, "FIRECRAWL_API_KEY", key)


async def _fake_crowd(cands):
    """Make the crowd rule pass for the fetched candidates (heat 60)."""
    for c in cands:
        c.fomo_heat = 60
        c.crowd_heat_source = "fomo"


# --- contract 1: staging ------------------------------------------------------

async def test_failed_candidate_is_never_searched(monkeypatch):
    """§44 discipline applied to the web quota: a liquidity-failed candidate
    never costs a search; the all-passed candidate does."""
    _live(monkeypatch)
    searched: list = []

    async def fake_web(c):
        searched.append(c.symbol)
        c.web_summary = "evidence line"

    good = make_candidate(symbol="GOOD")
    thin = make_candidate(symbol="THIN", liquidity_usd=100.0)

    portfolio = PortfolioState(cash_usd=1_000.0)
    regime = make_regime(True)
    await dp.gate_candidate_staged(good, portfolio, regime, ACTIVE_RULES,
                                   crowd_fetch=_fake_crowd,
                                   web_fetch=fake_web)
    await dp.gate_candidate_staged(thin, portfolio, regime, ACTIVE_RULES,
                                   crowd_fetch=_fake_crowd,
                                   web_fetch=fake_web)

    assert searched == ["GOOD"]
    assert good.web_summary == "evidence line"
    assert thin.web_summary is None


async def test_web_fetch_never_blocks_the_decision(monkeypatch):
    """A raising web fetch is swallowed: the decision still returns."""
    _live(monkeypatch)

    async def boom(c):
        raise RuntimeError("search down")

    c = make_candidate()
    decision = await dp.gate_candidate_staged(
        c, PortfolioState(cash_usd=1_000.0), make_regime(True),
        ACTIVE_RULES, crowd_fetch=_fake_crowd, web_fetch=boom)
    assert decision.all_passed is True


async def test_mock_mode_never_searches(monkeypatch):
    """Mock runs: stage 4 never runs — hermeticity preserved."""
    monkeypatch.setattr(config, "DATA_BACKEND", "mock")

    async def must_not_run(c):
        raise AssertionError("web fetch ran in mock mode")

    c = make_candidate()
    await dp.gate_candidate_staged(
        c, PortfolioState(cash_usd=1_000.0), make_regime(True),
        ACTIVE_RULES, crowd_fetch=_fake_crowd, web_fetch=must_not_run)
    assert c.web_summary is None


# --- contract 4b: no transport configured = stage off --------------------------

async def test_no_transport_configured_disables_search(monkeypatch):
    """§51: empty Brave key + empty SearXNG + empty Firecrawl = the stage
    is off — no network call can even be attempted."""
    _live(monkeypatch, key="")
    calls = []

    async def real_httpx_search(client, symbol):
        calls.append(symbol)   # must never happen
        return "should not appear"

    monkeypatch.setattr(wr, "search_web", real_httpx_search)
    c = make_candidate()
    out = await wr.search_for_candidate(c)
    assert out == "" and calls == []


async def test_brave_only_enables_the_stage_free_first(monkeypatch):
    """§51: a configured Brave key alone turns the stage on, and the chain
    prefers it (the free hop) — Firecrawl is configured as failover here
    but must never be reached when Brave answers."""
    _live(monkeypatch)                      # firecrawl key = "fc-x"
    monkeypatch.setattr(config, "BRAVE_SEARCH_API_KEY", "brave-free")
    hits: list = []

    class BraveResp:
        status_code = 200
        text = ""

        def json(self):
            return {"web": {"results": [
                {"title": "Brave free evidence", "description": "d"}]}}

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, headers=None):
            hits.append(("brave", params["q"]))
            return BraveResp()

        def build_request(self, *a, **kw):
            hits.append(("firecrawl", a))
            return None

        async def send(self, req):
            hits.append(("firecrawl", "sent"))
            raise AssertionError("paid Firecrawl reached although Brave answered")

    monkeypatch.setattr(wr.httpx, "AsyncClient", FakeClient)
    c = make_candidate()
    out = await wr.search_for_candidate(c)
    assert "Brave free evidence" in out
    assert hits and hits[0][0] == "brave"     # the free hop led


# --- contract 2: two-tier cache -------------------------------------------------

async def test_cache_hit_costs_no_network(monkeypatch):
    _live(monkeypatch)
    net_calls = []

    class FakeResp:
        status_code = 200
        text = ""

        def json(self):
            return {"data": [{"title": "Fresh news", "description": "d"}]}

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def build_request(self, *a, **kw):
            net_calls.append("req")
            return None

        async def send(self, req):
            net_calls.append("send")
            return FakeResp()

    monkeypatch.setattr(wr.httpx, "AsyncClient", FakeClient)

    c = make_candidate()
    first = await wr.search_for_candidate(c)
    assert "Fresh news" in first
    assert len(net_calls) == 2            # one build + one send

    # second call within TTL: served from cache, zero network
    net_calls.clear()
    second = await wr.search_for_candidate(c)
    assert "Fresh news" in second
    assert net_calls == []


async def test_miss_entry_expires_on_the_short_window(monkeypatch):
    """An empty result is cached for the MISS TTL only — after that the
    next call re-searches (attention may have started between searches)."""
    _live(monkeypatch)
    net_calls = []

    class EmptyResp:
        status_code = 200
        text = ""

        def json(self):
            return {"data": []}

    class HitResp:
        status_code = 200
        text = ""

        def json(self):
            return {"data": [{"title": "Now it has news",
                              "description": "d"}]}

    state = {"empty": True}

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def build_request(self, *a, **kw):
            net_calls.append(1)
            return None

        async def send(self, req):
            return EmptyResp() if state["empty"] else HitResp()

    monkeypatch.setattr(wr.httpx, "AsyncClient", FakeClient)

    c = make_candidate()
    assert await wr.search_for_candidate(c) == ""      # miss, cached 30m
    assert len(net_calls) == 1

    # simulate the miss window elapsing: rewind the cache timestamp
    key = wr.cache_key_for(c)
    ts, value = wr._evidence_cache._data[key]
    wr._evidence_cache._data[key] = (ts - 1801.0, value)
    state["empty"] = False

    out = await wr.search_for_candidate(c)             # re-searched
    assert "Now it has news" in out
    assert len(net_calls) == 2


# --- contract 3: mint keying -----------------------------------------------------

async def test_same_ticker_different_mints_never_share_evidence(monkeypatch):
    _live(monkeypatch)
    served = []

    class FakeResp:
        status_code = 200
        text = ""

        def __init__(self, title):
            self._title = title

        def json(self):
            return {"data": [{"title": self._title, "description": "d"}]}

    current = {"title": "AAA evidence"}

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def build_request(self, *a, **kw):
            return None

        async def send(self, req):
            served.append(current["title"])
            return FakeResp(current["title"])

    monkeypatch.setattr(wr.httpx, "AsyncClient", FakeClient)

    a = make_candidate(symbol="PUMP", mint_address="Mint" + "A" * 40)
    b = make_candidate(symbol="PUMP", mint_address="Mint" + "B" * 40)
    out_a = await wr.search_for_candidate(a)
    current["title"] = "BBB evidence"
    out_b = await wr.search_for_candidate(b)

    assert "AAA evidence" in out_a
    assert "BBB evidence" in out_b
    assert served == ["AAA evidence", "BBB evidence"]   # two real searches
    assert wr.cache_key_for(a) != wr.cache_key_for(b)


# --- contract 5: fail-soft --------------------------------------------------------

async def test_search_error_returns_empty_not_raise(monkeypatch):
    _live(monkeypatch)

    class BoomClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def build_request(self, *a, **kw):
            raise RuntimeError("net down")

    monkeypatch.setattr(wr.httpx, "AsyncClient", BoomClient)
    assert await wr.search_for_candidate(make_candidate()) == ""

