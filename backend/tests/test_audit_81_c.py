"""
backend/tests/test_audit_81_c.py — §81 Phase C: Close remaining signing-guard gaps.

Tests:
  C-1: Post-state token account control fields checked for newly created accounts (and existing accounts)
  C-2: Strict quorum token account comparison in responses_agree
  C-3: Single refusal counter and single alert on quote mismatch (no double counting)
  C-4: simulate_and_assert requires intent (AST check: no _helpers in prod, TypeError on missing intent)
  C-5: usd_to_raw_amount consistent rounding & cent property test (0.01..100.00)
  C-6: Reference price check runs BEFORE memo in place_buy (no memo fee, no sealed commit)
  C-7: Priority fee calculation scales by non-ComputeBudget instruction count
"""
from __future__ import annotations

import ast
import base64
from decimal import Decimal
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.system_program import ID as SYSTEM_PROGRAM_ID
from solders.transaction import VersionedTransaction

from live_execution import config as le_config
from live_execution.sim_guard import (
    simulate_and_assert,
    validate_quote_against_intent,
    QuoteMismatch,
    SimulationRefused,
)
from live_execution.solana import responses_agree
from live_execution.trade_intent import TradeIntent, usd_to_raw_amount
from live_execution.jupiter_executor import (
    inspect_swap_transaction,
    ExecutionError,
    _BACKEND_USDC_MINT,
    _SPL_TOKEN_PROGRAM_ID,
    _TOKEN_2022_PROGRAM_ID,
    _COMPUTE_BUDGET_PROGRAM_ID,
)
from tests._helpers import intent_from_quote_for_tests, encode_token_account_data


USDC = _BACKEND_USDC_MINT
MINT = "So11111111111111111111111111111111111111112"


def _make_dummy_quote(out_amount: int = 1_000_000, slippage: int = 50) -> dict:
    threshold = out_amount * (10_000 - slippage) // 10_000
    return {
        "inputMint": USDC,
        "outputMint": MINT,
        "inAmount": "1000000",
        "outAmount": str(out_amount),
        "otherAmountThreshold": str(threshold),
        "slippageBps": slippage,
        "swapMode": "ExactIn",
    }


# ---------------------------------------------------------------------------
# C-1: Post-state newly created ATA control fields
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corrupt_field,corrupt_val,expected_marker",
    [
        ("delegate", "attacker", "delegate"),
        ("close_authority", "attacker", "close_authority"),
        ("delegated_amount", 500, "delegated_amount"),
        ("state", 2, "state"),
    ],
)
async def test_c1_new_ata_control_field_violations_refused(monkeypatch, corrupt_field, corrupt_val, expected_marker):
    """
    C-1 [PROVEN]: A new ATA created in the transaction (not in pre-state)
    with non-clear control fields (attacker delegate, close_authority, delegated_amount > 0, state == 2)
    MUST be refused by sim_guard section E with 'created account' in the error.

    Mutation test: Restricting the loop back to wallet_owned_pre_accounts allows these attacks to pass.
    """
    payer = Keypair()
    payer_pk = str(payer.pubkey())
    attacker = str(Keypair().pubkey())

    quote = _make_dummy_quote()
    intent = intent_from_quote_for_tests(quote, payer_pk)

    msg = MessageV0.try_compile(payer.pubkey(), [], [], Hash.default())
    tx = VersionedTransaction(msg, [payer])
    tx_b64 = base64.b64encode(bytes(tx)).decode()

    # Pre-state: wallet has SOL, no token account
    async def fake_get_multi(addrs, endpoints=None, side="buy", wallet_owned_addresses=None):
        res = []
        for a in addrs:
            if a == payer_pk:
                res.append({"lamports": 1_000_000_000, "owner": str(SYSTEM_PROGRAM_ID), "data": ["", "base64"]})
            else:
                res.append(None)
        return res

    # Set up fields
    del_val = attacker if corrupt_field == "delegate" else None
    close_val = attacker if corrupt_field == "close_authority" else None
    del_amt = corrupt_val if corrupt_field == "delegated_amount" else 0
    state_val = corrupt_val if corrupt_field == "state" else 1

    async def fake_simulate(b64, addrs, endpoints=None, side="buy", wallet_owned_addresses=None):
        posts = []
        for a in addrs:
            if a == payer_pk:
                posts.append({"lamports": 999_990_000, "owner": str(SYSTEM_PROGRAM_ID), "data": ["", "base64"]})
            else:
                tok_data = encode_token_account_data(
                    mint=MINT,
                    owner=payer_pk,
                    amount=1_000_000,
                    delegate=del_val,
                    state=state_val,
                    delegated_amount=del_amt,
                    close_authority=close_val,
                )
                posts.append({
                    "lamports": 2_039_280,
                    "owner": _SPL_TOKEN_PROGRAM_ID,
                    "data": [base64.b64encode(tok_data).decode(), "base64"],
                })
        return {"err": None, "logs": [], "post_accounts": posts}

    from live_execution import solana
    monkeypatch.setattr(solana, "get_multiple_accounts", fake_get_multi)
    monkeypatch.setattr(solana, "simulate_transaction", fake_simulate)

    with pytest.raises(SimulationRefused) as excinfo:
        await simulate_and_assert(tx_b64, quote, payer_pk, intent=intent)

    msg = str(excinfo.value).lower()
    assert expected_marker in msg
    assert "created account" in msg
    assert excinfo.value.category == "guard_refused"


