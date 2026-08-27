"""
tests/test_llm_brain.py — the reference-style brain (routing, prompt, parse/validate,
verdict mapping, wallet mimicry). ALL offline: providers are faked; no network.

Covers the defense-first guarantees that make the brain safe to sit in front of
the money path:
  * fail CLOSED — mock mode, keyless provider, unparsable body, or a missing
    verdict all degrade to an empty verdict map (never a buy);
  * strict validation — invented symbols and invalid calls are dropped;
  * verdict mapping — only a valid "buying" sets wants_entry, and that is still
    only a NECESSARY input to the deterministic gate (main.py keeps the AND);
  * honest routing — a fallback is labelled degraded, never claimed as primary.
"""
from __future__ import annotations

import pytest

import config
import llm.llm_brain as ob
from llm.client import LLMResult
from models import Candidate, PortfolioState, Trade


def make_candidate(symbol="ALPHA", **o) -> Candidate:
    base = dict(
        symbol=symbol, mint_address=symbol.lower() + "mint1111111111111111111",
        price_usd=0.001, liquidity_usd=50_000.0, market_cap_usd=100_000.0,
        volume_24h_usd=100_000.0,
        volume_1h_usd=20_000.0, buys_1h=300, sells_1h=200,
        price_change_1h_pct=5.0, age_hours=48.0,
        has_twitter=True, has_telegram=False, has_website=True,
    )
    base.update(o)
    return Candidate(**base)


class FakeClient:
    """Minimal stand-in for an LLMClient (provider/model/api_key/complete_json)."""

    def __init__(self, provider, model, text="", degradation=None, key="k"):
        self.provider = provider
        self.model = model
        self.api_key = key
        self._text = text
        self._deg = degradation
        self.closed = False

    async def complete_json(self, task, system_prompt, user_prompt,
                            budget=None, json_mode=True):
        if self._deg or not self._text:
            return LLMResult(text=self._text or "", provider=self.provider,
                             model=self.model, task=task,
                             degradation_reason=self._deg or "empty_content")
        return LLMResult(text=self._text, provider=self.provider,
                         model=self.model, task=task)

    async def aclose(self):
        self.closed = True


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(ob, "_UNAVAILABLE", set())
    monkeypatch.setattr(config, "LLM_BRAIN", True)


GOOD_TICK = (
    '{"thoughts":["buyers still in front on ALPHA"],"actions":[],'
    '"verdicts":[{"symbol":"ALPHA","call":"buying",'
    '"checks":["buyers led the hour","volume rising","counter-case: thin liq"],'
    '"entry":"on a retest","invalidation":"if the base breaks","reason":"flow"}],'
    '"theses":[],"watchlist":[],"remember":[],"fomo":62,'
    '"break":{"taking":false,"minutes":15,"reason":""}}'
)


# --- parse + validate -------------------------------------------------------

def test_parse_valid_tick_maps_buying():
    parsed = ob.parse_llm_tick(GOOD_TICK, {"ALPHA"})
    assert parsed is not None
    v = parsed["verdicts"]["ALPHA"]
    assert v.call == "buying" and v.wants_entry is True
    assert parsed["fomo"] == 62
    assert len(v.checks) == 3


def test_parse_drops_invented_symbol():
    raw = GOOD_TICK.replace("ALPHA", "NOTREAL")
    parsed = ob.parse_llm_tick(raw, {"ALPHA"})
    assert parsed is not None and parsed["verdicts"] == {}


@pytest.mark.parametrize("call,wants", [
    ("buying", True), ("stalking", False), ("pass", False), ("holding", False)])
def test_parse_call_mapping(call, wants):
    raw = GOOD_TICK.replace('"call":"buying"', f'"call":"{call}"')
    parsed = ob.parse_llm_tick(raw, {"ALPHA"})
    v = parsed["verdicts"]["ALPHA"]
    assert v.call == call and v.wants_entry is wants


def test_parse_drops_invalid_call_fail_closed():
    raw = GOOD_TICK.replace('"call":"buying"', '"call":"yolo"')
    parsed = ob.parse_llm_tick(raw, {"ALPHA"})
    assert parsed["verdicts"] == {}          # invalid call -> dropped, not guessed


@pytest.mark.parametrize("bad", ["", "no json here", "{not valid json}", "[1,2]"])
def test_parse_malformed_returns_none(bad):
    assert ob.parse_llm_tick(bad, {"ALPHA"}) is None


def test_parse_clamps_fomo_and_strips_dollar():
    raw = GOOD_TICK.replace('"fomo":62', '"fomo":420').replace(
        '"symbol":"ALPHA"', '"symbol":"$alpha"')
    parsed = ob.parse_llm_tick(raw, {"ALPHA"})
    assert parsed["fomo"] == 100
    assert "ALPHA" in parsed["verdicts"]     # $-prefix + case normalized


