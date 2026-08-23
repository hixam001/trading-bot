"""
tests/test_crowd.py — crowd conviction feeds (fomo.fun board, pump.fun
comments) and their wiring into crowd_heat.

ALL offline: HTTP is faked via a stub AsyncClient; no live network anywhere.
Covers: omo heat formula + clamps, fail-soft degradation (unconfigured token,
HTTP 500), response parsing (responseObject.items / authorTrade fields),
cache behavior, source priority (fomo > pumpfun > proxy), and the crowd_heat
rule consuming real feed heat with a tagged detail.
"""
from __future__ import annotations

import pytest

import config
import data_providers.crowd as crowd
from models import Candidate
from rule_engine import rules as rules_mod


MINT = "Mint1111111111111111111111111111111111111"


def make_candidate(**overrides) -> Candidate:
    base = dict(
        symbol="TEST", mint_address=MINT, price_usd=0.001,
        liquidity_usd=50_000.0, volume_24h_usd=100_000.0,
        market_cap_usd=100_000.0, volume_1h_usd=20_000.0,
        buys_1h=300, sells_1h=200, price_change_1h_pct=5.0,
        age_hours=48.0, has_twitter=True, has_telegram=True,
    )
    base.update(overrides)
    return Candidate(**base)


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    """Reset module globals + caches so tests never see each other's data."""
    monkeypatch.setattr(crowd, "_token", None)
    monkeypatch.setattr(crowd, "_token_expires_at", 0.0)
    monkeypatch.setattr(crowd, "_fomo_cache", crowd._TtlCache(60))
    monkeypatch.setattr(crowd, "_pump_cache", crowd._TtlCache(60))


class FakeResponse:
    def __init__(self, status_code: int = 200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class FakeClient:
    """Replaces httpx.AsyncClient inside data_providers.crowd."""

    get_result = None
    post_result = None
    get_calls = []
    post_calls = []

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, **kw):
        FakeClient.get_calls.append(url)
        return FakeClient.get_result or FakeResponse(200, {})

    async def post(self, url, **kw):
        FakeClient.post_calls.append((url, kw))
        return FakeClient.post_result or FakeResponse(200, {})


def install_fake_http(monkeypatch, get=None, post=None):
    FakeClient.get_result = get
    FakeClient.post_result = post
    FakeClient.get_calls = []
    FakeClient.post_calls = []
    monkeypatch.setattr(crowd.httpx, "AsyncClient", FakeClient)


# --- heat formula ----------------------------------------------------------------

def test_heat_from_count_matches_omo_formula():
    assert crowd.heat_from_count(0) == 20
    assert crowd.heat_from_count(2) == 36
    assert crowd.heat_from_count(5) == 60
    assert crowd.heat_from_count(10) == 100
    assert crowd.heat_from_count(99) == 100          # clamped
    assert crowd.heat_from_count(-3) == 20           # floor at zero items


# --- fomo feed ----------------------------------------------------------------------

async def test_fomo_unconfigured_returns_none(monkeypatch):
    monkeypatch.setattr(config, "FOMO_PRIVY_REFRESH_TOKEN", "")
    assert await crowd.fetch_fomo_theses(MINT) is None


async def test_fomo_parses_response_object_items(monkeypatch):
    monkeypatch.setattr(config, "FOMO_PRIVY_REFRESH_TOKEN", "refresh-token")
    install_fake_http(
        monkeypatch,
        get=FakeResponse(200, {"responseObject": {"items": [
            {"userHandle": "whale", "text": "floor holds",
             "authorTrade": {"usdValue": 12000.0, "unrealizedPnlUsd": 3400.0,
                             "realizedPnlUsd": 0.0, "closedAt": None}},
            {"displayName": "anon", "text": "gm",
             "authorTrade": {"usdValue": 5.0, "realizedPnlUsd": -2.0,
                             "closedAt": "2026-08-20"}},
        ]}}),
        post=FakeResponse(200, {"token": "x.y.z"}),
    )
    theses = await crowd.fetch_fomo_theses(MINT)
    assert theses is not None and len(theses) == 2
    assert theses[0]["who"] == "whale"
    assert theses[0]["size_usd"] == 12_000.0
    assert theses[0]["unrealized_usd"] == 3_400.0
    assert theses[1]["closed"] is True

    # Cached second call: no new HTTP GET.
    calls_before = len(FakeClient.get_calls)
    again = await crowd.fetch_fomo_theses(MINT)
    assert again == theses
    assert len(FakeClient.get_calls) == calls_before


