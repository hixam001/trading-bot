"""
Regression: an EMPTY LIVE_EXECUTION_STATE_DIR must fall back to the default
state directory (live_execution/state/), not resolve to Path("") = the
process CWD. The empty-string variant once put the live CommitLedger
(commits.json, real order nonces) at the repo root — one `git add -A` away
from being published.
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest


def _reload_config(monkeypatch, env_value: str | None):
    """Reload live_execution.config with LIVE_EXECUTION_STATE_DIR set to
    env_value (None = unset), returning (module, restore)."""
    import live_execution.config as lcfg

    if env_value is None:
        monkeypatch.delenv("LIVE_EXECUTION_STATE_DIR", raising=False)
    else:
        monkeypatch.setenv("LIVE_EXECUTION_STATE_DIR", env_value)
    return importlib.reload(lcfg)


@pytest.mark.parametrize("env_value", [None, ""])
def test_state_dir_empty_env_falls_back_to_default(monkeypatch, env_value):
    default = Path(__file__).resolve().parent.parent / "state"
    lcfg = _reload_config(monkeypatch, env_value)
    try:
        assert lcfg.STATE_DIR == default
    finally:
        # Restore the module to the process-wide env state for other tests.
        importlib.reload(lcfg)


def test_state_dir_explicit_override_still_wins(monkeypatch, tmp_path):
    lcfg = _reload_config(monkeypatch, str(tmp_path))
    try:
        assert lcfg.STATE_DIR == tmp_path
    finally:
        importlib.reload(lcfg)
