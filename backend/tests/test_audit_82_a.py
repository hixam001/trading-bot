"""
test_audit_82_a.py — §82 Phase A: SendRejected containment.

Tests that SendRejected is properly caught at all call sites:
  1. memo.publish_commit_memo raises MemoPublishError (not SendRejected)
  2. place_sell and place_buy fail cleanly with "commit memo failed:" reason
  3. execute_confirmed_trade raises ExecutionError (not SendRejected)
  4. drill.py logs a failed step (not crash)
  5. AST guard: every send_raw_transaction/send_checked_swap call site is
     inside a try with a handler for SendRejected, ExecutionError, MemoPublishError, or Exception.

All tests must be seen RED before the Phase A fix is applied.
"""
from __future__ import annotations

import ast
import asyncio
import base64
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import live_execution.config as le_config
from live_execution import executor as ex
from live_execution import memo as memo_mod
from live_execution import solana as sol_mod
from live_execution.exit_errors import SendRejected
from live_execution.models import ExecutionLedger

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------
MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WALLET = "GsbwXfJraMomNxBcpR3DBDr4GEEr2kPpGEe1c1FQFPB1"

# Real attempt dict shape per §82 ground rule 0.4
_RPC_REJECT_ATTEMPT = {
    "endpoint": "https://api.mainnet-beta.solana.com",
    "kind": "rpc_error",
    "code": -32002,
    "message": "Transaction simulation failed: Attempt to debit an account but found no record of a prior credit.",
    "err": "AccountNotFound",
    "data": None,
}


def _make_send_rejected() -> SendRejected:
    return SendRejected(
        "every rpc refused the transaction",
        attempts=[_RPC_REJECT_ATTEMPT],
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def env(monkeypatch, tmp_path):
    """Armed-but-hermetic environment."""
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

    # fill actuals always OK for success paths
    async def fake_fill_actuals(side, mint, sig):
        return {"usdc_delta": 1.20, "tokens_delta": 1.0}

    monkeypatch.setattr(ex, "_fill_actuals", fake_fill_actuals)

    return payer


def _ledger_with_position(env_fixture, tmp_path, mint=MINT, tokens=1.0):
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
# A-1: publish_commit_memo with SendRejected → MemoPublishError
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_publish_commit_memo_send_rejected_becomes_memo_publish_error(monkeypatch):
    """
    publish_commit_memo must catch SendRejected from send_raw_transaction
    and raise MemoPublishError — not let SendRejected escape.
    """
    async def fake_blockhash(endpoint):
        return "FakeBlockhash1111111111111111111111111111111"

    monkeypatch.setattr(sol_mod, "latest_blockhash", fake_blockhash)

    def fake_build(memo_text, payer, blockhash):
        return b"fake_raw_tx"

    monkeypatch.setattr(memo_mod, "build_memo_transaction", fake_build)

    exc = _make_send_rejected()

    async def fake_send(raw, endpoints=None):
        raise exc

    monkeypatch.setattr(sol_mod, "send_raw_transaction", fake_send)

    class _FakePayer:
        pass

    with pytest.raises(memo_mod.MemoPublishError) as exc_info:
        await memo_mod.publish_commit_memo(
            _FakePayer(), "abc123", endpoints=["https://fake.rpc"]
        )
    assert "rpc refused" in str(exc_info.value).lower() or "refused" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_publish_commit_memo_two_endpoints_send_rejected(monkeypatch):
    """Two endpoints, both rpc_error → MemoPublishError (not SendRejected)."""
    async def fake_blockhash(endpoint):
        return "FakeBlockhash1111111111111111111111111111111"

    monkeypatch.setattr(sol_mod, "latest_blockhash", fake_blockhash)

    def fake_build(memo_text, payer, blockhash):
        return b"fake_raw_tx"

    monkeypatch.setattr(memo_mod, "build_memo_transaction", fake_build)

    attempts = [
        {**_RPC_REJECT_ATTEMPT, "endpoint": "https://rpc1.example"},
        {**_RPC_REJECT_ATTEMPT, "endpoint": "https://rpc2.example"},
    ]
    exc = SendRejected("every rpc refused", attempts=attempts)

    async def fake_send(raw, endpoints=None):
        raise exc

    monkeypatch.setattr(sol_mod, "send_raw_transaction", fake_send)

    class _FakePayer:
        pass

    with pytest.raises(memo_mod.MemoPublishError):
        await memo_mod.publish_commit_memo(
            _FakePayer(), "abc123",
            endpoints=["https://rpc1.example", "https://rpc2.example"]
        )


# ---------------------------------------------------------------------------
# A-2: place_sell with memo SendRejected → clean failed OrderResult + closed commit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_place_sell_memo_rejected_fails_cleanly_and_closes_commit(
    env, monkeypatch, tmp_path
):
    """
    When publish_commit_memo raises MemoPublishError (triggered by SendRejected),
    place_sell must:
      - return status=="failed"
      - reason starts with "commit memo failed:"
      - the sealed commit entry is marked "failed" (not left "sealed")
    """
    ledger = _ledger_with_position(env, tmp_path)

    # Patch publish_commit_memo to raise MemoPublishError
    async def fake_memo_fail(payer, seal_hash, endpoints=None):
        raise memo_mod.MemoPublishError("every rpc refused the memo transaction: AccountNotFound")

    monkeypatch.setattr(ex.memo, "publish_commit_memo", fake_memo_fail)

    result = await ex.place_sell(
        mint=MINT,
        symbol="SOL",
        fraction=1.0,
        ledger=ledger,
        full_close=True,
        reference_price_usd=1.0,
    )

    assert result.status == "failed", f"expected failed, got {result.status!r}"
    assert result.reason and result.reason.startswith("commit memo failed:"), \
        f"expected reason starting with 'commit memo failed:', got {result.reason!r}"

    # Check the commit log entry is marked failed
    from live_execution.commit_log import CommitLog
    logc = CommitLog(le_config.STATE_DIR / "commits.json")
    entries = logc.all()
    assert entries, "expected at least one commit log entry"
    # The sealed entry should be "failed", not "sealed"
    latest = entries[-1]
    assert latest["status"] == "failed", \
        f"commit entry status should be 'failed', got {latest['status']!r}"


