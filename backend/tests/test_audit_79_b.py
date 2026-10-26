"""
backend/tests/test_audit_79_b.py — Tests for Phase B audit findings (§79 Phase B).

Covers:
  - B-1: Quote parameter parity across buy path, sell path, and inspection script (build_quote_params).
  - B-2: DEX labels filter inclusion when configured, omission when empty; Lifinity v1 and v2 in approved programs.
  - B-3: Compute budget program guard decoding, priority-fee hard cap, duplicate instruction refusal,
         and default 200k CU fallback.
  - B-4: Exit slippage ladder for sells only, non-retryable refusal aborts, double-fill safety on pending
         valid blockhash, and recovery on late landed fills.
"""
from __future__ import annotations

import asyncio
import base64
import json
import struct
from pathlib import Path
from typing import Optional

import pytest
from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from live_execution import config, solana, wallet
from live_execution import executor
from live_execution.commit_log import CommitLog
from live_execution.jupiter_executor import (
    _COMPUTE_BUDGET_PROGRAM_ID,
    ExecutionError,
    inspect_swap_transaction,
)
from live_execution.executor import OrderResult
from live_execution.models import ExecutionLedger
from live_execution.quote_params import build_quote_params
from live_execution.trade_intent import TradeIntent

CB_PROGRAM_ID = Pubkey.from_string(_COMPUTE_BUDGET_PROGRAM_ID)
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN_MINT = "So11111111111111111111111111111111111111112"


# ---------------------------------------------------------------------------
# B-1: Unified quote parameters parity
# ---------------------------------------------------------------------------

def test_quote_params_parity_across_buy_sell_script():
    """
    Mutation discipline: Removing `"asLegacyTransaction": "true"` from build_quote_params
    fails this test.
    Asserts dict equality modulo amount and mints across buy, sell, and inspection calls.
    """
    buy_params = build_quote_params(
        input_mint=USDC_MINT,
        output_mint=TOKEN_MINT,
        amount_raw=10_000_000,
        slippage_bps=50,
    )
    sell_params = build_quote_params(
        input_mint=TOKEN_MINT,
        output_mint=USDC_MINT,
        amount_raw=50_000_000,
        slippage_bps=50,
    )
    script_params = build_quote_params(
        input_mint=USDC_MINT,
        output_mint=TOKEN_MINT,
        amount_raw=10_000_000,
        slippage_bps=50,
    )

    def _strip(p: dict) -> dict:
        return {k: v for k, v in p.items() if k not in ("inputMint", "outputMint", "amount")}

    assert _strip(buy_params) == _strip(sell_params)
    assert _strip(buy_params) == _strip(script_params)
    assert buy_params["asLegacyTransaction"] == "true"
    assert buy_params["swapMode"] == "ExactIn"
    assert sell_params["asLegacyTransaction"] == "true"
    assert sell_params["swapMode"] == "ExactIn"


# ---------------------------------------------------------------------------
# B-2: DEX labels filter and Lifinity program IDs
# ---------------------------------------------------------------------------

def test_dex_labels_inclusion_and_empty_omission():
    """When labels are empty, 'dexes' is omitted; when present, it is comma-joined."""
    p_empty = build_quote_params(USDC_MINT, TOKEN_MINT, 1000, 50, dex_labels=[])
    assert "dexes" not in p_empty

    p_filtered = build_quote_params(USDC_MINT, TOKEN_MINT, 1000, 50, dex_labels=["Raydium", "Orca V2", "Meteora"])
    assert p_filtered["dexes"] == "Raydium,Orca V2,Meteora"


def test_lifinity_program_ids_in_approved_swap_program_ids():
    """§81 Phase D-1: Both Lifinity v1 and v2 program IDs must be ABSENT from APPROVED_SWAP_PROGRAM_IDS."""
    lifinity_v1 = "EewxydAPCCVuNEyrVN68PuSYdQ7wKn27V9Gjeoi8dy3S"
    lifinity_v2 = "2wT8Yq49kHgDzXuPxZSaeLaH1qbmGXtEyPy64bL7aD3c"

    assert lifinity_v1 not in config.APPROVED_SWAP_PROGRAM_IDS
    assert lifinity_v2 not in config.APPROVED_SWAP_PROGRAM_IDS


