"""
tests for live_execution.jupiter_executor — the OFFLINE-testable subset.

Covers: preflight refusals (kill switch / flag / size / exposure / position
count / decimals) happening BEFORE any network call, quote math + request
shape, confirmation gating, idempotent replay, and a fully mocked
propose→approve→execute→confirm happy path with ledger recording.
NOT covered (needs funded wallet + RPC): real signing, sendTransaction,
on-chain confirmation — stated in the module docstring too.
"""
from __future__ import annotations

import asyncio
import base64
import json

import pytest

import live_execution.config as le_config
from live_execution import jupiter_executor as je
from live_execution import kill_switch, wallet
from live_execution.confirmation_queue import ConfirmationQueue
from live_execution.models import ExecutionLedger


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Armed-but-hermetic environment: enabled, no manual confirm, tmp state."""
    monkeypatch.setattr(le_config, "LIVE_TRADING_ENABLED", True)
    monkeypatch.setattr(le_config, "REQUIRE_MANUAL_CONFIRMATION", False)
    monkeypatch.setattr(le_config, "STATE_DIR", tmp_path)
    kp = tmp_path / "kp.json"
    kp.write_text("[1,2,3]")           # content irrelevant unless signed
    monkeypatch.setattr(le_config, "WALLET_KEYPAIR_PATH", str(kp))
    return tmp_path


def no_network(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("network call attempted after refusal!")
    monkeypatch.setattr(je, "_post_json", _boom)


# --- refusals BEFORE any network call ----------------------------------------

def test_disabled_flag_refuses_before_network(env, monkeypatch):
    monkeypatch.setattr(le_config, "LIVE_TRADING_ENABLED", False)
    no_network(monkeypatch)
    with pytest.raises(je.Refusal, match="LIVE_TRADING_ENABLED"):
        asyncio.run(je.execute_confirmed_trade(
            "MINT", 6, 10.0, idempotency_key="k1"))


def test_oversize_refuses_before_network(env, monkeypatch):
    no_network(monkeypatch)
    with pytest.raises(je.Refusal, match="MAX_TRADE_USD"):
        asyncio.run(je.execute_confirmed_trade(
            "MINT", 6, 999.0, idempotency_key="k2"))


def test_zero_and_negative_usd_refuse_before_network(env, monkeypatch):
    no_network(monkeypatch)
    for bad in (0.0, -5.0):
        with pytest.raises(je.Refusal, match="usd_size"):
            asyncio.run(je.execute_confirmed_trade(
                "MINT", 6, bad, idempotency_key="k3"))


def test_unknown_decimals_refuses_before_network(env, monkeypatch):
    """THE carry-over from the BARRON 96k% bug: None decimals must refuse."""
    no_network(monkeypatch)
    with pytest.raises(je.Refusal, match="UNKNOWN decimals"):
        asyncio.run(je.execute_confirmed_trade(
            "MINT", None, 10.0, idempotency_key="k4"))


def test_kill_switch_refuses_before_network(env, monkeypatch):
    kill_switch.trip("drill", state_dir=env)
    no_network(monkeypatch)
    with pytest.raises(kill_switch.KillSwitchTripped, match="drill"):
        asyncio.run(je.execute_confirmed_trade(
            "MINT", 6, 10.0, idempotency_key="k5"))


def test_total_exposure_cap_refuses(env, monkeypatch):
    # Lower the total so the branch is reachable under the per-trade cap.
    monkeypatch.setattr(le_config, "MAX_TOTAL_EXPOSURE_USD", 120.0)
    ledger = ExecutionLedger(env / "executions.json")
    ledger.record_buy("b1", "AAA", 100.0, 1.0, 100.0, "sig",
                      status="confirmed")
    no_network(monkeypatch)
    with pytest.raises(je.Refusal, match="MAX_TOTAL_EXPOSURE_USD"):
        asyncio.run(je.preflight(25.0, "BBB", 6, ledger))


def test_position_count_cap_refuses(env, monkeypatch):
    ledger = ExecutionLedger(env / "executions.json")
    for i in range(le_config.MAX_OPEN_POSITIONS):
        ledger.record_buy(f"b{i}", f"M{i}", 10.0, 1.0, 10.0, "sig")
    no_network(monkeypatch)
    with pytest.raises(je.Refusal, match="MAX_OPEN_POSITIONS"):
        asyncio.run(je.preflight(10.0, "NEW", 6, ledger))   # new mint blocked
    # Re-buying an already-held mint is allowed by the count cap.
    assert je.preflight(10.0, "M0", 6, ledger) == 6


# --- gating: idempotency key + manual confirmation ----------------------------

def test_idempotency_key_required(env, monkeypatch):
    no_network(monkeypatch)
    with pytest.raises(je.Refusal, match="idempotency_key"):
        asyncio.run(je.execute_confirmed_trade("MINT", 6, 10.0))


def test_manual_confirmation_required_by_default(env, monkeypatch):
    monkeypatch.setattr(le_config, "REQUIRE_MANUAL_CONFIRMATION", True)
    no_network(monkeypatch)
    with pytest.raises(je.Refusal, match="REQUIRE_MANUAL_CONFIRMATION"):
        asyncio.run(je.execute_confirmed_trade(
            "MINT", 6, 10.0, idempotency_key="k6"))
    # An unknown confirmation id also refuses (never network).
    with pytest.raises(je.Refusal, match="confirmation refused"):
        asyncio.run(je.execute_confirmed_trade(
            "MINT", 6, 10.0, confirmation_id="nope",
            idempotency_key="k7"))


def test_missing_wallet_refuses_before_network(env, monkeypatch):
    monkeypatch.setattr(le_config, "WALLET_KEYPAIR_PATH", "/nonexistent/kp.json")
    no_network(monkeypatch)
    with pytest.raises(wallet.WalletError, match="WALLET_KEYPAIR_PATH"):
        asyncio.run(je.execute_confirmed_trade(
            "MINT", 6, 10.0, idempotency_key="k8"))


# --- quote math + request shape (mocked _post_json; no real network) ----------

def capture_post(monkeypatch, response: dict) -> list[dict]:
    calls: list[dict] = []

    async def fake(url, payload):
        calls.append({"url": url, "payload": payload})
        return response

    monkeypatch.setattr(je, "_post_json", fake)
    return calls


def test_quote_happy_path_six_decimals(env, monkeypatch):
    # $10 of a 6-decimal token at ~$14.47/token -> 0.691 tokens
    # -> outAmount raw = 0.691 * 1e6 = 691000
    calls = capture_post(monkeypatch, {"outAmount": "691000"})
    q = asyncio.run(je.get_jupiter_quote("MINT", 6, 10.0))

    assert q["tokens_out"] == pytest.approx(0.691)
    assert q["price_usd"] == pytest.approx(10.0 / 0.691)
    assert calls[0]["payload"]["amount"] == str(10_000_000)      # USDC 6-dec,
    # computed via the IMPORTED raw_units_for_one_token(6), not a literal.
    assert calls[0]["payload"]["outputMint"] == "MINT"
    assert "quote-api.jup.ag" not in calls[0]["url"]             # dead endpoint gone
    assert calls[0]["url"] == "https://lite-api.jup.ag/swap/v1/quote"
    assert calls[0]["url"] == je._BACKEND_QUOTE_URL              # single source


def test_quote_happy_path_nine_decimals_in_cap(env, monkeypatch):
    # SOL-like: 9 decimals. $47.22 (UNDER MAX_TRADE_USD=50) at $94.44/token
    # buys exactly 0.5 tokens -> outAmount raw = 0.5 * 1e9 = 500000000.
    capture_post(monkeypatch, {"outAmount": "500000000"})
    q = asyncio.run(je.get_jupiter_quote("MINT", 9, 47.22))
    assert q["tokens_out"] == pytest.approx(0.5)
    assert q["price_usd"] == pytest.approx(94.44)


def test_quote_garbage_out_amount_fails_closed(env, monkeypatch):
    capture_post(monkeypatch, {"outAmount": "not-a-number"})
    with pytest.raises(je.ExecutionError, match="unparseable outAmount"):
        asyncio.run(je.get_jupiter_quote("MINT", 6, 10.0))


@pytest.mark.asyncio
async def test_swap_build_payload_shape(env, monkeypatch):
    captured = capture_post(
        monkeypatch,
        {"swapTransaction": base64.b64encode(b"x").decode()},
    )
    quote = {"a": 1}
    b64 = await je._build_swap_transaction(quote, "PubKey123")
    assert base64.b64decode(b64) == b"x"
    assert captured[0]["url"] == le_config.JUPITER_SWAP_URL
    assert captured[0]["payload"]["quoteResponse"] == quote
    assert captured[0]["payload"]["userPublicKey"] == "PubKey123"
    assert captured[0]["payload"]["wrapAndUnwrapSol"] is True


# --- full mocked flow: propose -> approve -> execute -> confirm ---------------

class _StubPayer:
    pass


def _wire_fake_network(monkeypatch, calls: list[dict]):
    async def fake(url, payload):
        calls.append({"url": url, "payload": payload})
        if url.endswith("/quote"):
            # $10 buys 10.0 tokens of a 6-dec mint @ $1.00/token:
            return {"outAmount": str(10 * 1_000_000)}
        if url.endswith("/swap"):
            return {"swapTransaction":
                    base64.b64encode(b"unsigned-tx").decode()}
        method = payload.get("method")
        if method == "sendTransaction":
            return {"result": "sent"}
        if method == "getSignatureStatuses":
            return {"result": {"value": [
                {"confirmationStatus": "finalized", "err": None}]}}
        raise AssertionError(f"unexpected rpc call {method}")

    monkeypatch.setattr(je, "_post_json", fake)


def test_full_mocked_flow_records_ledger_and_dedupes(env, monkeypatch):
    monkeypatch.setattr(je.wallet, "load_keypair", lambda: _StubPayer())
    monkeypatch.setattr(je.wallet, "pubkey_string", lambda k: "PubKey123")
    monkeypatch.setattr(
        je, "_sign_transaction", lambda b64, payer: ("SIG123", b"signed-tx"))
    calls: list[dict] = []
    _wire_fake_network(monkeypatch, calls)

    queue = ConfirmationQueue(env / "confirmations.json")
    ledger = ExecutionLedger(env / "executions.json")

    result = asyncio.run(je.execute_confirmed_trade(
        "MINT", 6, 10.0,
        idempotency_key="trade-1", queue=queue, ledger=ledger,
    ))
    assert result["deduplicated"] is False
    assert result["signature"] == "SIG123"
    assert result["status"] == "confirmed"      # mocked finalized → recorded
    assert result["tokens_out"] == pytest.approx(10.0)
    assert result["price_usd"] == pytest.approx(1.0)

    rec = ledger.get_by_idempotency_key("trade-1")
    assert rec is not None and rec.signature == "SIG123"
    assert ledger.total_open_exposure() == pytest.approx(10.0)

    swap_calls = [c for c in calls if c["url"].endswith("/swap")]
    assert len(swap_calls) == 1

    # Retry with the SAME key returns the prior outcome — never re-sends.
    calls.clear()
    again = asyncio.run(je.execute_confirmed_trade(
        "MINT", 6, 10.0,
        idempotency_key="trade-1", queue=queue, ledger=ledger,
    ))
    assert again["deduplicated"] is True
    assert again["signature"] == "SIG123"
    assert all(not c["url"].endswith("/swap") for c in calls)


def test_propose_then_approve_then_execute_consumes_confirmation(
        env, monkeypatch):
    monkeypatch.setattr(je.wallet, "load_keypair", lambda: _StubPayer())
    monkeypatch.setattr(je.wallet, "pubkey_string", lambda k: "PubKey123")
    monkeypatch.setattr(le_config, "REQUIRE_MANUAL_CONFIRMATION", True)
    monkeypatch.setattr(
        je, "_sign_transaction", lambda b64, payer: ("SIG9", b"s"))
    calls: list[dict] = []
    _wire_fake_network(monkeypatch, calls)

    queue = ConfirmationQueue(env / "confirmations.json")
    ledger = ExecutionLedger(env / "executions.json")

    pc = asyncio.run(je.propose_trade("MINT", 6, 10.0,
                                      queue=queue, ledger=ledger))
    assert pc.status == "pending"
    assert pc.quote_snapshot["price_usd"] == pytest.approx(1.0)
    assert pc.quote_snapshot["tokens_out"] == pytest.approx(10.0)

    queue.approve(pc.id)
    result = asyncio.run(je.execute_confirmed_trade(
        "MINT", 6, 10.0,
        confirmation_id=pc.id, idempotency_key="trade-2",
        queue=queue, ledger=ledger,
    ))
    assert result["deduplicated"] is False
    stored = json.loads((env / "confirmations.json").read_text())
    assert stored["confirmations"][pc.id]["status"] == "consumed"

    # The consumed id cannot execute a SECOND trade.
    with pytest.raises(je.Refusal, match="confirmation refused"):
        asyncio.run(je.execute_confirmed_trade(
            "OTHER", 6, 10.0,
            confirmation_id=pc.id, idempotency_key="trade-3",
            queue=queue, ledger=ledger,
        ))


def test_daily_loss_breaker_trips_and_blocks_execution(env, monkeypatch):
    ledger = ExecutionLedger(env / "executions.json")
    ledger.record_buy("b1", "AAA", 50.0, 1.0, 50.0, "sig")
    ledger.mark_close("AAA", proceeds_usd=-30.0)     # realized -80 < -75
    assert ledger.realized_pnl_today() == pytest.approx(-80.0)

    tripped = kill_switch.check_daily_loss_breaker(ledger, state_dir=env)
    assert tripped is True
    assert kill_switch.is_tripped(env) is True

    no_network(monkeypatch)
    with pytest.raises(kill_switch.KillSwitchTripped):
        asyncio.run(je.preflight(10.0, "NEW", 6, ledger))
