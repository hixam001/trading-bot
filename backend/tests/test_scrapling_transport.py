"""
tests/test_scrapling_transport.py — §47 local stealth transport (Scrapling).

ALL offline: the two free hops (curl-cffi impersonated GET, patchright
stealth browser) are faked with stubs; scrapling itself is never imported
during the suite. Pins the integration contract:
  1. chain order — scrapling FIRST, paid providers follow;
  2. mock/hermetic runs never touch either hop (gating on live mode);
  3. benching — a failing scrapling hop benches like any paid provider,
     and the chain fails over to the paid hop with no code changes;
  4. the curl hop and browser hop NEVER raise into the tick (None only);
  5. idle close: a quiet bot drops the browser session.
"""
from __future__ import annotations

import asyncio
import json
import types

import pytest

import config
import data_providers.crowd as crowd
import data_providers.stealth_browser as sb


GOOD_PAYLOAD = json.dumps({"success": True, "responseObject": {"items": []}})


def live_mode(monkeypatch):
    """Point the transport at the live-mode scrapling hops."""
    monkeypatch.setattr(config, "SCRAPLING_ENABLED", True)
    monkeypatch.setattr(config, "DATA_BACKEND", "live")


def install_fake_scrapling_import(monkeypatch, async_fetcher=None,
                                  stealth_session=None):
    """Make `from scrapling.fetchers import X` hit our stubs for the rest
    of this test (function-local imports resolve at call time)."""
    fake_mod = types.ModuleType("scrapling.fetchers")
    if async_fetcher is not None:
        fake_mod.AsyncFetcher = async_fetcher
    if stealth_session is not None:
        fake_mod.AsyncStealthySession = stealth_session
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "scrapling.fetchers":
            return fake_mod
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    return fake_mod


class StubResponse:
    def __init__(self, status=200, body=GOOD_PAYLOAD):
        self.status = status
        self.body = body.encode() if isinstance(body, str) else body


class StubAsyncFetcher:
    calls: list = []
    result = None

    @classmethod
    async def get(cls, url, **kw):
        cls.calls.append((url, kw))
        if isinstance(cls.result, Exception):
            raise cls.result
        return cls.result


@pytest.fixture(autouse=True)
def reset_browser_state(monkeypatch):
    """Fresh module globals per test; scrapling off unless a test opts in."""
    monkeypatch.setattr(sb, "_session", None)
    monkeypatch.setattr(sb, "_consecutive_failures", 0)
    monkeypatch.setattr(sb, "_import_ok", None)
    monkeypatch.setattr(sb, "_last_used", 0.0)
    monkeypatch.setattr(config, "SCRAPLING_ENABLED", False)
    monkeypatch.setattr(config, "DATA_BACKEND", "mock")
    StubAsyncFetcher.calls = []
    StubAsyncFetcher.result = StubResponse()
    yield


# --- contract 2: hermeticity --------------------------------------------------

async def test_mock_mode_never_uses_the_curl_hop(monkeypatch):
    """Mock runs: the scrapling curl hop must not run — the transport
    degrades to the plain-httpx fallback exactly as pre-§47."""
    install_fake_scrapling_import(monkeypatch, async_fetcher=StubAsyncFetcher)
    out = await crowd._direct_get("https://x.example", {})
    # x.example isn't reachable; whatever happens, zero scrapling calls
    assert StubAsyncFetcher.calls == [], "curl hop must not run in mock"


async def test_chain_omits_scrapling_in_mock(monkeypatch):
    live_mode(monkeypatch)
    monkeypatch.setattr(config, "DATA_BACKEND", "mock")
    chain = [name for name, _ in crowd._configured_scrapers()]
    assert "scrapling" not in chain


async def test_chain_leads_with_scrapling_in_live(monkeypatch):
    live_mode(monkeypatch)
    monkeypatch.setattr(config, "FIRECRAWL_API_KEY", "fc-x")
    chain = [name for name, _ in crowd._configured_scrapers()]
    assert chain[0] == "scrapling"
    assert "firecrawl" in chain


async def test_scrapling_disabled_falls_back_to_paid(monkeypatch):
    live_mode(monkeypatch)
    monkeypatch.setattr(config, "SCRAPLING_ENABLED", False)
    monkeypatch.setattr(config, "FIRECRAWL_API_KEY", "fc-x")
    chain = [name for name, _ in crowd._configured_scrapers()]
    assert "scrapling" not in chain
    assert chain[0] == "firecrawl"


