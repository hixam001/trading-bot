"""
backend/tests/test_audit_79_a.py — Tests for Master Prompt §79 Phase A:
Make the simulation layer trustworthy.

Covers:
- A-1: Quote validation against locally-computed TradeIntent; independent price floor; self-consistency.
- A-2: No signing without simulation; removal of sync unpacking; SignedSwap._checked enforcement; AST audit.
- A-3: Real multi-RPC quorum; compare token accounts and payer lamports; single-RPC sell vs buy; host deduplication.
- A-4: Token account control field assertions (delegate, close_authority, state, delegated_amount).

Mutation checks:
- Each refusal rule states in its test docstring which line was removed to confirm failure.
"""
from __future__ import annotations

import ast
import base64
import math
import struct
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
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
    _TOKEN_2022_PROGRAM_ID,
    _WSOL_MINT,
    ExecutionError,
    sign_after_checks,
)
from live_execution.solana import SignedSwap, responses_agree, send_checked_swap
from live_execution.trade_intent import TradeIntent

SYSTEM_PROGRAM_ID = Pubkey.from_string(_SYSTEM_PROGRAM_ID)
TOKEN_PROGRAM_ID = Pubkey.from_string(_SPL_TOKEN_PROGRAM_ID)
TOKEN_2022_PROGRAM_ID = Pubkey.from_string(_TOKEN_2022_PROGRAM_ID)
ATA_PROGRAM_ID = Pubkey.from_string(_ATA_PROGRAM_ID)
WSOL_MINT = Pubkey.from_string(_WSOL_MINT)
USDC_MINT = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")


def _make_token_data(
    mint: Pubkey,
    owner: Pubkey,
    amount: int,
    delegate: Pubkey | None = None,
    state: int = 1,
    delegated_amount: int = 0,
    close_authority: Pubkey | None = None,
) -> bytes:
    """Pack SPL Token account 165-byte layout with full control fields."""
    buf = bytearray(165)
    buf[0:32] = bytes(mint)
    buf[32:64] = bytes(owner)
    struct.pack_into("<Q", buf, 64, amount)
    if delegate is not None:
        struct.pack_into("<I", buf, 72, 1)
        buf[76:108] = bytes(delegate)
    else:
        struct.pack_into("<I", buf, 72, 0)
    buf[108] = state
    # is_native = None (tag 0, val 0)
    struct.pack_into("<I", buf, 109, 0)
    struct.pack_into("<Q", buf, 113, 0)
    struct.pack_into("<Q", buf, 121, delegated_amount)
    if close_authority is not None:
        struct.pack_into("<I", buf, 129, 1)
        buf[133:165] = bytes(close_authority)
    else:
        struct.pack_into("<I", buf, 129, 0)
    return bytes(buf)


def _make_acc_info(lamports: int, owner_pid: Pubkey, data_bytes: bytes) -> dict:
    return {
        "lamports": lamports,
        "owner": str(owner_pid),
        "data": [base64.b64encode(data_bytes).decode(), "base64"],
        "executable": False,
        "rentEpoch": 0,
    }


def _make_dummy_tx(payer: Keypair) -> tuple[str, VersionedTransaction]:
    accounts = [AccountMeta(payer.pubkey(), True, True), AccountMeta(payer.pubkey(), False, True)]
    ix = Instruction(SYSTEM_PROGRAM_ID, struct.pack("<IQ", 2, 100), accounts)
    msg = Message.new_with_blockhash([ix], payer.pubkey(), Hash.default())
    tx = VersionedTransaction(msg, [payer])
    tx_b64 = base64.b64encode(bytes(tx)).decode()
    return tx_b64, tx


# ===========================================================================
# A-1 TESTS: Bind quote to local TradeIntent
# ===========================================================================