@pytest.mark.asyncio
async def test_c1_new_ata_control_field_clean_passes(monkeypatch):
    """Control test: new ATA with all control fields clear passes simulation."""
    payer = Keypair()
    payer_pk = str(payer.pubkey())

    quote = _make_dummy_quote()
    intent = intent_from_quote_for_tests(quote, payer_pk)

    msg = MessageV0.try_compile(payer.pubkey(), [], [], Hash.default())
    tx = VersionedTransaction(msg, [payer])
    tx_b64 = base64.b64encode(bytes(tx)).decode()

    async def fake_get_multi(addrs, endpoints=None, side="buy", wallet_owned_addresses=None):
        return [
            {"lamports": 1_000_000_000, "owner": str(SYSTEM_PROGRAM_ID), "data": ["", "base64"]}
            if a == payer_pk else None
            for a in addrs
        ]

    async def fake_simulate(b64, addrs, endpoints=None, side="buy", wallet_owned_addresses=None):
        posts = []
        for a in addrs:
            if a == payer_pk:
                posts.append({"lamports": 999_990_000, "owner": str(SYSTEM_PROGRAM_ID), "data": ["", "base64"]})
            else:
                tok_data = encode_token_account_data(
                    mint=MINT,
                    owner=payer_pk,
                    amount=1_000_000,
                    delegate=None,
                    state=1,
                    delegated_amount=0,
                    close_authority=None,
                )
                posts.append({
                    "lamports": 2_039_280,
                    "owner": _SPL_TOKEN_PROGRAM_ID,
                    "data": [base64.b64encode(tok_data).decode(), "base64"],
                })
        return {"err": None, "logs": [], "post_accounts": posts}

    from live_execution import solana
    monkeypatch.setattr(solana, "get_multiple_accounts", fake_get_multi)
    monkeypatch.setattr(solana, "simulate_transaction", fake_simulate)

    res = await simulate_and_assert(tx_b64, quote, payer_pk, intent=intent)
    assert res["status"] == "passed"


# ---------------------------------------------------------------------------
# C-2: Quorum token account comparison in responses_agree
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "field,val_a,val_b",
    [
        ("delegate", "del_a", "del_b"),
        ("close_authority", "auth_a", "auth_b"),
        ("delegated_amount", 10, 50),
        ("state", 1, 2),
    ],
)
def test_c2_responses_agree_control_fields_differ(field, val_a, val_b):
    """
    C-2 [READ]: responses_agree must compare exact delegate, close_authority,
    delegated_amount, and state. If endpoints differ on any of these, responses_agree must return False.
    Identical responses must return True.

    Mutation test: removing any of the checks in solana.py:responses_agree causes this test to fail.
    """
    mint = str(Keypair().pubkey())
    owner = str(Keypair().pubkey())
    payer = str(Keypair().pubkey())
    tok_addr = "token_account_address"

    kwargs_a = {"mint": mint, "owner": owner, "amount": 100}
    kwargs_b = {"mint": mint, "owner": owner, "amount": 100}

    kwargs_a[field] = str(Keypair().pubkey()) if "auth" in str(val_a) or "del_a" in str(val_a) else val_a
    kwargs_b[field] = str(Keypair().pubkey()) if "auth" in str(val_b) or "del_b" in str(val_b) else val_b

    data_a = encode_token_account_data(**kwargs_a)
    data_b = encode_token_account_data(**kwargs_b)

    payer_acc = {"owner": str(SYSTEM_PROGRAM_ID), "lamports": 1_000_000_000, "data": ["", "base64"]}
    acc_a = {"owner": _SPL_TOKEN_PROGRAM_ID, "lamports": 2_000_000, "data": [base64.b64encode(data_a).decode(), "base64"]}
    acc_b = {"owner": _SPL_TOKEN_PROGRAM_ID, "lamports": 2_000_000, "data": [base64.b64encode(data_b).decode(), "base64"]}

    res_a = {"err": None, "post_accounts": [payer_acc, acc_a]}
    res_b = {"err": None, "post_accounts": [payer_acc, acc_b]}

    wallet_owned = [payer, tok_addr]
    all_addrs = [payer, tok_addr]

    # Differing field must NOT agree
    assert responses_agree(res_a, res_b, wallet_owned_addrs=wallet_owned, all_addresses=all_addrs) is False
    # Identical responses MUST agree
    assert responses_agree(res_a, res_a, wallet_owned_addrs=wallet_owned, all_addresses=all_addrs) is True


