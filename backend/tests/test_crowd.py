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
    monkeypatch.setattr(crowd, "_sessions", {})
    monkeypatch.setattr(crowd, "_fomo_cache", crowd._TtlCache(60))
    monkeypatch.setattr(config, "FIRECRAWL_API_KEY", "")


class FakeResponse:
    def __init__(self, status_code: int = 200, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self._text = text
        import json as _json
        if not self._text and self._payload:
            self._text = _json.dumps(self._payload)

    @property
    def text(self) -> str:
        return self._text

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
            {"userHandle": "whale", "ticker": "WHALE",
             "comment": {"comment": "floor holds", "numLikes": 3,
                         "olderThesis": 0, "newerThesis": 0},
             "authorTrade": {"usdValue": 12000.0, "unrealizedPnlUsd": 3400.0,
                             "percentageUnrealizedPnl": 39.5,
                             "realizedPnlUsd": 0.0, "closedAt": None}},
            {"displayName": "anon", "ticker": "WHALE",
             "comment": {"comment": "gm"},
             "authorTrade": {"usdValue": 5.0, "realizedPnlUsd": -2.0,
                             "closedAt": "2026-08-20"}},
        ]}}),
        post=FakeResponse(200, {"token": "x.y.z"}),
    )
    data = await crowd.fetch_fomo_theses(MINT)
    assert data is not None
    # "gm" is junk (<3 chars) -> dropped by _is_substantive:
    assert len(data["theses"]) == 1 and data["theses"][0]["who"] == "whale"
    assert data["total"] == 2          # older+newer+page = 0+0+2
    t = data["theses"][0]
    assert t["who"] == "whale"
    assert t["size_usd"] == 12_000.0
    assert t["unrealized_usd"] == 3_400.0
    assert t["pnl_pct"] == pytest.approx(39.5)

    # Cached second call: no new HTTP GET.
    calls_before = len(FakeClient.get_calls)
    again = await crowd.fetch_fomo_theses(MINT)
    assert again == data
    assert len(FakeClient.get_calls) == calls_before


def test_substantive_filter_drops_junk():
    assert crowd._is_substantive("trust the bot")            # real thesis
    assert not crowd._is_substantive("🚀")                    # emoji only
    assert not crowd._is_substantive("join https://discord.gg/abc")  # invite
    assert crowd._is_substantive("https://x.com/a but also real analysis here")


async def test_fomo_http_500_fails_soft(monkeypatch):
    monkeypatch.setattr(config, "FOMO_PRIVY_REFRESH_TOKEN", "refresh-token")
    install_fake_http(monkeypatch, get=FakeResponse(500, {}),
                      post=FakeResponse(200, {"token": "t"}))
    assert await crowd.fetch_fomo_theses(MINT) is None


# --- transport: direct -> firecrawl stealth fallback --------------------------

async def test_fomo_falls_back_to_firecrawl_on_challenge(monkeypatch):
    """Direct GET returns the Cloudflare challenge page; Firecrawl stealth
    scrape returns the JSON. The parsed result must be identical."""
    monkeypatch.setattr(config, "FOMO_PRIVY_REFRESH_TOKEN", "refresh-token")
    monkeypatch.setattr(config, "FIRECRAWL_API_KEY", "fc-key")

    challenge = FakeResponse(
        403, text="<!DOCTYPE html><html class='no-js'>Just a moment...</html>")
    payload = {"responseObject": {"items": [
        {"userHandle": "whale", "ticker": "WHALE",
         "comment": {"comment": "floor holds"},
         "authorTrade": {"usdValue": 9000.0, "unrealizedPnlUsd": 1200.0}}]}}

    calls = {"direct": 0, "firecrawl": 0}

    class RoutedClient(FakeClient):
        async def get(self, url, headers=None, **kw):
            if url.startswith(config.FOMO_API_BASE):
                calls["direct"] += 1
                return challenge
            return FakeResponse(200, {})

        async def post(self, url, **kw):
            if "firecrawl" in url:
                calls["firecrawl"] += 1
                return FakeResponse(200, {"data": {
                    "rawHtml": __import__("json").dumps(payload),
                    "metadata": {"statusCode": 200}}})
            # privy session mint
            return FakeResponse(200, {"token": "x.y.z"})

    monkeypatch.setattr(crowd.httpx, "AsyncClient", RoutedClient)

    data = await crowd.fetch_fomo_theses(MINT)
    assert data is not None and len(data["theses"]) == 1
    assert data["theses"][0]["size_usd"] == 9_000.0
    assert calls == {"direct": 1, "firecrawl": 1}


# --- enrichment: fomo board is the sole real source (pump deferred) -----------

async def test_enrich_uses_fomo_board(monkeypatch):
    async def fomo_ok(mint):
        return {"theses": [{"who": "w", "text": "t"}] * 3, "total": 3}

    monkeypatch.setattr(crowd, "fetch_fomo_theses", fomo_ok)

    c = make_candidate()
    await crowd.enrich_crowd_heat([c])
    assert c.fomo_heat == 44 and c.crowd_heat_source == "fomo"


async def test_enrich_no_feed_leaves_proxy_intact(monkeypatch):
    async def fomo_down(mint):
        return None
    monkeypatch.setattr(crowd, "fetch_fomo_theses", fomo_down)

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