@pytest.mark.asyncio
async def test_place_buy_memo_rejected_fails_cleanly_and_closes_commit(
    env, monkeypatch, tmp_path
):
    """
    When publish_commit_memo raises MemoPublishError in place_buy,
    it must return status=="failed" with reason starting "commit memo failed:".
    """
    ledger = ExecutionLedger(tmp_path / "ledger.json")

    async def fake_memo_fail(payer, seal_hash, endpoints=None):
        raise memo_mod.MemoPublishError("every rpc refused the memo transaction: AccountNotFound")

    monkeypatch.setattr(ex.memo, "publish_commit_memo", fake_memo_fail)

    result = await ex.place_buy(
        mint=MINT,
        symbol="SOL",
        usd=1.0,
        output_decimals=6,
        idempotency_key="test-buy-key",
        ledger=ledger,
        reference_price_usd=1.0,
    )

    assert result.status == "failed", f"expected failed, got {result.status!r}"
    assert result.reason and result.reason.startswith("commit memo failed:"), \
        f"expected reason starting 'commit memo failed:', got {result.reason!r}"

    from live_execution.commit_log import CommitLog
    logc = CommitLog(le_config.STATE_DIR / "commits.json")
    entries = logc.all()
    assert entries, "expected commit log entry"
    latest = entries[-1]
    assert latest["status"] == "failed", \
        f"commit entry should be 'failed', got {latest['status']!r}"


# ---------------------------------------------------------------------------
# A-3: execute_confirmed_trade with SendRejected → ExecutionError
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_confirmed_trade_send_rejected_is_execution_error(monkeypatch, tmp_path):
    """
    execute_confirmed_trade must catch SendRejected from send_checked_swap
    and raise ExecutionError.
    """
    from live_execution import jupiter_executor as jex
    from live_execution.solana import ExecutionError

    monkeypatch.setattr(le_config, "LIVE_TRADING_ENABLED", True)
    monkeypatch.setattr(le_config, "REQUIRE_MANUAL_CONFIRMATION", False)
    monkeypatch.setattr(le_config, "STATE_DIR", tmp_path)

    class _FakePayer:
        pass

    payer = _FakePayer()
    monkeypatch.setattr(jex.wallet, "load_keypair", lambda: payer)
    monkeypatch.setattr(jex.wallet, "verify_expected_address", lambda p: None)
    monkeypatch.setattr(jex.wallet, "pubkey_string", lambda p: WALLET)

    async def fake_sol(addr):
        return 1.0

    async def fake_usdc(addr):
        return 100.0

    monkeypatch.setattr(jex.solana, "get_sol_balance", fake_sol)
    monkeypatch.setattr(jex.solana, "get_usdc_balance", fake_usdc)

    async def fake_get_json(url, params=None, headers=None):
        # Return a self-consistent quote
        return {
            "inputMint": USDC_MINT,
            "outputMint": MINT,
            "inAmount": "1000000",
            "outAmount": "1000000",
            "otherAmountThreshold": "995000",
            "slippageBps": 50,
            "priceImpactPct": "0.001",
            "swapMode": "ExactIn",
            "routePlan": [],
        }

    monkeypatch.setattr(jex, "_get_json", fake_get_json)

    async def fake_build_swap(quote, addr):
        return base64.b64encode(b"\x00" * 64).decode()

    monkeypatch.setattr(jex, "_build_swap_transaction", fake_build_swap)

    # sign_after_checks needs a SignedSwap; patch it
    class _FakeSignedSwap:
        raw = b"\x00" * 64
        signature = "FAKESIG" + "x" * 80
        _checked = True
        intent = None

    async def fake_sign(swap_b64, payer, intent=None, quote=None):
        return _FakeSignedSwap()

    monkeypatch.setattr(jex, "sign_after_checks", fake_sign)

    exc = _make_send_rejected()

    async def fake_send_checked(signed_swap, endpoints=None):
        raise exc

    monkeypatch.setattr(jex.solana, "send_checked_swap", fake_send_checked)

    ledger = ExecutionLedger(tmp_path / "ledger.json")

    with pytest.raises(ExecutionError) as exc_info:
        await jex.execute_confirmed_trade(
            output_mint=MINT,
            output_decimals=6,
            usd_size=1.0,
            ledger=ledger,
            idempotency_key="test-exec-key",
            reference_price_usd=1.0,
        )
    # The exception should be ExecutionError (not SendRejected) — and should mention refusal
    assert "rpc refused" in str(exc_info.value).lower() or \
        "refused" in str(exc_info.value).lower() or \
        "send" in str(exc_info.value).lower(), \
        f"unexpected message: {exc_info.value}"