# ---------------------------------------------------------------------------
# C-3: No double-counting on quote mismatch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_c3_quote_mismatch_single_refusal_counter_and_alert(monkeypatch, tmp_path):
    """
    C-3 [READ]: validate_quote_against_intent records safety refusal once and raises QuoteMismatch.
    simulate_and_assert catches QuoteMismatch and calls _refuse(..., count=False),
    so the safety counter increases by EXACTLY 1 and exactly one alert is fired.
    """
    payer = str(Keypair().pubkey())
    quote = _make_dummy_quote(out_amount=1_000_000)
    lying_quote = dict(quote, inAmount="999999")
    intent = intent_from_quote_for_tests(quote, payer)

    refusal_records = []
    alert_records = []

    def fake_record(category, reason):
        refusal_records.append((category, reason))

    def fake_alert(ev, msg, key=None, **kw):
        alert_records.append((ev, msg))

    from live_execution import safety_counters
    import alerts
    monkeypatch.setattr(safety_counters, "record_safety_refusal", fake_record)
    monkeypatch.setattr(alerts, "alert_nowait", fake_alert)

    with pytest.raises(SimulationRefused) as excinfo:
        await simulate_and_assert("dummy_b64", lying_quote, payer, intent=intent)

    assert excinfo.value.category == "quote_mismatch"
    # Recorded EXACTLY once
    assert len(refusal_records) == 1, f"Expected 1 refusal recorded, got {refusal_records}"
    assert refusal_records[0][0] == "quote_mismatch"
    # Alert emitted EXACTLY once
    assert len(alert_records) == 1, f"Expected 1 alert fired, got {alert_records}"


# ---------------------------------------------------------------------------
# C-4: simulate_and_assert requires intent (and AST check)
# ---------------------------------------------------------------------------

def test_c4_simulate_and_assert_signature_requires_intent():
    """
    C-4 [READ]: simulate_and_assert must require intent (no default None).
    """
    sig = inspect.signature(simulate_and_assert)
    param = sig.parameters.get("intent")
    assert param is not None, "intent parameter must exist"
    assert param.default is inspect.Parameter.empty, \
        "intent must be a required parameter without default = None"


@pytest.mark.asyncio
async def test_c4_calling_simulate_and_assert_without_intent_raises_type_error():
    """Calling simulate_and_assert without intent raises TypeError."""
    with pytest.raises(TypeError):
        await simulate_and_assert("b64", {}, "payer")  # Missing required argument 'intent'


def test_c4_ast_production_code_never_imports_helpers():
    """
    C-4: AST check ensuring production modules under live_execution/
    never import _helpers (test helper isolation).
    """
    repo_root = Path(__file__).resolve().parent.parent
    live_exec_dir = repo_root / "live_execution"

    for py_file in live_exec_dir.glob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "_helpers" not in alias.name and "tests.helpers" not in alias.name, \
                        f"{py_file.name} illegally imports {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert "_helpers" not in mod and "tests.helpers" not in mod, \
                    f"{py_file.name} illegally imports from {mod}"


# ---------------------------------------------------------------------------
# C-5: usd_to_raw_amount consistent rounding & cent property test
# ---------------------------------------------------------------------------

def test_c5_usd_to_raw_amount_cent_property_test():
    """
    C-5 [READ]: Property test over every cent value 0.01..100.00:
    usd_to_raw_amount produces the exact integer equal to Decimal(str(x)) * 10**6.
    """
    for cents in range(1, 10001):
        usd = cents / 100.0
        expected = int(Decimal(str(usd)) * Decimal("1000000"))
        actual = usd_to_raw_amount(usd, 6)
        assert actual == expected, f"Discrepancy at usd={usd}: got {actual} != expected {expected}"


