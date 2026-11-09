"""§93 R4: public signatures survive scrubbing; registered secrets always win."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from io import StringIO

import pytest

import alerts
import config
from api.error_handler import SecretRedactorFilter
from redaction import redact_text
from secret_registry import register_secret

# Deliberately synthetic base58 values, never wallet material.
SIG = "5" * 88
WALLET_SECRET = "6" * 88


@pytest.fixture(autouse=True)
def isolate_registries(monkeypatch):
    import secret_registry
    monkeypatch.setattr(secret_registry, "_RUNTIME_SECRETS", set())
    from public_registry import clear_public_values
    clear_public_values()
    yield
    clear_public_values()


def _filtered(message, *args):
    record = logging.LogRecord("r4", logging.INFO, __file__, 1, message, args, None)
    SecretRedactorFilter().filter(record)
    return record.getMessage()


def test_registered_signature_round_trips_logs_and_alerts_with_wallet_secret(monkeypatch):
    from public_registry import clear_public_values, register_public_value
    clear_public_values()
    monkeypatch.setattr(config, "SECRET_STORE_KEY", WALLET_SECRET)
    register_public_value(SIG)
    line = f"FILLED buy MINT $2.00 -> 3.000000 tokens sig {SIG} wallet {WALLET_SECRET}"
    for scrub in (_filtered, alerts.sanitize_alert_text):
        result = scrub(line)
        assert SIG in result
        assert WALLET_SECRET not in result
    clear_public_values()


@pytest.mark.parametrize("kind", ["configured", "runtime", "extra"])
def test_public_registration_cannot_shield_secrets(monkeypatch, kind):
    from public_registry import clear_public_values, register_public_value
    clear_public_values()
    register_public_value(WALLET_SECRET)
    kwargs = {}
    if kind == "configured":
        monkeypatch.setattr(config, "SECRET_STORE_KEY", WALLET_SECRET)
    elif kind == "runtime":
        register_secret(WALLET_SECRET)
    else:
        kwargs["extra_secrets"] = [WALLET_SECRET]
    assert WALLET_SECRET not in redact_text(f"sig {WALLET_SECRET}", **kwargs)
    clear_public_values()


def test_public_registry_is_bounded_lru_and_thread_safe():
    from public_registry import clear_public_values, is_public_value, register_public_value
    clear_public_values()
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    def value(i):
        return "7" * 85 + alphabet[i // (58 * 58)] + alphabet[(i // 58) % 58] + alphabet[i % 58]
    for i in range(5000):
        register_public_value(value(i))
    assert is_public_value(value(0))  # refresh the oldest entry
    register_public_value(value(5000))
    assert is_public_value(value(0))
    assert not is_public_value(value(1))
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(register_public_value, (value(i) for i in range(10000))))
    assert sum(is_public_value(value(i)) for i in range(10000)) == 5000
    clear_public_values()
    assert SIG not in redact_text(f"unregistered {SIG}")


@pytest.mark.parametrize("text", ["token=BONK rank", "key=value pair", "secret=noncredential label", "password=example label"])
def test_generic_parameter_names_do_not_mask_plain_diagnostics(text):
    assert redact_text(text) == text


@pytest.mark.parametrize("text", [
    "?api-key=abc123456789", "api_key=abc123456789", "apikey=abc123456789",
    "access_token=abc123456789", "auth_token=abc123456789", "refresh_token=abc123456789",
    "?token=abc123456789", "&key=abc123456789", "?secret=abc123456789",
    "Authorization: token=abc123456789", "Cookie: session=ok; key=abc123456789",
    "Set-Cookie: token=abc123456789; Secure", "Proxy-Authorization: password=abc123456789",
    "Authorization: Bearer token=abc123456789",
])
def test_credential_parameters_and_header_values_stay_masked(text):
    assert "abc123456789" not in redact_text(text)


async def test_signed_swap_and_confirmation_register_signatures(monkeypatch):
    from public_registry import clear_public_values, is_public_value
    from live_execution import solana
    clear_public_values()
    solana.SignedSwap(SIG, b"not-a-real-transaction", _checked=True)
    assert is_public_value(SIG)
    clear_public_values()
    async def rpc(*args, **kwargs):
        return {"value": [{"confirmationStatus": "confirmed", "err": None, "slot": 1}]}
    monkeypatch.setattr(solana, "rpc", rpc)
    assert (await solana.confirm_signature(SIG))["confirmed"]
    assert is_public_value(SIG)
    clear_public_values()


async def test_rpc_send_registers_received_signature(monkeypatch):
    import httpx
    from public_registry import clear_public_values, is_public_value
    from live_execution import solana
    clear_public_values()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"result": SIG}))
    original = httpx.AsyncClient
    monkeypatch.setattr(solana.httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs))
    assert await solana.send_raw_transaction(b"not-a-real-transaction", endpoints=["https://rpc.invalid"]) == SIG
    assert is_public_value(SIG)
    clear_public_values()


@pytest.mark.parametrize("side", ["buy", "sell"])
async def test_actual_filled_log_contains_full_signature(side, tmp_path, monkeypatch):
    from public_registry import clear_public_values
    from live_execution import executor as ex, config as le_config
    from live_execution.models import ExecutionLedger
    from live_execution.tests.test_executor_memo_flow import _arm_balances, _arm_fill, _arm_sell_network
    clear_public_values()
    monkeypatch.setattr(le_config, "LIVE_TRADING_ENABLED", True)
    monkeypatch.setattr(le_config, "REQUIRE_MANUAL_CONFIRMATION", False)
    monkeypatch.setattr(le_config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(ex.wallet, "load_keypair", lambda: object())
    monkeypatch.setattr(ex.wallet, "verify_expected_address", lambda p: None)
    monkeypatch.setattr(ex.wallet, "pubkey_string", lambda p: "WALLETADDR")
    _arm_balances(monkeypatch)
    if side == "buy":
        _arm_fill(monkeypatch, [])
    else:
        _arm_sell_network(monkeypatch, [])
    async def sign(*args, **kwargs):
        return ex.solana.SignedSwap(SIG, b"mock", _checked=True)
    async def send(*args, **kwargs):
        return SIG
    async def actuals(*args, **kwargs):
        return {"tokens_delta": 1.0, "usdc_delta": 1.2}
    monkeypatch.setattr(ex, "sign_after_checks", sign)
    monkeypatch.setattr(ex.solana, "send_checked_swap", send)
    monkeypatch.setattr(ex, "_fill_actuals", actuals)
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(SecretRedactorFilter())
    ex.log.addHandler(handler)
    old_level = ex.log.level
    ex.log.setLevel(logging.INFO)
    try:
        ledger = ExecutionLedger(tmp_path / "exec.json")
        if side == "buy":
            result = await ex.place_buy("MINT", "SYM", 1.5, output_decimals=6, idempotency_key="r4", ledger=ledger, reference_price_usd=1.0)
        else:
            ledger.record_buy(idempotency_key="open", mint="MINT", usd_size=1.0, tokens_out=1.0, price_usd=1.0, signature="OPENSIG", status="confirmed")
            result = await ex.place_sell("MINT", "SYM", 1.0, ledger=ledger, reference_price_usd=1.2)
        assert result.status == "filled", result
        filled = [line for line in stream.getvalue().splitlines() if line.startswith(f"FILLED {side}")]
        assert filled and SIG in filled[-1]
    finally:
        ex.log.removeHandler(handler)
        ex.log.setLevel(old_level)
        clear_public_values()


@pytest.mark.parametrize("confirmed", [True, False])
async def test_pending_buy_alert_has_full_registered_signature(tmp_path, monkeypatch, confirmed):
    from public_registry import clear_public_values, is_public_value
    import run_live_cycle as runner
    from live_execution.models import ExecutionLedger
    clear_public_values()
    ledger = ExecutionLedger(tmp_path / "pending.json", now_fn=lambda: 1000.0)
    ledger.record_pending_buy(idempotency_key="pending", mint="MINT", usd_size=1.0, tokens_out=2.0, price_usd=0.5, signature=SIG, blockhash="BH")
    monkeypatch.setattr(runner.wallet, "load_keypair", lambda: object())
    monkeypatch.setattr(runner.wallet, "pubkey_string", lambda _: "WALLETADDR")
    async def status(*args, **kwargs):
        return {"confirmationStatus": "confirmed", "err": None} if confirmed else None
    async def valid(*args, **kwargs):
        return True
    async def actuals(*args, **kwargs):
        return {"tokens_delta": 2.0, "usdc_delta": 1.0}
    monkeypatch.setattr(runner.solana, "get_signature_status", status)
    monkeypatch.setattr(runner.solana, "is_blockhash_valid", valid)
    monkeypatch.setattr(runner.time, "time", lambda: 2000.0)
    monkeypatch.setattr(runner, "_fill_actuals", actuals)
    sent = []
    monkeypatch.setattr(alerts, "alert_nowait", lambda event, text, **kwargs: sent.append(alerts.sanitize_alert_text(text)))
    assert (await runner.resolve_pending_buys(ledger))["adopted" if confirmed else "pending"] == 1
    assert is_public_value(SIG)
    assert len(sent) == 1 and SIG in sent[0]
    clear_public_values()


def test_produced_signature_survives_filter_before_any_manual_registration():
    from live_execution import solana
    solana.SignedSwap(SIG, b"mock", _checked=True)
    assert SIG in _filtered("FILLED buy MINT $1.00 -> 2.000000 tokens sig %s", SIG)


def test_locally_built_memo_registers_its_signature():
    from public_registry import is_public_value
    from live_execution import memo
    from solders.keypair import Keypair
    from solders.hash import Hash
    from solders.transaction import VersionedTransaction
    raw = memo.build_memo_transaction("commit:v1:test", Keypair.from_seed(bytes(32)), str(Hash.default()))
    signature = str(VersionedTransaction.from_bytes(raw).signatures[0])
    assert is_public_value(signature)
    assert signature in alerts.sanitize_alert_text(f"memo sig {signature}")


async def test_received_memo_signature_is_registered_before_confirmation(monkeypatch):
    from public_registry import is_public_value
    from live_execution import memo
    async def blockhash(*args, **kwargs):
        return "BLOCKHASH"
    async def send(*args, **kwargs):
        return SIG
    async def confirm(signature, **kwargs):
        assert is_public_value(signature)
        return {"confirmed": True, "slot": 1, "err": None}
    monkeypatch.setattr(memo.solana, "latest_blockhash", blockhash)
    monkeypatch.setattr(memo, "build_memo_transaction", lambda *args: b"mock")
    monkeypatch.setattr(memo.solana, "send_raw_transaction", send)
    monkeypatch.setattr(memo.solana, "confirm_signature", confirm)
    assert (await memo.publish_commit_memo(object(), "hash", endpoints=["https://rpc.invalid"]))["signature"] == SIG