# ---------------------------------------------------------------------------
# A-4: drill.py with SendRejected → failed step, not crash
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drill_broadcast_send_rejected_is_failed_step(monkeypatch, tmp_path):
    """
    drill.run_drill must catch SendRejected from send_raw_transaction,
    record a failed "broadcast" step, and return the steps list (not crash).
    """
    import live_execution.drill as drill_mod
    from live_execution import solana as _sol

    monkeypatch.setattr(le_config, "DRILL_RPC_URL", "https://api.devnet.solana.com")

    async def fake_get_sol(addr, endpoints=None):
        return 1.0

    monkeypatch.setattr(_sol, "get_sol_balance", fake_get_sol)
    # Patch sol_rpc (imported as sol_rpc in drill) so balance and decimals checks pass
    # drill does: bal_raw = await sol_rpc("getBalance", [addr]); bal = int((bal_raw or {}).get("value", 0)) / LAMPORTS_PER_SOL
    async def fake_rpc(method, params=None, endpoints=None, timeout=None):
        if method == "getBalance":
            return {"value": 1_000_000_000}  # 1 SOL
        if method == "getTokenSupply":
            return {"value": {"decimals": 9}}
        return None
    monkeypatch.setattr(drill_mod, "sol_rpc", fake_rpc)

    async def fake_decimals(mint, endpoints=None):
        return 9

    monkeypatch.setattr(_sol, "get_mint_decimals", fake_decimals)

    async def fake_blockhash(endpoint):
        # Must be a valid base58-encoded 32-byte hash for solders
        return "4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4P3bkLKi"

    monkeypatch.setattr(drill_mod, "latest_blockhash", fake_blockhash)

    async def fake_simulate(tx_b64, accounts=None, endpoints=None, require_dual_rpc=False):
        return {"units": 100, "err": None}

    monkeypatch.setattr(_sol, "simulate_transaction", fake_simulate)

    exc = _make_send_rejected()

    async def fake_send(raw, endpoints=None):
        raise exc

    # drill imports send_raw_transaction directly — patch the module attribute
    monkeypatch.setattr(drill_mod, "send_raw_transaction", fake_send)

    # Use a real solders Keypair (drill builds a real VersionedTransaction)
    from solders.keypair import Keypair  # type: ignore
    payer_kp = Keypair()

    monkeypatch.setattr(drill_mod.wallet, "load_keypair", lambda: payer_kp)
    # verify_expected_address must return the address string
    monkeypatch.setattr(drill_mod.wallet, "verify_expected_address",
                        lambda p: str(payer_kp.pubkey()))
    monkeypatch.setattr(drill_mod.wallet, "pubkey_string",
                        lambda p: str(payer_kp.pubkey()))

    steps = await drill_mod.run_drill()
    # Should NOT raise — should return a list of steps
    assert isinstance(steps, list), f"expected list, got {type(steps)}"
    # Find the broadcast step — it should be False (failed)
    # drill uses key "step" for name
    broadcast_steps = [s for s in steps if s.get("step") == "broadcast"]
    assert broadcast_steps, f"expected a 'broadcast' step in the results, got steps: {steps}"
    assert not broadcast_steps[-1]["ok"], \
        f"broadcast step should have ok=False, got {broadcast_steps[-1]}"
    # The failure detail should mention the rejection
    detail = broadcast_steps[-1].get("detail", "")
    assert detail, "broadcast step detail should not be empty"


