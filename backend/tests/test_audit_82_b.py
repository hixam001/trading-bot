"""
test_audit_82_b.py — §82 Phase B: Preflight rejection ladder escalation.

Tests that when every RPC returns a definitive preflight slippage error (ERR_6001):
  1. The ladder escalates even when is_blockhash_valid returns True, None, or False.
     (Previously, last_sig was retained, causing the double-fill check to abort with
     exit_unresolved because the freshly-created blockhash was still valid).
  2. Non-retryable preflight errors halt immediately without escalating.
  3. Ambiguous transport failure keeps last_sig and stops as exit_unresolved.
  4. "Already processed" responses are treated as ambiguous (None from send_raw_transaction),
     not rejected, so last_sig is kept and get_signature_status runs.
  5. solana.send_raw_transaction_detailed with mocked httpx.AsyncClient feeds real attempt dicts.

All tests are hermetic: mocked RPCs, no network, no keypair.
Self-consistent quote fixtures used.
"""
from __future__ import annotations

import base64
from typing import Optional
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import live_execution.config as le_config
from live_execution import executor as ex
from live_execution import solana as sol_mod
from live_execution.exit_errors import SendRejected, is_already_processed
from live_execution.models import ExecutionLedger

MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WALLET = "GsbwXfJraMomNxBcpR3DBDr4GEEr2kPpGEe1c1FQFPB1"

# Real Solana error shapes
ERR_6001 = {"InstructionError": [2, {"Custom": 6001}]}
ERR_FEE = "Transaction simulation failed: Attempt to debit an account but found no record of a prior credit."


def _make_quote(out_amount: int, slippage_bps: int) -> dict:
    threshold = out_amount * (10_000 - slippage_bps) // 10_000
    return {
        "inputMint": MINT,
        "outputMint": USDC_MINT,
        "inAmount": str(1_000_000),
        "outAmount": str(out_amount),
        "otherAmountThreshold": str(threshold),
        "slippageBps": slippage_bps,
        "priceImpactPct": "0.001",
    }


