"""
tests/test_docker_entrypoint.py — the container entrypoint's live-money gate.

`backend/docker-entrypoint.sh` decides, at container start, whether the REAL
decision cycle runs. That makes it safety-critical shell: the same
"ARMED flag AND wallet configured" double-gate start.sh enforces must hold
in a deployment, and a crashed live cycle must never hide behind a healthy
dashboard (an armed deployment silently not trading is a failure mode).

Hermetic by construction: every case runs in a temp sandbox with a stub
`live_execution/config.py`, and stub `python`/`uvicorn` executables on PATH
that only record their argv. Nothing real is started, no port is bound, no
network or wallet is touched.
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

import config

ENTRYPOINT = config.BASE_DIR / "docker-entrypoint.sh"

_PY_STUB = """#!/usr/bin/env bash
echo "PYTHON_CALLED: $*" >> "$CALLS"
sleep "${STUB_LIVE_SLEEP:-0}"
exit "${STUB_LIVE_RC:-0}"
"""

_UVICORN_STUB = """#!/usr/bin/env bash
echo "UVICORN_CALLED: $*" >> "$CALLS"
sleep "${STUB_API_SLEEP:-0}"
exit "${STUB_API_RC:-0}"
"""

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash unavailable"
)


def _run(tmp_path, *, armed: bool, kp_path: str = "", kp_json: str = "",
         live_rc: int = 0, live_sleep: int = 0,
         api_rc: int = 0, api_sleep: int = 0):
    """Run the entrypoint in a sandbox; return (exit_status, output, calls)."""
    app = tmp_path / "app"
    (app / "live_execution").mkdir(parents=True)
    (app / "live_execution" / "config.py").write_text(
        f"LIVE_TRADING_ENABLED: bool = {armed}\n"
    )
    binp = tmp_path / "bin"
    binp.mkdir()
    for name, body in (("python", _PY_STUB), ("uvicorn", _UVICORN_STUB)):
        p = binp / name
        p.write_text(body)
        p.chmod(0o755)

    calls = tmp_path / "calls.txt"
    calls.write_text("")
    env = {
        **os.environ,
        "APP_DIR": str(app),
        "WALLET_KEYPAIR_PATH": kp_path,
        "WALLET_KEYPAIR_JSON": kp_json,
        "CALLS": str(calls),
        "STUB_LIVE_RC": str(live_rc),
        "STUB_LIVE_SLEEP": str(live_sleep),
        "STUB_API_RC": str(api_rc),
        "STUB_API_SLEEP": str(api_sleep),
        "PATH": f"{binp}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    proc = subprocess.run(
        ["bash", str(ENTRYPOINT)], env=env, capture_output=True,
        text=True, timeout=60,
    )
    return proc.returncode, proc.stdout + proc.stderr, calls.read_text()


# --- the entrypoint exists and is runnable ------------------------------------

def test_entrypoint_is_executable_with_a_shebang():
    assert ENTRYPOINT.is_file()
    assert ENTRYPOINT.read_text().startswith("#!"), (
        "no shebang: the container would run this with /bin/sh at best"
    )
    assert os.access(ENTRYPOINT, os.X_OK)


def test_entrypoint_is_valid_bash():
    proc = subprocess.run(["bash", "-n", str(ENTRYPOINT)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


# --- the API always starts ----------------------------------------------------

def test_api_starts_even_when_disarmed(tmp_path):
    status, out, calls = _run(tmp_path, armed=False)
    assert status == 0
    assert "UVICORN_CALLED" in calls
    assert "0.0.0.0" in calls          # container must bind all interfaces


def test_api_port_follows_the_port_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PORT", "9123")
    _, _, calls = _run(tmp_path, armed=False)
    assert "--port 9123" in calls


# --- THE live-money gate ------------------------------------------------------

def test_armed_without_wallet_does_not_start_the_live_cycle(tmp_path):
    """The load-bearing gate: armed but no wallet configured = reads only."""
    status, out, calls = _run(tmp_path, armed=True)
    assert "PYTHON_CALLED" not in calls
    assert "NOT started" in out
    assert "UVICORN_CALLED" in calls


def test_disarmed_with_wallet_does_not_start_the_live_cycle(tmp_path):
    _, out, calls = _run(tmp_path, armed=False, kp_json="[1,2,3]")
    assert "PYTHON_CALLED" not in calls
    assert "NOT started" in out


def test_armed_keypair_path_must_actually_exist(tmp_path):
    """A configured-but-missing keypair file must not count as a wallet."""
    _, out, calls = _run(tmp_path, armed=True,
                         kp_path=str(tmp_path / "absent.json"))
    assert "PYTHON_CALLED" not in calls
    assert "NOT started" in out


def test_armed_with_existing_keypair_file_starts_the_live_cycle(tmp_path):
    kp = tmp_path / "kp.json"
    kp.write_text("[1,2,3]")
    _, out, calls = _run(tmp_path, armed=True, kp_path=str(kp))
    assert "run_live_cycle.py" in calls
    assert "ARMED + wallet configured" in out


def test_armed_with_env_json_wallet_starts_the_live_cycle(tmp_path):
    """Deployment parity: the env secret channel arms a file-less host."""
    _, out, calls = _run(tmp_path, armed=True, kp_json="[1,2,3]")
    assert "run_live_cycle.py" in calls
    assert "ARMED + wallet configured" in out


# --- supervision: neither half may fail silently ------------------------------

def test_live_cycle_crash_takes_the_container_down(tmp_path):
    """An armed deployment must never keep serving a green dashboard while
    the decision cycle is dead — the platform has to see a non-zero exit."""
    status, out, _ = _run(tmp_path, armed=True, kp_json="[1,2,3]",
                          live_rc=3, api_sleep=3)
    assert status == 3
    assert "live cycle exited" in out


def test_api_crash_takes_the_container_down(tmp_path):
    status, out, _ = _run(tmp_path, armed=True, kp_json="[1,2,3]",
                          api_rc=4, live_sleep=3)
    assert status == 4
    assert "API exited" in out


# --- fail-closed on a broken image layout -------------------------------------

def test_missing_app_dir_aborts_instead_of_starting_in_the_wrong_cwd(tmp_path):
    env = {**os.environ, "APP_DIR": str(tmp_path / "nonexistent")}
    proc = subprocess.run(["bash", str(ENTRYPOINT)], env=env,
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode != 0
    assert "FATAL" in (proc.stdout + proc.stderr)