"""
test_audit_82_c.py — §82 Phase C: Kill switch file integrity.

Tests that:
  1. WALLET_DROP_TRIPS_KILL_SWITCH = True trips kill switch via kill_switch.trip()
     creating kill_switch.json (not kill_switch.txt).
  2. trip_reason() contains the channel drop text.
  3. If already tripped with an earlier reason, the earlier reason is not overwritten.
  4. Negative case: WALLET_DROP_TRIPS_KILL_SWITCH = False does NOT trip the kill switch.
  5. Static test: No non-test source file in backend/ or scripts/ contains "kill_switch.txt".
"""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from live_execution import config as live_config
from live_execution import kill_switch
from live_execution.reconcile import check_wallet_drop


class FakeLedger:
    def __init__(self, records):
        self._records = records

    def _load(self):
        return self._records


def test_wallet_drop_trips_kill_switch_json(tmp_path, monkeypatch):
    """
    Phase C: WALLET_DROP_TRIPS_KILL_SWITCH = True must trip kill_switch.json
    via kill_switch.trip() so is_tripped() returns True and trip_reason()
    contains the channel text.
    """
    monkeypatch.setattr(live_config, "WALLET_DROP_TRIPS_KILL_SWITCH", True)
    monkeypatch.setattr(live_config, "STATE_DIR", tmp_path)

    snap_file = tmp_path / "wallet_snapshot.json"
    snap_file.write_text(json.dumps({"ts": 100.0, "usdc": 100.0, "sol": 1.0}))

    assert not kill_switch.is_tripped(state_dir=tmp_path)

    res = check_wallet_drop(
        current_usdc=50.0,
        current_sol=1.0,
        ledger=FakeLedger([]),
        state_dir=tmp_path,
        now_fn=lambda: 200.0,
    )

    assert res.get("alerted") is True
    # Must trip kill_switch.json (via kill_switch module)
    assert kill_switch.is_tripped(state_dir=tmp_path), \
        "kill_switch.is_tripped() must be True when WALLET_DROP_TRIPS_KILL_SWITCH=True"
    reason = kill_switch.trip_reason(state_dir=tmp_path)
    assert "wallet_drop" in reason, f"Expected wallet_drop in reason, got {reason!r}"
    assert "USDC drop" in reason, f"Expected channel info in reason, got {reason!r}"

    # Must NOT create kill_switch.txt
    assert not (tmp_path / "kill_switch.txt").exists(), \
        "kill_switch.txt must NOT be created"


def test_wallet_drop_does_not_overwrite_earlier_kill_switch_reason(tmp_path, monkeypatch):
    """
    If kill switch is already tripped with an earlier reason,
    wallet drop must NOT overwrite the earlier reason.
    """
    monkeypatch.setattr(live_config, "WALLET_DROP_TRIPS_KILL_SWITCH", True)
    monkeypatch.setattr(live_config, "STATE_DIR", tmp_path)

    # Trip earlier
    earlier_reason = "MANUAL: operator emergency stop"
    kill_switch.trip(earlier_reason, state_dir=tmp_path)
    assert kill_switch.trip_reason(state_dir=tmp_path) == earlier_reason

    snap_file = tmp_path / "wallet_snapshot.json"
    snap_file.write_text(json.dumps({"ts": 100.0, "usdc": 100.0, "sol": 1.0}))

    res = check_wallet_drop(
        current_usdc=50.0,
        current_sol=1.0,
        ledger=FakeLedger([]),
        state_dir=tmp_path,
        now_fn=lambda: 200.0,
    )

    assert res.get("alerted") is True
    # Reason must still be the earlier reason
    assert kill_switch.trip_reason(state_dir=tmp_path) == earlier_reason, \
        f"Earlier reason was overwritten! Expected {earlier_reason!r}, got {kill_switch.trip_reason(state_dir=tmp_path)!r}"


def test_wallet_drop_option_off_does_not_trip_kill_switch(tmp_path, monkeypatch):
    """
    When WALLET_DROP_TRIPS_KILL_SWITCH = False (default), drop check alerts
    but does NOT trip the kill switch.
    """
    monkeypatch.setattr(live_config, "WALLET_DROP_TRIPS_KILL_SWITCH", False)
    monkeypatch.setattr(live_config, "STATE_DIR", tmp_path)

    snap_file = tmp_path / "wallet_snapshot.json"
    snap_file.write_text(json.dumps({"ts": 100.0, "usdc": 100.0, "sol": 1.0}))

    res = check_wallet_drop(
        current_usdc=50.0,
        current_sol=1.0,
        ledger=FakeLedger([]),
        state_dir=tmp_path,
        now_fn=lambda: 200.0,
    )

    assert res.get("alerted") is True
    assert not kill_switch.is_tripped(state_dir=tmp_path), \
        "kill switch must not be tripped when option is False"


def test_no_kill_switch_txt_in_production_source():
    """
    Grep backend/ and scripts/ (excluding tests and test directories) for the literal
    'kill_switch.txt' and fail if found.
    """
    repo_root = Path(__file__).parent.parent.parent
    violations = []

    for search_dir in [repo_root / "backend", repo_root / "scripts"]:
        for py_path in search_dir.rglob("*.py"):
            # Exclude test files and test directories
            parts = py_path.parts
            if "tests" in parts or "test" in parts or py_path.name.startswith("test_"):
                continue
            content = py_path.read_text()
            if "kill_switch.txt" in content:
                violations.append(f"{py_path.relative_to(repo_root)} contains 'kill_switch.txt'")

    assert not violations, f"Found literal 'kill_switch.txt' in non-test source:\n" + "\n".join(violations)