# --- prompt builders --------------------------------------------------------

def test_wallet_block_flat_and_none():
    assert "flat" in ob.build_wallet_block(None)
    flat = ob.build_wallet_block(PortfolioState(cash_usd=1000.0))
    assert "cash $1,000" in flat and "flat" in flat


def test_wallet_block_with_position():
    t = Trade(symbol="ALPHA", position_size_usd=100.0, entry_price_usd=0.001,
              thesis="buyers lead")
    block = ob.build_wallet_block(PortfolioState(cash_usd=900.0,
                                                 open_positions=[t]))
    assert "ALPHA" in block and "deployed $100" in block and "entry" in block


def test_snapshot_block_none_safe():
    c = make_candidate("BETA", buys_1h=None, sells_1h=None,
                       price_change_1h_pct=None, age_hours=None,
                       volume_1h_usd=None)
    block = ob.build_snapshot_block([c])
    assert "BETA" in block and "?" in block   # Optional fields render as '?'


# --- role routing -----------------------------------------------------------

async def test_run_role_primary_then_honest_label(monkeypatch):
    def fake(pid, timeout_override=None):
        return FakeClient("deepseek", "deepseek-v4-flash", text=GOOD_TICK)
    monkeypatch.setattr(ob, "_build_provider", fake)
    monkeypatch.setattr(config, "MAIN_LLM_PROVIDER", "deepseek")
    result, resolved = await ob.run_role("reasoning", "t", "s", "u")
    assert result is not None and resolved.degraded is False
    assert resolved.provider == "deepseek"


async def test_run_role_falls_back_and_labels_degraded(monkeypatch):
    calls = []

    def fake(pid, timeout_override=None):
        calls.append(pid)
        if pid == "main":
            return FakeClient("deepseek", "m", degradation="empty_content")
        return FakeClient("groq", "g", text=GOOD_TICK)
    monkeypatch.setattr(ob, "_build_provider", fake)
    result, resolved = await ob.run_role("reasoning", "t", "s", "u")
    assert calls == ["main", "groq"]
    assert result is not None and resolved.degraded is True
    assert resolved.provider == "groq" and "routed from main" in resolved.label


async def test_run_role_unsupported_model_benches_provider(monkeypatch):
    def fake(pid, timeout_override=None):
        if pid == "main":
            return FakeClient("deepseek", "m", degradation="model_not_found")
        return FakeClient("groq", "g", text=GOOD_TICK)
    monkeypatch.setattr(ob, "_build_provider", fake)
    await ob.run_role("reasoning", "t", "s", "u")
    assert "main" in ob._UNAVAILABLE          # not retried this process


async def test_run_role_all_fail_returns_none(monkeypatch):
    def fake(pid, timeout_override=None):
        return FakeClient("deepseek", "m", degradation="timeout")
    monkeypatch.setattr(ob, "_build_provider", fake)
    result, resolved = await ob.run_role("reasoning", "t", "s", "u")
    assert result is None and resolved.label == "template"
    assert "main" not in ob._UNAVAILABLE      # timeout is NOT unsupported-model


# --- LLMBrain.tick fail-closed behaviour ------------------------------------

async def test_tick_mock_mode_is_hermetic_template(monkeypatch):
    monkeypatch.setattr(config, "DATA_BACKEND", "mock")
    brain = ob.LLMBrain()
    res = await brain.tick([make_candidate()])
    assert res.verdicts == {} and res.source == "template" and res.degraded


async def test_tick_live_unparsable_fails_closed(monkeypatch):
    monkeypatch.setattr(config, "DATA_BACKEND", "live")

    def fake(pid, timeout_override=None):
        return FakeClient("deepseek", "m", text="garbage not json")
    monkeypatch.setattr(ob, "_build_provider", fake)
    res = await ob.LLMBrain().tick([make_candidate()])
    assert res.verdicts == {} and res.degraded is True   # never a buy


async def test_tick_live_valid_maps_verdicts(monkeypatch):
    monkeypatch.setattr(config, "DATA_BACKEND", "live")

    def fake(pid, timeout_override=None):
        return FakeClient("deepseek", "deepseek-v4-flash", text=GOOD_TICK)
    monkeypatch.setattr(ob, "_build_provider", fake)
    cands = [make_candidate("ALPHA")]
    res = await ob.LLMBrain().tick(cands, PortfolioState(cash_usd=1000.0))
    v = res.verdict_for("ALPHA")
    assert v is not None and v.wants_entry is True
    assert res.fomo == 62 and res.degraded is False
    assert res.verdict_for("alpha") is v       # case-insensitive lookup


async def test_tick_empty_candidates_fails_closed(monkeypatch):
    monkeypatch.setattr(config, "DATA_BACKEND", "live")
    res = await ob.LLMBrain().tick([])
    assert res.verdicts == {} and res.source == "template"

