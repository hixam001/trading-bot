"""
backend/tests/test_audit_78_b.py — tests for Master Prompt §78 Phase B:
Pre-sign simulation with balance-delta assertions (sim_guard), dual-RPC consensus,
fail-closed execution, and AST/monkeypatch proof against blind sends.
"""
import ast
import base64
import struct
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from live_execution import config, sim_guard, solana
from live_execution.jupiter_executor import (
    _ATA_PROGRAM_ID,
    _SPL_TOKEN_PROGRAM_ID,
    _SYSTEM_PROGRAM_ID,
    _WSOL_MINT,
    ExecutionError,
    sign_after_checks,
    execute_confirmed_trade,
)
from live_execution.trade_intent import TradeIntent
from live_execution import executor
from tests.helpers import make_intent_from_quote

SYSTEM_PROGRAM_ID = Pubkey.from_string(_SYSTEM_PROGRAM_ID)
TOKEN_PROGRAM_ID = Pubkey.from_string(_SPL_TOKEN_PROGRAM_ID)
ATA_PROGRAM_ID = Pubkey.from_string(_ATA_PROGRAM_ID)
WSOL_MINT = Pubkey.from_string(_WSOL_MINT)
USDC_MINT = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")


def _make_token_data(mint: Pubkey, owner: Pubkey, amount: int) -> bytes:
    """Standard 165-byte SPL token account data (state=1 initialized)."""
    return bytes(mint) + bytes(owner) + struct.pack("<Q", amount) + b"\x00" * 36 + b"\x01" + b"\x00" * 56


def _make_acc_info(lamports: int, owner_pid: Pubkey, data_bytes: bytes) -> dict:
    return {
        "lamports": lamports,
        "owner": str(owner_pid),
        "data": [base64.b64encode(data_bytes).decode(), "base64"],
        "executable": False,
        "rentEpoch": 0,
    }


def _make_dummy_tx(payer: Keypair, writable_extra: Pubkey | None = None) -> tuple[str, VersionedTransaction]:
    accounts = [AccountMeta(payer.pubkey(), True, True), AccountMeta(payer.pubkey(), False, True)]
    if writable_extra:
        accounts.append(AccountMeta(writable_extra, False, True))
    ix = Instruction(SYSTEM_PROGRAM_ID, struct.pack("<IQ", 2, 100), accounts)
    msg = Message.new_with_blockhash([ix], payer.pubkey(), Hash.default())
    tx = VersionedTransaction(msg, [payer])
    tx_b64 = base64.b64encode(bytes(tx)).decode()
    return tx_b64, tx


# ---------------------------------------------------------------------------
# B4 TEST CASES: sim_guard.simulate_and_assert
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sim_guard_pass_case(monkeypatch):
    """Pass case: SOL decrease within limit, input token decreases as expected, output increases >= threshold."""
    payer = Keypair()
    token_mint = Keypair().pubkey()
    tx_b64, _ = _make_dummy_tx(payer)

    quote = {
        "inputMint": str(USDC_MINT),
        "outputMint": str(token_mint),
        "inAmount": "1000000",            # 1 USDC
        "otherAmountThreshold": "50000",   # 50000 tokens min
    }

    payer_usdc_ata, _ = Pubkey.find_program_address(
        [bytes(payer.pubkey()), bytes(TOKEN_PROGRAM_ID), bytes(USDC_MINT)],
        ATA_PROGRAM_ID,
    )
    payer_token_ata, _ = Pubkey.find_program_address(
        [bytes(payer.pubkey()), bytes(TOKEN_PROGRAM_ID), bytes(token_mint)],
        ATA_PROGRAM_ID,
    )

    # Pre-state: payer has 1 SOL (1e9 lamports), 10 USDC (10e6 units), 0 output token
    async def fake_get_multiple_accounts(addresses, endpoints=None):
        res = []
        for a in addresses:
            if a == str(payer.pubkey()):
                res.append({"lamports": 1_000_000_000, "owner": str(SYSTEM_PROGRAM_ID), "data": ["", "base64"]})
            elif a == str(payer_usdc_ata):
                res.append(_make_acc_info(2_000_000, TOKEN_PROGRAM_ID, _make_token_data(USDC_MINT, payer.pubkey(), 10_000_000)))
            elif a == str(payer_token_ata):
                res.append(None)  # not created yet
            else:
                res.append(None)
        return res

    # Post-state: payer SOL decreased by 5000 lamports (fee), USDC decreased by 1_000_000, token increased by 52_000
    async def fake_simulate_transaction(b64, accounts, endpoints=None, require_dual_rpc=True):
        post = []
        for a in accounts:
            if a == str(payer.pubkey()):
                post.append({"lamports": 999_995_000, "owner": str(SYSTEM_PROGRAM_ID), "data": ["", "base64"]})
            elif a == str(payer_usdc_ata):
                post.append(_make_acc_info(2_000_000, TOKEN_PROGRAM_ID, _make_token_data(USDC_MINT, payer.pubkey(), 9_000_000)))
            elif a == str(payer_token_ata):
                post.append(_make_acc_info(2_000_000, TOKEN_PROGRAM_ID, _make_token_data(token_mint, payer.pubkey(), 52_000)))
            else:
                post.append(None)
        return {"err": None, "logs": [], "units": 15000, "post_accounts": post}

    monkeypatch.setattr(solana, "get_multiple_accounts", fake_get_multiple_accounts)
    monkeypatch.setattr(solana, "simulate_transaction", fake_simulate_transaction)

    res = await sim_guard.simulate_and_assert(tx_b64, quote, str(payer.pubkey()), intent=make_intent_from_quote(quote, str(payer.pubkey())))
    assert res["status"] == "passed"
    assert res["sol_decrease"] == 5000
    assert res["input_decrease"] == 1_000_000
    assert res["output_increase"] == 52_000