# ---------------------------------------------------------------------------
# C-6: Reference price check runs BEFORE memo
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_c6_reference_price_check_before_memo_in_place_buy(monkeypatch, tmp_path):
    """
    C-6 [READ]: In place_buy, missing or invalid reference_price_usd
    must block the order BEFORE publish_commit_memo is called and without
    creating a commit log entry.
    """
    from live_execution import executor as ex
    from live_execution import config as le_cfg

    monkeypatch.setattr(le_cfg, "LIVE_TRADING_ENABLED", True)
    monkeypatch.setattr(le_cfg, "REQUIRE_MANUAL_CONFIRMATION", False)
    monkeypatch.setattr(le_cfg, "STATE_DIR", tmp_path)

    class _Payer:
        pass
    monkeypatch.setattr(ex.wallet, "load_keypair", lambda: _Payer())
    monkeypatch.setattr(ex.wallet, "verify_expected_address", lambda p: None)
    monkeypatch.setattr(ex.wallet, "pubkey_string", lambda p: "GsbwXfJraMomNxBcpR3DBDr4GEEr2kPpGEe1c1FQFPB1")

    async def fake_sol(addr):
        return 1.0
    async def fake_usdc(addr):
        return 100.0

    monkeypatch.setattr(ex.solana, "get_sol_balance", fake_sol)
    monkeypatch.setattr(ex.solana, "get_usdc_balance", fake_usdc)

    memo_called = []
    async def fake_publish_memo(payer, h, endpoints=None):
        memo_called.append(h)
        return {"signature": "MEMO", "slot": 1}
    monkeypatch.setattr(ex.memo, "publish_commit_memo", fake_publish_memo)

    # Call place_buy without reference_price_usd
    res = await ex.place_buy(
        mint=MINT,
        symbol="TEST",
        usd=10.0,
        output_decimals=6,
        idempotency_key="k1",
        reference_price_usd=None,  # Missing reference price!
    )

    assert res.status == "blocked"
    assert "reference price" in res.reason.lower()
    assert not memo_called, "publish_commit_memo must NOT be called when reference price is missing"
    assert not res.commit_hash, "commit hash must not be generated when blocked before memo"


# ---------------------------------------------------------------------------
# C-7: Priority fee limit calculation scales with non-ComputeBudget instructions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "num_dex_ixs,unit_price,should_pass",
    [
        (1, 2_000_000, True),   # 1 non-CB ix * 200k CU = 200,000 CU. Fee = 200,000 * 2 = 400,000 lamports (<= 1M hard cap) -> PASS
        (3, 2_000_000, False),  # 3 non-CB ix * 200k CU = 600,000 CU. Fee = 600,000 * 2 = 1,200,000 lamports (> 1M hard cap) -> REFUSED
        (9, 1_000_000, False),  # min(9 * 200k, 1.4M) = 1,400,000 CU. Fee = 1,400,000 * 1 = 1,400,000 lamports (> 1M hard cap) -> REFUSED
    ],
)
def test_c7_priority_fee_scales_by_instruction_count(num_dex_ixs, unit_price, should_pass):
    """
    C-7 [READ]: When SetComputeUnitLimit is omitted, effective CU limit is
    min(200_000 * n_non_cb_instructions, 1_400_000).
    """
    import struct

    payer = Keypair()
    payer_pk = payer.pubkey()
    prog_id = Pubkey.from_string("JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4")
    cb_pid = Pubkey.from_string(_COMPUTE_BUDGET_PROGRAM_ID)

    dex_ixs = [
        Instruction(prog_id, b"\x00" * 8, [AccountMeta(payer_pk, True, True)])
        for _ in range(num_dex_ixs)
    ]
    cb_ix = Instruction(cb_pid, b"\x03" + struct.pack("<Q", unit_price), [])

    all_ixs = dex_ixs + [cb_ix]
    msg = MessageV0.try_compile(payer_pk, all_ixs, [], Hash.default())
    tx = VersionedTransaction(msg, [payer])

    if should_pass:
        # Should not raise ExecutionError on fee cap
        inspect_swap_transaction(tx, str(payer_pk))
    else:
        with pytest.raises(ExecutionError) as excinfo:
            inspect_swap_transaction(tx, str(payer_pk))
        assert "exceeds priority_fee_hard_cap_lamports" in str(excinfo.value).lower()

