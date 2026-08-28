"""
tests/test_llm_provider_swap.py — handoff §18: main LLM Groq -> DeepSeek.

Covers the provider-neutral main path without touching any real API:
  * DeepSeekClient / MainGroqClient construction + provider-aware timeouts
  * build_main_client() factory selection incl. fail-closed on unknown value
  * main_max_tokens() budget follows the selected provider
  * _estimate_cost(): groq unchanged; deepseek off-peak / peak (2x) /
    cache-hit branches against hand-computed values (docs/08 §3 anchor)
  * complete_json() against mocked DeepSeek responses: usage parsing incl.
    prompt_cache_hit_tokens, degradation reasons, missing-key fail-closed
  * Thinker + Narrator source labels and fail-closed degradation
    (a provider failure yields a template PASS, never a buy)
  * Reflection peak-window skip (docs/08 §5)
  * /api/system-status narration_mode reports the active main provider

HTTP is mocked with httpx.MockTransport (built into httpx — no new deps).
"""
from __future__ import annotations

import json

import httpx
import pytest
import pytest_asyncio

import config
import llm.client as client_mod
import llm.narrator as narrator_mod
from llm.client import (
    DeepSeekClient,
    GroqClient,
    MainGroqClient,
    _estimate_cost,
    build_main_client,
    main_max_tokens,
)
from llm.narrator import Narrator, generate_reflection
from llm.thinker import Thinker
from models import GateDecision, RuleResult, Trade
from tests.test_rules import make_candidate


# ---------------------------------------------------------------- helpers ----

def _chat_body(content: str, prompt_tokens=600, completion_tokens=100,
               cache_hit=None) -> dict:
    usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    if cache_hit is not None:
        usage["prompt_cache_hit_tokens"] = cache_hit
    return {
        "id": "chatcmpl-test",
        "choices": [{
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": content},
        }],
        "usage": usage,
    }


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _ok_handler(body: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)
    return handler


VERDICT_JSON = json.dumps({
    "thesis": "1h volume $20,000 with 300 buys vs 200 sells.",
    "invalidation": "wrong if buys drop below sells within the hour.",
    "verdict": "buy",
    "break": {"taking": False, "minutes": 0, "reason": ""},
})


@pytest.fixture(autouse=True)
def _off_peak(monkeypatch):
    """Deterministic pricing window for every test in this module."""
    monkeypatch.setattr(client_mod, "_is_peak_window", lambda: False)
    monkeypatch.setattr(narrator_mod, "_is_peak_window", lambda: False)


# ------------------------------------------------------------ construction ---

def test_deepseek_client_construction():
    c = DeepSeekClient()
    assert c.provider == "deepseek"
    assert c.model == config.DEEPSEEK_MODEL
    assert c.base_url == config.DEEPSEEK_BASE_URL.rstrip("/")
    assert c.is_main is True
    assert c.timeout_seconds == config.DEEPSEEK_TIMEOUT_SECONDS


def test_main_groq_client_construction():
    c = MainGroqClient()
    assert c.provider == "groq"
    assert c.model == config.GROQ_MODEL
    assert c.is_main is True
    assert c.timeout_seconds == config.GROQ_TIMEOUT_SECONDS


def test_social_client_timeout_unchanged():
    c = GroqClient()
    assert c.timeout_seconds == config.SOCIAL_LLM_TIMEOUT_SECONDS
    assert not getattr(c, "is_main", False)


# ---------------------------------------------------------------- factory ----

def test_factory_selects_deepseek(monkeypatch):
    monkeypatch.setattr(config, "MAIN_LLM_PROVIDER", "deepseek")
    c = build_main_client()
    assert isinstance(c, DeepSeekClient)


def test_factory_selects_groq(monkeypatch):
    monkeypatch.setattr(config, "MAIN_LLM_PROVIDER", "groq")
    c = build_main_client()
    assert isinstance(c, MainGroqClient)


def test_factory_unknown_value_fails_closed_to_groq(monkeypatch):
    monkeypatch.setattr(config, "MAIN_LLM_PROVIDER", "openai")
    c = build_main_client()
    assert isinstance(c, MainGroqClient)


def test_main_max_tokens_follows_provider(monkeypatch):
    monkeypatch.setattr(config, "MAIN_LLM_PROVIDER", "deepseek")
    assert main_max_tokens() == config.DEEPSEEK_MAX_TOKENS
    monkeypatch.setattr(config, "MAIN_LLM_PROVIDER", "groq")
    assert main_max_tokens() == config.GROQ_MAX_TOKENS
    monkeypatch.setattr(config, "MAIN_LLM_PROVIDER", "bogus")
    assert main_max_tokens() == config.GROQ_MAX_TOKENS