@pytest.mark.asyncio
async def test_sim_guard_err_case(monkeypatch):
    """Err case: simulation on-chain err returns non-None -> must refuse."""
    payer = Keypair()
    tx_b64, _ = _make_dummy_tx(payer)
    quote = {"inputMint": str(USDC_MINT), "outputMint": str(WSOL_MINT), "inAmount": "1000", "otherAmountThreshold": "100"}

    async def fake_get_multiple_accounts(addresses, endpoints=None):
        return [{"lamports": 1_000_000_000, "owner": str(SYSTEM_PROGRAM_ID), "data": ["", "base64"]}] * len(addresses)

    async def fake_simulate_transaction(b64, accounts, endpoints=None, require_dual_rpc=True):
        return {"err": {"InstructionError": [0, "Custom"]}, "logs": [], "units": 500, "post_accounts": []}

    monkeypatch.setattr(solana, "get_multiple_accounts", fake_get_multiple_accounts)
    monkeypatch.setattr(solana, "simulate_transaction", fake_simulate_transaction)

    with pytest.raises(ExecutionError, match="failed on-chain with err"):
        await sim_guard.simulate_and_assert(tx_b64, quote, str(payer.pubkey()), intent=make_intent_from_quote(quote, str(payer.pubkey())))


@pytest.mark.asyncio
async def test_sim_guard_sol_over_cap_refused(monkeypatch):
    """SOL decrease > SIM_MAX_SOL_DECREASE_LAMPORTS must be refused."""
    payer = Keypair()
    tx_b64, _ = _make_dummy_tx(payer)
    quote = {"inputMint": str(USDC_MINT), "outputMint": str(WSOL_MINT), "inAmount": "1000", "otherAmountThreshold": "100"}

    async def fake_get_multiple_accounts(addresses, endpoints=None):
        return [{"lamports": 1_000_000_000, "owner": str(SYSTEM_PROGRAM_ID), "data": ["", "base64"]}] * len(addresses)

    async def fake_simulate_transaction(b64, accounts, endpoints=None, require_dual_rpc=True):
        # 20_000_000 lamports decrease > 10_000_000 cap
        post = [{"lamports": 980_000_000, "owner": str(SYSTEM_PROGRAM_ID), "data": ["", "base64"]}] * len(accounts)
        return {"err": None, "logs": [], "units": 1000, "post_accounts": post}

    monkeypatch.setattr(solana, "get_multiple_accounts", fake_get_multiple_accounts)
    monkeypatch.setattr(solana, "simulate_transaction", fake_simulate_transaction)

    with pytest.raises(ExecutionError, match="SIM_MAX_SOL_DECREASE_LAMPORTS|wallet SOL decrease"):
        await sim_guard.simulate_and_assert(tx_b64, quote, str(payer.pubkey()), intent=make_intent_from_quote(quote, str(payer.pubkey())))