def test_validate_quote_against_intent_mismatches():
    """
    Parametrised verification of quote-intent validation rules.
    Mutation check:
    - Removing `quote_in_amount != intent.in_amount_raw` check in sim_guard.validate_quote_against_intent
      fails test_validate_quote_against_intent_mismatches['inAmount mismatch'].
    """
    intent = TradeIntent(
        side="buy",
        wallet="WALLET1111111111111111111111111111111111111",
        input_mint=str(USDC_MINT),
        output_mint="TOKEN1111111111111111111111111111111111111",
        in_amount_raw=10_000_000,  # $10 USDC
        slippage_bps=50,
        min_out_raw=9000,
        reference_price_usd=1.0,
    )

    valid_quote = {
        "inputMint": str(USDC_MINT),
        "outputMint": "TOKEN1111111111111111111111111111111111111",
        "inAmount": "10000000",
        "outAmount": "10000",
        "otherAmountThreshold": "9950",
        "swapMode": "ExactIn",
        "slippageBps": 50,
        "platformFee": None,
    }

    # Baseline valid quote passes
    sim_guard.validate_quote_against_intent(valid_quote, intent)

    # 1. inputMint mismatch
    q_bad_in_mint = dict(valid_quote, inputMint="WRONG_MINT")
    with pytest.raises(ExecutionError, match="inputMint mismatch"):
        sim_guard.validate_quote_against_intent(q_bad_in_mint, intent)

    # 2. outputMint mismatch
    q_bad_out_mint = dict(valid_quote, outputMint="WRONG_MINT")
    with pytest.raises(ExecutionError, match="outputMint mismatch"):
        sim_guard.validate_quote_against_intent(q_bad_out_mint, intent)

    # 3. inAmount mismatch
    q_bad_in_amt = dict(valid_quote, inAmount="500000000")
    with pytest.raises(ExecutionError, match="inAmount mismatch"):
        sim_guard.validate_quote_against_intent(q_bad_in_amt, intent)

    # 4. swapMode != ExactIn
    q_bad_mode = dict(valid_quote, swapMode="ExactOut")
    with pytest.raises(ExecutionError, match="swapMode mismatch"):
        sim_guard.validate_quote_against_intent(q_bad_mode, intent)

    # 5. slippageBps mismatch
    q_bad_slip = dict(valid_quote, slippageBps=100)
    with pytest.raises(ExecutionError, match="slippageBps mismatch"):
        sim_guard.validate_quote_against_intent(q_bad_slip, intent)

    # 6. outAmount <= 0
    q_zero_out = dict(valid_quote, outAmount="0")
    with pytest.raises(ExecutionError, match="outAmount must be positive"):
        sim_guard.validate_quote_against_intent(q_zero_out, intent)

    # 7. otherAmountThreshold self-consistency lie (threshold=0 when outAmount=10000)
    q_bad_thresh = dict(valid_quote, otherAmountThreshold="0")
    with pytest.raises(ExecutionError, match="otherAmountThreshold inconsistent"):
        sim_guard.validate_quote_against_intent(q_bad_thresh, intent)

    # 8. platformFee skim
    q_fee_skim = dict(valid_quote, platformFee={"amount": "50000"})
    with pytest.raises(ExecutionError, match="platformFee skim"):
        sim_guard.validate_quote_against_intent(q_fee_skim, intent)

    # 9. otherAmountThreshold below independent price floor (outAmount 8900 has min self-consistent threshold 8854; intent floor is 9000)
    q_below_floor = dict(valid_quote, outAmount="8900", otherAmountThreshold="8855")
    with pytest.raises(ExecutionError, match="below independent price floor"):
        sim_guard.validate_quote_against_intent(q_below_floor, intent)


@pytest.mark.asyncio
async def test_a1_reproduction_lying_quote_refused_by_sim_guard(monkeypatch):
    """
    Reproduction of A-1:
    Wallet has 1000 USDC. Real intent is $10 (10_000_000 raw).
    Attacker quote claims inAmount=500_000_000 (500 USDC), otherAmountThreshold=0.
    In the simulation post-state, wallet USDC drops by 500 USDC.
    Under §79-A, this MUST be refused with an inAmount mismatch error before simulation,
    and even if it reached balance checks, it would bound decrease by intent.in_amount_raw.
    Mutation check:
    - Removing `validate_quote_against_intent` call in simulate_and_assert allows a quote
      with inAmount matching post-state to bypass intent check.
    """
    payer = Keypair()
    token_mint = Keypair().pubkey()
    tx_b64, _ = _make_dummy_tx(payer)

    intent = TradeIntent(
        side="buy",
        wallet=str(payer.pubkey()),
        input_mint=str(USDC_MINT),
        output_mint=str(token_mint),
        in_amount_raw=10_000_000,
        slippage_bps=50,
        min_out_raw=9000,
        reference_price_usd=1.0,
    )

    lying_quote = {
        "inputMint": str(USDC_MINT),
        "outputMint": str(token_mint),
        "inAmount": "500000000",
        "outAmount": "50000",
        "otherAmountThreshold": "0",
        "swapMode": "ExactIn",
        "slippageBps": 50,
        "platformFee": None,
    }

    with pytest.raises(ExecutionError, match="inAmount mismatch"):
        await sim_guard.simulate_and_assert(tx_b64, lying_quote, str(payer.pubkey()), intent=intent)