# ------------------------------------------------------------ cost model -----

def test_estimate_cost_groq_unchanged():
    # 1M input @ $0.80 + 1M output @ $4.00
    assert _estimate_cost("groq", 1_000_000, 1_000_000, 0, False) == pytest.approx(4.80)


def test_estimate_cost_deepseek_offpeak_docs_anchor():
    # docs/08 §3 planning example: 500 input / 100 output -> $0.000176
    assert _estimate_cost("deepseek", 500, 100, 0, False) == pytest.approx(0.000176)


def test_estimate_cost_deepseek_peak_doubles():
    assert _estimate_cost("deepseek", 500, 100, 0, True) == pytest.approx(0.000352)


def test_estimate_cost_deepseek_cache_hit_rate():
    # 1M cached input tokens: $0.007 off-peak, $0.014 peak
    assert _estimate_cost("deepseek", 0, 0, 1_000_000, False) == pytest.approx(0.007)
    assert _estimate_cost("deepseek", 0, 0, 1_000_000, True) == pytest.approx(0.014)


def test_estimate_cost_unknown_provider_zero():
    assert _estimate_cost("template", 500, 100, 0, False) == 0.0


# ------------------------------------------------------- complete_json -------

async def test_complete_json_parses_deepseek_usage_and_cache():
    # 600 prompt tokens with 100 cached -> 500 billed as cache-miss input.
    c = DeepSeekClient(client=_mock_client(_ok_handler(_chat_body("hello", cache_hit=100))))
    c.api_key = "test-key"
    res = await c.complete_json(task="t", system_prompt="s", user_prompt="u")
    assert res is not None and res.text == "hello"
    assert res.input_tokens == 500
    assert res.cache_hit_tokens == 100
    assert res.output_tokens == 100
    # 500*$0.22/1M + 100*$0.007/1M + 100*$0.66/1M (off-peak)
    assert res.estimated_cost_usd == pytest.approx(0.00011 + 0.0000007 + 0.000066)
    assert res.pricing_snapshot_id == "deepseek_20260827"
    assert res.is_peak_window is False
    assert res.request_id == "chatcmpl-test"
    await c.aclose()


async def test_complete_json_sends_json_mode_and_model():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json=_chat_body("ok"))

    c = DeepSeekClient(client=_mock_client(handler))
    c.api_key = "test-key"
    await c.complete_json(task="t", system_prompt="s", user_prompt="u", json_mode=True)
    assert seen["payload"]["model"] == config.DEEPSEEK_MODEL
    assert seen["payload"]["response_format"] == {"type": "json_object"}
    # V4 Flash defaults to thinking mode, which empties `content` within our
    # token budget — the hot path must always disable it (handoff §18).
    assert seen["payload"]["thinking"] == {"type": "disabled"}
    assert seen["auth"] == "Bearer test-key"  # key only in the header
    await c.aclose()


async def test_complete_json_groq_payload_has_no_thinking_param():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_chat_body("ok"))

    c = MainGroqClient(client=_mock_client(handler))
    c.api_key = "test-key"
    await c.complete_json(task="t", system_prompt="s", user_prompt="u")
    assert "thinking" not in seen["payload"]
    await c.aclose()


async def test_complete_json_missing_key_fails_closed(monkeypatch):
    monkeypatch.setattr(config, "DEEPSEEK_API_KEY", "")
    c = DeepSeekClient()
    assert await c.complete_json(task="t", system_prompt="s", user_prompt="u") is None
    await c.aclose()


async def test_complete_json_http_500_degrades():
    c = DeepSeekClient(client=_mock_client(lambda r: httpx.Response(500, json={})))
    c.api_key = "test-key"
    res = await c.complete_json(task="t", system_prompt="s", user_prompt="u")
    assert res is not None and res.text == ""
    assert res.degradation_reason == "http_500"
    await c.aclose()


async def test_complete_json_empty_content_degrades():
    body = _chat_body("   ")
    c = DeepSeekClient(client=_mock_client(_ok_handler(body)))
    c.api_key = "test-key"
    res = await c.complete_json(task="t", system_prompt="s", user_prompt="u")
    assert res is not None and res.text == ""
    assert res.degradation_reason == "empty_content"
    await c.aclose()


