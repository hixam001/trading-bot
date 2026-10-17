"""
test_audit_81_b.py — §81 Phase B: Fix wallet-drop alarm reliability & ledger flow counting.

Tests for:
  B-1: check_wallet_drop reads usd_size for close records, not proceeds_usd
  B-2: RPC failure (None) skips drop check and does not corrupt snapshot
  B-3: Two-channel checks (USDC flow and SOL gas flow) without hardcoded SOL price
  B-4: run_live_cycle does not use `sol_bal or 0.0` and logs check crashes
  B-5: scripts/ack_wallet_change.py updates snapshot without load_keypair
  B-6: WALLET_DROP_TRIPS_KILL_SWITCH config option trips kill switch when enabled
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from live_execution.reconcile import check_wallet_drop, WalletDropResult


class FakeLedger:
    def __init__(self, records: list[dict]):
        self._records = records

    def _load(self) -> list[dict]:
        return self._records


def test_b1_close_proceeds_counted_from_usd_size(tmp_path):
    """
    B-1 [PROVEN]: ExecutionRecord kind='close' stores usd_size=proceeds.
    check_wallet_drop must read usd_size (with proceeds_usd fallback)
    so legitimate sells do not trigger a false wallet drop alarm.
    """
    snap_file = tmp_path / "wallet_snapshot.json"
    # Initial snapshot: $50 USDC, 1.0 SOL at t=100
    snap_file.write_text(json.dumps({
        "ts": 100.0,
        "usdc": 50.0,
        "sol": 1.0,
    }))

    # Ledger record at t=150: close with usd_size=100.0 (proceeds)
    # Notice: 'proceeds_usd' is NOT present, matching real ExecutionRecord.to_dict()
    records = [
        {
            "id": "c1",
            "ts": 150.0,
            "kind": "close",
            "usd_size": 100.0,
            "mint": "So11111111111111111111111111111111111111112",
            "symbol": "SOL",
        }
    ]
    ledger = FakeLedger(records)

    # Current state at t=200: $150 USDC (50 + 100 proceeds), 0.999 SOL (gas fee)
    res = check_wallet_drop(
        current_usdc=150.0,
        current_sol=0.999,
        ledger=ledger,
        state_dir=tmp_path,
        now_fn=lambda: 200.0,
    )

    # Must NOT alert! 100 proceeds fully explains the $100 USDC increase
    assert not res.get("alerted"), f"False alarm triggered: {res}"
    assert res.get("unexplained_drop_usd", 0.0) == 0.0


def test_b2_unreadable_balances_skip_and_do_not_corrupt_snapshot(tmp_path):
    """
    B-2 [READ]: When USDC or SOL balance is None (RPC failure),
    check_wallet_drop must skip without alerting and must NOT overwrite snapshot.
    """
    snap_file = tmp_path / "wallet_snapshot.json"
    initial_data = {
        "ts": 100.0,
        "usdc": 100.0,
        "sol": 1.0,
    }
    snap_file.write_text(json.dumps(initial_data))

    ledger = FakeLedger([])

    # sol is None (RPC failed)
    res = check_wallet_drop(
        current_usdc=100.0,
        current_sol=None,
        ledger=ledger,
        state_dir=tmp_path,
        now_fn=lambda: 200.0,
    )

    assert not res.get("alerted")
    assert res.get("skipped") is True
    assert res.get("reason") == "balances_unreadable"

    # Snapshot MUST NOT have been overwritten
    saved = json.loads(snap_file.read_text())
    assert saved["ts"] == 100.0, "Snapshot was corrupted by None balance"
    assert saved["sol"] == 1.0


def test_b2_usdc_none_skips_and_preserves_snapshot(tmp_path):
    """USDC=None also skips cleanly."""
    snap_file = tmp_path / "wallet_snapshot.json"
    snap_file.write_text(json.dumps({"ts": 100.0, "usdc": 100.0, "sol": 1.0}))

    res = check_wallet_drop(
        current_usdc=None,
        current_sol=1.0,
        ledger=FakeLedger([]),
        state_dir=tmp_path,
        now_fn=lambda: 200.0,
    )

    assert res.get("skipped") is True
    assert not res.get("alerted")


def test_b3_unexplained_usdc_drop_triggers_alarm(tmp_path):
    """
    B-3: Real USDC theft/outflow (> UNEXPLAINED_OUTFLOW_USD, default $10)
    without corresponding ledger records triggers an alert.
    """
    snap_file = tmp_path / "wallet_snapshot.json"
    snap_file.write_text(json.dumps({
        "ts": 100.0,
        "usdc": 100.0,
        "sol": 1.0,
    }))

    # No ledger records, but USDC dropped from 100 to 50 ($50 drop)
    res = check_wallet_drop(
        current_usdc=50.0,
        current_sol=1.0,
        ledger=FakeLedger([]),
        state_dir=tmp_path,
        now_fn=lambda: 200.0,
    )

    assert res.get("alerted") is True
    assert res.get("unexplained_drop_usd") == pytest.approx(50.0, abs=0.5)


def test_b3_unexplained_sol_drop_triggers_alarm(tmp_path):
    """
    B-3: SOL channel drop exceeding tolerance (0.003/tx + 0.02 buffer)
    triggers an alarm without relying on a hardcoded SOL price.
    """
    snap_file = tmp_path / "wallet_snapshot.json"
    snap_file.write_text(json.dumps({
        "ts": 100.0,
        "usdc": 100.0,
        "sol": 2.0,
    }))

    # 1 transaction, expected gas <= 0.023 SOL.
    # Current SOL dropped from 2.0 to 1.5 (0.5 SOL drop > 0.023)
    records = [{"id": "b1", "ts": 150.0, "kind": "buy", "usd_size": 10.0}]
    res = check_wallet_drop(
        current_usdc=90.0,
        current_sol=1.5,
        ledger=FakeLedger(records),
        state_dir=tmp_path,
        now_fn=lambda: 200.0,
    )

    assert res.get("alerted") is True
    assert "sol" in str(res.get("channel", "")).lower() or res.get("unexplained_sol_drop", 0) > 0.4


def test_b3_normal_gas_fees_do_not_alarm(tmp_path):
    """Normal gas fee burn within tolerance does not alarm."""
    snap_file = tmp_path / "wallet_snapshot.json"
    snap_file.write_text(json.dumps({
        "ts": 100.0,
        "usdc": 100.0,
        "sol": 1.0,
    }))

    # 2 transactions, burning 0.004 SOL (well within 2 * 0.003 + 0.02 = 0.026 buffer)
    records = [
        {"id": "b1", "ts": 120.0, "kind": "buy", "usd_size": 10.0},
        {"id": "c1", "ts": 140.0, "kind": "close", "usd_size": 12.0},
    ]
    res = check_wallet_drop(
        current_usdc=102.0,
        current_sol=0.996,
        ledger=FakeLedger(records),
        state_dir=tmp_path,
        now_fn=lambda: 200.0,
    )

    assert not res.get("alerted")


def test_b4_kill_switch_tripped_when_configured(tmp_path, monkeypatch):
    """
    B-6: WALLET_DROP_TRIPS_KILL_SWITCH = True trips kill switch on alert.
    When False (default), alert only.
    """
    from live_execution import config as live_config

    monkeypatch.setattr(live_config, "WALLET_DROP_TRIPS_KILL_SWITCH", True)
    monkeypatch.setattr(live_config, "STATE_DIR", tmp_path)

    snap_file = tmp_path / "wallet_snapshot.json"
    snap_file.write_text(json.dumps({"ts": 100.0, "usdc": 100.0, "sol": 1.0}))

    kill_file = tmp_path / "kill_switch.txt"
    assert not kill_file.exists()

    res = check_wallet_drop(
        current_usdc=50.0,
        current_sol=1.0,
        ledger=FakeLedger([]),
        state_dir=tmp_path,
        now_fn=lambda: 200.0,
    )

    assert res.get("alerted") is True
    assert kill_file.exists(), "Kill switch was not tripped despite WALLET_DROP_TRIPS_KILL_SWITCH=True"


def test_b5_ack_wallet_change_script_isolation(tmp_path):
    """
    B-5: scripts/ack_wallet_change.py:
    1. Must NOT import load_keypair (AST verified).
    2. Writes updated snapshot with operator reason and ts.
    """
    import ast

    script_path = Path(__file__).resolve().parents[2] / "scripts" / "ack_wallet_change.py"
    assert script_path.exists(), f"Script {script_path} does not exist"

    tree = ast.parse(script_path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "live_execution.wallet", \
                "ack_wallet_change.py must not import from live_execution.wallet"
            for alias in node.names:
                assert alias.name != "load_keypair", \
                    "ack_wallet_change.py must not import load_keypair"

    # Test running script CLI to acknowledge a balance change
    cmd = [
        sys.executable,
        str(script_path),
        "--state-dir", str(tmp_path),
        "--usdc", "250.0",
        "--sol", "1.5",
        "--reason", "manual operator deposit for test",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, f"Script failed: {proc.stderr}"

    snap_file = tmp_path / "wallet_snapshot.json"
    assert snap_file.exists()
    data = json.loads(snap_file.read_text())
    assert data["usdc"] == 250.0
    assert data["sol"] == 1.5
    assert data.get("reason") == "manual operator deposit for test"
    assert data.get("acknowledged_by") == "operator"