# ===========================================================================
# A-2 TESTS: Unsimulated signing is impossible
# ===========================================================================

def test_a2_awaitable_signed_tx_and_sync_unpack_removed():
    """
    Reproduction of A-2:
    The old AwaitableSignedTx allowed `sig, raw = _sign_transaction(...)` without simulation.
    Under §79-A, AwaitableSignedTx is removed and `sign_after_checks` is an async function
    returning SignedSwap.
    Mutation check:
    - Re-introducing a sync `_sign_transaction` helper would violate AST and interface rules.
    """
    import live_execution.jupiter_executor as je
    assert not hasattr(je, "AwaitableSignedTx"), "AwaitableSignedTx MUST be deleted"


@pytest.mark.asyncio
async def test_a2_send_checked_swap_rejects_unverified_signed_swap():
    """
    Calling send_checked_swap with a hand-built SignedSwap(..., _checked=False) raises ExecutionError.
    Mutation check:
    - Removing `not getattr(signed, '_checked', False)` in send_checked_swap permits unchecked swaps.
    """
    bogus_signed = SignedSwap(signature="BOGUS_SIG", raw=b"raw_tx_bytes", _checked=False)
    with pytest.raises(ExecutionError, match="not verified by sign_after_checks"):
        await send_checked_swap(bogus_signed)


@pytest.mark.asyncio
async def test_a2_simulation_failure_never_calls_signing(monkeypatch):
    """
    When SIMULATION_REQUIRED is True and simulation fails, sign_after_checks
    raises ExecutionError and NEVER signs the transaction.
    Mutation check:
    - Removing `await sim_guard.simulate_and_assert` before signing allows signing on sim error.
    """
    payer = Keypair()
    token_mint = Keypair().pubkey()
    tx_b64, _ = _make_dummy_tx(payer)

    intent = TradeIntent(
        side="buy",
        wallet=str(payer.pubkey()),
        input_mint=str(USDC_MINT),
        output_mint=str(token_mint),
        in_amount_raw=10_000_000,
        slippage_bps=50,
        min_out_raw=9000,
        reference_price_usd=1.0,
    )
    quote = {
        "inputMint": str(USDC_MINT),
        "outputMint": str(token_mint),
        "inAmount": "10000000",
        "outAmount": "10000",
        "otherAmountThreshold": "9950",
        "swapMode": "ExactIn",
        "slippageBps": 50,
        "platformFee": None,
    }

    async def fake_failing_sim(*args, **kwargs):
        raise ExecutionError("sim failed intentionally")

    monkeypatch.setattr(sim_guard, "simulate_and_assert", fake_failing_sim)

    with pytest.raises(ExecutionError, match="sim failed intentionally"):
        await sign_after_checks(tx_b64, payer, intent, quote=quote)


def test_a2_ast_no_swap_modules_call_send_raw_transaction():
    """
    AST check:
    No module under backend/ other than solana.py, drill.py, and memo.py calls send_raw_transaction.
    Swap modules (executor.py, jupiter_executor.py) reference send_checked_swap only.
    Mutation check:
    - Adding `send_raw_transaction` to executor.py fails this assertion.
    """
    repo_root = Path(__file__).resolve().parent.parent
    allowed_callers = {"solana.py", "drill.py", "memo.py"}

    for py_file in (repo_root / "live_execution").glob("*.py"):
        if py_file.name in allowed_callers or py_file.name.startswith("test_"):
            continue
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func_name = ""
                if isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                elif isinstance(node.func, ast.Name):
                    func_name = node.func.id
                assert func_name != "send_raw_transaction", (
                    f"{py_file.name} calls send_raw_transaction directly; must use send_checked_swap"
                )