async def test_complete_json_peak_window_flag_and_cost(monkeypatch):
    """Regression: is_peak must come from _is_peak_window(), not a hardcoded
    False — peak cost is exactly 2x off-peak (DeepSeek billing)."""
    monkeypatch.setattr(client_mod, "_is_peak_window", lambda: True)
    c = DeepSeekClient(client=_mock_client(_ok_handler(_chat_body("hello"))))
    c.api_key = "test-key"
    res = await c.complete_json(task="t", system_prompt="s", user_prompt="u")
    assert res is not None and res.is_peak_window is True
    # 600 input @ $0.44/1M + 100 output @ $1.32/1M (peak)
    assert res.estimated_cost_usd == pytest.approx(600 / 1e6 * 0.44 + 100 / 1e6 * 1.32)
    await c.aclose()


async def test_complete_json_groq_never_marks_peak(monkeypatch):
    """Groq has no peak pricing — the flag must stay False even inside the
    DeepSeek peak window."""
    monkeypatch.setattr(client_mod, "_is_peak_window", lambda: True)
    c = MainGroqClient(client=_mock_client(_ok_handler(_chat_body("hi"))))
    c.api_key = "test-key"
    res = await c.complete_json(task="t", system_prompt="s", user_prompt="u")
    assert res is not None and res.is_peak_window is False
    await c.aclose()


# ---------------------------------------------------------------- thinker ----

async def test_thinker_deepseek_source_label(monkeypatch):
    monkeypatch.setattr(config, "DATA_BACKEND", "live")
    monkeypatch.setattr(config, "MAIN_LLM_PROVIDER", "deepseek")
    t = Thinker()
    assert isinstance(t._main_llm, DeepSeekClient)
    t._main_llm._client = _mock_client(_ok_handler(_chat_body(VERDICT_JSON)))
    t._main_llm.api_key = "test-key"
    res = await t.think(make_candidate())
    assert res.verdict == "buy"
    assert res.source == f"deepseek:{config.DEEPSEEK_MODEL}"
    assert res.llm_usage is not None and res.llm_usage.provider == "deepseek"
    await t.aclose()


async def test_thinker_provider_failure_degrades_to_pass_never_buy(monkeypatch):
    """make_candidate() is a template BUY (buys>sells, +5% 1h) — a provider
    failure must still force the verdict to pass (fail-closed, docs/08 §2)."""
    monkeypatch.setattr(config, "DATA_BACKEND", "live")
    monkeypatch.setattr(config, "MAIN_LLM_PROVIDER", "deepseek")
    t = Thinker()
    t._main_llm._client = _mock_client(lambda r: httpx.Response(500, json={}))
    t._main_llm.api_key = "test-key"
    res = await t.think(make_candidate())
    assert res.verdict == "pass"
    assert res.source.startswith("degraded:")
    assert not res.wants_entry
    await t.aclose()


async def test_thinker_malformed_json_degrades_to_pass(monkeypatch):
    monkeypatch.setattr(config, "DATA_BACKEND", "live")
    monkeypatch.setattr(config, "MAIN_LLM_PROVIDER", "deepseek")
    t = Thinker()
    t._main_llm._client = _mock_client(_ok_handler(_chat_body("not json at all")))
    t._main_llm.api_key = "test-key"
    res = await t.think(make_candidate())
    assert res.verdict == "pass"
    assert res.source.startswith("degraded:")
    await t.aclose()


# ---------------------------------------------------------------- narrator ---

def _gate():
    c = make_candidate()
    rules = [
        RuleResult(rule_id="liquidity_floor", passed=True, detail="liq $50,000 >= $15,000"),
        RuleResult(rule_id="volume_alive", passed=False, detail="1h vol $20,000"),
    ]
    return GateDecision(candidate=c, rules=rules, all_passed=False,
                        failed_rule_ids=["volume_alive"])


async def test_narrator_deepseek_source_label(monkeypatch):
    monkeypatch.setattr(config, "DATA_BACKEND", "live")
    monkeypatch.setattr(config, "MAIN_LLM_PROVIDER", "deepseek")
    n = Narrator()
    assert isinstance(n._main_llm, DeepSeekClient)
    n._main_llm._client = _mock_client(_ok_handler(_chat_body("Rejected TEST: volume_alive failed.")))
    n._main_llm.api_key = "test-key"
    res = await n.narrate(_gate())
    assert res.thesis == "Rejected TEST: volume_alive failed."
    assert res.source == f"deepseek:{config.DEEPSEEK_MODEL}"
    await n.aclose()


# ------------------------------------------- item 1: narration anti-repetition

