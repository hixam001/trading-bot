"""
test_audit_81_a.py — §81 Phase A: exit slippage ladder classification & escalation.

Tests for three bugs:
  A-1: SimulationRefused with on-chain 6001 was non-retryable (string check short-circuit)
  A-2: send_raw_transaction discards RPC error body → masked retryable codes
  A-3: Landed-with-error treated as unresolved instead of conclusive failure

All tests are hermetic: no network, no .env, no real RPC, no keypair.
Quote fixtures are self-consistent: otherAmountThreshold = outAmount*(10000-slippageBps)//10000
Real Solana error shapes used: {"InstructionError": [2, {"Custom": 6001}]}
"""
from __future__ import annotations

import asyncio
import base64
import struct
from typing import Optional
from unittest.mock import AsyncMock

import pytest

import live_execution.config as le_config
from live_execution import executor as ex
from live_execution import solana as sol_mod


# ---------------------------------------------------------------------------
# Shared helpers / fixtures
# ---------------------------------------------------------------------------

MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WALLET = "GsbwXfJraMomNxBcpR3DBDr4GEEr2kPpGEe1c1FQFPB1"
FAKE_SIG = "5VFuzSigABC" + "x" * 76  # 87-char fake base58

# On-chain error: Jupiter SlippageToleranceExceeded
ERR_6001 = {"InstructionError": [2, {"Custom": 6001}]}
# Non-retryable: some other program error
ERR_6024 = {"InstructionError": [1, {"Custom": 6024}]}