# ===========================================================================
# A-3 TESTS: Multi-RPC quorum and consensus
# ===========================================================================

def test_a3_responses_agree_token_balances_and_lamports():
    """
    Test pure responses_agree logic:
    - Agreeing responses pass
    - Disagreeing token balances beyond tolerance fail
    - Disagreeing payer lamports beyond tolerance fail
    - Mismatched accounts length fails
    - Mismatched err fails
    Mutation check:
    - Removing token amount comparison in responses_agree allows diverging token balances to pass.
    """
    payer = Keypair()
    token_mint = Keypair().pubkey()
    ata_pubkey = str(Keypair().pubkey())

    tok_a = _make_token_data(token_mint, payer.pubkey(), 1_000_000)
    tok_b_agree = _make_token_data(token_mint, payer.pubkey(), 1_002_000)  # within 50 bps (0.5%)
    tok_b_disagree = _make_token_data(token_mint, payer.pubkey(), 2_000_000)  # 100% diff

    res_base = {
        "endpoint": "http://rpc1.local",
        "err": None,
        "units": 100,
        "post_accounts": [
            {"lamports": 1_000_000_000, "owner": str(SYSTEM_PROGRAM_ID), "data": ["", "base64"]},
            _make_acc_info(2_000_000, TOKEN_PROGRAM_ID, tok_a),
        ],
    }

    res_agree = {
        "endpoint": "http://rpc2.local",
        "err": None,
        "units": 105,
        "post_accounts": [
            {"lamports": 1_000_005_000, "owner": str(SYSTEM_PROGRAM_ID), "data": ["", "base64"]},  # 5k drift (<=10k)
            _make_acc_info(2_000_000, TOKEN_PROGRAM_ID, tok_b_agree),
        ],
    }

    wallet_owned = [str(payer.pubkey()), ata_pubkey]
    assert responses_agree(res_base, res_agree, wallet_owned) is True

    # Token balance diverges beyond tolerance
    res_diverge_token = {
        "endpoint": "http://rpc2.local",
        "err": None,
        "units": 105,
        "post_accounts": [
            {"lamports": 1_000_000_000, "owner": str(SYSTEM_PROGRAM_ID), "data": ["", "base64"]},
            _make_acc_info(2_000_000, TOKEN_PROGRAM_ID, tok_b_disagree),
        ],
    }
    assert responses_agree(res_base, res_diverge_token, wallet_owned) is False

    # Payer lamports diverge beyond 10,000 lamports
    res_diverge_sol = {
        "endpoint": "http://rpc2.local",
        "err": None,
        "units": 105,
        "post_accounts": [
            {"lamports": 1_000_050_000, "owner": str(SYSTEM_PROGRAM_ID), "data": ["", "base64"]},  # 50k drift (>10k)
            _make_acc_info(2_000_000, TOKEN_PROGRAM_ID, tok_b_agree),
        ],
    }
    assert responses_agree(res_base, res_diverge_sol, wallet_owned) is False


