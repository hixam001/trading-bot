"""Offline tests for the provider-agnostic social read stage."""
import json
import httpx
import pytest

import config
from llm.social import SocialReader, enrich_social, parse_social
from llm.thinker import build_think_prompt
from models import Candidate


def _mk(**kw):
    return Candidate(symbol="TEST", mint_address="Mint1111", price_usd=0.001,
                     liquidity_usd=20000.0, volume_24h_usd=90000.0,
                     market_cap_usd=500000.0, **kw)


def test_parse_social():
    payload = json.dumps({"interest": "peaked", "note": "n"})
    assert parse_social("noise " + payload) == ("peaked", "n")
    assert parse_social(chr(123)) is None
    bad = json.dumps({"interest": "moon", "note": "n"})
    assert parse_social(bad) is None


def test_disabled_without_key(monkeypatch):
    monkeypatch.setattr(config, "SOCIAL_LLM_API_KEY", "")
    c = _mk()
    import asyncio
    assert asyncio.run(enrich_social([c])) == 0
    assert c.social_interest is None


def test_read_request_shape_and_parse(monkeypatch):
    monkeypatch.setattr(config, "SOCIAL_LLM_API_KEY", "KEY")
    seen = {}
    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        body = json.dumps({"choices": [{"message": {"content": "{" + chr(34) + "interest" + chr(34) + ": " + chr(34) + "organic" + chr(34) + ", " + chr(34) + "note" + chr(34) + ": " + chr(34) + "fresh bids" + chr(34) + "}"}}]})
        return httpx.Response(200, text=body)
    transport = httpx.MockTransport(handler)
    reader = SocialReader(client=httpx.AsyncClient(transport=transport))
    import asyncio
    res = asyncio.run(reader.read(_mk()))
    assert seen["path"].endswith("/chat/completions")
    assert seen["auth"] == "Bearer KEY"
    assert res == ("organic", "fresh bids")


def test_think_prompt_includes_social_line():
    c = _mk(social_interest="organic", social_note="fresh bids, two-sided tape")
    prompt = build_think_prompt(c)
    assert "Social read (evidence only): interest looks organic" in prompt
    c2 = _mk()
    assert "Social read" not in build_think_prompt(c2)