def test_template_opener_rotates_across_calls():
    # Same gate every time — only the framing opener should vary.
    outputs = {narrator_mod._template_thesis(_gate()) for _ in range(6)}
    # The reject gate cites the same failing rule, but the rotation must
    # produce more than one distinct framing sentence over 6 calls.
    assert len(outputs) > 1
    # Every variant still names the symbol and the failing rule (grounded).
    for text in outputs:
        assert "TEST" in text
        assert "volume_alive" in text


def test_build_prompt_carries_a_style_angle():
    prompt = narrator_mod.build_prompt(_gate())
    assert "Style for this one:" in prompt
    # The angle is one of the curated set (style-only, no new facts).
    angle = prompt.split("Style for this one:", 1)[1].strip()
    assert angle in narrator_mod._ANGLES


def test_angle_rotation_advances():
    before = narrator_mod._angle_index
    narrator_mod._next_angle()
    assert narrator_mod._angle_index == before + 1


async def test_narrator_provider_failure_falls_back_to_template(monkeypatch):
    monkeypatch.setattr(config, "DATA_BACKEND", "live")
    monkeypatch.setattr(config, "MAIN_LLM_PROVIDER", "deepseek")
    n = Narrator()
    n._main_llm._client = _mock_client(lambda r: httpx.Response(500, json={}))
    n._main_llm.api_key = "test-key"
    res = await n.narrate(_gate())
    assert res.source.startswith("degraded:")
    assert "TEST" in res.thesis  # grounded template narration of the same gate
    await n.aclose()


# ------------------------------------------------------------- reflection ----

def _closed_trade() -> Trade:
    return Trade(symbol="TEST", exit_reason="stop_loss",
                 entry_price_usd=0.001, exit_price_usd=0.0008,
                 realized_pnl_usd=-1.5, realized_pnl_pct=-15.0)


async def test_reflection_skips_llm_during_deepseek_peak_window(monkeypatch):
    monkeypatch.setattr(config, "DATA_BACKEND", "live")
    monkeypatch.setattr(config, "MAIN_LLM_PROVIDER", "deepseek")
    monkeypatch.setattr(narrator_mod, "_is_peak_window", lambda: True)

    class _BoomNarrator:
        def __init__(self):
            raise AssertionError("reflection must not call the LLM during the deepseek peak window")

    monkeypatch.setattr(narrator_mod, "Narrator", _BoomNarrator)
    text = await generate_reflection(_closed_trade(), "liq+vol passed")
    assert text.startswith("[template reflection]")


async def test_reflection_uses_provider_off_peak(monkeypatch):
    monkeypatch.setattr(config, "DATA_BACKEND", "live")
    monkeypatch.setattr(config, "MAIN_LLM_PROVIDER", "deepseek")
    ds = DeepSeekClient(client=_mock_client(_ok_handler(_chat_body("Reflected."))))
    ds.api_key = "test-key"
    monkeypatch.setattr(narrator_mod, "build_main_client", lambda: ds)
    text = await generate_reflection(_closed_trade(), "liq+vol passed")
    assert text == "Reflected."


# ------------------------------------------------- system-status endpoint ----

@pytest_asyncio.fixture
async def status_client(tmp_path, monkeypatch):
    from api import db
    from api.main import app

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "status.db")
    monkeypatch.setattr(config, "INGESTED_KNOWLEDGE_DIR", tmp_path / "ingested")
    await db.init_db()

    async def _no_ollama(self):
        return False

    monkeypatch.setattr(Narrator, "check_ollama_health", _no_ollama)
    app.state.narrator = Narrator()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_narration_mode_reports_deepseek(status_client, monkeypatch):
    monkeypatch.setattr(config, "DATA_BACKEND", "live")
    monkeypatch.setattr(config, "MAIN_LLM_PROVIDER", "deepseek")
    r = await status_client.get("/api/system-status")
    assert r.status_code == 200
    assert r.json()["narration_mode"] == "deepseek"


async def test_narration_mode_reports_groq(status_client, monkeypatch):
    monkeypatch.setattr(config, "DATA_BACKEND", "live")
    monkeypatch.setattr(config, "MAIN_LLM_PROVIDER", "groq")
    r = await status_client.get("/api/system-status")
    assert r.status_code == 200
    assert r.json()["narration_mode"] == "groq"


async def test_narration_mode_template_in_mock(status_client, monkeypatch):
    monkeypatch.setattr(config, "DATA_BACKEND", "mock")
    monkeypatch.setattr(config, "MAIN_LLM_PROVIDER", "deepseek")
    r = await status_client.get("/api/system-status")
    assert r.status_code == 200
    assert r.json()["narration_mode"] == "template"