# ---------------------------------------------------------------------------
# B-3: Compute budget program guard decoding and priority-fee cap
# ---------------------------------------------------------------------------

def _make_compute_budget_tx(payer: Keypair, instructions: list[Instruction]) -> VersionedTransaction:
    msg = Message.new_with_blockhash(instructions, payer.pubkey(), Hash.default())
    return VersionedTransaction(msg, [payer])


def test_compute_budget_guard_priority_fee_under_cap_passes():
    """
    Legit limit/price under cap passes inspection.
    Limit = 200,000 CU, Price = 1,000,000 micro-lamports/CU => fee = 200,000 lamports <= 1,000,000 cap.
    """
    payer = Keypair()
    ix_limit = Instruction(CB_PROGRAM_ID, bytes([2]) + struct.pack("<I", 200_000), [])
    ix_price = Instruction(CB_PROGRAM_ID, bytes([3]) + struct.pack("<Q", 1_000_000), [])
    tx = _make_compute_budget_tx(payer, [ix_limit, ix_price])

    # Must pass without raising
    inspect_swap_transaction(tx, str(payer.pubkey()))


def test_compute_budget_guard_priority_fee_over_cap_refused():
    """
    Mutation discipline: Removing the check
    `if priority_fee_lamports > config.PRIORITY_FEE_HARD_CAP_LAMPORTS` in inspect_swap_transaction
    fails this test.
    Limit = 300,000 CU, Price = 5,000,000 micro-lamports/CU => fee = 1,500,000 lamports > 1,000,000 cap.
    """
    payer = Keypair()
    ix_limit = Instruction(CB_PROGRAM_ID, bytes([2]) + struct.pack("<I", 300_000), [])
    ix_price = Instruction(CB_PROGRAM_ID, bytes([3]) + struct.pack("<Q", 5_000_000), [])
    tx = _make_compute_budget_tx(payer, [ix_limit, ix_price])

    with pytest.raises(ExecutionError, match="priority fee .* exceeds PRIORITY_FEE_HARD_CAP_LAMPORTS"):
        inspect_swap_transaction(tx, str(payer.pubkey()))


def test_compute_budget_guard_duplicate_instructions_refused():
    """
    Mutation discipline: Removing `compute_unit_price_count > 1` in inspect_swap_transaction
    fails this test.
    Duplicate SetComputeUnitPrice or SetComputeUnitLimit must be refused.
    """
    payer = Keypair()
    ix_price1 = Instruction(CB_PROGRAM_ID, bytes([3]) + struct.pack("<Q", 100_000), [])
    ix_price2 = Instruction(CB_PROGRAM_ID, bytes([3]) + struct.pack("<Q", 200_000), [])
    tx = _make_compute_budget_tx(payer, [ix_price1, ix_price2])

    with pytest.raises(ExecutionError, match="SetComputeUnitPrice .* appears more than once"):
        inspect_swap_transaction(tx, str(payer.pubkey()))

    ix_limit1 = Instruction(CB_PROGRAM_ID, bytes([2]) + struct.pack("<I", 200_000), [])
    ix_limit2 = Instruction(CB_PROGRAM_ID, bytes([2]) + struct.pack("<I", 300_000), [])
    tx2 = _make_compute_budget_tx(payer, [ix_limit1, ix_limit2])

    with pytest.raises(ExecutionError, match="SetComputeUnitLimit .* appears more than once"):
        inspect_swap_transaction(tx2, str(payer.pubkey()))


