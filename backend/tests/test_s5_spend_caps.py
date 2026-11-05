"""
backend/tests/test_s5_spend_caps.py — §92 S5 hard daily spend ceilings.

The ceilings are deliberately small, persistent, and fail-closed:

  * a paid call is reserved before the network request;
  * call/token/cost counters persist across restart and roll over at UTC
    midnight;
  * zero/negative caps mean zero calls (never "unlimited");
  * a corrupt or unreadable counter file refuses paid calls;
  * the degradation is a normal fail-closed result, so the entry path returns
    "pass" instead of buying;
  * exits do not call the model at all, so a spent LLM budget cannot disable
    a stop-loss;
  * one deduped alert per provider/day tells the operator.
"""
from __future__ import annotations

import inspect
import json
import stat

import httpx
import pytest

import config
import llm.narrator as narrator_mod
import run_live_cycle as rlc
from llm import spend_guard
from llm.client import LLMClient, LLMResult
from llm.thinker import Thinker
from models import Trade
from tests.test_rules import make_candidate


def _chat_body(content: str = "{}", prompt_tokens: int = 600,
               completion_tokens: int = 100) -> dict:
    return {
        "id": "chatcmpl-s5",
        "choices": [{"finish_reason": "stop",
                     "message": {"role": "assistant", "content": content}}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(config, "LIVE_STATE_DIR", state)
    monkeypatch.setattr(config, "LLM_DAILY_CALL_CAP", 3)
    monkeypatch.setattr(config, "LLM_DAILY_TOKEN_CAP", 10_000)
    monkeypatch.setattr(config, "LLM_DAILY_COST_CAP_USD", 5.0)
    monkeypatch.setattr(config, "SEARCH_DAILY_CALL_CAP", 3)
    spend_guard.reset_for_tests()
    yield state
    spend_guard.reset_for_tests()


@pytest.fixture
def alerts(monkeypatch):
    sent: list[tuple] = []

    def fake_alert(event, message, **kwargs):
        sent.append((event, message, kwargs))

    import alerts as alerts_mod
    monkeypatch.setattr(alerts_mod, "alert_nowait", fake_alert)
    return sent


def _state_file(state_dir):
    return state_dir / spend_guard.STATE_FILE_NAME


def test_llm_call_cap_is_hard_and_persists_across_restart(state_dir):
    spend_guard.reserve_llm("groq")
    spend_guard.reserve_llm("groq")
    spend_guard.reserve_llm("groq")

    with pytest.raises(spend_guard.SpendCapExceeded, match="call cap 3"):
        spend_guard.reserve_llm("groq")

    raw = json.loads(_state_file(state_dir).read_text())
    assert raw["providers"]["groq"]["calls"] == 3
    mode = stat.S_IMODE(_state_file(state_dir).stat().st_mode)
    assert mode == 0o600

    # A new process reads the same file: a restart cannot reset the day.
    with pytest.raises(spend_guard.SpendCapExceeded):
        spend_guard.reserve_llm("groq")


def test_token_cap_blocks_the_next_call(state_dir, alerts):
    monkeypatch_cap = 100
    config.LLM_DAILY_TOKEN_CAP = monkeypatch_cap
    spend_guard.reserve_llm("groq")
    spend_guard.record_llm("groq", tokens=monkeypatch_cap, cost_usd=0.01)

    with pytest.raises(spend_guard.SpendCapExceeded, match="token cap"):
        spend_guard.reserve_llm("groq")
    assert alerts and alerts[0][0].value == "SPEND_CAP"
    assert alerts[0][2]["kind"] == "llm"


def test_cost_cap_blocks_the_next_call(state_dir):
    config.LLM_DAILY_COST_CAP_USD = 1.0
    spend_guard.reserve_llm("deepseek")
    spend_guard.record_llm("deepseek", tokens=10, cost_usd=1.0)

    with pytest.raises(spend_guard.SpendCapExceeded, match="cost cap"):
        spend_guard.reserve_llm("deepseek")


def test_zero_or_negative_cap_means_zero_calls(state_dir):
    for cap in (0, -1):
        config.LLM_DAILY_CALL_CAP = cap
        with pytest.raises(spend_guard.SpendCapExceeded):
            spend_guard.reserve_llm("groq")

    config.SEARCH_DAILY_CALL_CAP = 0
    with pytest.raises(spend_guard.SpendCapExceeded):
        spend_guard.reserve_search()


def test_daily_rollover_resets_the_counters(state_dir, monkeypatch):
    monkeypatch.setattr(spend_guard, "_today", lambda: "2026-11-05")
    spend_guard.reserve_llm("groq")
    spend_guard.reserve_llm("groq")
    spend_guard.reserve_llm("groq")
    with pytest.raises(spend_guard.SpendCapExceeded):
        spend_guard.reserve_llm("groq")

    monkeypatch.setattr(spend_guard, "_today", lambda: "2026-11-06")
    spend_guard.reserve_llm("groq")
    state = spend_guard.status()
    assert state["day"] == "2026-11-06"
    assert state["providers"]["groq"]["calls"] == 1


def test_corrupt_state_fails_closed(state_dir):
    _state_file(state_dir).write_text("{not-json", encoding="utf-8")
    with pytest.raises(spend_guard.SpendCapExceeded, match="corrupt"):
        spend_guard.reserve_llm("groq")
    # The guard never silently replaces evidence of corruption.
    assert _state_file(state_dir).read_text() == "{not-json"


def test_unreadable_state_fails_closed(state_dir):
    # A directory at the file path makes read_text raise OSError.
    _state_file(state_dir).mkdir()
    with pytest.raises(spend_guard.SpendCapExceeded, match="unreadable"):
        spend_guard.reserve_llm("groq")


def test_alert_is_deduped_per_provider_per_day(state_dir, alerts):
    config.LLM_DAILY_CALL_CAP = 1
    spend_guard.reserve_llm("groq")
    for _ in range(3):
        with pytest.raises(spend_guard.SpendCapExceeded):
            spend_guard.reserve_llm("groq")
    assert len(alerts) == 1

    # Another provider is a separate budget: one allowed call, then blocked,
    # with its own alert.
    spend_guard.reserve_llm("deepseek")
    with pytest.raises(spend_guard.SpendCapExceeded):
        spend_guard.reserve_llm("deepseek")
    assert len(alerts) == 2
    assert {a[2]["provider"] for a in alerts} == {"groq", "deepseek"}


def test_llm_client_returns_spend_cap_without_touching_network(alerts):
    config.LLM_DAILY_CALL_CAP = 0
    called = {"n": 0}

    def handler(request):
        called["n"] += 1
        return httpx.Response(200, json=_chat_body())

    async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = LLMClient("groq", "https://example.invalid", "test-key", "m",
                       client=async_client)

    import asyncio
    result = asyncio.run(client.complete_json(
        task="thinker", system_prompt="s", user_prompt="u"))
    asyncio.run(client.aclose())

    assert result is not None
    assert result.text == ""
    assert result.degradation_reason.startswith("spend_cap:")
    assert called["n"] == 0
    assert alerts and alerts[0][0].value == "SPEND_CAP"


def test_llm_client_records_tokens_and_cost(state_dir):
    def handler(request):
        return httpx.Response(200, json=_chat_body("{}", 600, 100))

    async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = LLMClient("groq", "https://example.invalid", "test-key", "m",
                       client=async_client)

    import asyncio
    result = asyncio.run(client.complete_json(
        task="thinker", system_prompt="s", user_prompt="u"))
    asyncio.run(client.aclose())

    assert result and result.text == "{}"
    state = spend_guard.status()["providers"]["groq"]
    assert state["calls"] == 1
    assert state["tokens"] == 700
    assert state["cost_usd"] > 0


def test_thinker_spend_cap_degrades_to_pass(monkeypatch):
    class FakeClient:
        provider = "groq"
        model = "test-model"

        async def complete_json(self, **kwargs):
            return LLMResult(
                text="", provider="groq", model="test-model", task="thinker",
                degradation_reason="spend_cap: llm call cap 3 (used 3)")

    monkeypatch.setattr(config, "DATA_BACKEND", "live")
    import llm.thinker as thinker_mod
    monkeypatch.setattr(thinker_mod, "build_main_client", lambda: FakeClient())

    result = __import__("asyncio").run(Thinker().think(make_candidate()))
    assert result.verdict == "pass"
    assert result.wants_entry is False
    assert result.source.startswith("degraded:spend_cap:")


def test_search_cap_stops_calling_the_transport(state_dir, monkeypatch):
    import llm.web_research as web
    calls = {"n": 0}

    async def fake_search(client, symbol):
        calls["n"] += 1
        return [{"title": "ok", "description": "evidence"}], 200, ""

    monkeypatch.setattr(web, "_configured_searchers",
                        lambda: [("fake", fake_search)])
    config.SEARCH_DAILY_CALL_CAP = 1

    import asyncio
    first = asyncio.run(web.search_web(None, "TEST"))
    second = asyncio.run(web.search_web(None, "TEST"))

    assert "evidence" in first
    assert second == ""
    assert calls["n"] == 1


def test_exit_scan_has_no_llm_dependency():
    manage_src = inspect.getsource(rlc._manage)
    scan_src = inspect.getsource(rlc._scan_exits_for_position)
    assert "llm" not in manage_src.lower()
    assert "llm" not in scan_src.lower()
    # Post-close reflection is scheduled as a separate task, never awaited by
    # the sell path.
    source = inspect.getsource(rlc)
    assert "asyncio.create_task(_store_live_reflection(" in source


async def test_reflection_failure_cannot_escape_into_the_exit_path(monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("provider down")

    class Ctx:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *exc):
            return False

    async def fake_trade(conn, trade_id):
        return Trade(trade_id=trade_id, symbol="TEST", is_open=False)

    monkeypatch.setattr(narrator_mod, "generate_reflection", boom)
    monkeypatch.setattr(rlc.db, "get_db", lambda: Ctx())
    monkeypatch.setattr(rlc.db, "get_trade_by_id", fake_trade)

    # Fire-and-forget contract: the exception is logged, never raised.
    await rlc._store_live_reflection("live-TEST", "exit stop")