# ---------------------------------------------------------------------------
# A-5: AST guard — every send call site has a handler
# ---------------------------------------------------------------------------

def test_every_send_call_site_handles_send_rejected():
    """
    Parse every non-test module under backend/live_execution/ with ast.
    For each call to send_raw_transaction or send_checked_swap, assert it is
    lexically inside a try that has a handler for SendRejected, ExecutionError,
    MemoPublishError, or Exception.

    Allowlisted sites (where the caller handles it) must have a comment
    indicating who handles it, OR be in the allowlist below.
    """
    # Allowlisted functions that deliberately delegate to their callers:
    # Key: "filename.py:func_name"
    ALLOWLIST = {
        # send_checked_swap delegates send_raw_transaction to its callers
        # (executor._broadcast_and_confirm, jupiter_executor.execute_confirmed_trade)
        "solana.py:send_checked_swap": "callers (executor._broadcast_and_confirm, jupiter_executor) handle SendRejected",
    }

    live_exec_dir = Path(__file__).parent.parent / "live_execution"
    assert live_exec_dir.is_dir(), f"live_execution dir not found at {live_exec_dir}"

    SEND_FUNCS = {"send_raw_transaction", "send_checked_swap"}
    HANDLERS = {"SendRejected", "ExecutionError", "MemoPublishError", "Exception"}

    violations = []

    for py_file in sorted(live_exec_dir.glob("*.py")):
        if py_file.name.startswith("test_"):
            continue
        source = py_file.read_text()
        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            continue

        class _Visitor(ast.NodeVisitor):
            def __init__(self):
                self.violations = []
                self._try_stack: list[list[str]] = []
                self._func_stack: list[str] = []

            def _handler_names(self, handlers):
                names = []
                for h in handlers:
                    if h.type is None:
                        names.append("Exception")  # bare except
                    elif isinstance(h.type, ast.Name):
                        names.append(h.type.id)
                    elif isinstance(h.type, ast.Attribute):
                        names.append(h.type.attr)
                    elif isinstance(h.type, ast.Tuple):
                        for elt in h.type.elts:
                            if isinstance(elt, ast.Name):
                                names.append(elt.id)
                            elif isinstance(elt, ast.Attribute):
                                names.append(elt.attr)
                return names

            def visit_FunctionDef(self, node):
                self._func_stack.append(node.name)
                self.generic_visit(node)
                self._func_stack.pop()

            def visit_AsyncFunctionDef(self, node):
                self._func_stack.append(node.name)
                self.generic_visit(node)
                self._func_stack.pop()

            def visit_Try(self, node):
                handler_names = self._handler_names(node.handlers)
                self._try_stack.append(handler_names)
                self.generic_visit(node)
                self._try_stack.pop()

            def visit_TryStar(self, node):
                self.visit_Try(node)

            def visit_Call(self, node):
                func = node.func
                func_name = None
                if isinstance(func, ast.Attribute):
                    func_name = func.attr
                elif isinstance(func, ast.Name):
                    func_name = func.id

                if func_name in SEND_FUNCS:
                    current_func = self._func_stack[-1] if self._func_stack else "<module>"
                    site_key = f"{py_file.name}:{current_func}"
                    if site_key not in ALLOWLIST:
                        # Check if any ancestor try has a relevant handler
                        covered = False
                        for handler_list in self._try_stack:
                            if any(h in HANDLERS for h in handler_list):
                                covered = True
                                break
                        if not covered:
                            self.violations.append(
                                f"{py_file.name}:{node.lineno} (in {current_func}()): call to {func_name}() "
                                f"not inside a try with SendRejected/ExecutionError/"
                                f"MemoPublishError/Exception handler"
                            )
                self.generic_visit(node)

        v = _Visitor()
        v.visit(tree)
        violations.extend(v.violations)

    if violations:
        msg = "\n".join(violations)
        pytest.fail(
            f"Send call sites not covered by exception handler:\n{msg}\n"
            f"Add a try/except SendRejected (or ExecutionError/MemoPublishError/Exception) "
            f"around each, or document in the allowlist."
        )


# ---------------------------------------------------------------------------
# A-6: SendRejected is subclass of ExecutionError
# ---------------------------------------------------------------------------

def test_send_rejected_is_subclass_of_execution_error():
    """SendRejected must be a subclass of ExecutionError (Phase A fix #1)."""
    from live_execution.exit_errors import SendRejected
    from live_execution.solana import ExecutionError
    assert issubclass(SendRejected, ExecutionError), \
        "SendRejected must subclass ExecutionError"