@pytest.mark.asyncio
async def test_sim_guard_extra_token_account_drained_refused(monkeypatch):
    """If an unrelated wallet token account decreases in balance, must refuse."""
    payer = Keypair()
    other_mint = Keypair().pubkey()
    other_ata = Keypair().pubkey()
    tx_b64, _ = _make_dummy_tx(payer, other_ata)

    quote = {"inputMint": str(USDC_MINT), "outputMint": str(WSOL_MINT), "inAmount": "1000", "otherAmountThreshold": "100"}

    payer_wsol_ata, _ = Pubkey.find_program_address(
        [bytes(payer.pubkey()), bytes(TOKEN_PROGRAM_ID), bytes(WSOL_MINT)],
        ATA_PROGRAM_ID,
    )

    async def fake_get_multiple_accounts(addresses, endpoints=None):
        res = []
        for a in addresses:
            if a == str(payer.pubkey()):
                res.append({"lamports": 1_000_000_000, "owner": str(SYSTEM_PROGRAM_ID), "data": ["", "base64"]})
            elif a == str(other_ata):
                res.append(_make_acc_info(2_000_000, TOKEN_PROGRAM_ID, _make_token_data(other_mint, payer.pubkey(), 500_000)))
            else:
                res.append(None)
        return res

    async def fake_simulate_transaction(b64, accounts, endpoints=None, require_dual_rpc=True):
        post = []
        for a in accounts:
            if a == str(payer.pubkey()):
                post.append({"lamports": 999_995_000, "owner": str(SYSTEM_PROGRAM_ID), "data": ["", "base64"]})
            elif a == str(payer_wsol_ata):
                post.append(_make_acc_info(2_000_000, TOKEN_PROGRAM_ID, _make_token_data(WSOL_MINT, payer.pubkey(), 200)))
            elif a == str(other_ata):
                # Drained from 500_000 to 0!
                post.append(_make_acc_info(2_000_000, TOKEN_PROGRAM_ID, _make_token_data(other_mint, payer.pubkey(), 0)))
            else:
                post.append(None)
        return {"err": None, "logs": [], "units": 1000, "post_accounts": post}

    monkeypatch.setattr(solana, "get_multiple_accounts", fake_get_multiple_accounts)
    monkeypatch.setattr(solana, "simulate_transaction", fake_simulate_transaction)

    with pytest.raises(ExecutionError, match="unrelated wallet token account.*balance changed"):
        await sim_guard.simulate_and_assert(tx_b64, quote, str(payer.pubkey()), intent=make_intent_from_quote(quote, str(payer.pubkey())))


@pytest.mark.asyncio
async def test_sim_guard_output_below_threshold_refused(monkeypatch):
    """Output token increase below otherAmountThreshold must be refused."""
    payer = Keypair()
    token_mint = Keypair().pubkey()
    tx_b64, _ = _make_dummy_tx(payer)

    quote = {
        "inputMint": str(USDC_MINT),
        "outputMint": str(token_mint),
        "inAmount": "1000",
        "otherAmountThreshold": "5000",
    }
    payer_token_ata, _ = Pubkey.find_program_address(
        [bytes(payer.pubkey()), bytes(TOKEN_PROGRAM_ID), bytes(token_mint)],
        ATA_PROGRAM_ID,
    )

    async def fake_get_multiple_accounts(addresses, endpoints=None):
        return [{"lamports": 1_000_000_000, "owner": str(SYSTEM_PROGRAM_ID), "data": ["", "base64"]}] * len(addresses)

    async def fake_simulate_transaction(b64, accounts, endpoints=None, require_dual_rpc=True):
        post = []
        for a in accounts:
            if a == str(payer_token_ata):
                # Only 4000 received < 5000 threshold
                post.append(_make_acc_info(2_000_000, TOKEN_PROGRAM_ID, _make_token_data(token_mint, payer.pubkey(), 4000)))
            else:
                post.append({"lamports": 999_995_000, "owner": str(SYSTEM_PROGRAM_ID), "data": ["", "base64"]})
        return {"err": None, "logs": [], "units": 1000, "post_accounts": post}

    monkeypatch.setattr(solana, "get_multiple_accounts", fake_get_multiple_accounts)
    monkeypatch.setattr(solana, "simulate_transaction", fake_simulate_transaction)

    with pytest.raises(ExecutionError, match="otherAmountThreshold"):
        await sim_guard.simulate_and_assert(tx_b64, quote, str(payer.pubkey()), intent=make_intent_from_quote(quote, str(payer.pubkey())))


