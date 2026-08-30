"""
tests/test_state_path_colocation.py — §42 regression: every live-state reader
must point at the SAME directory.

The 2026-08-30 restructure moved live_execution INSIDE backend/. Two readers
anchored their paths on `config.BASE_DIR.parent` (the old repo root):
`rule_engine/liveness.py` (break state) and `api/routes/disclosure.py`
(kill switch). A stale anchor does not raise — it silently creates a SECOND
state directory at the old path, so the break state the bot writes and the
kill switch an operator trips can live in different places. That is a
real-money safety failure (an engaged kill switch that nothing reads).

These tests pin the co-location contract: break state, kill switch, and the
live_execution package's own STATE_DIR all resolve to
`backend/live_execution/state/`.
"""
from __future__ import annotations

from pathlib import Path

import config
from live_execution import config as le_config
from rule_engine import liveness


EXPECTED_STATE_DIR = config.BASE_DIR / "live_execution" / "state"


def test_break_state_lives_in_backend_live_execution_state():
    assert liveness._state_path() == EXPECTED_STATE_DIR / "break_state.json"


def test_kill_switch_disclosure_path_matches_break_state_dir():
    """disclosure.py reads the kill switch by path — same dir, or the
    operator's kill switch is invisible to the public disclosure feed."""
    import inspect

    from api.routes import disclosure

    src = inspect.getsource(disclosure._kill_switch_state)
    assert "BASE_DIR.parent" not in src, (
        "stale repo-root anchor: live_execution now lives inside backend/"
    )
    assert 'config.BASE_DIR / "live_execution"' in src


def test_live_execution_state_dir_is_the_same_directory():
    """The package's own STATE_DIR (used by kill_switch/ledger/commit log)
    must be the directory the paper-side readers look at."""
    assert Path(le_config.STATE_DIR).resolve() == EXPECTED_STATE_DIR.resolve()


def test_no_module_anchors_live_state_on_the_repo_root():
    """Codebase-wide guard: nothing may resolve live state via BASE_DIR.parent."""
    offenders = []
    for path in config.BASE_DIR.rglob("*.py"):
        if "__pycache__" in path.parts or path.name == Path(__file__).name:
            continue
        text = path.read_text(errors="ignore")
        for line in text.splitlines():
            if "BASE_DIR.parent" in line and "live_execution" in line:
                offenders.append(f"{path.name}: {line.strip()}")
    assert offenders == [], offenders