def test_compute_budget_guard_price_present_limit_absent_evaluates_default_200k():
    """
    Price present but limit absent evaluates against default 200,000 CU limit.
    Default 200,000 CU * 10,000,000 micro-lamports/CU => fee = 2,000,000 lamports > 1,000,000 cap -> REFUSED.
    Default 200,000 CU * 2,000,000 micro-lamports/CU => fee = 400,000 lamports <= 1,000,000 cap -> PASSES.
    """
    payer = Keypair()
    ix_high_price = Instruction(CB_PROGRAM_ID, bytes([3]) + struct.pack("<Q", 10_000_000), [])
    tx_over = _make_compute_budget_tx(payer, [ix_high_price])

    with pytest.raises(ExecutionError, match="exceeds PRIORITY_FEE_HARD_CAP_LAMPORTS"):
        inspect_swap_transaction(tx_over, str(payer.pubkey()))

    ix_ok_price = Instruction(CB_PROGRAM_ID, bytes([3]) + struct.pack("<Q", 2_000_000), [])
    tx_ok = _make_compute_budget_tx(payer, [ix_ok_price])
    inspect_swap_transaction(tx_ok, str(payer.pubkey()))


def test_compute_budget_guard_unapproved_tag_refused():
    """Unknown or unapproved ComputeBudget tag (e.g. 0 or 5) must be refused."""
    payer = Keypair()
    ix_invalid = Instruction(CB_PROGRAM_ID, bytes([0]) + struct.pack("<I", 123), [])
    tx = _make_compute_budget_tx(payer, [ix_invalid])

    with pytest.raises(ExecutionError, match="unapproved tag 0"):
        inspect_swap_transaction(tx, str(payer.pubkey()))


# ---------------------------------------------------------------------------
# B-4: Exit slippage ladder and double-fill safety
# ---------------------------------------------------------------------------

@pytest.fixture
def arm_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LIVE_TRADING_ENABLED", True)
    monkeypatch.setattr(config, "REQUIRE_MANUAL_CONFIRMATION", False)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "MIN_SOL_RESERVE", 0.01)

    payer = Keypair()
    monkeypatch.setattr(wallet, "load_keypair", lambda: payer)
    monkeypatch.setattr(wallet, "verify_expected_address", lambda kp: None)
    monkeypatch.setattr(wallet, "pubkey_string", lambda kp: str(payer.pubkey()))

    async def fake_decimals(mint):
        return 6

    async def fake_sol(addr):
        return 1.0

    monkeypatch.setattr(solana, "get_mint_decimals", fake_decimals)
    monkeypatch.setattr(solana, "get_sol_balance", fake_sol)

    async def fake_memo(p, digest):
        return {"signature": "MEMO_SIG_123", "slot": 100}

    from live_execution import memo
    monkeypatch.setattr(memo, "publish_commit_memo", fake_memo)
    return tmp_path, payer


