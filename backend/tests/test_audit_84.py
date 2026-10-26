"""
tests/test_audit_84.py — August 2026 external repo audit (handoff §84).

Phases covered here:
  A. public proof/exits truth (gate constants + seal-time ordering)
  B. retro attribution identity (mint + real signature only)
  C. LLM failure containment (health TTL, malformed bodies, fail-closed)
  D. data honesty (unknown sells, active Token-2022 fee, decimals)
  E. execution safety (blockhash unknown, owner-scoped fill actuals, HWM)
  G. session cookie expiry is a real date
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import config
import run_live_cycle as rlc


# ---------------------------------------------------------------------------
# Phase A — exits API publishes the gate it actually uses
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_exits_reports_sell_clip_and_cooldown_units():
    from api.routes import proof

    @asynccontextmanager
    async def _fake_get_db():
        yield None

    with patch.object(proof.db, "get_db", _fake_get_db), \
         patch.object(proof.db, "get_open_position_marks",
                      AsyncMock(return_value=[])):
        out = await proof.get_exits()

    gate = out["thresholds"]["sell_risk_gate"]
    assert gate["min_clip_usd"] == config.SELL_MIN_CLIP_USD
    assert gate["cooldown_minutes_per_mint"] == config.SELL_COOLDOWN_MINUTES
    # The endpoint used to publish 1800 for a 30-minute cooldown.
    assert gate["cooldown_minutes_per_mint"] == 30.0


@pytest.mark.asyncio
async def test_journal_created_at_is_seal_time_not_insert_time(monkeypatch):
    captured: dict = {}

    async def fake_insert(conn, **kwargs):
        captured.update(kwargs)
        return 1

    fake_db = SimpleNamespace(
        insert_decision_commit=fake_insert,
        get_commit_id_by_hash=AsyncMock(return_value=1),
        bind_commit_memo=AsyncMock(),
        bind_commit_signature=AsyncMock(),
        bind_commit_venue=AsyncMock(),
    )
    monkeypatch.setattr(rlc, "db", fake_db)

    sealed_at = 1_700_000_000.0
    result = SimpleNamespace(
        commit_hash="hash84",
        commit_nonce="nonce84",
        commit_payload={"mint": "MINT456", "kind": "buy"},
        commit_sealed_at=sealed_at,
        memo_signature="",
        signature="",
    )
    await rlc._journal_live_commit(
        None, symbol="BONK", mint_address="MINT456", verdict="buy",
        entry_allowed=True, result=result,
    )

    expected = datetime.fromtimestamp(sealed_at, tz=timezone.utc).isoformat()
    assert captured["created_at"] == expected

    # And the independent verifier now matches a fill that lands after the
    # seal instead of flagging real fills as time-ordering mismatches.
    from api.routes.proof import _verify_binding_checks

    tx = {
        "blockTime": int(sealed_at) + 5,
        "meta": {
            "err": None,
            "preTokenBalances": [{"mint": "MINT456"}],
            "postTokenBalances": [{"mint": "MINT456"}],
        },
        "transaction": {"message": {"accountKeys": [{"pubkey": "WALLET"}]}},
    }
    check = _verify_binding_checks(
        tx, {"mint": "MINT456", "kind": "buy"}, captured["created_at"],
        "WALLET",
    )
    assert check["status"] == "matched"


# ---------------------------------------------------------------------------
# Phase B — retro fill query excludes rows already bound verbatim
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retro_query_excludes_already_bound_trade_ids(tmp_path, monkeypatch):
    import config as cfg
    from api import db
    from models import Trade

    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "retro84.db")
    await db.init_db()

    async with db.get_db() as conn:
        t = Trade(
            trade_id="live-MINTAAAA", symbol="AAA",
            mint_address="MINTAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            opened_at=datetime.now(timezone.utc).isoformat(),
            entry_price_usd=1.0, position_size_usd=10.0, quantity=10.0,
            candidate_snapshot={}, thesis="retro query test", is_open=True,
        )
        assert await db.try_insert_open_trade(conn, t) == 1
        await db.insert_decision_commit(
            conn, created_at=t.opened_at, tick_ts=t.opened_at, symbol="AAA",
            mint_address=t.mint_address, verdict="buy", entry_allowed=True,
            nonce="n84", payload_json='{"mint":"%s"}' % t.mint_address,
            payload_hash="h84",
        )
        commit_id = await db.get_commit_id_by_hash(conn, "h84")
        assert commit_id is not None
        await db.bind_commit_signature(
            conn, commit_id, t.trade_id, "filled", "exact")

        fills = await db.get_recent_fills_for_retro(conn)
    assert fills == []


# ---------------------------------------------------------------------------
# Phase C — LLM failure containment
# ---------------------------------------------------------------------------

async def _fake_health_client(client_mod, statuses):
    import httpx
    calls = {"n": 0}

    def handler(request):
        status = statuses[min(calls["n"], len(statuses) - 1)]
        calls["n"] += 1
        return httpx.Response(status, json={})

    c = client_mod.LLMClient(
        "groq", "https://llm.test", "key", "model",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return c, calls


@pytest.mark.asyncio
async def test_llm_health_reprobes_after_ttl(monkeypatch):
    import llm.client as client_mod

    c, calls = await _fake_health_client(client_mod, [200, 500])
    clock = iter([0.0, 1.0, 100.0])
    monkeypatch.setattr(
        client_mod, "time", SimpleNamespace(monotonic=lambda: next(clock)))

    assert await c.health() is True       # probe 1
    assert await c.health() is True       # cached (1s < 60s TTL)
    assert calls["n"] == 1
    assert await c.health() is False      # TTL expired -> probe 2
    assert calls["n"] == 2
    await c.aclose()


@pytest.mark.asyncio
async def test_complete_json_non_object_body_degrades_not_raises():
    import httpx
    from llm.client import DeepSeekClient

    c = DeepSeekClient(client=httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=["not", "an", "object"]))))
    c.api_key = "key"
    res = await c.complete_json(task="t", system_prompt="s", user_prompt="u")
    assert res is not None
    assert res.text == ""
    assert res.degradation_reason == "unparsable_response"
    await c.aclose()


@pytest.mark.asyncio
async def test_complete_json_non_dict_choice_degrades_not_raises():
    import httpx
    from llm.client import DeepSeekClient

    c = DeepSeekClient(client=httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"choices": ["junk"]}))))
    c.api_key = "key"
    res = await c.complete_json(task="t", system_prompt="s", user_prompt="u")
    assert res is not None and res.text == ""
    assert res.degradation_reason == "unparsable_response"
    await c.aclose()


@pytest.mark.asyncio
async def test_run_role_closes_client_on_success(monkeypatch):
    import llm.llm_brain as brain_mod
    from llm.client import LLMResult

    closed = {"n": 0}

    class FakeClient:
        api_key = "key"
        provider = "deepseek"
        model = "model"

        async def complete_json(self, **kwargs):
            return LLMResult(text='{"ok":true}', provider="deepseek",
                             model="model", task=kwargs.get("task", ""))

        async def aclose(self):
            closed["n"] += 1

    monkeypatch.setattr(brain_mod, "_UNAVAILABLE", set())
    monkeypatch.setattr(brain_mod, "_build_provider",
                        lambda pid, timeout_override=None: FakeClient())

    result, resolved = await brain_mod.run_role(
        "reasoning", "task", "system", "user")
    assert result is not None and result.text
    assert resolved.provider == "deepseek"
    # The success path used to return without closing the per-call client.
    assert closed["n"] == 1


def test_llm_brain_parses_string_false_and_bad_minutes():
    import llm.llm_brain as brain_mod

    raw = (
        '{"thoughts":[],"actions":[],"verdicts":[],"theses":[],'
        '"watchlist":[],"remember":[],"fomo":10,'
        '"break":{"taking":"false","minutes":"15m","reason":"x"}}'
    )
    parsed = brain_mod.parse_llm_tick(raw, set())
    assert parsed is not None
    assert parsed["break_taking"] is False
    assert parsed["break_minutes"] == 0

    raw_true = raw.replace('"false"', '"true"')
    parsed_true = brain_mod.parse_llm_tick(raw_true, set())
    assert parsed_true["break_taking"] is True


# ---------------------------------------------------------------------------
# Phase D — data honesty
# ---------------------------------------------------------------------------

def test_birdeye_missing_volume_and_mcap_stay_none():
    from data_providers.birdeye import _parse_candidate

    c = _parse_candidate({
        "address": "Mint111111111111111111111111111111111111111",
        "symbol": "X", "name": "X", "price": 1.0,
        "liquidity": 10_000, "decimals": 6,
    })
    assert c is not None
    assert c.volume_24h_usd is None
    assert c.market_cap_usd is None


def test_zero_decimals_is_valid_for_quoting():
    from data_providers.jupiter import raw_units_for_one_token
    from live_execution.jupiter_executor import assert_decimals_known

    assert raw_units_for_one_token(0) == 1
    assert assert_decimals_known(0, "Mint") == 0