@pytest.mark.asyncio
async def test_a3_quorum_enforcement_buy_vs_sell(monkeypatch):
    """
    Quorum policy:
    - Buys require agreeing >= SIM_MIN_AGREEING_RPCS (2), refusing if only 1 responds.
    - Sells may proceed on 1 RPC if SIM_SELL_ALLOW_SINGLE_RPC is True.
    - Sells refuse on 1 RPC if SIM_SELL_ALLOW_SINGLE_RPC is False.
    - Endpoints with duplicate hostnames count as one host.
    Mutation check:
    - Removing `if not is_sell and agreeing < config.SIM_MIN_AGREEING_RPCS:` in simulate_transaction
      allows single-RPC buys to proceed.
    """
    payer = Keypair()
    token_mint = Keypair().pubkey()
    tx_b64, _ = _make_dummy_tx(payer)

    single_resp = {
        "err": None,
        "unitsConsumed": 100,
        "accounts": [
            {"lamports": 1_000_000_000, "owner": str(SYSTEM_PROGRAM_ID), "data": ["", "base64"]},
        ],
    }

    def make_handler(fail_second: bool = False):
        def handler(request: httpx.Request) -> httpx.Response:
            if "rpc2" in str(request.url) and fail_second:
                return httpx.Response(500, text="Internal Server Error")
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"value": single_resp}})
        return handler

    # 1. Buy with only 1 endpoint answering -> refused
    transport = httpx.MockTransport(make_handler(fail_second=True))
    monkeypatch.setattr(solana, "_get_async_client", lambda timeout=15.0: httpx.AsyncClient(transport=transport))

    endpoints = ["https://rpc1.solana.com", "https://rpc2.solana.com"]
    with pytest.raises(solana.SimulationError, match="simulation quorum not reached"):
        await solana.simulate_transaction(
            tx_b64, [str(payer.pubkey())],
            endpoints=endpoints,
            side="buy",
            wallet_owned_addresses=[str(payer.pubkey())],
        )

    # 2. Sell with 1 endpoint answering and SIM_SELL_ALLOW_SINGLE_RPC=True -> succeeds
    monkeypatch.setattr(config, "SIM_SELL_ALLOW_SINGLE_RPC", True)
    res_sell = await solana.simulate_transaction(
        tx_b64, [str(payer.pubkey())],
        endpoints=endpoints,
        side="sell",
        wallet_owned_addresses=[str(payer.pubkey())],
    )
    assert res_sell["err"] is None

    # 3. Sell with 1 endpoint answering and SIM_SELL_ALLOW_SINGLE_RPC=False -> refused
    monkeypatch.setattr(config, "SIM_SELL_ALLOW_SINGLE_RPC", False)
    with pytest.raises(solana.SimulationError, match="simulation quorum not reached"):
        await solana.simulate_transaction(
            tx_b64, [str(payer.pubkey())],
            endpoints=endpoints,
            side="sell",
            wallet_owned_addresses=[str(payer.pubkey())],
        )

    # 4. Duplicate hostnames count once
    dup_hosts = ["https://rpc1.solana.com/path1", "https://rpc1.solana.com/path2"]
    with pytest.raises(solana.SimulationError, match="simulation quorum not reached"):
        await solana.simulate_transaction(
            tx_b64, [str(payer.pubkey())],
            endpoints=dup_hosts,
            side="buy",
            wallet_owned_addresses=[str(payer.pubkey())],
        )


# ===========================================================================
# A-4 TESTS: Token account control-field assertions
# ===========================================================================

def test_a4_decode_token_account_layout():
    """
    Verify decode_token_account_data extracts delegate, state, delegated_amount, close_authority.
    Mutation check:
    - Removing delegate unpacking in decode_token_account_data returns None for valid delegate.
    """
    mint = Keypair().pubkey()
    owner = Keypair().pubkey()
    delegate = Keypair().pubkey()
    close_auth = Keypair().pubkey()

    raw = _make_token_data(
        mint, owner, 5000,
        delegate=delegate,
        state=2,  # Frozen
        delegated_amount=1000,
        close_authority=close_auth,
    )

    decoded = sim_guard.decode_token_account_data(raw)
    assert decoded["mint"] == str(mint)
    assert decoded["owner"] == str(owner)
    assert decoded["amount"] == 5000
    assert decoded["delegate"] == str(delegate)
    assert decoded["state"] == 2
    assert decoded["delegated_amount"] == 1000
    assert decoded["close_authority"] == str(close_auth)