@pytest.mark.asyncio
async def test_exit_slippage_ladder_escalates_on_slippage_error(arm_environment, monkeypatch):
    """
    Mutation discipline: Removing retry logic in place_sell fails this test.
    Attempt 1 fails with custom program error 0x1771 (slippage).
    Attempt 2 succeeds and fills.
    """
    tmp_path, payer = arm_environment
    ledger = ExecutionLedger(tmp_path / "executions.json")
    ledger.record_buy("buy-1", TOKEN_MINT, 100.0, 10.0, 10.0, "BUY_SIG")

    attempt_count = 0
    requested_slippages = []

    async def fake_get_json(url, params):
        nonlocal attempt_count
        attempt_count += 1
        requested_slippages.append(int(params["slippageBps"]))
        return {
            "outAmount": "95000000",
            "otherAmountThreshold": "94000000",
            "priceImpactPct": "0.005",
            "inputMint": TOKEN_MINT,
            "outputMint": USDC_MINT,
            "inAmount": params["amount"],
            "slippageBps": params["slippageBps"],
            "swapMode": "ExactIn",
        }

    async def fake_build(quote, user_pk):
        # Create a dummy valid VersionedTransaction
        ix = Instruction(CB_PROGRAM_ID, bytes([2]) + struct.pack("<I", 200_000), [])
        msg = Message.new_with_blockhash([ix], payer.pubkey(), Hash.default())
        tx = VersionedTransaction(msg, [payer])
        return base64.b64encode(bytes(tx)).decode()

    async def fake_broadcast(swap_b64, p, intent=None, logc=None, sealed=None, quote=None):
        if attempt_count == 1:
            # Slippage error on attempt 1
            raise ExecutionError("Transaction simulation failed: Error processing Instruction 1: custom program error: 0x1771")
        # Success on attempt 2
        return OrderResult(status="filled", signature="SELL_SIG_ATTEMPT_2", slot=101)

    async def fake_actuals(side, mint, sig, owner=None):
        return {"usdc_delta": 95.0, "tokens_delta": 10.0}

    monkeypatch.setattr(executor, "_get_json", fake_get_json)
    monkeypatch.setattr(executor, "_build_swap_transaction", fake_build)
    monkeypatch.setattr(executor, "_broadcast_and_confirm", fake_broadcast)
    monkeypatch.setattr(executor, "_fill_actuals", fake_actuals)

    result = await executor.place_sell(
        mint=TOKEN_MINT,
        symbol="TOK",
        fraction=1.0,
        ledger=ledger,
        reference_price_usd=10.0,
    )

    print("RESULT STATUS:", result.status, "REASON:", result.reason)
    assert result.status == "filled"
    assert result.signature == "SELL_SIG_ATTEMPT_2"
    assert attempt_count == 2
    assert requested_slippages == [50, 150]

    # Verify notes in commit log
    cl = CommitLog(tmp_path / "commits.json")
    all_commits = cl.all()
    assert len(all_commits) == 1
    notes = [n["text"] for n in all_commits[0].get("notes", [])]
    assert any("attempt 1/3 with slippage 50 bps" in n for n in notes)
    assert any("attempt 2/3 with slippage 150 bps" in n for n in notes)


@pytest.mark.asyncio
async def test_exit_slippage_ladder_no_retry_on_guard_refusal(arm_environment, monkeypatch):
    """
    Mutation discipline: Allowing retries when is_retryable_exit_error is False fails this test.
    Guard refusal (e.g. unapproved program or SOL drain) must abort immediately after attempt 1.
    """
    tmp_path, payer = arm_environment
    ledger = ExecutionLedger(tmp_path / "executions.json")
    ledger.record_buy("buy-1", TOKEN_MINT, 100.0, 10.0, 10.0, "BUY_SIG")

    attempt_count = 0

    async def fake_get_json(url, params):
        nonlocal attempt_count
        attempt_count += 1
        return {
            "outAmount": "95000000",
            "otherAmountThreshold": "94000000",
            "priceImpactPct": "0.005",
            "inputMint": TOKEN_MINT,
            "outputMint": USDC_MINT,
            "inAmount": params["amount"],
            "slippageBps": params["slippageBps"],
            "swapMode": "ExactIn",
        }

    async def fake_build(quote, user_pk):
        ix = Instruction(CB_PROGRAM_ID, bytes([2]) + struct.pack("<I", 200_000), [])
        msg = Message.new_with_blockhash([ix], payer.pubkey(), Hash.default())
        tx = VersionedTransaction(msg, [payer])
        return base64.b64decode(bytes(tx)).decode() if False else base64.b64encode(bytes(tx)).decode()

    async def fake_broadcast(swap_b64, p, intent=None, logc=None, sealed=None, quote=None):
        raise ExecutionError("simulation assertion failure: wallet SOL decrease 50000000 exceeds 10000000")

    monkeypatch.setattr(executor, "_get_json", fake_get_json)
    monkeypatch.setattr(executor, "_build_swap_transaction", fake_build)
    monkeypatch.setattr(executor, "_broadcast_and_confirm", fake_broadcast)

    result = await executor.place_sell(
        mint=TOKEN_MINT,
        symbol="TOK",
        fraction=1.0,
        ledger=ledger,
        reference_price_usd=10.0,
    )

    assert result.status == "failed"
    assert "simulation assertion failure" in result.reason
    assert attempt_count == 1  # Strictly 1 attempt, zero retry


