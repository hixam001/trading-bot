"""
tests/test_state_path_colocation.py — §42 regression: every live-state reader
must resolve the SAME directory, through config.

The 2026-08-30 restructure moved live_execution INSIDE backend/. Two readers
anchored their paths on `config.BASE_DIR.parent` (the old repo root):
`rule_engine/liveness.py` (break state) and `api/routes/disclosure.py`
(kill switch). A stale anchor does not raise — it silently creates a SECOND
state directory at the old path, so the break state the bot writes and the
kill switch an operator trips can live in different places. That is a
real-money safety failure (an engaged kill switch that nothing reads).

The fix made `config` the single source of truth (`LIVE_STATE_DIR`,
`BREAK_STATE_FILE`, `KILL_SWITCH_FILE`) and both readers resolve it per call,
which also lets the suite point them at a tmp dir (see conftest) instead of
reading the operator's real state.

These tests pin both halves: the shipped DEFAULTS co-locate everything in
`backend/live_execution/state/`, and the readers honour config at runtime.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import config
from api.routes import disclosure
from rule_engine import liveness


def _shipped_defaults() -> dict:
    """config's defaults in a FRESH process — the session fixture retargets
    them in-process, so the real shipped values must be read out-of-band."""
    code = (
        "import json, config; "
        "print(json.dumps({"
        "'live_state_dir': str(config.LIVE_STATE_DIR), "
        "'break_state': str(config.BREAK_STATE_FILE), "
        "'kill_switch': str(config.KILL_SWITCH_FILE), "
        "'base_dir': str(config.BASE_DIR)}))"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=config.BASE_DIR,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


# --- the shipped defaults co-locate every live-state file --------------------

def test_shipped_defaults_put_all_live_state_in_backend_live_execution_state():
    d = _shipped_defaults()
    expected_dir = Path(d["base_dir"]) / "live_execution" / "state"
    assert Path(d["live_state_dir"]) == expected_dir
    assert Path(d["break_state"]) == expected_dir / "break_state.json"
    assert Path(d["kill_switch"]) == expected_dir / "kill_switch.json"


def test_live_execution_package_state_dir_matches_the_paper_side_default():
    """The package's own STATE_DIR (kill switch, ledger, commit log writer)
    must be the directory the paper-side readers look at."""
    code = (
        "import json, config; from live_execution import config as le; "
        "print(json.dumps({'pkg': str(le.STATE_DIR), "
        "'paper': str(config.LIVE_STATE_DIR)}))"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=config.BASE_DIR,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    d = json.loads(proc.stdout.strip().splitlines()[-1])
    assert Path(d["pkg"]).resolve() == Path(d["paper"]).resolve()


# --- readers resolve through config (retargetable, per call) ------------------

def test_break_state_reader_follows_config(tmp_path, monkeypatch):
    target = tmp_path / "break_state.json"
    monkeypatch.setattr(config, "BREAK_STATE_FILE", str(target))
    assert liveness._state_path() == target


def test_break_state_reader_resolves_per_call(tmp_path, monkeypatch):
    """Not captured at import time — otherwise the suite (and drills) would
    read the operator's real state no matter what config says."""
    first = tmp_path / "a.json"
    monkeypatch.setattr(config, "BREAK_STATE_FILE", str(first))
    assert liveness._state_path() == first
    second = tmp_path / "b.json"
    monkeypatch.setattr(config, "BREAK_STATE_FILE", str(second))
    assert liveness._state_path() == second


def test_kill_switch_reader_follows_config(tmp_path, monkeypatch):
    """disclosure.py must read the kill switch config points at — otherwise
    an operator-tripped switch is invisible to the public disclosure feed."""
    target = tmp_path / "kill_switch.json"
    target.write_text(json.dumps({"active": True, "reason": "operator test"}))
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", str(target))
    state = disclosure._kill_switch_state()
    assert state["active"] is True
    assert state["reason"] == "operator test"


# --- codebase-wide guard ------------------------------------------------------

def test_no_module_anchors_live_state_on_the_repo_root():
    """Nothing may resolve live state via BASE_DIR.parent (the old layout)."""
    offenders = []
    for path in config.BASE_DIR.rglob("*.py"):
        if "__pycache__" in path.parts or path.name == Path(__file__).name:
            continue
        for line in path.read_text(errors="ignore").splitlines():
            if "BASE_DIR.parent" in line and "live_execution" in line:
                offenders.append(f"{path.name}: {line.strip()}")
    assert offenders == [], offenders