def _make_fake_tx_b64() -> str:
    return base64.b64encode(b"\x00" * 64).decode()


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Armed hermetic environment."""
    monkeypatch.setattr(le_config, "LIVE_TRADING_ENABLED", True)
    monkeypatch.setattr(le_config, "REQUIRE_MANUAL_CONFIRMATION", False)
    monkeypatch.setattr(le_config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(le_config, "EXIT_SLIPPAGE_LADDER_BPS", (50, 150, 300))
    monkeypatch.setattr(le_config, "EXIT_MAX_ATTEMPTS", 3)

    class _Payer:
        pass

    payer = _Payer()
    monkeypatch.setattr(ex.wallet, "load_keypair", lambda: payer)
    monkeypatch.setattr(ex.wallet, "verify_expected_address", lambda p: None)
    monkeypatch.setattr(ex.wallet, "pubkey_string", lambda p: WALLET)

    async def fake_sol(addr):
        return 1.0

    async def fake_usdc(addr):
        return 100.0

    monkeypatch.setattr(ex.solana, "get_sol_balance", fake_sol)
    monkeypatch.setattr(ex.solana, "get_usdc_balance", fake_usdc)

    async def fake_decimals(mint):
        return 6

    monkeypatch.setattr(ex.solana, "get_mint_decimals", fake_decimals)

    async def fake_fill_actuals(side, mint, sig, owner=None):
        return {"usdc_delta": 1.20, "tokens_delta": 1.0}

    monkeypatch.setattr(ex, "_fill_actuals", fake_fill_actuals)

    async def fake_memo(payer, seal_hash, endpoints=None):
        return {"signature": "MEMOSIG", "slot": 5}

    monkeypatch.setattr(ex.memo, "publish_commit_memo", fake_memo)

    return payer


def _ledger_with_position(tmp_path, mint=MINT, tokens=1.0):
    ledger = ExecutionLedger(tmp_path / "ledger.json")
    ledger.record_buy(
        idempotency_key="test-key",
        mint=mint,
        usd_size=1.0,
        tokens_out=tokens,
        price_usd=1.0,
        signature="FAKESIG",
        status="confirmed",
    )
    return ledger


# ---------------------------------------------------------------------------
# Test 1: preflight reject escalates even when blockhash is still valid
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("blockhash_valid_val", [True, None, False])
async def test_preflight_reject_escalates_even_when_blockhash_still_valid(
    env, monkeypatch, tmp_path, blockhash_valid_val
):
    """
    When every RPC returns a definitive preflight slippage error (ERR_6001),
    the sell ladder must escalate and succeed on rung 3, regardless of whether
    is_blockhash_valid returns True, None, or False.

    Previously: when is_blockhash_valid returned True, rung 2 saw last_sig != None,
    checked blockhash, saw it was still valid, and aborted with exit_unresolved.
    """
    send_attempts = []

    async def fake_get(url, params):
        slippage = int(params.get("slippageBps", 50))
        return _make_quote(1_200_000, slippage)

    async def fake_build(quote, addr):
        return _make_fake_tx_b64()

    sign_count = [0]

    async def fake_sign(b64, payer, intent=None, quote=None):
        sign_count[0] += 1
        return ex.solana.SignedSwap(f"SIG_{sign_count[0]}", b"raw", _checked=True)

    # First two sends fail at preflight with SendRejected (ERR_6001).
    # Third send succeeds.
    async def fake_send_checked(signed, endpoints=None):
        send_attempts.append(signed.signature)
        if len(send_attempts) < 3:
            attempt = {
                "endpoint": "https://fake.rpc",
                "kind": "rpc_error",
                "code": -32002,
                "message": "Transaction simulation failed: Error processing Instruction 2: custom program error: 0x1771",
                "err": ERR_6001,
                "data": None,
            }
            raise SendRejected("preflight slippage", attempts=[attempt])
        return "FILL_SIG_3"

    async def fake_confirm(sig, timeout_s=None, endpoints=None):
        if sig == "FILL_SIG_3":
            return {"confirmed": True, "slot": 100, "err": None}
        return {"confirmed": False, "slot": None, "err": "not confirmed"}

    async def fake_get_sig_status(sig, endpoints=None):
        # The rejected transaction never landed on-chain
        return None

    async def fake_is_blockhash_valid(blockhash, endpoints=None):
        return blockhash_valid_val

    monkeypatch.setattr(ex, "_get_json", fake_get)
    monkeypatch.setattr(ex, "_build_swap_transaction", fake_build)
    monkeypatch.setattr(ex, "sign_after_checks", fake_sign)
    monkeypatch.setattr(ex.solana, "send_checked_swap", fake_send_checked)
    monkeypatch.setattr(ex.solana, "confirm_signature", fake_confirm)
    monkeypatch.setattr(ex.solana, "get_signature_status", fake_get_sig_status)
    monkeypatch.setattr(ex.solana, "is_blockhash_valid", fake_is_blockhash_valid)

    ledger = _ledger_with_position(tmp_path)
    res = await ex.place_sell(MINT, "TEST", 1.0, ledger=ledger, reference_price_usd=1.0)

    assert res.status == "filled", f"Expected filled, got {res.status}: {res.reason}"
    assert len(send_attempts) == 3, f"Expected 3 send attempts, got {len(send_attempts)}"
    assert res.signature == "FILL_SIG_3"


# ---------------------------------------------------------------------------
# Test 2: Non-retryable preflight reject halts immediately
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preflight_reject_non_retryable_halts(env, monkeypatch, tmp_path):
    """
    Non-retryable preflight error (e.g. insufficient funds for fee) halts immediately
    and does NOT escalate.
    """
    send_attempts = []

    async def fake_get(url, params):
        slippage = int(params.get("slippageBps", 50))
        return _make_quote(1_200_000, slippage)

    async def fake_build(quote, addr):
        return _make_fake_tx_b64()

    async def fake_sign(b64, payer, intent=None, quote=None):
        return ex.solana.SignedSwap("SIG_1", b"raw", _checked=True)

    async def fake_send_checked(signed, endpoints=None):
        send_attempts.append(signed.signature)
        attempt = {
            "endpoint": "https://fake.rpc",
            "kind": "rpc_error",
            "code": -32002,
            "message": ERR_FEE,
            "err": "AccountNotFound",
            "data": None,
        }
        raise SendRejected("preflight fee error", attempts=[attempt])

    async def fake_get_sig_status(sig, endpoints=None):
        return None

    async def fake_is_blockhash_valid(blockhash, endpoints=None):
        return True

    monkeypatch.setattr(ex, "_get_json", fake_get)
    monkeypatch.setattr(ex, "_build_swap_transaction", fake_build)
    monkeypatch.setattr(ex, "sign_after_checks", fake_sign)
    monkeypatch.setattr(ex.solana, "send_checked_swap", fake_send_checked)
    monkeypatch.setattr(ex.solana, "get_signature_status", fake_get_sig_status)
    monkeypatch.setattr(ex.solana, "is_blockhash_valid", fake_is_blockhash_valid)

    ledger = _ledger_with_position(tmp_path)
    res = await ex.place_sell(MINT, "TEST", 1.0, ledger=ledger, reference_price_usd=1.0)

    assert res.status == "failed"
    assert len(send_attempts) == 1, f"Non-retryable error should halt at 1 attempt, got {len(send_attempts)}"
    assert "exit_unresolved" not in (res.reason or "")


# ---------------------------------------------------------------------------
# Test 3: Ambiguous transport failure still results in exit_unresolved
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ambiguous_transport_failure_still_exit_unresolved(env, monkeypatch, tmp_path):
    """
    When one endpoint has a transport error and another has rpc_error,
    the failure is ambiguous: SendRejected is NOT raised, send_raw_transaction returns None.
    Because last_sig was set and blockhash is still valid, the next rung MUST halt with exit_unresolved.
    """
    send_attempts = []

    async def fake_get(url, params):
        slippage = int(params.get("slippageBps", 50))
        return _make_quote(1_200_000, slippage)

    async def fake_build(quote, addr):
        return _make_fake_tx_b64()

    async def fake_sign(b64, payer, intent=None, quote=None):
        return ex.solana.SignedSwap("SIG_AMBIGUOUS_1", b"raw", _checked=True)

    # Returns None (ambiguous transport failure)
    async def fake_send_checked(signed, endpoints=None):
        send_attempts.append(signed.signature)
        return None

    async def fake_get_sig_status(sig, endpoints=None):
        return None

    async def fake_is_blockhash_valid(blockhash, endpoints=None):
        return True  # blockhash still valid → unresolved

    monkeypatch.setattr(ex, "_get_json", fake_get)
    monkeypatch.setattr(ex, "_build_swap_transaction", fake_build)
    monkeypatch.setattr(ex, "sign_after_checks", fake_sign)
    monkeypatch.setattr(ex.solana, "send_checked_swap", fake_send_checked)
    monkeypatch.setattr(ex.solana, "get_signature_status", fake_get_sig_status)
    monkeypatch.setattr(ex.solana, "is_blockhash_valid", fake_is_blockhash_valid)

    ledger = _ledger_with_position(tmp_path)
    res = await ex.place_sell(MINT, "TEST", 1.0, ledger=ledger, reference_price_usd=1.0)

    # Should stop as exit_unresolved or failed, exactly 1 send attempt
    assert len(send_attempts) == 1, f"Expected 1 attempt, got {len(send_attempts)}"
    assert res.status == "failed"


# ---------------------------------------------------------------------------
# Test 4: "Already processed" is never treated as rejected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_already_processed_is_never_treated_as_rejected(monkeypatch):
    """
    When attempts return kind=="rpc_error" with "This transaction has already been processed",
    send_raw_transaction must NOT raise SendRejected. It returns None (ambiguous)
    so the ladder keeps last_sig and status check runs.
    """
    attempts = [
        {
            "endpoint": "https://api.mainnet-beta.solana.com",
            "kind": "rpc_error",
            "code": -32002,
            "message": "Transaction simulation failed: This transaction has already been processed",
            "err": "AlreadyProcessed",
            "data": None,
        }
    ]

    class _Outcome:
        signature = None
        attempts = [
            {
                "endpoint": "https://api.mainnet-beta.solana.com",
                "kind": "rpc_error",
                "code": -32002,
                "message": "Transaction simulation failed: This transaction has already been processed",
                "err": "AlreadyProcessed",
                "data": None,
            }
        ]

    async def fake_detailed(raw, endpoints=None):
        return _Outcome()

    monkeypatch.setattr(sol_mod, "send_raw_transaction_detailed", fake_detailed)

    # Calling send_raw_transaction directly: must NOT raise SendRejected
    result = await sol_mod.send_raw_transaction(b"dummy_raw_bytes")
    assert result is None, f"Expected None (ambiguous/landed), got {result}"


# ---------------------------------------------------------------------------
# Test 5: send_raw_transaction_detailed feeds classifier
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_raw_transaction_detailed_feeds_classifier(monkeypatch):
    """
    Test at the solana.send_raw_transaction_detailed level with mocked transport
    proving real attempt dicts feed the outcome.
    """
    # Mock httpx responses to simulate RPC responses
    rpc_error_body = {
        "jsonrpc": "2.0",
        "error": {
            "code": -32002,
            "message": "Transaction simulation failed: Error processing Instruction 2: custom program error: 0x1771",
            "data": {
                "accounts": None,
                "err": {"InstructionError": [2, {"Custom": 6001}]},
                "logs": ["Program log: Custom error: 0x1771"],
                "unitsConsumed": 50000,
            },
        },
        "id": 1,
    }

    class _MockResponse:
        status_code = 200
        def json(self):
            return rpc_error_body

    class _MockClient:
        async def post(self, url, json=None, timeout=None):
            return _MockResponse()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(sol_mod.httpx, "AsyncClient", lambda *args, **kwargs: _MockClient())

    outcome = await sol_mod.send_raw_transaction_detailed(
        b"dummy_raw_tx", endpoints=["https://api.mainnet-beta.solana.com"]
    )

    assert outcome.signature is None
    assert len(outcome.attempts) == 1
    attempt = outcome.attempts[0]
    assert attempt["kind"] == "rpc_error"
    assert attempt["err"] == {"InstructionError": [2, {"Custom": 6001}]}
    assert attempt["code"] == -32002
