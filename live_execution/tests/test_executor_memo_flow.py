"""
tests for the REF-R11 armed order flow in live_execution.executor.

The single most important property under test: the commit memo is published
and CONFIRMED before the fill is ever quoted/built/broadcast, and a memo that
cannot be confirmed blocks the fill entirely (the fill send is never
attempted). Also covers the micro-bootstrap funding guards (SOL reserve floor,
real USDC balance) refusing BEFORE any on-chain commitment is made.

Everything is hermetic: armed flags via monkeypatch, tmp state dir, mocked
wallet + RPC + memo + quote. No network.
"""
from __future__ import annotations

import base64

import pytest

import live_execution.config as le_config
from live_execution import executor as ex
from live_execution import memo as memo_mod
from live_execution.models import ExecutionLedger


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Armed-but-hermetic: enabled, no manual confirm, tmp state, stub wallet."""
    monkeypatch.setattr(le_config, "LIVE_TRADING_ENABLED", True)
    monkeypatch.setattr(le_config, "REQUIRE_MANUAL_CONFIRMATION", False)
    monkeypatch.setattr(le_config, "STATE_DIR", tmp_path)

    class _Payer:
        pass

    payer = _Payer()
    monkeypatch.setattr(ex.wallet, "load_keypair", lambda: payer)
    monkeypatch.setattr(ex.wallet, "verify_expected_address", lambda p: None)
    monkeypatch.setattr(ex.wallet, "pubkey_string", lambda p: "WALLETADDR")
    return tmp_path


def _arm_balances(monkeypatch, sol=1.0, usdc=100.0):
    async def fake_sol(addr):
        return sol

    async def fake_usdc(addr):
        return usdc

    monkeypatch.setattr(ex.solana, "get_sol_balance", fake_sol)
    monkeypatch.setattr(ex.solana, "get_usdc_balance", fake_usdc)


def _arm_fill(monkeypatch, order):
    """Mock memo + quote + build + sign + send + confirm, recording order."""

    async def fake_memo(payer, seal_hash, endpoints=None):
        order.append("memo")
        return {"signature": "MEMOSIG", "slot": 5}

    async def fake_quote(mint, decimals, usd, ledger=None):
        order.append("quote")
        return {"quote": {"priceImpactPct": "0.001"},
                "tokens_out": 1.0, "price_usd": usd}

    async def fake_build(quote, addr):
        order.append("build")
        return base64.b64encode(b"swap").decode()

    def fake_sign(b64, payer):
        order.append("sign")
        return "FILLSIG", b"raw"

    async def fake_send(raw, endpoints=None):
        order.append("send")
        return "FILLSIG"

    async def fake_confirm(sig, timeout_s=None, endpoints=None):
        order.append("confirm")
        return {"confirmed": True, "slot": 10, "err": None}

    monkeypatch.setattr(ex.memo, "publish_commit_memo", fake_memo)
    monkeypatch.setattr(ex, "get_jupiter_quote", fake_quote)
    monkeypatch.setattr(ex, "_build_swap_transaction", fake_build)
    monkeypatch.setattr(ex, "_sign_transaction", fake_sign)
    monkeypatch.setattr(ex.solana, "send_raw_transaction", fake_send)
    monkeypatch.setattr(ex.solana, "confirm_signature", fake_confirm)


async def test_happy_path_memo_before_fill(env, monkeypatch):
    order = []
    _arm_balances(monkeypatch)
    _arm_fill(monkeypatch, order)
    ledger = ExecutionLedger(env / "exec.json")

    res = await ex.place_buy("MINT", "SYM", 1.5, output_decimals=6,
                             idempotency_key="k1", ledger=ledger)

    assert res.status == "filled"
    # The memo is published BEFORE the quote/build/sign/send/confirm.
    assert order == ["memo", "quote", "build", "sign", "send", "confirm"]
    # The result carries the seal + memo so the bridge can journal them.
    assert res.commit_hash != ""
    assert res.commit_nonce != ""
    assert res.commit_payload["kind"] == "buy"
    assert res.memo_signature == "MEMOSIG"
    assert res.memo_slot == 5
    assert res.signature == "FILLSIG"


async def test_memo_failure_blocks_fill_entirely(env, monkeypatch):
    order = []
    _arm_balances(monkeypatch)

    async def failing_memo(payer, seal_hash, endpoints=None):
        order.append("memo")
        raise memo_mod.MemoPublishError("rpc down")

    def boom(*a, **k):
        raise AssertionError("fill path ran despite memo failure")

    monkeypatch.setattr(ex.memo, "publish_commit_memo", failing_memo)
    monkeypatch.setattr(ex, "get_jupiter_quote", boom)
    monkeypatch.setattr(ex, "_build_swap_transaction", boom)
    monkeypatch.setattr(ex, "_sign_transaction", boom)
    monkeypatch.setattr(ex.solana, "send_raw_transaction", boom)
    ledger = ExecutionLedger(env / "exec.json")

    res = await ex.place_buy("MINT", "SYM", 1.5, output_decimals=6,
                             idempotency_key="k2", ledger=ledger)

    assert res.status == "failed"
    assert "commit memo failed" in res.reason
    assert order == ["memo"]          # nothing after the memo ran
    # The seal is still recorded (honest refusal), carrying its hash.
    assert res.commit_hash != ""


async def test_insufficient_usdc_blocks_before_memo(env, monkeypatch):
    _arm_balances(monkeypatch, sol=1.0, usdc=0.5)   # below the 1.5 ticket

    def boom(*a, **k):
        raise AssertionError("memo/fill ran despite insufficient USDC")

    monkeypatch.setattr(ex.memo, "publish_commit_memo", boom)
    monkeypatch.setattr(ex, "get_jupiter_quote", boom)
    ledger = ExecutionLedger(env / "exec.json")

    res = await ex.place_buy("MINT", "SYM", 1.5, output_decimals=6,
                             idempotency_key="k3", ledger=ledger)
    assert res.status == "blocked"
    assert "insufficient USDC" in res.reason


async def test_unreadable_usdc_blocks_before_memo(env, monkeypatch):
    _arm_balances(monkeypatch, sol=1.0, usdc=None)  # None = unreadable

    def boom(*a, **k):
        raise AssertionError("memo ran despite unreadable USDC balance")

    monkeypatch.setattr(ex.memo, "publish_commit_memo", boom)
    ledger = ExecutionLedger(env / "exec.json")

    res = await ex.place_buy("MINT", "SYM", 1.5, output_decimals=6,
                             idempotency_key="k4", ledger=ledger)
    assert res.status == "blocked"
    assert "USDC balance unreadable" in res.reason


async def test_sol_reserve_floor_blocks_before_usdc(env, monkeypatch):
    # SOL below the (0.01) floor -> refuse before even reading USDC.
    _arm_balances(monkeypatch, sol=0.005, usdc=100.0)

    async def boom_usdc(addr):
        raise AssertionError("USDC read despite SOL reserve breach")

    monkeypatch.setattr(ex.solana, "get_usdc_balance", boom_usdc)
    ledger = ExecutionLedger(env / "exec.json")

    res = await ex.place_buy("MINT", "SYM", 1.5, output_decimals=6,
                             idempotency_key="k5", ledger=ledger)
    assert res.status == "blocked"
    assert "SOL reserve" in res.reason


async def test_disarmed_refuses_before_anything(env, monkeypatch):
    monkeypatch.setattr(le_config, "LIVE_TRADING_ENABLED", False)

    def boom(*a, **k):
        raise AssertionError("network/path ran while disarmed")

    monkeypatch.setattr(ex.solana, "get_sol_balance", boom)
    ledger = ExecutionLedger(env / "exec.json")
    res = await ex.place_buy("MINT", "SYM", 1.5, output_decimals=6,
                             idempotency_key="k6", ledger=ledger)
    assert res.status == "unarmed"
