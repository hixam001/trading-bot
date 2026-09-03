"""
tests/test_search_chain.py — §51 free-first search-chain transport rules
(Brave → self-hosted SearXNG → Firecrawl) in llm/web_research.py.

ALL offline: httpx.AsyncClient is faked per test; no network anywhere.
Pins the §34-style bench machinery so a dead hop can never stall ticks:
  1. 402 credit exhaustion benches the hop for the LONG window — later
     searches skip it entirely, the keyless hop carries the load, and the
     paid hop is never reached; the bench is time-based (rewinding it
     restores the hop);
  2. 429 is only a SHORT backoff (75s), not a long bench — after the
     window the hop is trusted again;
  3. two consecutive transport errors bench the hop — one timeout is
     tolerated, the second benches, and a benched hop costs ZERO calls;
  4. a transport SUCCESS resets the error streak (error→success→error is
     NOT two-in-a-row — flaky-but-alive hops keep working);
  5. an answered-but-EMPTY hop falls through to the next hop (a metasearch
     may find what the primary missed), and an all-empty chain caches the
     miss (short TTL) so the immediate repeat costs zero network.
"""
from __future__ import annotations

import time

import httpx
import pytest

import config
import llm.web_research as wr
from models import Candidate


def _cand(n: int) -> Candidate:
    """Unique mint per call — every search_for_candidate is a live walk."""
    return Candidate(
        symbol="TEST", mint_address="Mint" + str(n) * 40,
        price_usd=0.001, liquidity_usd=50_000.0, volume_24h_usd=100_000.0,
        market_cap_usd=100_000.0, volume_1h_usd=20_000.0,
        buys_1h=300, sells_1h=200, price_change_1h_pct=5.0,
        age_hours=48.0, has_twitter=True, has_telegram=True,
        mint_authority_revoked=True, freeze_authority_revoked=True,
        is_likely_honeypot=False,
    )


@pytest.fixture(autouse=True)
def fresh_chain(monkeypatch):
    """Fresh cache + mock backend + NO transport keys + clean bench state.
    BRAVE_SEARCH_URL is pointed at an inert test host so even a bug here can
    never reach the real API (keys are force-cleared because config loads
    the operator's REAL .env — load_dotenv leaks the host's keys)."""
    monkeypatch.setattr(wr, "_evidence_cache",
                        wr._EvidenceCache(7200.0, 1800.0))
    monkeypatch.setattr(config, "DATA_BACKEND", "mock")
    monkeypatch.setattr(config, "FIRECRAWL_API_KEY", "")
    monkeypatch.setattr(config, "BRAVE_SEARCH_API_KEY", "")
    monkeypatch.setattr(config, "SEARXNG_URL", "")
    monkeypatch.setattr(config, "BRAVE_SEARCH_URL",
                        "https://brave.test/res/v1/web/search")
    wr.reset_search_chain_state()
    yield
    wr.reset_search_chain_state()


def _live_all(monkeypatch):
    """All three hops configured (free-first order: brave, searxng, firecrawl)."""
    monkeypatch.setattr(config, "DATA_BACKEND", "live")
    monkeypatch.setattr(config, "BRAVE_SEARCH_API_KEY", "brave-free")
    monkeypatch.setattr(config, "SEARXNG_URL", "http://searxng.test:8080")
    monkeypatch.setattr(config, "FIRECRAWL_API_KEY", "fc-x")


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self.text = ""
        self._payload = payload

    def json(self):
        return self._payload


def _install_fake(monkeypatch, brave_fn, searxng_fn, firecrawl_fn=None):
    """Fake httpx.AsyncClient dispatching GET by URL. Each *_fn returns a
    _Resp or raises (transport error). Returns a per-hop call counter."""
    if firecrawl_fn is None:
        firecrawl_fn = lambda: _Resp(200, {"data": []})
    calls = {"brave": 0, "searxng": 0, "firecrawl": 0}

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, headers=None, timeout=None):
            if "brave.test" in str(url):
                calls["brave"] += 1
                return brave_fn()
            calls["searxng"] += 1
            return searxng_fn()

        def build_request(self, *a, **kw):
            return None

        async def send(self, req):
            calls["firecrawl"] += 1
            return firecrawl_fn()

    monkeypatch.setattr(wr.httpx, "AsyncClient", FakeClient)
    return calls


# --- contract 1: 402 = long bench ---------------------------------------------

async def test_402_benches_the_hop_for_the_long_window(monkeypatch):
    """Brave's credit-exhaustion answer benches it for the LONG window:
    the next search skips it entirely, the keyless hop carries the load,
    the paid hop is never reached — and the bench is time-based."""
    _live_all(monkeypatch)

    def brave_fn():
        return _Resp(402, None)          # credits out (payload unread)

    def searxng_fn():
        return _Resp(200, {"results": [
            {"title": "searxng evidence", "content": "c"}]})

    calls = _install_fake(monkeypatch, brave_fn, searxng_fn)

    out = await wr.search_for_candidate(_cand(1))
    assert "searxng evidence" in out    # fell through to the keyless hop
    assert calls["brave"] == 1 and calls["searxng"] == 1
    assert calls["firecrawl"] == 0      # paid hop never reached

    # Long bench: the next search must skip brave entirely
    out2 = await wr.search_for_candidate(_cand(2))
    assert "searxng evidence" in out2
    assert calls["brave"] == 1          # skipped — benched
    assert wr._BENCHED_UNTIL["brave"] > time.monotonic()

    # The bench is time-based: rewind it and brave is tried again
    wr._BENCHED_UNTIL["brave"] -= config.SEARCH_BENCH_SECONDS + 1
    await wr.search_for_candidate(_cand(3))
    assert calls["brave"] == 2         # trusted again after the window