@pytest.mark.asyncio
async def test_double_fill_safety_refuses_when_previous_signature_pending_and_blockhash_valid(arm_environment, monkeypatch):
    """
    Double-fill safety: If attempt 1 returned unconfirmed but blockhash is still valid,
    attempt 2 MUST be refused to prevent double fills.
    """
    tmp_path, payer = arm_environment
    ledger = ExecutionLedger(tmp_path / "executions.json")
    ledger.record_buy("buy-1", TOKEN_MINT, 100.0, 10.0, 10.0, "BUY_SIG")

    attempt_count = 0

    async def fake_get_json(url, params):
        nonlocal attempt_count
        attempt_count += 1
        return {
            "outAmount": "95000000",
            "otherAmountThreshold": "94000000",
            "priceImpactPct": "0.005",
            "inputMint": TOKEN_MINT,
            "outputMint": USDC_MINT,
            "inAmount": params["amount"],
            "slippageBps": params["slippageBps"],
            "swapMode": "ExactIn",
        }

    async def fake_build(quote, user_pk):
        ix = Instruction(CB_PROGRAM_ID, bytes([2]) + struct.pack("<I", 200_000), [])
        msg = Message.new_with_blockhash([ix], payer.pubkey(), Hash.default())
        tx = VersionedTransaction(msg, [payer])
        return base64.b64encode(bytes(tx)).decode()

    async def fake_broadcast(swap_b64, p, intent=None, logc=None, sealed=None, quote=None):
        return OrderResult(status="failed", reason="not confirmed before timeout", signature="SIG_PENDING_1")

    async def fake_get_sig_status(sig):
        # Signature not yet landed on chain
        return None

    async def fake_blockhash_valid(bh):
        # Blockhash is still valid!
        return True

    monkeypatch.setattr(executor, "_get_json", fake_get_json)
    monkeypatch.setattr(executor, "_build_swap_transaction", fake_build)
    monkeypatch.setattr(executor, "_broadcast_and_confirm", fake_broadcast)
    monkeypatch.setattr(solana, "get_signature_status", fake_get_sig_status)
    monkeypatch.setattr(solana, "is_blockhash_valid", fake_blockhash_valid)

    result = await executor.place_sell(
        mint=TOKEN_MINT,
        symbol="TOK",
        fraction=1.0,
        ledger=ledger,
        reference_price_usd=10.0,
    )

    assert result.status == "failed"
    assert "exit_unresolved" in result.reason
    assert "pending and blockhash" in result.reason
    # Only 1 quote was fetched; attempt 2 was blocked at the start of loop iteration 2!
    assert attempt_count == 1