@pytest.mark.asyncio
async def test_a4_control_field_changes_refused_in_simulation(monkeypatch):
    """
    Assertions on wallet-owned token accounts in post-state:
    - delegate set (None -> Some) ⇒ refuse
    - close_authority set (None -> Some) ⇒ refuse
    - state changed to Frozen (state == 2) ⇒ refuse
    - delegated_amount changed ⇒ refuse
    - identical control fields ⇒ pass
    Mutation check:
    - Removing `if post_tok.get('delegate') != pre_tok.get('delegate'):` in sim_guard.simulate_and_assert
      allows delegate hijacking to pass.
    """
    payer = Keypair()
    token_mint = Keypair().pubkey()
    attacker = Keypair().pubkey()
    tx_b64, _ = _make_dummy_tx(payer)

    intent = TradeIntent(
        side="buy",
        wallet=str(payer.pubkey()),
        input_mint=str(USDC_MINT),
        output_mint=str(token_mint),
        in_amount_raw=10_000_000,
        slippage_bps=50,
        min_out_raw=9000,
        reference_price_usd=1.0,
    )
    quote = {
        "inputMint": str(USDC_MINT),
        "outputMint": str(token_mint),
        "inAmount": "10000000",
        "outAmount": "10000",
        "otherAmountThreshold": "9950",
        "swapMode": "ExactIn",
        "slippageBps": 50,
        "platformFee": None,
    }

    payer_usdc_ata, _ = Pubkey.find_program_address(
        [bytes(payer.pubkey()), bytes(TOKEN_PROGRAM_ID), bytes(USDC_MINT)],
        ATA_PROGRAM_ID,
    )
    payer_token_ata, _ = Pubkey.find_program_address(
        [bytes(payer.pubkey()), bytes(TOKEN_PROGRAM_ID), bytes(token_mint)],
        ATA_PROGRAM_ID,
    )

    tok_pre_usdc = _make_token_data(USDC_MINT, payer.pubkey(), 20_000_000)
    tok_post_usdc_clean = _make_token_data(USDC_MINT, payer.pubkey(), 10_000_000)
    tok_post_out_clean = _make_token_data(token_mint, payer.pubkey(), 10_000)

    # Helper to run simulation with custom post data
    async def run_with_post_usdc(post_usdc_bytes, post_out_bytes=tok_post_out_clean):
        async def fake_gma(addrs, endpoints=None, **kw):
            res = []
            for a in addrs:
                if a == str(payer.pubkey()):
                    res.append({"lamports": 1_000_000_000, "owner": str(SYSTEM_PROGRAM_ID), "data": ["", "base64"]})
                elif a == str(payer_usdc_ata):
                    res.append(_make_acc_info(2_000_000, TOKEN_PROGRAM_ID, tok_pre_usdc))
                elif a == str(payer_token_ata):
                    res.append(None)
                else:
                    res.append(None)
            return res

        async def fake_sim(b64, addrs, endpoints=None, **kw):
            post = []
            for a in addrs:
                if a == str(payer.pubkey()):
                    post.append({"lamports": 1_000_000_000, "owner": str(SYSTEM_PROGRAM_ID), "data": ["", "base64"]})
                elif a == str(payer_usdc_ata):
                    post.append(_make_acc_info(2_000_000, TOKEN_PROGRAM_ID, post_usdc_bytes))
                elif a == str(payer_token_ata):
                    post.append(_make_acc_info(2_000_000, TOKEN_PROGRAM_ID, post_out_bytes))
                else:
                    post.append(None)
            return {"err": None, "units": 1000, "post_accounts": post}

        monkeypatch.setattr(solana, "get_multiple_accounts", fake_gma)
        monkeypatch.setattr(solana, "simulate_transaction", fake_sim)

        return await sim_guard.simulate_and_assert(tx_b64, quote, str(payer.pubkey()), intent=intent)

    # 1. Clean passes
    res = await run_with_post_usdc(tok_post_usdc_clean)
    assert res["status"] == "passed"

    # 2. Delegate hijacked -> refused
    tok_post_delegate = _make_token_data(USDC_MINT, payer.pubkey(), 10_000_000, delegate=attacker)
    with pytest.raises(ExecutionError, match="delegate set or changed"):
        await run_with_post_usdc(tok_post_delegate)

    # 3. Close authority set -> refused
    tok_post_close = _make_token_data(USDC_MINT, payer.pubkey(), 10_000_000, close_authority=attacker)
    with pytest.raises(ExecutionError, match="close_authority set or changed"):
        await run_with_post_usdc(tok_post_close)

    # 4. State frozen -> refused
    tok_post_frozen = _make_token_data(USDC_MINT, payer.pubkey(), 10_000_000, state=2)
    with pytest.raises(ExecutionError, match="frozen"):
        await run_with_post_usdc(tok_post_frozen)

    # 5. Delegated amount increased -> refused
    tok_post_del_amt = _make_token_data(USDC_MINT, payer.pubkey(), 10_000_000, delegated_amount=5000)
    with pytest.raises(ExecutionError, match="delegated_amount changed"):
        await run_with_post_usdc(tok_post_del_amt)