# --- contract 2: 429 = short backoff only --------------------------------------

async def test_429_is_only_a_short_backoff_not_a_long_bench(monkeypatch):
    """A 429 rate-limit answers fall through to the keyless hop too, but the
    backoff is the SHORT window: after rewinding only 75s the hop is trusted
    again — it was throttled, not out of credits."""
    _live_all(monkeypatch)

    def brave_fn():
        return _Resp(429, None)

    def searxng_fn():
        return _Resp(200, {"results": [
            {"title": "searxng evidence", "content": "c"}]})

    calls = _install_fake(monkeypatch, brave_fn, searxng_fn)

    await wr.search_for_candidate(_cand(1))
    assert calls["brave"] == 1
    assert "searxng evidence" in await wr.search_for_candidate(_cand(2))
    assert calls["brave"] == 1          # still inside the short window

    benched_until = wr._BENCHED_UNTIL["brave"]
    assert benched_until - time.monotonic() <= config.SEARCH_THROTTLE_BACKOFF_SECONDS

    # rewind 75s: the hop is trusted again
    wr._BENCHED_UNTIL["brave"] -= config.SEARCH_THROTTLE_BACKOFF_SECONDS + 1
    await wr.search_for_candidate(_cand(3))
    assert calls["brave"] == 2


# --- contract 3: transport errors bench after two in a row -----------------------

async def test_two_transport_errors_bench_the_hop(monkeypatch):
    """A dead transport (timeout/conn-refused — status None) is tolerated
    once; the SECOND consecutive one benches the hop so a dead free hop
    costs at most two timeouts, never one per candidate per tick. A benched
    hop costs ZERO calls on later searches (searxng keeps the chain alive)."""
    _live_all(monkeypatch)

    def brave_fn():
        raise httpx.ConnectTimeout("dead")   # -> status None

    def searxng_fn():
        return _Resp(200, {"results": [
            {"title": "searxng evidence", "content": "c"}]})

    calls = _install_fake(monkeypatch, brave_fn, searxng_fn)

    await wr.search_for_candidate(_cand(1))   # 1st error — streak 1
    assert calls["brave"] == 1
    await wr.search_for_candidate(_cand(2))   # 2nd error — bench
    assert calls["brave"] == 2
    await wr.search_for_candidate(_cand(3))   # benched: not even tried
    await wr.search_for_candidate(_cand(4))
    assert calls["brave"] == 2          # zero cost once benched
    assert wr._BENCHED_UNTIL["brave"] > time.monotonic()


# --- contract 4: a success resets the streak -------------------------------------

async def test_transport_success_resets_the_error_streak(monkeypatch):
    """error → success → error is NOT two-in-a-row: a flaky-but-alive hop
    keeps working (only CONSECUTIVE failures bench). Brave alternates
    dead/alive/dead and must never be benched across three searches."""
    _live_all(monkeypatch)
    state = {"dead": True}

    def brave_fn():
        if state["dead"]:
            state["dead"] = False
            raise httpx.ConnectTimeout("flaky")
        state["dead"] = True
        return _Resp(200, {"web": {"results": [
            {"title": "brave alive", "description": "d"}]}})

    def searxng_fn():
        return _Resp(200, {"results": [
            {"title": "searxng evidence", "content": "c"}]})

    calls = _install_fake(monkeypatch, brave_fn, searxng_fn)

    out1 = await wr.search_for_candidate(_cand(1))   # brave dead → searxng
    assert "searxng evidence" in out1
    out2 = await wr.search_for_candidate(_cand(2))   # brave alive
    assert "brave alive" in out2
    out3 = await wr.search_for_candidate(_cand(3))   # brave dead again —
    assert "searxng evidence" in out3                # streak was only 1
    assert calls["brave"] == 3                       # never benched
    assert "brave" not in wr._BENCHED_UNTIL


# --- contract 5: empty answers fall through; all-empty = cached miss ------------

async def test_empty_brave_falls_through_to_searxng(monkeypatch):
    """Brave answering 200-but-nothing (fresh memecoins are often absent
    from any single index) is NOT a dead end: the chain asks SearXNG — its
    ~270 engines may find what Brave missed. Rows are normalized so the
    evidence format is hop-independent."""
    _live_all(monkeypatch)

    def brave_fn():
        return _Resp(200, {"web": {"results": []}})   # answered, empty

    def searxng_fn():
        return _Resp(200, {"results": [
            {"title": "metasearch found it", "content": "row"}]})

    calls = _install_fake(monkeypatch, brave_fn, searxng_fn)

    out = await wr.search_for_candidate(_cand(1))
    assert "metasearch found it" in out
    assert calls["brave"] == 1 and calls["searxng"] == 1
    assert calls["firecrawl"] == 0        # free hops answered — paid never asked


async def test_all_empty_chain_caches_the_miss(monkeypatch):
    """When every configured hop answers empty, the miss is cached for the
    SHORT window — the immediate repeat costs zero network (and no paid
    failover either: Firecrawl answering empty still ends as a miss)."""
    _live_all(monkeypatch)

    def brave_fn():
        return _Resp(200, {"web": {"results": []}})

    def searxng_fn():
        return _Resp(200, {"results": []})

    calls = _install_fake(monkeypatch, brave_fn, searxng_fn)

    c = _cand(1)
    assert await wr.search_for_candidate(c) == ""
    assert calls == {"brave": 1, "searxng": 1, "firecrawl": 1}
    # immediate repeat: served from the miss cache — zero network
    assert await wr.search_for_candidate(c) == ""
    assert calls == {"brave": 1, "searxng": 1, "firecrawl": 1}