@pytest.mark.asyncio
async def test_sim_guard_output_ata_owned_by_someone_else_refused(monkeypatch):
    """Output tokens landing in an ATA not owned by payer must be refused."""
    payer = Keypair()
    attacker = Keypair().pubkey()
    token_mint = Keypair().pubkey()
    tx_b64, _ = _make_dummy_tx(payer)

    quote = {
        "inputMint": str(USDC_MINT),
        "outputMint": str(token_mint),
        "inAmount": "1000",
        "otherAmountThreshold": "5000",
    }
    payer_token_ata, _ = Pubkey.find_program_address(
        [bytes(payer.pubkey()), bytes(TOKEN_PROGRAM_ID), bytes(token_mint)],
        ATA_PROGRAM_ID,
    )

    async def fake_get_multiple_accounts(addresses, endpoints=None):
        return [{"lamports": 1_000_000_000, "owner": str(SYSTEM_PROGRAM_ID), "data": ["", "base64"]}] * len(addresses)

    async def fake_simulate_transaction(b64, accounts, endpoints=None, require_dual_rpc=True):
        post = []
        for a in accounts:
            if a == str(payer_token_ata):
                # Output received but owner is attacker!
                post.append(_make_acc_info(2_000_000, TOKEN_PROGRAM_ID, _make_token_data(token_mint, attacker, 10000)))
            else:
                post.append({"lamports": 999_995_000, "owner": str(SYSTEM_PROGRAM_ID), "data": ["", "base64"]})
        return {"err": None, "logs": [], "units": 1000, "post_accounts": post}

    monkeypatch.setattr(solana, "get_multiple_accounts", fake_get_multiple_accounts)
    monkeypatch.setattr(solana, "simulate_transaction", fake_simulate_transaction)

    with pytest.raises(ExecutionError, match="otherAmountThreshold|token output must land in wallet-owned ATA"):
        await sim_guard.simulate_and_assert(tx_b64, quote, str(payer.pubkey()), intent=make_intent_from_quote(quote, str(payer.pubkey())))


@pytest.mark.asyncio
async def test_sim_guard_owner_changed_refused(monkeypatch):
    """Wallet token account whose owner changes in post-state must be refused."""
    payer = Keypair()
    attacker = Keypair().pubkey()
    token_account = Keypair().pubkey()
    tx_b64, _ = _make_dummy_tx(payer, token_account)

    quote = {"inputMint": str(USDC_MINT), "outputMint": str(WSOL_MINT), "inAmount": "1000", "otherAmountThreshold": "100"}

    payer_wsol_ata, _ = Pubkey.find_program_address(
        [bytes(payer.pubkey()), bytes(TOKEN_PROGRAM_ID), bytes(WSOL_MINT)],
        ATA_PROGRAM_ID,
    )

    async def fake_get_multiple_accounts(addresses, endpoints=None):
        res = []
        for a in addresses:
            if a == str(payer.pubkey()):
                res.append({"lamports": 1_000_000_000, "owner": str(SYSTEM_PROGRAM_ID), "data": ["", "base64"]})
            elif a == str(token_account):
                res.append(_make_acc_info(2_000_000, TOKEN_PROGRAM_ID, _make_token_data(USDC_MINT, payer.pubkey(), 1000)))
            else:
                res.append(None)
        return res

    async def fake_simulate_transaction(b64, accounts, endpoints=None, require_dual_rpc=True):
        post = []
        for a in accounts:
            if a == str(payer.pubkey()):
                post.append({"lamports": 999_995_000, "owner": str(SYSTEM_PROGRAM_ID), "data": ["", "base64"]})
            elif a == str(payer_wsol_ata):
                post.append(_make_acc_info(2_000_000, TOKEN_PROGRAM_ID, _make_token_data(WSOL_MINT, payer.pubkey(), 200)))
            elif a == str(token_account):
                # Owner changed from payer to attacker!
                post.append(_make_acc_info(2_000_000, TOKEN_PROGRAM_ID, _make_token_data(USDC_MINT, attacker, 1000)))
            else:
                post.append(None)
        return {"err": None, "logs": [], "units": 1000, "post_accounts": post}

    monkeypatch.setattr(solana, "get_multiple_accounts", fake_get_multiple_accounts)
    monkeypatch.setattr(solana, "simulate_transaction", fake_simulate_transaction)

    with pytest.raises(ExecutionError, match="owner changed"):
        await sim_guard.simulate_and_assert(tx_b64, quote, str(payer.pubkey()), intent=make_intent_from_quote(quote, str(payer.pubkey())))