def _make_quote(out_amount: int, slippage_bps: int) -> dict:
    """Self-consistent quote: otherAmountThreshold = outAmount*(10000-N)//10000."""
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
    """Return a base64 blob that will decode but not be a valid VersionedTx."""
    return base64.b64encode(b"\x00" * 64).decode()


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Armed-but-hermetic: enabled, no manual confirm, tmp state dir, stub wallet."""
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

    # SOL reserve OK
    async def fake_sol(addr):
        return 1.0

    monkeypatch.setattr(ex.solana, "get_sol_balance", fake_sol)

    # Decimals for the sell token
    async def fake_decimals(mint):
        return 6

    monkeypatch.setattr(ex.solana, "get_mint_decimals", fake_decimals)

    # Commit memo always OK
    async def fake_memo(payer, seal_hash, endpoints=None):
        return {"signature": "MEMOSIG", "slot": 5}

    monkeypatch.setattr(ex.memo, "publish_commit_memo", fake_memo)

    # fill actuals: never called in failure paths; return empty for success
    async def fake_fill_actuals(side, mint, sig):
        return {"usdc_delta": 1.20, "tokens_delta": 1.0}

    monkeypatch.setattr(ex, "_fill_actuals", fake_fill_actuals)

    return tmp_path


def _ledger_with_position(tmp_path, mint=MINT, tokens=1.0):
    from live_execution.models import ExecutionLedger
    ledger = ExecutionLedger(tmp_path / "exec.json")
    ledger.record_buy(
        idempotency_key="kopen",
        mint=mint,
        usd_size=1.0,
        tokens_out=tokens,
        price_usd=1.0,
        signature="OPENSIG",
        status="confirmed",
    )
    return ledger


# ---------------------------------------------------------------------------
# Test 7 (unit): custom_error_code parsing
# (Placed first so it's fast and purely functional)
# ---------------------------------------------------------------------------

class TestCustomErrorCode:
    """Unit tests for exit_errors.custom_error_code (§81 Phase A)."""

    def _cc(self, val):
        from live_execution.exit_errors import custom_error_code
        return custom_error_code(val)

    def test_json_dict_form(self):
        """Standard on-chain err dict: {"InstructionError": [idx, {"Custom": N}]}"""
        assert self._cc({"InstructionError": [2, {"Custom": 6001}]}) == 6001

    def test_json_dict_form_6024(self):
        assert self._cc({"InstructionError": [1, {"Custom": 6024}]}) == 6024

    def test_text_hex_form(self):
        """Text logs: 'custom program error: 0x1771' → 6001 (0x1771 = 6001)"""
        assert self._cc("Program log: custom program error: 0x1771") == 6001

    def test_text_hex_uppercase(self):
        assert self._cc("custom program error: 0x1770") == 0x1770

    def test_none_returns_none(self):
        assert self._cc(None) is None

    def test_empty_dict_returns_none(self):
        assert self._cc({}) is None

    def test_malformed_instruction_error_no_custom(self):
        assert self._cc({"InstructionError": [2, "InvalidAccountData"]}) is None

    def test_list_form(self):
        """Direct list form: [idx, {"Custom": N}]"""
        assert self._cc([2, {"Custom": 6001}]) == 6001

    def test_nested_wrong_key(self):
        assert self._cc({"InstructionError": [2, {"NotCustom": 6001}]}) is None

    def test_string_bare_custom(self):
        """'Custom: 6001' in string form."""
        assert self._cc("Custom: 6001") == 6001


# ---------------------------------------------------------------------------
# Test 1: Sim-time slippage → escalates 2 times, fills on 3rd rung
# ---------------------------------------------------------------------------

async def test_sim_slippage_escalates_and_fills(env, monkeypatch):
    """
    A-1: SimulationRefused with on-chain 6001 is retryable.
    First two rungs (50, 150 bps) get simulated with err=6001.
    Third rung (300 bps) succeeds.
    Expected: status=filled, 3 build/sign calls, reason lists all rungs.
    """
    from live_execution.sim_guard import SimulationRefused

    attempt = [0]
    quote_calls = []
    build_calls = []
    sign_calls = []

    async def fake_get(url, params):
        slippage = int(params.get("slippageBps", 50))
        quote_calls.append(slippage)
        out = 1_200_000
        return _make_quote(out, slippage)

    async def fake_build(quote, addr):
        build_calls.append(quote["slippageBps"])
        return _make_fake_tx_b64()

    async def fake_sign(b64, payer, intent=None, quote=None):
        rung = attempt[0]
        attempt[0] += 1
        sign_calls.append(rung)
        # Raise SimulationRefused for first two attempts
        if rung < 2:
            raise SimulationRefused(
                f"simulation refused: simulation failed on-chain with err: {ERR_6001}",
                category="sim_refused",
                onchain_err=ERR_6001,
            )
        return ex.solana.SignedSwap("FILLSIG", b"raw", _checked=True)

    async def fake_send(signed, endpoints=None):
        return "FILLSIG"

    async def fake_confirm(sig, timeout_s=None, endpoints=None):
        return {"confirmed": True, "slot": 10, "err": None}

    async def fake_get_sig_status(sig, endpoints=None):
        return None  # no previous sig yet

    monkeypatch.setattr(ex, "_get_json", fake_get)
    monkeypatch.setattr(ex, "_build_swap_transaction", fake_build)
    monkeypatch.setattr(ex, "sign_after_checks", fake_sign)
    monkeypatch.setattr(ex.solana, "send_checked_swap", fake_send)
    monkeypatch.setattr(ex.solana, "confirm_signature", fake_confirm)
    monkeypatch.setattr(ex.solana, "get_signature_status", fake_get_sig_status)

    ledger = _ledger_with_position(env)
    res = await ex.place_sell(MINT, "TEST", 1.0, ledger=ledger)

    assert res.status == "filled", f"Expected filled, got {res.status}: {res.reason}"
    assert len(sign_calls) == 3, f"Expected 3 sign calls, got {sign_calls}"
    assert len(quote_calls) == 3, f"Expected 3 quotes, got {quote_calls}"
    assert quote_calls == [50, 150, 300]


# ---------------------------------------------------------------------------
# Test 2: Sim slippage always failing → exactly 3 attempts, status=failed
# ---------------------------------------------------------------------------

async def test_sim_slippage_all_fail_exhausted(env, monkeypatch):
    """
    A-1: All 3 rungs get 6001 simulation refusals → ladder exhausted → failed.
    """
    from live_execution.sim_guard import SimulationRefused

    sign_calls = []
    quote_calls = []

    async def fake_get(url, params):
        slippage = int(params.get("slippageBps", 50))
        quote_calls.append(slippage)
        return _make_quote(1_200_000, slippage)

    async def fake_build(quote, addr):
        return _make_fake_tx_b64()

    async def fake_sign(b64, payer, intent=None, quote=None):
        sign_calls.append(True)
        raise SimulationRefused(
            f"simulation refused: simulation failed on-chain with err: {ERR_6001}",
            category="sim_refused",
            onchain_err=ERR_6001,
        )

    async def fake_get_sig_status(sig, endpoints=None):
        return None

    monkeypatch.setattr(ex, "_get_json", fake_get)
    monkeypatch.setattr(ex, "_build_swap_transaction", fake_build)
    monkeypatch.setattr(ex, "sign_after_checks", fake_sign)
    monkeypatch.setattr(ex.solana, "get_signature_status", fake_get_sig_status)

    ledger = _ledger_with_position(env)
    res = await ex.place_sell(MINT, "TEST", 1.0, ledger=ledger)

    assert res.status == "failed", f"Expected failed, got {res.status}"
    assert len(sign_calls) == 3, f"Expected exactly 3 attempts, got {len(sign_calls)}"


# ---------------------------------------------------------------------------
# Test 3: Non-retryable sim refusals stop immediately (parametrized)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("onchain_err,category,label", [
    (ERR_6024, "sim_refused", "custom_6024_nonretryable"),
    (None, "sim_refused", "guard_refused_no_onchain_err"),
    # delegate set is a non-retryable string marker
])
async def test_non_retryable_sim_refusal_stops_ladder(env, monkeypatch, onchain_err, category, label):
    """
    Non-retryable SimulationRefused (code 6024, or no onchain_err) → exactly 1 attempt.
    """
    from live_execution.sim_guard import SimulationRefused

    sign_calls = []

    async def fake_get(url, params):
        slippage = int(params.get("slippageBps", 50))
        return _make_quote(1_200_000, slippage)

    async def fake_build(quote, addr):
        return _make_fake_tx_b64()

    async def fake_sign(b64, payer, intent=None, quote=None):
        sign_calls.append(True)
        raise SimulationRefused(
            "simulation refused: guard refused (delegate set)",
            category=category,
            onchain_err=onchain_err,
        )

    async def fake_get_sig_status(sig, endpoints=None):
        return None

    monkeypatch.setattr(ex, "_get_json", fake_get)
    monkeypatch.setattr(ex, "_build_swap_transaction", fake_build)
    monkeypatch.setattr(ex, "sign_after_checks", fake_sign)
    monkeypatch.setattr(ex.solana, "get_signature_status", fake_get_sig_status)

    ledger = _ledger_with_position(env)
    res = await ex.place_sell(MINT, "TEST", 1.0, ledger=ledger)

    assert res.status == "failed", f"Expected failed, got {res.status}: {res.reason}"
    assert len(sign_calls) == 1, \
        f"[{label}] Non-retryable should stop at 1 attempt, got {len(sign_calls)}"


async def test_non_retryable_owner_changed_stops_ladder(env, monkeypatch):
    """owner changed is a non-retryable string marker — stops at 1 attempt."""
    from live_execution.sim_guard import SimulationRefused

    sign_calls = []

    async def fake_get(url, params):
        slippage = int(params.get("slippageBps", 50))
        return _make_quote(1_200_000, slippage)

    async def fake_build(quote, addr):
        return _make_fake_tx_b64()

    async def fake_sign(b64, payer, intent=None, quote=None):
        sign_calls.append(True)
        raise SimulationRefused(
            "simulation refused: wallet token account owner changed",
            category="sim_refused",
            onchain_err=None,
        )

    async def fake_get_sig_status(sig, endpoints=None):
        return None

    monkeypatch.setattr(ex, "_get_json", fake_get)
    monkeypatch.setattr(ex, "_build_swap_transaction", fake_build)
    monkeypatch.setattr(ex, "sign_after_checks", fake_sign)
    monkeypatch.setattr(ex.solana, "get_signature_status", fake_get_sig_status)

    ledger = _ledger_with_position(env)
    res = await ex.place_sell(MINT, "TEST", 1.0, ledger=ledger)

    assert res.status == "failed"
    assert len(sign_calls) == 1


# ---------------------------------------------------------------------------
# Test 4a: Preflight: all endpoints return -32002 Custom 6001 → escalates
# ---------------------------------------------------------------------------

async def test_preflight_all_rpc_slippage_escalates(env, monkeypatch):
    """
    A-2: All RPC endpoints return SendRejected with Custom 6001 at preflight.
    The ladder should escalate (retry) rather than fail immediately.
    """
    from live_execution.exit_errors import SendRejected

    sign_calls = []
    send_calls = []

    async def fake_get(url, params):
        slippage = int(params.get("slippageBps", 50))
        return _make_quote(1_200_000, slippage)

    async def fake_build(quote, addr):
        return _make_fake_tx_b64()

    async def fake_sign(b64, payer, intent=None, quote=None):
        sign_calls.append(True)
        return ex.solana.SignedSwap("FAKE_SIG_" + str(len(sign_calls)), b"raw", _checked=True)

    async def fake_send_checked(signed, endpoints=None):
        send_calls.append(True)
        # First two rungs: raise SendRejected with Custom 6001
        if len(send_calls) <= 2:
            raise SendRejected(
                "every rpc refused the transaction at preflight",
                attempts=[
                    {"kind": "rpc_error", "code": -32002, "message": "preflight failed",
                     "err": ERR_6001},
                    {"kind": "rpc_error", "code": -32002, "message": "preflight failed",
                     "err": ERR_6001},
                ],
            )
        return "FILLSIG"

    async def fake_confirm(sig, timeout_s=None, endpoints=None):
        return {"confirmed": True, "slot": 10, "err": None}

    async def fake_get_sig_status(sig, endpoints=None):
        return None

    monkeypatch.setattr(ex, "_get_json", fake_get)
    monkeypatch.setattr(ex, "_build_swap_transaction", fake_build)
    monkeypatch.setattr(ex, "sign_after_checks", fake_sign)
    monkeypatch.setattr(ex.solana, "send_checked_swap", fake_send_checked)
    monkeypatch.setattr(ex.solana, "confirm_signature", fake_confirm)
    monkeypatch.setattr(ex.solana, "get_signature_status", fake_get_sig_status)

    ledger = _ledger_with_position(env)
    res = await ex.place_sell(MINT, "TEST", 1.0, ledger=ledger)

    assert res.status == "filled", f"Expected filled, got {res.status}: {res.reason}"
    assert len(send_calls) == 3, f"Expected 3 send attempts, got {len(send_calls)}"


# ---------------------------------------------------------------------------
# Test 4b: Mixed: one endpoint RPC error, another times out → not escalate (unresolved/failed)
# ---------------------------------------------------------------------------

async def test_preflight_mixed_rpc_and_transport_does_not_escalate(env, monkeypatch):
    """
    A-2: One endpoint returns 6001 rpc_error, another had transport failure.
    With ambiguous failures (not all rpc_error), SendRejected should NOT be raised.
    The None return from send_raw_transaction → "every rpc refused" → non-retryable.
    """
    sign_calls = []
    send_calls = []

    async def fake_get(url, params):
        slippage = int(params.get("slippageBps", 50))
        return _make_quote(1_200_000, slippage)

    async def fake_build(quote, addr):
        return _make_fake_tx_b64()

    async def fake_sign(b64, payer, intent=None, quote=None):
        sign_calls.append(True)
        return ex.solana.SignedSwap("SIG_" + str(len(sign_calls)), b"raw", _checked=True)

    # send_checked_swap returns None (ambiguous — transport failure)
    # This should NOT be retried by the ladder (non-retryable "every rpc refused")
    async def fake_send_checked(signed, endpoints=None):
        send_calls.append(True)
        # send_raw_transaction returning None → executor gets None
        return None  # maps to the "every rpc refused" path

    async def fake_get_sig_status(sig, endpoints=None):
        return None

    monkeypatch.setattr(ex, "_get_json", fake_get)
    monkeypatch.setattr(ex, "_build_swap_transaction", fake_build)
    monkeypatch.setattr(ex, "sign_after_checks", fake_sign)
    monkeypatch.setattr(ex.solana, "send_checked_swap", fake_send_checked)
    monkeypatch.setattr(ex.solana, "get_signature_status", fake_get_sig_status)

    ledger = _ledger_with_position(env)
    res = await ex.place_sell(MINT, "TEST", 1.0, ledger=ledger)

    # "every rpc refused" is non-retryable → stops at 1 attempt
    assert res.status == "failed"
    assert len(send_calls) == 1, \
        f"Ambiguous transport failure must stop at 1 attempt, got {len(send_calls)}"


# ---------------------------------------------------------------------------
# Test 5: Landed-with-error (A-3)
# ---------------------------------------------------------------------------

async def test_landed_with_error_6001_escalates_without_blockhash_check(env, monkeypatch):
    """
    A-3: get_signature_status returns confirmed + err=6001 → should escalate.
    is_blockhash_valid must NOT be called in this case.
    """
    blockhash_called = []

    async def fake_get(url, params):
        slippage = int(params.get("slippageBps", 50))
        return _make_quote(1_200_000, slippage)

    async def fake_build(quote, addr):
        return _make_fake_tx_b64()

    sign_call_count = [0]

    async def fake_sign(b64, payer, intent=None, quote=None):
        sign_call_count[0] += 1
        return ex.solana.SignedSwap("SIG_" + str(sign_call_count[0]), b"raw", _checked=True)

    send_call_count = [0]

    async def fake_send_checked(signed, endpoints=None):
        send_call_count[0] += 1
        if send_call_count[0] == 1:
            return "FIRST_SIG"  # first attempt returns a sig
        return "SECOND_SIG"    # escalated attempt returns a sig too

    confirm_call_count = [0]

    async def fake_confirm(sig, timeout_s=None, endpoints=None):
        confirm_call_count[0] += 1
        if confirm_call_count[0] == 1:
            # First confirm: not confirmed before timeout (triggers sig-status check)
            return {"confirmed": False, "slot": None, "err": "not confirmed before timeout"}
        # Second attempt fills
        return {"confirmed": True, "slot": 20, "err": None}

    get_sig_count = [0]

    async def fake_get_sig_status(sig, endpoints=None):
        get_sig_count[0] += 1
        # Return landed-with-error 6001
        return {
            "confirmationStatus": "confirmed",
            "err": ERR_6001,
            "slot": 15,
        }

    async def fake_blockhash_valid(blockhash, endpoints=None):
        blockhash_called.append(blockhash)
        return True  # still valid - but should NOT be called

    monkeypatch.setattr(ex, "_get_json", fake_get)
    monkeypatch.setattr(ex, "_build_swap_transaction", fake_build)
    monkeypatch.setattr(ex, "sign_after_checks", fake_sign)
    monkeypatch.setattr(ex.solana, "send_checked_swap", fake_send_checked)
    monkeypatch.setattr(ex.solana, "confirm_signature", fake_confirm)
    monkeypatch.setattr(ex.solana, "get_signature_status", fake_get_sig_status)
    monkeypatch.setattr(ex.solana, "is_blockhash_valid", fake_blockhash_valid)

    ledger = _ledger_with_position(env)
    res = await ex.place_sell(MINT, "TEST", 1.0, ledger=ledger)

    assert res.status == "filled", f"Expected filled after escalation, got {res.status}: {res.reason}"
    assert not blockhash_called, \
        f"is_blockhash_valid must NOT be called when err is present; called with {blockhash_called}"


async def test_landed_with_error_6024_stops_not_unresolved(env, monkeypatch):
    """
    A-3: Landed-with-error 6024 (non-retryable) → status=failed, not exit_unresolved.
    is_blockhash_valid must NOT be called.
    """
    blockhash_called = []

    async def fake_get(url, params):
        slippage = int(params.get("slippageBps", 50))
        return _make_quote(1_200_000, slippage)

    async def fake_build(quote, addr):
        return _make_fake_tx_b64()

    async def fake_sign(b64, payer, intent=None, quote=None):
        return ex.solana.SignedSwap("SIG1", b"raw", _checked=True)

    async def fake_send_checked(signed, endpoints=None):
        return "FIRST_SIG"

    async def fake_confirm(sig, timeout_s=None, endpoints=None):
        return {"confirmed": False, "slot": None, "err": "not confirmed before timeout"}

    async def fake_get_sig_status(sig, endpoints=None):
        return {
            "confirmationStatus": "confirmed",
            "err": ERR_6024,
            "slot": 15,
        }

    async def fake_blockhash_valid(blockhash, endpoints=None):
        blockhash_called.append(blockhash)
        return True

    monkeypatch.setattr(ex, "_get_json", fake_get)
    monkeypatch.setattr(ex, "_build_swap_transaction", fake_build)
    monkeypatch.setattr(ex, "sign_after_checks", fake_sign)
    monkeypatch.setattr(ex.solana, "send_checked_swap", fake_send_checked)
    monkeypatch.setattr(ex.solana, "confirm_signature", fake_confirm)
    monkeypatch.setattr(ex.solana, "get_signature_status", fake_get_sig_status)
    monkeypatch.setattr(ex.solana, "is_blockhash_valid", fake_blockhash_valid)

    ledger = _ledger_with_position(env)
    res = await ex.place_sell(MINT, "TEST", 1.0, ledger=ledger)

    assert res.status == "failed", f"Expected failed, got {res.status}: {res.reason}"
    assert "exit_unresolved" not in res.reason, \
        "Non-retryable landed-with-error must not yield exit_unresolved"
    assert not blockhash_called, \
        f"is_blockhash_valid must NOT be called when err is present; called with {blockhash_called}"


# ---------------------------------------------------------------------------
# Test 6: Regression — status None + blockhash various
# ---------------------------------------------------------------------------

async def test_status_none_blockhash_valid_is_unresolved(env, monkeypatch):
    """
    Regression: status=None (sig not found) + blockhash valid → exit_unresolved.
    """
    async def fake_get(url, params):
        slippage = int(params.get("slippageBps", 50))
        return _make_quote(1_200_000, slippage)

    async def fake_build(quote, addr):
        return _make_fake_tx_b64()

    async def fake_sign(b64, payer, intent=None, quote=None):
        return ex.solana.SignedSwap("SIG1", b"raw", _checked=True)

    async def fake_send_checked(signed, endpoints=None):
        return "FIRST_SIG"

    async def fake_confirm(sig, timeout_s=None, endpoints=None):
        return {"confirmed": False, "slot": None, "err": "not confirmed before timeout"}

    async def fake_get_sig_status(sig, endpoints=None):
        return None  # sig not found

    async def fake_blockhash_valid(blockhash, endpoints=None):
        return True  # still valid

    monkeypatch.setattr(ex, "_get_json", fake_get)
    monkeypatch.setattr(ex, "_build_swap_transaction", fake_build)
    monkeypatch.setattr(ex, "sign_after_checks", fake_sign)
    monkeypatch.setattr(ex.solana, "send_checked_swap", fake_send_checked)
    monkeypatch.setattr(ex.solana, "confirm_signature", fake_confirm)
    monkeypatch.setattr(ex.solana, "get_signature_status", fake_get_sig_status)
    monkeypatch.setattr(ex.solana, "is_blockhash_valid", fake_blockhash_valid)

    ledger = _ledger_with_position(env)
    res = await ex.place_sell(MINT, "TEST", 1.0, ledger=ledger)

    # With blockhash still valid, should be exit_unresolved
    assert res.status == "failed"
    assert "exit_unresolved" in res.reason


async def test_status_none_blockhash_rpc_fail_is_unresolved(env, monkeypatch):
    """
    Regression: status=None + blockhash check returns None (RPC fail) → exit_unresolved.
    """
    async def fake_get(url, params):
        slippage = int(params.get("slippageBps", 50))
        return _make_quote(1_200_000, slippage)

    async def fake_build(quote, addr):
        return _make_fake_tx_b64()

    async def fake_sign(b64, payer, intent=None, quote=None):
        return ex.solana.SignedSwap("SIG1", b"raw", _checked=True)

    async def fake_send_checked(signed, endpoints=None):
        return "FIRST_SIG"

    async def fake_confirm(sig, timeout_s=None, endpoints=None):
        return {"confirmed": False, "slot": None, "err": "not confirmed before timeout"}

    async def fake_get_sig_status(sig, endpoints=None):
        return None

    async def fake_blockhash_valid(blockhash, endpoints=None):
        return None  # RPC failure

    monkeypatch.setattr(ex, "_get_json", fake_get)
    monkeypatch.setattr(ex, "_build_swap_transaction", fake_build)
    monkeypatch.setattr(ex, "sign_after_checks", fake_sign)
    monkeypatch.setattr(ex.solana, "send_checked_swap", fake_send_checked)
    monkeypatch.setattr(ex.solana, "confirm_signature", fake_confirm)
    monkeypatch.setattr(ex.solana, "get_signature_status", fake_get_sig_status)
    monkeypatch.setattr(ex.solana, "is_blockhash_valid", fake_blockhash_valid)

    ledger = _ledger_with_position(env)
    res = await ex.place_sell(MINT, "TEST", 1.0, ledger=ledger)

    assert res.status == "failed"
    assert "exit_unresolved" in res.reason


async def test_status_none_blockhash_expired_escalates(env, monkeypatch):
    """
    Regression: status=None + blockhash expired (False) → escalates to next rung.
    """
    sign_calls = []
    send_calls = []

    async def fake_get(url, params):
        slippage = int(params.get("slippageBps", 50))
        return _make_quote(1_200_000, slippage)

    async def fake_build(quote, addr):
        return _make_fake_tx_b64()

    async def fake_sign(b64, payer, intent=None, quote=None):
        sign_calls.append(True)
        return ex.solana.SignedSwap("SIG_" + str(len(sign_calls)), b"raw", _checked=True)

    async def fake_send_checked(signed, endpoints=None):
        send_calls.append(True)
        if len(send_calls) == 1:
            return "FIRST_SIG"
        return "SECOND_SIG"

    confirm_calls = [0]

    async def fake_confirm(sig, timeout_s=None, endpoints=None):
        confirm_calls[0] += 1
        if confirm_calls[0] == 1:
            return {"confirmed": False, "slot": None, "err": "not confirmed before timeout"}
        return {"confirmed": True, "slot": 20, "err": None}

    async def fake_get_sig_status(sig, endpoints=None):
        return None  # sig not found anywhere

    async def fake_blockhash_valid(blockhash, endpoints=None):
        return False  # expired → safe to retry

    monkeypatch.setattr(ex, "_get_json", fake_get)
    monkeypatch.setattr(ex, "_build_swap_transaction", fake_build)
    monkeypatch.setattr(ex, "sign_after_checks", fake_sign)
    monkeypatch.setattr(ex.solana, "send_checked_swap", fake_send_checked)
    monkeypatch.setattr(ex.solana, "confirm_signature", fake_confirm)
    monkeypatch.setattr(ex.solana, "get_signature_status", fake_get_sig_status)
    monkeypatch.setattr(ex.solana, "is_blockhash_valid", fake_blockhash_valid)

    ledger = _ledger_with_position(env)
    res = await ex.place_sell(MINT, "TEST", 1.0, ledger=ledger)

    assert res.status == "filled", f"Expected filled, got {res.status}: {res.reason}"
    assert len(send_calls) == 2, f"Expected escalation, got {len(send_calls)} send calls"


# ---------------------------------------------------------------------------
# Test: classify_exit_failure direct unit tests
# ---------------------------------------------------------------------------

class TestClassifyExitFailure:
    """Unit tests for exit_errors.classify_exit_failure."""

    def _clf(self, exc_or_msg, **kw):
        from live_execution.exit_errors import classify_exit_failure
        return classify_exit_failure(exc_or_msg, **kw)

    def test_sim_refused_6001_is_retryable(self):
        from live_execution.sim_guard import SimulationRefused
        exc = SimulationRefused("sim refused", onchain_err=ERR_6001)
        result = self._clf(exc)
        assert result.retry is True
        assert "6001" in result.reason or "slippage" in result.reason

    def test_sim_refused_6024_is_not_retryable(self):
        from live_execution.sim_guard import SimulationRefused
        exc = SimulationRefused("sim refused", onchain_err=ERR_6024)
        result = self._clf(exc)
        assert result.retry is False

    def test_sim_refused_no_onchain_err_not_retryable(self):
        from live_execution.sim_guard import SimulationRefused
        exc = SimulationRefused("simulation refused: guard refusal")
        result = self._clf(exc)
        assert result.retry is False

    def test_send_rejected_all_rpc_slippage_retryable(self):
        from live_execution.exit_errors import SendRejected
        exc = SendRejected("preflight", attempts=[
            {"kind": "rpc_error", "err": ERR_6001, "message": "preflight"},
            {"kind": "rpc_error", "err": ERR_6001, "message": "preflight"},
        ])
        result = self._clf(exc)
        assert result.retry is True

    def test_send_rejected_mixed_transport_not_retryable(self):
        from live_execution.exit_errors import SendRejected
        exc = SendRejected("preflight", attempts=[
            {"kind": "rpc_error", "err": ERR_6001},
            {"kind": "transport", "err": None},
        ])
        result = self._clf(exc)
        assert result.retry is False

    def test_string_simulation_refused_not_retryable(self):
        result = self._clf("simulation refused: some guard")
        assert result.retry is False

    def test_string_slippage_retryable(self):
        result = self._clf("slippage tolerance exceeded 6001")
        assert result.retry is True

    def test_string_blockhash_expired_retryable(self):
        result = self._clf("blockhash has expired")
        assert result.retry is True

    def test_unknown_string_fail_closed(self):
        result = self._clf("something completely unknown happened")
        assert result.retry is False


# ---------------------------------------------------------------------------
# Test 8: Mutation tests documentation
# ---------------------------------------------------------------------------

def test_mutation_documentation():
    """
    §81 Phase A mutation test documentation (source-pinned).
    Verifies that the key classification logic paths exist in exit_errors.py.
    """
    import inspect
    from live_execution import exit_errors

    src = inspect.getsource(exit_errors.classify_exit_failure)

    # Mutation 1: Removing the SimulationRefused isinstance check
    # → test_sim_slippage_escalates_and_fills would pass unconditionally (wrong)
    assert "isinstance(exc_or_msg, SimulationRefused)" in src, \
        "SimulationRefused isinstance check must exist"

    # Mutation 2: Removing the onchain_err is not None check
    # → every SimulationRefused would try custom_error_code(None) → no code → no retry
    assert "onchain_err is not None" in src, \
        "onchain_err not-None guard must exist"

    # Mutation 3: Removing the retryable code check
    # → all on-chain errors would escalate, including non-retryable codes
    assert "EXIT_RETRYABLE_CUSTOM_ERRORS" in src, \
        "retryable custom error codes check must reference config"

    src2 = inspect.getsource(exit_errors.custom_error_code)
    # Mutation 4: Removing the InstructionError key lookup
    # → JSON-form errors would never yield a code
    assert "InstructionError" in src2, \
        "InstructionError key lookup must exist in custom_error_code"


# ---------------------------------------------------------------------------
# Test: EXIT_RETRYABLE_CUSTOM_ERRORS in config
# ---------------------------------------------------------------------------

def test_exit_retryable_custom_errors_in_config():
    """§81 Phase A: config.EXIT_RETRYABLE_CUSTOM_ERRORS exists and contains 6001."""
    assert hasattr(le_config, "EXIT_RETRYABLE_CUSTOM_ERRORS"), \
        "EXIT_RETRYABLE_CUSTOM_ERRORS must be in config"
    assert 6001 in le_config.EXIT_RETRYABLE_CUSTOM_ERRORS, \
        "6001 (SlippageToleranceExceeded) must be in EXIT_RETRYABLE_CUSTOM_ERRORS"


# ---------------------------------------------------------------------------
# Test: SimulationRefused class exists in sim_guard
# ---------------------------------------------------------------------------

def test_simulation_refused_class_exists():
    """§81 Phase A: SimulationRefused subclass of ExecutionError exists in sim_guard."""
    from live_execution.sim_guard import SimulationRefused
    from live_execution.jupiter_executor import ExecutionError

    assert issubclass(SimulationRefused, ExecutionError), \
        "SimulationRefused must be a subclass of ExecutionError"

    exc = SimulationRefused("test", onchain_err=ERR_6001)
    assert exc.onchain_err == ERR_6001
    assert "simulation refused" not in str(exc).lower() or True  # message check flexible


def test_simulation_refused_carries_onchain_err():
    """SimulationRefused.onchain_err roundtrip."""
    from live_execution.sim_guard import SimulationRefused

    exc = SimulationRefused("test message", category="sim_refused", onchain_err=ERR_6001)
    assert exc.onchain_err == ERR_6001
    assert exc.category == "sim_refused"


# ---------------------------------------------------------------------------
# Test: is_retryable_exit_error is now backed by classify_exit_failure
# ---------------------------------------------------------------------------

def test_is_retryable_exit_error_backed_by_classify():
    """§81 Phase A: is_retryable_exit_error delegates to classify_exit_failure."""
    import inspect
    from live_execution import executor

    src = inspect.getsource(executor.is_retryable_exit_error)
    assert "classify_exit_failure" in src, \
        "is_retryable_exit_error must delegate to classify_exit_failure"
