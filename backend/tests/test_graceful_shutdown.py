"""
tests/test_graceful_shutdown.py — §87 Phase B.

`docker stop` / `compose up -d --build` must let an in-flight sign/send/ledger
write finish. These tests exercise the real signal path in a subprocess (with
stubbed cycle/provider/exit-scanner) and the lock ordering of the shutdown
helper. Hermetic: no network, no wallet, no Docker.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

import config
import run_live_cycle as rlc

BACKEND = Path(config.BASE_DIR)

_CHILD = r"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.environ["BACKEND_DIR"])
import run_live_cycle as m

class Provider:
    async def aclose(self):
        pass

class Thinker:
    async def aclose(self):
        pass

async def fake_exit_scan(*args, **kwargs):
    await asyncio.Event().wait()

async def fake_cycle(once=False, provider=None, thinker=None):
    print("CYCLE_START", flush=True)
    delay = float(os.environ.get("CYCLE_SLEEP", "0"))
    if delay:
        await asyncio.sleep(delay)
    print("CYCLE_FINISHED", flush=True)
    return {}

import learning_loop
async def fake_learning():
    return None
learning_loop.run_daily_learning = fake_learning

m.run_cycle = fake_cycle
m.run_cycle._hwm = {}
m.run_cycle._chain_flags = {}
m.build_provider = lambda: Provider()
m.Thinker = lambda: Thinker()
m._exit_scan_loop = fake_exit_scan
m.solana.check_rpc_diversity = lambda: (1, False)
m.paper_config.DATA_BACKEND = "live"
m.paper_config.TICK_INTERVAL_SECONDS = 0.2
m.main()
"""


def _start(tmp_path, cycle_sleep: float = 0.0) -> subprocess.Popen:
    state = tmp_path / "state"
    state.mkdir()
    env = {
        **os.environ,
        "BACKEND_DIR": str(BACKEND),
        "LIVE_EXECUTION_STATE_DIR": str(state),
        "CYCLE_SLEEP": str(cycle_sleep),
        "PYTHONUNBUFFERED": "1",
    }
    return subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(_CHILD)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True,
    )


def _wait_for_heartbeat(state_dir: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (state_dir / "heartbeat.txt").is_file():
            return
        time.sleep(0.02)
    raise AssertionError("cycle never wrote its first heartbeat")


def test_sigterm_exits_cleanly_within_grace(tmp_path):
    proc = _start(tmp_path)
    try:
        _wait_for_heartbeat(tmp_path / "state")
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    assert proc.returncode == 0, out
    assert "graceful shutdown complete" in out


def test_sigterm_lets_an_in_progress_cycle_finish(tmp_path):
    proc = _start(tmp_path, cycle_sleep=1.0)
    try:
        _wait_for_heartbeat(tmp_path / "state")
        time.sleep(0.15)                 # cycle is now inside its 1s stage
        start = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=10)
        elapsed = time.monotonic() - start
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    assert proc.returncode == 0, out
    assert "CYCLE_START" in out
    assert "CYCLE_FINISHED" in out, (
        "the in-flight cycle was cancelled mid-stage instead of finishing"
    )
    # It really waited for the remaining ~0.85s instead of aborting instantly.
    assert elapsed >= 0.6, elapsed


async def test_exit_lock_held_by_a_sell_delays_scanner_cancellation():
    sell_started = asyncio.Event()
    release_sell = asyncio.Event()
    sell_finished = asyncio.Event()

    async def in_flight_sell_scanner():
        async with rlc._EXIT_LOCK:
            sell_started.set()
            await release_sell.wait()      # simulated sign/send/ledger write
            sell_finished.set()
        await asyncio.sleep(60)            # back to idle scanning

    class Provider:
        closed = False
        async def aclose(self):
            self.closed = True

    class Thinker:
        closed = False
        async def aclose(self):
            self.closed = True

    provider, thinker = Provider(), Thinker()
    task = asyncio.create_task(in_flight_sell_scanner())
    await sell_started.wait()
    shutdown = asyncio.create_task(
        rlc._graceful_shutdown(task, provider, thinker))
    await asyncio.sleep(0.05)
    assert not shutdown.done(), "shutdown cancelled a sell still holding _EXIT_LOCK"
    assert not sell_finished.is_set()

    release_sell.set()
    await asyncio.wait_for(shutdown, timeout=5)
    await asyncio.sleep(0)
    assert sell_finished.is_set()
    assert task.cancelled()
    assert provider.closed and thinker.closed


def test_heartbeat_helper_refreshes_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(rlc.live_config, "STATE_DIR", tmp_path)
    rlc._beat()
    first = (tmp_path / "heartbeat.txt").read_text().strip()
    assert first.isdigit()
    time.sleep(1.05)
    rlc._beat()
    second = (tmp_path / "heartbeat.txt").read_text().strip()
    assert second.isdigit()
    assert float(second) > float(first)


def test_run_cycle_beats_at_read_brain_and_candidate_boundaries():
    src = inspect.getsource(rlc.run_cycle)
    # Read stage, brain stage, and the per-candidate loop each refresh it.
    assert src.index("await enrich_candidates(candidates)") < src.index("_beat()")
    assert src.index("brain_result = None\n") < src.index("_beat()", src.index("brain_result = None\n"))
    assert src.index("_journal_feed_event") < src.index("_beat()", src.index("_journal_feed_event"))
    assert src.count("_beat()") >= 3