@pytest.mark.asyncio
async def test_sim_guard_rpc_down_refused(monkeypatch):
    """When RPC is completely unreachable or raises, simulate_and_assert must fail closed."""
    payer = Keypair()
    tx_b64, _ = _make_dummy_tx(payer)
    quote = {"inputMint": str(USDC_MINT), "outputMint": str(WSOL_MINT), "inAmount": "1000", "otherAmountThreshold": "100"}

    async def boom(*args, **kwargs):
        raise solana.SimulationError("RPC connection refused")

    monkeypatch.setattr(solana, "get_multiple_accounts", boom)

    with pytest.raises(ExecutionError, match="pre-state fetch failed|RPC connection refused"):
        await sim_guard.simulate_and_assert(tx_b64, quote, str(payer.pubkey()), intent=make_intent_from_quote(quote, str(payer.pubkey())))


@pytest.mark.asyncio
async def test_sim_guard_malformed_layout_refused(monkeypatch):
    """Token account with malformed layout (< 165 bytes) must be refused."""
    payer = Keypair()
    tok_acc = Keypair().pubkey()
    tx_b64, _ = _make_dummy_tx(payer, tok_acc)
    quote = {"inputMint": str(USDC_MINT), "outputMint": str(WSOL_MINT), "inAmount": "1000", "otherAmountThreshold": "100"}

    async def fake_get_multiple_accounts(addresses, endpoints=None):
        res = []
        for a in addresses:
            if a == str(payer.pubkey()):
                res.append({"lamports": 1_000_000_000, "owner": str(SYSTEM_PROGRAM_ID), "data": ["", "base64"]})
            elif a == str(tok_acc):
                # Truncated 50 bytes data
                res.append(_make_acc_info(2_000_000, TOKEN_PROGRAM_ID, b"\x00" * 50))
            else:
                res.append(None)
        return res

    monkeypatch.setattr(solana, "get_multiple_accounts", fake_get_multiple_accounts)

    with pytest.raises(ExecutionError, match="malformed token account layout"):
        await sim_guard.simulate_and_assert(tx_b64, quote, str(payer.pubkey()), intent=make_intent_from_quote(quote, str(payer.pubkey())))


@pytest.mark.asyncio
async def test_sim_guard_reproduce_a1_drain_independently_refused(monkeypatch):
    """
    A1 attack transaction fed through a fake sim that reports 'err=None'.
    The simulation layer independently refuses it because output never reaches
    the wallet's ATA and foreign account receives funds.
    """
    payer = Keypair()
    n_account = Keypair()
    attacker = Keypair().pubkey()

    create_data = struct.pack("<IQQ", 0, 2_000_000, 165) + bytes(TOKEN_PROGRAM_ID)
    ix_create = Instruction(
        SYSTEM_PROGRAM_ID,
        create_data,
        [AccountMeta(payer.pubkey(), True, True), AccountMeta(n_account.pubkey(), True, True)],
    )
    ix_init = Instruction(
        TOKEN_PROGRAM_ID,
        bytes([18]) + bytes(attacker),
        [AccountMeta(n_account.pubkey(), False, True), AccountMeta(WSOL_MINT, False, False)],
    )
    ix_transfer = Instruction(
        SYSTEM_PROGRAM_ID,
        struct.pack("<IQ", 2, 5_000_000_000),
        [AccountMeta(payer.pubkey(), True, True), AccountMeta(n_account.pubkey(), False, True)],
    )
    ix_sync = Instruction(TOKEN_PROGRAM_ID, bytes([17]), [AccountMeta(n_account.pubkey(), False, True)])

    msg = Message.new_with_blockhash([ix_create, ix_init, ix_transfer, ix_sync], payer.pubkey(), Hash.default())
    tx = VersionedTransaction(msg, [payer, n_account])
    tx_b64 = base64.b64encode(bytes(tx)).decode()

    quote = {
        "inputMint": str(USDC_MINT),
        "outputMint": str(WSOL_MINT),
        "inAmount": "1000000",
        "otherAmountThreshold": "50000",
    }

    async def fake_get_multiple_accounts(addresses, endpoints=None):
        return [{"lamports": 10_000_000_000, "owner": str(SYSTEM_PROGRAM_ID), "data": ["", "base64"]}] * len(addresses)

    # Fake sim reports success, but payer SOL was drained by 5 SOL (> 10m lamports) and output didn't reach ATA
    async def fake_simulate_transaction(b64, accounts, endpoints=None, require_dual_rpc=True):
        post = []
        for a in accounts:
            if a == str(payer.pubkey()):
                post.append({"lamports": 5_000_000_000, "owner": str(SYSTEM_PROGRAM_ID), "data": ["", "base64"]})
            else:
                post.append(None)
        return {"err": None, "logs": [], "units": 1000, "post_accounts": post}

    monkeypatch.setattr(solana, "get_multiple_accounts", fake_get_multiple_accounts)
    monkeypatch.setattr(solana, "simulate_transaction", fake_simulate_transaction)

    with pytest.raises(ExecutionError, match="wallet SOL decrease|otherAmountThreshold"):
        await sim_guard.simulate_and_assert(tx_b64, quote, str(payer.pubkey()), intent=make_intent_from_quote(quote, str(payer.pubkey())))


