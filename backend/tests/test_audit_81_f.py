"""
backend/tests/test_audit_81_f.py — Tests for honest tests, CI, and docs (§81 Phase F).

1. STATE_DIR is under the pytest temp root (not under the repo).
2. Session-finish guard catches and fails on any modification to backend/live_execution/state/.
3. .gitleaks.toml has no paths allowlist containing .github.
4. Docs check: decisionLog.md does not claim complete closure; systemPatterns.md references scripts/prepush_check.sh.
5. CI workflow (.github/workflows/tests.yml) does not set EXPECTED_WALLET_ADDRESS.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tomllib
import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = PROJECT_ROOT / "backend"


def test_state_dir_is_under_pytest_temp_root():
    """
    F-1: STATE_DIR must be isolated under a temporary directory,
    never pointing directly to the repository's real live_execution/state/.
    """
    from live_execution import config as le_config

    real_state_dir = BACKEND_DIR / "live_execution" / "state"
    assert le_config.STATE_DIR != real_state_dir
    # Verify it is not inside PROJECT_ROOT
    try:
        le_config.STATE_DIR.resolve().relative_to(PROJECT_ROOT.resolve())
        in_repo = True
    except ValueError:
        in_repo = False
    assert not in_repo, f"STATE_DIR {le_config.STATE_DIR} was inside repository root {PROJECT_ROOT}"


def test_session_finish_guard_catches_state_dir_mutations(tmp_path):
    """
    F-1: Running a test that mutates backend/live_execution/state/
    must cause the session-finish guard to fail the pytest session with exit code != 0.
    """
    real_state_dir = BACKEND_DIR / "live_execution" / "state"
    real_state_dir.mkdir(parents=True, exist_ok=True)
    probe_file = real_state_dir / f".hermeticity_test_probe_{os.getpid()}"

    # Write a test file inside backend/tests so backend/conftest.py is loaded
    test_file = BACKEND_DIR / "tests" / f"_tmp_pollute_test_{os.getpid()}.py"
    test_file.write_text(f"""
def test_pollute():
    from pathlib import Path
    p = Path({str(probe_file)!r})
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("polluted")
""", encoding="utf-8")

    try:
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            str(test_file),
        ]
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0
        assert "HERMETICITY VIOLATION" in (proc.stdout + proc.stderr)
    finally:
        if probe_file.exists():
            probe_file.unlink(missing_ok=True)
        if test_file.exists():
            test_file.unlink(missing_ok=True)


def test_gitleaks_toml_has_no_github_path_allowlist():
    """
    F-2: .gitleaks.toml must NOT allowlist the entire .github directory or workflows file.
    Only narrow regexes and .gitleaksignore fingerprints are allowed.
    """
    gitleaks_path = PROJECT_ROOT / ".gitleaks.toml"
    assert gitleaks_path.is_file(), ".gitleaks.toml missing"

    data = tomllib.loads(gitleaks_path.read_text(encoding="utf-8"))
    allowlist = data.get("allowlist", {})
    paths = allowlist.get("paths", [])

    for p in paths:
        assert ".github" not in p, f".gitleaks.toml still contains broad allowlist path: {p}"


def test_docs_honesty_and_prepush_references():
    """
    F-4: memory-bank/decisionLog.md must not contain 'Complete closure of all audit findings',
    and memory-bank/systemPatterns.md must reference scripts/prepush_check.sh.
    """
    decision_log = PROJECT_ROOT / "memory-bank" / "decisionLog.md"
    assert decision_log.is_file()
    decision_text = decision_log.read_text(encoding="utf-8")
    assert "Complete closure of all audit findings" not in decision_text

    patterns = PROJECT_ROOT / "memory-bank" / "systemPatterns.md"
    assert patterns.is_file()
    patterns_text = patterns.read_text(encoding="utf-8")
    assert "scripts/prepush_check.sh" in patterns_text


def test_ci_workflow_does_not_set_expected_wallet_address():
    """
    F-3: .github/workflows/tests.yml must NOT set EXPECTED_WALLET_ADDRESS
    on the mock backend, preventing flaky mainnet RPC calls during CI runs.
    """
    ci_workflow_path = PROJECT_ROOT / ".github" / "workflows" / "tests.yml"
    assert ci_workflow_path.is_file(), "tests.yml missing"

    content = ci_workflow_path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(content)

    # Check text and parsed YAML
    assert "EXPECTED_WALLET_ADDRESS" not in content