async def test_fomo_http_500_fails_soft(monkeypatch):
    monkeypatch.setattr(config, "FOMO_PRIVY_REFRESH_TOKEN", "refresh-token")
    install_fake_http(monkeypatch, get=FakeResponse(500, {}),
                      post=FakeResponse(200, {"token": "t"}))
    assert await crowd.fetch_fomo_theses(MINT) is None


# --- pump.fun feed ---------------------------------------------------------------------

async def test_pump_comments_accept_list_and_dict_bodies(monkeypatch):
    install_fake_http(monkeypatch, get=FakeResponse(200, [
        {"user": "a", "text": "x"}, {"user": "b", "text": "y"}]))
    got = await crowd.fetch_pump_comments(MINT)
    assert len(got) == 2 and got[0]["who"] == "a"

    other = "OtherMint22222222222222222222222222222222222"
    install_fake_http(monkeypatch, get=FakeResponse(200, {"items": [
        {"user": "c", "text": "z"}]}))
    got2 = await crowd.fetch_pump_comments(other)
    assert len(got2) == 1 and got2[0]["who"] == "c"


async def test_pump_comments_fail_soft_on_error(monkeypatch):
    install_fake_http(monkeypatch, get=FakeResponse(404, {}))
    assert await crowd.fetch_pump_comments(MINT) is None


# --- enrichment priority: fomo > pumpfun > proxy -----------------------------------------

async def test_enrich_prefers_fomo_board(monkeypatch):
    async def fomo_ok(mint):
        return [{"who": "w", "text": "t"}] * 3        # 3 theses -> heat 44

    pump_called = []

    async def pump_spy(mint):
        pump_called.append(mint)
        return None

    monkeypatch.setattr(crowd, "fetch_fomo_theses", fomo_ok)
    monkeypatch.setattr(crowd, "fetch_pump_comments", pump_spy)

    c = make_candidate()
    await crowd.enrich_crowd_heat([c])
    assert c.fomo_heat == 44 and c.crowd_heat_source == "fomo"
    assert pump_called == []                          # never consulted


async def test_enrich_falls_back_to_pumpfun(monkeypatch):
    async def fomo_down(mint):
        return None

    async def pump_one(mint):
        return [{"who": "a", "text": "b"}]

    monkeypatch.setattr(crowd, "fetch_fomo_theses", fomo_down)
    monkeypatch.setattr(crowd, "fetch_pump_comments", pump_one)

    c = make_candidate()
    await crowd.enrich_crowd_heat([c])
    # 1 comment -> heat 28 (< band MIN 36): a real feed still wins over proxy.
    assert c.fomo_heat == 28 and c.crowd_heat_source == "pumpfun"


async def test_enrich_no_feed_leaves_proxy_intact(monkeypatch):
    async def both_down(mint):
        return None
    monkeypatch.setattr(crowd, "fetch_fomo_theses", both_down)
    monkeypatch.setattr(crowd, "fetch_pump_comments", both_down)

    c = make_candidate()
    await crowd.enrich_crowd_heat([c])
    assert c.fomo_heat is None and c.crowd_heat_source == ""


# --- the rule consumes real feed heat ---------------------------------------------------

def test_crowd_heat_rule_uses_real_feed_and_tags_source():
    r = rules_mod.crowd_heat(make_candidate(fomo_heat=44, crowd_heat_source="fomo"),
                             None, None)
    assert r.passed and "[fomo]" in r.detail and r.value == 44


def test_crowd_heat_rule_blocks_low_real_feed_heat():
    r = rules_mod.crowd_heat(make_candidate(fomo_heat=20, crowd_heat_source="fomo"),
                             None, None)
    assert not r.passed and "[fomo]" in r.detail


def test_crowd_heat_rule_proxy_tag_when_no_feed():
    r = rules_mod.crowd_heat(make_candidate(), None, None)
    assert r.passed and "[proxy]" in r.detail