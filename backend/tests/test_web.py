"""Offline tests for the web-read stage (Firecrawl search)."""
import json
import httpx
import pytest

import config
from llm.web_research import summarize_hits, enrich_web
from models import Candidate


def _mk():
    return Candidate(symbol="TEST", mint_address="Mint1111", price_usd=0.001,
                     liquidity_usd=20000.0, volume_24h_usd=90000.0,
                     market_cap_usd=500000.0)


def test_summarize_hits():
    rows = [dict(title="T1", description="D1"), dict(title="T2", description="D2"), "junk"]
    s = summarize_hits(rows)
    assert "T1 - D1" in s and "T2 - D2" in s
    assert summarize_hits(None) == ""


def test_enrich_web_disabled_without_key(monkeypatch):
    monkeypatch.setattr(config, "FIRECRAWL_API_KEY", "")
    c = _mk()
    import asyncio
    assert asyncio.run(enrich_web([c])) == 0
    assert c.web_summary is None


def test_enrich_web_applies_summary(monkeypatch):
    monkeypatch.setattr(config, "FIRECRAWL_API_KEY", "KEY")
    seen = dict()
    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        body = json.dumps(dict(data=[dict(title="H1", description="D1")]))
        return httpx.Response(200, text=body)
    transport = httpx.MockTransport(handler)
    c = _mk()
    import asyncio
    n = asyncio.run(enrich_web([c], client=httpx.AsyncClient(transport=transport)))
    assert n == 1
    assert "H1 - D1" in c.web_summary