# --- contract 4: never raise ---------------------------------------------------

async def test_curl_hop_swallows_everything(monkeypatch):
    install_fake_scrapling_import(monkeypatch, async_fetcher=StubAsyncFetcher)
    StubAsyncFetcher.result = RuntimeError("boom")
    assert await sb.curl_get("https://x.example", {}) is None


async def test_curl_hop_rejects_challenges_and_error_envelopes(monkeypatch):
    install_fake_scrapling_import(monkeypatch, async_fetcher=StubAsyncFetcher)
    StubAsyncFetcher.result = StubResponse(200, "<!doctype html>challenge")
    assert await sb.curl_get("https://x.example", {}) is None
    StubAsyncFetcher.result = StubResponse(430, '{"statusCode": 430}')
    assert await sb.curl_get("https://x.example", {}) is None
    StubAsyncFetcher.result = StubResponse(200, GOOD_PAYLOAD)
    out = await sb.curl_get("https://x.example", {})
    assert out == json.loads(GOOD_PAYLOAD)


async def test_browser_hop_never_raises(monkeypatch):
    """A broken browser session must return None, never raise into the
    tick — and consecutive failures close the zombie session."""
    live_mode(monkeypatch)

    class ExplodingSession:
        def __init__(self):
            self.exited = False

        async def __aexit__(self, *a):
            self.exited = True
            return False

    stub_session = ExplodingSession()

    class StubStealthySession:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            stub_session.exited = False
            return ExplodingSession()

        async def __aexit__(self, *a):
            return False

    install_fake_scrapling_import(
        monkeypatch, stealth_session=StubStealthySession)

    class RealFetchExplodes(ExplodingSession):
        async def fetch(self, url, extra_headers=None):
            raise RuntimeError("browser died")

    # _get_browser_session returns whatever __aenter__ gave; make fetch explode
    monkeypatch.setattr(sb, "_MAX_CONSECUTIVE_FAILURES", 3)
    for _ in range(3):
        assert await sb.browser_fetch_json("https://x.example", {}) is None
    # after the streak the module hard-closes (session was set then cleared)
    assert sb._session is None or sb._session.exited


# --- contract 3: benching + paid failover --------------------------------------

async def test_failing_scrapling_benches_and_paid_takes_over(monkeypatch):
    """A scrapling hop that keeps failing must bench after the documented
    streaks, and _get_json_via must then hand the read to the paid
    provider — the §34 discipline applied to the free hop."""
    live_mode(monkeypatch)
    monkeypatch.setattr(config, "FIRECRAWL_API_KEY", "fc-x")

    async def dead_fetch(url, headers):
        return None
    monkeypatch.setattr(sb, "browser_fetch_json", dead_fetch)

    monkeypatch.setattr(crowd, "_BENCHED_UNTIL", {})
    monkeypatch.setattr(crowd, "_CONSECUTIVE_REJECTIONS", {})
    monkeypatch.setattr(crowd, "_CONSECUTIVE_ERRORS", {})

    # direct hop also fails (httpx 430-style refusal)
    class Refused:
        status_code = 430
        text = "refused"

        def json(self):
            return {}
    class RefusedClient:
        def __init__(self, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, headers=None, **kw):
            return Refused()
    monkeypatch.setattr(crowd.httpx, "AsyncClient", RefusedClient)

    # firecrawl (paid) answers with real JSON
    async def fake_paid(url, headers):
        return {"success": True, "responseObject": {"items": []}}
    monkeypatch.setattr(crowd, "_scrape_firecrawl", fake_paid)

    assert await crowd._get_json_via("https://x.example", {}) is not None
    assert await crowd._get_json_via("https://x.example", {}) is not None
    # two consecutive None-hops -> rejection streak -> benched (§34)
    assert crowd._is_benched("scrapling")


# --- contract 5: idle close -----------------------------------------------------

async def test_idle_close_drops_the_session(monkeypatch):
    live_mode(monkeypatch)
    closed = {"n": 0}

    class FakeSession:
        async def __aexit__(self, *a):
            closed["n"] += 1
            return False

    monkeypatch.setattr(sb, "_session", FakeSession())
    monkeypatch.setattr(config, "SCRAPLING_IDLE_CLOSE_SECONDS", 600)
    monkeypatch.setattr(sb, "_last_used", 0.0)
    monkeypatch.setattr(sb.time, "monotonic", lambda: 10_000.0)
    await sb.idle_housekeeping()
    assert closed["n"] == 1
    assert sb._session is None

