"""
tests/test_launchers.py — static contracts for the local launchers.

`start.sh` / `stop.sh` are the non-Docker operator path. They share the same
money-safety rules as the compose deployment: one armed engine per wallet,
and a SIGTERM that gives an in-flight order/ledger write the full grace window
before SIGKILL. These are source contracts; executing the launchers would
start real processes and is covered by the operator drills instead.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

import config

ROOT = config.BASE_DIR.parent
START = ROOT / "start.sh"
STOP = ROOT / "stop.sh"


def _text(path):
    return path.read_text()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash unavailable")
@pytest.mark.parametrize(
    "path",
    [START, STOP, ROOT / "deploy" / "backup.sh",
     config.BASE_DIR / "docker-entrypoint.sh"],
)
def test_shell_scripts_parse(path):
    proc = subprocess.run(["bash", "-n", str(path)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_stop_waits_the_same_150s_grace_as_compose():
    text = _text(STOP)
    assert 'STOP_GRACE_SECONDS="${STOP_GRACE_SECONDS:-150}"' in text
    assert 'API_STOP_GRACE_SECONDS="${API_STOP_GRACE_SECONDS:-30}"' in text
    # SIGKILL is the fallback, not the first response.
    kill_term = text.index('kill "$pid"')
    wait = text.index('wait_for_exit "$pid" "$grace"', kill_term)
    kill_9 = text.index("kill -9", wait)
    assert kill_term < wait < kill_9
    assert "did not exit within" in text
    assert "treat as an incident" in text


def test_stop_sweeps_are_scoped_and_never_use_pkill():
    """A host `pkill -f run_live_cycle.py` would also signal the Python
    process inside a Docker `live` container, and a stale PID could point at
    an unrelated process. Every sweep must pass the cwd/executable check."""
    text = _text(STOP)
    assert "pkill" not in text
    assert "pid_is_local_python" in text
    assert '"/proc/$pid/cwd"' in text
    assert "readlink -f" in text
    # The live cycle is stopped before the API.
    live = text.index('stop_pidfile "$RUN/live_cycle.pid"')
    api = text.index('stop_pidfile "$RUN/backend.pid"')
    assert live < api


def test_start_refuses_a_second_signer_when_compose_live_is_running():
    text = _text(START)
    assert "docker_live_running" in text
    assert "name=^trading-bot-live$" in text
    assert "Refusing to start a second wallet signer" in text
    assert "docker compose stop live" in text


def test_start_resolves_relative_wallet_paths_like_the_runner():
    """The child runs from $ROOT/backend; a relative WALLET_KEYPAIR_PATH in
    .env must be resolved there, not from the caller's cwd."""
    text = _text(START)
    assert 'KP="$ROOT/backend/$KP"' in text
    assert 'WALLET_KEYPAIR_PATH="$KP"' in text


def test_start_fails_fast_when_the_live_cycle_dies():
    text = _text(START)
    assert "local_live_running" in text
    assert "FAILED to start" in text
    assert 'tail -20 "$LOGS/live_cycle.log"' in text
