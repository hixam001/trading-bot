"""
§60 tests — dashboard freshness surfaces (read-path only; no money math).

  1. live_book._wallet_balances: TTL reuse (one RPC per window), stale
     reuse during an RPC outage (fail-soft), unknown past 3x TTL, and an
     EMPTY wallet being a good cached read ({} is an answer, None is not).
  2. system_status._llm_health: verdict cached inside the window,
     re-probed after it, probe exceptions cached as False.

The client's own health() probe has no TTL (§56 deferred item); the §60
surfaces re-probe on their own cadence instead of trusting it forever.
"""
from __future__ import annotations

import time

import pytest

import config
from api.routes import live_book as lb
from api.routes import system_status as ss


@pytest.fixture(autouse=True)
def _reset_caches():
    lb._scan_cache.update(balances=None, at=0.0, good_at=0.0)
    ss._health_cache.update(ok=None, at=0.0)
    yield
    lb._scan_cache.update(balances=None, at=0.0, good_at=0.0)
    ss._health_cache.update(ok=None, at=0.0)


def _patch_scan(monkeypatch, result, calls):
    async def fake(pubkey):
        calls.append(pubkey)
        return result

    from live_execution import solana

    monkeypatch.setattr(solana, "get_token_balances", fake)


async def test_wallet_scan_ttl_reuses_one_read(monkeypatch):
    calls: list = []
    _patch_scan(monkeypatch, {"MINTA": 1.0}, calls)
    got, stale = await lb._wallet_balances("PK")
    assert got == {"MINTA": 1.0} and stale is False
    got2, stale2 = await lb._wallet_balances("PK")
    assert got2 == {"MINTA": 1.0} and stale2 is False
    assert len(calls) == 1  # one RPC per TTL window, not per poll


async def test_wallet_scan_stale_reuse_during_outage(monkeypatch):
    calls: list = []
    _patch_scan(monkeypatch, {"MINTA": 1.0}, calls)
    await lb._wallet_balances("PK")
    ttl = config.WALLET_SCAN_TTL_SECONDS
    lb._scan_cache["at"] = time.monotonic() - ttl * 1.5
    lb._scan_cache["good_at"] = time.monotonic() - ttl * 1.5
    _patch_scan(monkeypatch, None, calls)  # RPC down
    got, stale = await lb._wallet_balances("PK")
    assert got == {"MINTA": 1.0} and stale is True  # fail-soft stale reuse


async def test_wallet_scan_unknown_after_outage_window(monkeypatch):
    calls: list = []
    _patch_scan(monkeypatch, {"MINTA": 1.0}, calls)
    await lb._wallet_balances("PK")
    ttl = config.WALLET_SCAN_TTL_SECONDS
    lb._scan_cache["at"] = time.monotonic() - ttl * 5
    lb._scan_cache["good_at"] = time.monotonic() - ttl * 5
    _patch_scan(monkeypatch, None, calls)
    got, stale = await lb._wallet_balances("PK")
    assert got is None and stale is False  # unknown, never fabricated


async def test_wallet_scan_empty_wallet_is_a_good_read(monkeypatch):
    calls: list = []
    _patch_scan(monkeypatch, {}, calls)  # answered read: wallet is empty
    got, stale = await lb._wallet_balances("PK")
    assert got == {} and stale is False
    await lb._wallet_balances("PK")
    assert len(calls) == 1  # {} is a good value; None would not be cached


async def test_health_ttl_cached_then_reprobed(monkeypatch):
    class _FakeLLM:
        def __init__(self):
            self.probes = 0

        async def health(self):
            self.probes += 1
            return self.probes == 1  # up first, down after

    class _FakeNarrator:
        def __init__(self):
            self._main_llm = _FakeLLM()

    n = _FakeNarrator()
    assert await ss._llm_health(n) is True
    assert await ss._llm_health(n) is True  # cached inside the window
    assert n._main_llm.probes == 1
    ttl = config.LLM_HEALTH_TTL_SECONDS
    ss._health_cache["at"] = time.monotonic() - ttl * 2
    assert await ss._llm_health(n) is False  # re-probed after the TTL
    assert n._main_llm.probes == 2


async def test_health_probe_exception_caches_false():
    class _FakeLLM:
        async def health(self):
            raise RuntimeError("down")

    class _FakeNarrator:
        _main_llm = _FakeLLM()

    assert await ss._llm_health(_FakeNarrator()) is False
    assert ss._health_cache["ok"] is False