from __future__ import annotations

import httpx
import pytest

import config
from llm.client import DeepSeekJSONClient
from llm.thinker import Thinker
from tests.test_rules import make_candidate


def _client(body: dict, status_code: int = 200) -> DeepSeekJSONClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=body, request=request)

    transport = httpx.MockTransport(handler)
    return DeepSeekJSONClient(httpx.AsyncClient(transport=transport))


@pytest.mark.asyncio
async def test_deepseek_json_client_captures_completion_metadata(monkeypatch):
    monkeypatch.setattr(config, "DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(config, "DEEPSEEK_MODEL", "deepseek-test")
    client = _client({
        "id": "req-1",
        "choices": [{"message": {"content": '{"verdict":"pass"}'}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 5,
                   "total_tokens": 17},
    })
    try:
        result = await client.complete_json("system", "user")
    finally:
        await client.aclose()
    assert result is not None
    assert result.provider == "deepseek"
    assert result.model == "deepseek-test"
    assert result.request_id == "req-1"
    assert (result.input_tokens, result.output_tokens, result.total_tokens) == (12, 5, 17)
    assert result.response_hash


@pytest.mark.asyncio
async def test_deepseek_client_rejects_malformed_response(monkeypatch):
    monkeypatch.setattr(config, "DEEPSEEK_API_KEY", "test-key")
    client = _client({"choices": []})
    try:
        assert await client.complete_json("system", "user") is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_live_thinker_provider_failure_is_pass(monkeypatch):
    monkeypatch.setattr(config, "DATA_BACKEND", "live")

    class FailedProvider:
        async def complete_json(self, system_prompt, user_prompt):
            return None

        async def aclose(self):
            pass

    thinker = Thinker()
    thinker._deepseek = FailedProvider()
    result = await thinker.think(make_candidate(buys_1h=500, sells_1h=100))
    assert result.verdict == "pass"
    assert result.source == "degraded:deepseek-unavailable"