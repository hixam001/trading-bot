"""
test_audit_82_d.py — §82 Phase D: Unreadable USDC passed to wallet-drop check as None.

Tests:
  1. Patch solana.get_usdc_balance -> None, get_sol_balance -> 1.0, with an existing snapshot
     of USDC 200. Call _live_portfolio.
     Assert check_wallet_drop was called with current_usdc is None, no alert fired,
     and the snapshot file bytes are unchanged.
  2. Opposite: USDC readable -> the value is passed through to check_wallet_drop.
  3. Both unreadable (None or raised) -> skipped, snapshot unchanged.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import live_execution.config as live_config
from live_execution.models import ExecutionLedger
import run_live_cycle as rlc


@pytest.fixture
def mock_wallet_and_ledger(tmp_path, monkeypatch):
    """Hermetic setup for _live_portfolio."""
    monkeypatch.setattr(live_config, "STATE_DIR", tmp_path)

    class _FakePayer:
        pass

    monkeypatch.setattr(rlc.wallet, "load_keypair", lambda: _FakePayer())
    monkeypatch.setattr(rlc.wallet, "pubkey_string", lambda p: "FakeWalletAddr1111111111111111111111111111111")
    monkeypatch.setattr(rlc, "_reconcile_with_chain", AsyncMock(return_value={"checked": True, "discrepancies": []}))

    ledger = ExecutionLedger(tmp_path / "ledger.json")
    return tmp_path, ledger


@pytest.mark.asyncio
async def test_unreadable_usdc_passes_none_to_check_wallet_drop(mock_wallet_and_ledger, monkeypatch):
    """
    When get_usdc_balance returns None:
      - check_wallet_drop must be called with current_usdc is None (NOT 0.0)
      - no alert fired
      - snapshot file bytes are completely unchanged (not overwritten with usdc: 0.0)
    """
    tmp_path, ledger = mock_wallet_and_ledger

    # Create an existing snapshot with USDC 200
    snap_file = tmp_path / "wallet_snapshot.json"
    initial_snap = json.dumps({"ts": 100.0, "usdc": 200.0, "sol": 1.5}, indent=2)
    snap_file.write_text(initial_snap)

    monkeypatch.setattr(rlc.solana, "get_usdc_balance", AsyncMock(return_value=None))
    monkeypatch.setattr(rlc.solana, "get_sol_balance", AsyncMock(return_value=1.5))

    from live_execution import reconcile
    drop_check_calls = []
    original_check = reconcile.check_wallet_drop

    def spy_check(current_usdc, current_sol, *args, **kwargs):
        drop_check_calls.append((current_usdc, current_sol))
        return original_check(current_usdc, current_sol, *args, **kwargs)

    monkeypatch.setattr(reconcile, "check_wallet_drop", spy_check)

    portfolio, meta, chain_report = await rlc._live_portfolio(ledger)

    # Cash in portfolio for sizing should still fail closed to 0.0
    assert portfolio.cash_usd == 0.0, "portfolio cash for sizing must fail closed to 0.0"

    # check_wallet_drop must have been called with current_usdc is None, NOT 0.0
    assert drop_check_calls, "check_wallet_drop was not called"
    called_usdc, called_sol = drop_check_calls[0]
    assert called_usdc is None, f"Expected current_usdc is None in check_wallet_drop, got {called_usdc!r}"
    assert called_sol == 1.5

    # Snapshot file bytes must be unchanged
    assert snap_file.read_text() == initial_snap, \
        "Snapshot file was modified when USDC was unreadable!"


@pytest.mark.asyncio
async def test_readable_usdc_passes_value_to_check_wallet_drop(mock_wallet_and_ledger, monkeypatch):
    """When get_usdc_balance returns 250.0, that exact value is passed to check_wallet_drop."""
    tmp_path, ledger = mock_wallet_and_ledger

    snap_file = tmp_path / "wallet_snapshot.json"
    snap_file.write_text(json.dumps({"ts": 100.0, "usdc": 250.0, "sol": 1.5}, indent=2))

    monkeypatch.setattr(rlc.solana, "get_usdc_balance", AsyncMock(return_value=250.0))
    monkeypatch.setattr(rlc.solana, "get_sol_balance", AsyncMock(return_value=1.5))

    from live_execution import reconcile
    drop_check_calls = []
    original_check = reconcile.check_wallet_drop

    def spy_check(current_usdc, current_sol, *args, **kwargs):
        drop_check_calls.append((current_usdc, current_sol))
        return original_check(current_usdc, current_sol, *args, **kwargs)

    monkeypatch.setattr(reconcile, "check_wallet_drop", spy_check)

    portfolio, meta, chain_report = await rlc._live_portfolio(ledger)

    assert portfolio.cash_usd == 250.0
    assert drop_check_calls
    called_usdc, called_sol = drop_check_calls[0]
    assert called_usdc == 250.0
    assert called_sol == 1.5


@pytest.mark.asyncio
async def test_both_unreadable_or_raising_skips_and_preserves_snapshot(mock_wallet_and_ledger, monkeypatch):
    """When balances raise exceptions, both are treated as None for check_wallet_drop."""
    tmp_path, ledger = mock_wallet_and_ledger

    snap_file = tmp_path / "wallet_snapshot.json"
    initial_snap = json.dumps({"ts": 100.0, "usdc": 200.0, "sol": 1.5}, indent=2)
    snap_file.write_text(initial_snap)

    async def raise_err(*args, **kwargs):
        raise RuntimeError("RPC connection failed")

    monkeypatch.setattr(rlc.solana, "get_usdc_balance", raise_err)
    monkeypatch.setattr(rlc.solana, "get_sol_balance", raise_err)

    from live_execution import reconcile
    drop_check_calls = []
    original_check = reconcile.check_wallet_drop

    def spy_check(current_usdc, current_sol, *args, **kwargs):
        drop_check_calls.append((current_usdc, current_sol))
        return original_check(current_usdc, current_sol, *args, **kwargs)

    monkeypatch.setattr(reconcile, "check_wallet_drop", spy_check)

    portfolio, meta, chain_report = await rlc._live_portfolio(ledger)

    assert portfolio.cash_usd == 0.0
    # Snapshot must be unchanged
    assert snap_file.read_text() == initial_snap