@pytest.mark.asyncio
async def test_double_fill_safety_unparseable_blockhash_is_unresolved(arm_environment, monkeypatch):
    """§84: an empty/unknown blockhash must NOT be treated as expired."""
    tmp_path, payer = arm_environment
    ledger = ExecutionLedger(tmp_path / "executions.json")
    ledger.record_buy("buy-1", TOKEN_MINT, 100.0, 10.0, 10.0, "BUY_SIG")

    attempt_count = 0

    async def fake_get_json(url, params):
        nonlocal attempt_count
        attempt_count += 1
        return {
            "outAmount": "95000000",
            "priceImpactPct": "0.005",
            "inputMint": TOKEN_MINT,
            "outputMint": USDC_MINT,
            "inAmount": params["amount"],
            "slippageBps": params["slippageBps"],
        }

    async def fake_build(quote, user_pk):
        # Decodes, but cannot be parsed as a VersionedTransaction: the
        # executor cannot learn the blockhash.
        return base64.b64encode(b"not-a-transaction").decode()

    async def fake_broadcast(swap_b64, p, intent=None, logc=None, sealed=None, quote=None):
        return OrderResult(status="failed", reason="not confirmed before timeout",
                           signature="SIG_PENDING_1")

    async def fake_get_sig_status(sig):
        return None

    async def must_not_be_called(bh):
        raise AssertionError("validity checked for an unknown blockhash")

    monkeypatch.setattr(executor, "_get_json", fake_get_json)
    monkeypatch.setattr(executor, "_build_swap_transaction", fake_build)
    monkeypatch.setattr(executor, "_broadcast_and_confirm", fake_broadcast)
    monkeypatch.setattr(solana, "get_signature_status", fake_get_sig_status)
    monkeypatch.setattr(solana, "is_blockhash_valid", must_not_be_called)

    result = await executor.place_sell(
        mint=TOKEN_MINT, symbol="TOK", fraction=1.0, ledger=ledger,
        reference_price_usd=10.0,
    )

    assert result.status == "failed"
    assert "exit_unresolved" in result.reason
    assert "blockhash is unknown" in result.reason
    assert attempt_count == 1


@pytest.mark.asyncio
async def test_double_fill_safety_recovers_when_previous_signature_landed_late(arm_environment, monkeypatch):
    """
    Double-fill safety: If attempt 1 was unconfirmed at first but landed before attempt 2,
    place_sell detects the fill, binds the signature, updates the ledger, and stops the ladder.
    """
    tmp_path, payer = arm_environment
    ledger = ExecutionLedger(tmp_path / "executions.json")
    ledger.record_buy("buy-1", TOKEN_MINT, 100.0, 10.0, 10.0, "BUY_SIG")

    attempt_count = 0

    async def fake_get_json(url, params):
        nonlocal attempt_count
        attempt_count += 1
        return {
            "outAmount": "95000000",
            "otherAmountThreshold": "94000000",
            "priceImpactPct": "0.005",
            "inputMint": TOKEN_MINT,
            "outputMint": USDC_MINT,
            "inAmount": params["amount"],
            "slippageBps": params["slippageBps"],
            "swapMode": "ExactIn",
        }

    async def fake_build(quote, user_pk):
        ix = Instruction(CB_PROGRAM_ID, bytes([2]) + struct.pack("<I", 200_000), [])
        msg = Message.new_with_blockhash([ix], payer.pubkey(), Hash.default())
        tx = VersionedTransaction(msg, [payer])
        return base64.b64encode(bytes(tx)).decode()

    async def fake_broadcast(swap_b64, p, intent=None, logc=None, sealed=None, quote=None):
        return OrderResult(status="failed", reason="not confirmed before timeout", signature="SIG_LATE_LANDED")

    async def fake_get_sig_status(sig):
        # Now landed!
        return {"confirmationStatus": "confirmed", "err": None, "slot": 200}

    async def fake_actuals(side, mint, sig, owner=None):
        return {"usdc_delta": 95.0, "tokens_delta": 10.0}

    monkeypatch.setattr(executor, "_get_json", fake_get_json)
    monkeypatch.setattr(executor, "_build_swap_transaction", fake_build)
    monkeypatch.setattr(executor, "_broadcast_and_confirm", fake_broadcast)
    monkeypatch.setattr(executor, "_fill_actuals", fake_actuals)
    monkeypatch.setattr(solana, "get_signature_status", fake_get_sig_status)

    result = await executor.place_sell(
        mint=TOKEN_MINT,
        symbol="TOK",
        fraction=1.0,
        ledger=ledger,
        reference_price_usd=10.0,
    )

    assert result.status == "filled"
    assert result.signature == "SIG_LATE_LANDED"
    # Position closed cleanly in ledger
    assert ledger.open_token_amounts().get(TOKEN_MINT, 0.0) == 0.0
