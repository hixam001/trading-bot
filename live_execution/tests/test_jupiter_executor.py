"""
tests for live_execution.jupiter_executor — the OFFLINE-testable subset.

Covers: fail-closed refusals (flag/size/keypair/decimals) happening BEFORE
any network call, quote math + request shape, swap-build payload shape.
NOT covered (needs funded wallet + RPC): signing, sendTransaction,
on-chain confirmation — stated in the module docstring too.
"""
from __future__ import annotations

import asyncio
import base64

import pytest

import live_execution.config as le_config
from live_execution import jupiter_executor as je


@pytest.fixture
def enabled(monkeypatch, tmp_path):
    monkeypatch.setattr(le_config, "EXECUTION_ENABLED", True)
    kp = tmp_path / "kp.json"
    kp.write_text("[1,2,3]")           # content irrelevant; existence checked
    monkeypatch.setattr(le_config, "WALLET_KEYPAIR_PATH", str(kp))
    return tmp_path


def no_network(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("network call attempted after refusal!")
    monkeypatch.setattr(je, "_post_json", _boom)


# --- refusals BEFORE any network call ---------------------------------------

def test_disabled_flag_refuses_before_network(enabled, monkeypatch):
    monkeypatch.setattr(le_config, "EXECUTION_ENABLED", False)
    no_network(monkeypatch)
    with pytest.raises(je.Refusal, match="disabled"):
        asyncio.run(je.execute_confirmed_trade("MINT", 6, 10.0))


def test_oversize_refuses_before_network(enabled, monkeypatch):
    no_network(monkeypatch)
    with pytest.raises(je.Refusal, match="MAX_TRADE_USD"):
        asyncio.run(je.execute_confirmed_trade("MINT", 6, 999.0))


def test_missing_keypair_refuses_before_network(enabled, monkeypatch):
    monkeypatch.setattr(le_config, "WALLET_KEYPAIR_PATH", "/nonexistent/kp.json")
    no_network(monkeypatch)
    with pytest.raises(je.Refusal, match="WALLET_KEYPAIR_PATH"):
        asyncio.run(je.execute_confirmed_trade("MINT", 6, 10.0))


def test_unknown_decimals_refuses_before_network(enabled, monkeypatch):
    """THE carry-over from the BARRON 96k% bug: None decimals must refuse."""
    no_network(monkeypatch)
    with pytest.raises(je.Refusal, match="UNKNOWN decimals"):
        asyncio.run(je.execute_confirmed_trade("MINT", None, 10.0))


def test_zero_usd_refuses_before_network(enabled, monkeypatch):
    no_network(monkeypatch)
    with pytest.raises(je.Refusal, match="usd_size"):
        asyncio.run(je.execute_confirmed_trade("MINT", 6, 0.0))


# --- quote math + request shape (mocked _post_json; no real network) --------

def capture_post(monkeypatch, response: dict) -> list[dict]:
    calls: list[dict] = []

    async def fake(url, payload):
        calls.append({"url": url, "payload": payload})
        return response

    monkeypatch.setattr(je, "_post_json", fake)
    return calls


def test_quote_happy_path_six_decimals(enabled, monkeypatch):
    # $10 of a 6-decimal token at ~$14.47/token -> 0.691 tokens
    # -> outAmount raw = 0.691 * 1e6 = 691000
    calls = capture_post(monkeypatch, {"outAmount": "691000"})
    q = asyncio.run(je.get_jupiter_quote("MINT", 6, 10.0))

    assert q["tokens_out"] == pytest.approx(0.691)
    assert q["price_usd"] == pytest.approx(10.0 / 0.691)
    assert calls[0]["payload"]["amount"] == str(10_000_000)      # USDC 6-dec
    assert calls[0]["payload"]["outputMint"] == "MINT"
    assert "quote-api.jup.ag" not in calls[0]["url"]             # dead endpoint gone
    assert "lite-api.jup.ag" in calls[0]["url"]


def test_quote_happy_path_nine_decimals(enabled, monkeypatch):
    # SOL-like: 9 decimals, $94.44 buys exactly 1.0 token
    capture_post(monkeypatch, {"outAmount": "1000000000"})
    q = asyncio.run(je.get_jupiter_quote("MINT", 9, 94.44))
    assert q["tokens_out"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_swap_build_payload_shape(enabled, monkeypatch):
    captured = capture_post(
        monkeypatch,
        {"swapTransaction": base64.b64encode(b"x").decode()},
    )
    quote = {"a": 1}
    b64 = await je._build_swap_transaction(quote, "PubKey123")
    assert base64.b64decode(b64) == b"x"
    assert captured[0]["payload"]["quoteResponse"] == quote
    assert captured[0]["payload"]["userPublicKey"] == "PubKey123"
    assert captured[0]["payload"]["wrapAndUnwrapSol"] is True