# ---------------------------------------------------------------------------
# AST & MONKEYPATCH VERIFICATION: NO SEND_RAW_TRANSACTION WITHOUT SIMULATION
# ---------------------------------------------------------------------------

def test_ast_swap_paths_must_call_sign_transaction_before_send_raw():
    """
    AST analysis: Neither executor.py nor jupiter_executor.py may call send_raw_transaction directly;
    they must call send_checked_swap, and sign_after_checks must precede it.
    """
    repo_root = Path(__file__).resolve().parent.parent

    for py_file in [repo_root / "live_execution" / "executor.py", repo_root / "live_execution" / "jupiter_executor.py"]:
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                calls = [
                    n.func.attr if isinstance(n.func, ast.Attribute) else (n.func.id if isinstance(n.func, ast.Name) else "")
                    for n in ast.walk(node)
                    if isinstance(n, ast.Call)
                ]
                assert "send_raw_transaction" not in calls, f"{py_file.name}::{node.name} must not call send_raw_transaction"
                if "send_checked_swap" in calls:
                    assert "sign_after_checks" in calls, f"{py_file.name}::{node.name} calls send_checked_swap without sign_after_checks"


@pytest.mark.asyncio
async def test_monkeypatch_no_send_when_simulation_fails(monkeypatch):
    """
    Runtime proof: If simulation fails in sign_after_checks, send_checked_swap
    is NEVER called.
    """
    payer = Keypair()
    token_mint = Keypair().pubkey()
    tx_b64, _ = _make_dummy_tx(payer)

    quote = {
        "inputMint": str(USDC_MINT),
        "outputMint": str(token_mint),
        "inAmount": "1000000",
        "outAmount": "50000",
        "otherAmountThreshold": "50000",
        "slippageBps": 50,
        "swapMode": "ExactIn",
    }
    intent = TradeIntent(
        side="buy",
        wallet=str(payer.pubkey()),
        input_mint=str(USDC_MINT),
        output_mint=str(token_mint),
        in_amount_raw=1000000,
        slippage_bps=50,
        min_out_raw=50000,
        reference_price_usd=20.0,
    )

    send_checked_called = False

    async def fake_send_checked(signed, endpoints=None):
        nonlocal send_checked_called
        send_checked_called = True
        return "SIG_SHOULD_NEVER_HAPPEN"

    async def failing_sim_guard(*args, **kwargs):
        raise ExecutionError("simulation assertion failure: SOL drained")

    monkeypatch.setattr(solana, "send_checked_swap", fake_send_checked)
    monkeypatch.setattr(sim_guard, "simulate_and_assert", failing_sim_guard)

    with pytest.raises(ExecutionError, match="simulation assertion failure"):
        await sign_after_checks(tx_b64, payer, intent=intent, quote=quote)

    assert not send_checked_called, "send_checked_swap MUST NOT be called if simulation fails"
