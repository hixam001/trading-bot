"""§95 S1: private, best-effort live counters and a read-only operator view."""
import copy
import json
import logging
import os
import signal
from pathlib import Path

import httpx
import pytest

CATEGORIES = ("structural", "control", "wallet_amount", "wallet_lamports", "vault_amount", "malformed")


def _snapshot(written_at=1000):
    return {"scope": "live-process", "written_at": written_at, "since_boot_at": 1,
            "attempts": {"buy": 7, "sell": 3},
            "refused": {s: {c: i for i, c in enumerate(CATEGORIES)} for s in ("buy", "sell")}}


async def test_public_health_is_exactly_status_ok():
    from api.main import app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_live_heartbeat_writes_atomic_fixed_schema_snapshot(tmp_path, monkeypatch):
    import run_live_cycle as live
    monkeypatch.setattr(live.live_config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(live.time, "time", lambda: 1000)
    status = _snapshot()
    status.update(scope="process", wallet="dummy-sensitive-wallet", mint="dummy-sensitive-mint", signature="dummy-sensitive-signature")
    status["attempts"]["secret"] = "dummy-sensitive-extra"
    status["refused"]["buy"]["extra"] = "dummy-sensitive-extra"
    monkeypatch.setattr(live.solana, "get_consensus_status", lambda: status)
    live._beat()
    path = tmp_path / "consensus_counters.json"
    assert json.loads(path.read_text()) == _snapshot()
    assert (tmp_path / "heartbeat.txt").read_text() == "1000\n"
    assert {p.name for p in tmp_path.iterdir()} == {"heartbeat.txt", "consensus_counters.json"}


@pytest.mark.parametrize("failure", ["write", "replace", "status"])
def test_snapshot_write_failure_never_stops_heartbeat(tmp_path, monkeypatch, caplog, failure):
    import run_live_cycle as live
    monkeypatch.setattr(live.live_config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(live.time, "time", lambda: 1000)
    snapshot_file = tmp_path / "consensus_counters.json"
    snapshot_file.write_text(json.dumps(_snapshot(900)))
    original_write, original_replace = Path.write_text, Path.replace
    def write(path, *args, **kwargs):
        if path.name == "consensus_counters.json.tmp" and failure == "write":
            raise OSError("synthetic snapshot write failure")
        return original_write(path, *args, **kwargs)
    def replace(path, *args, **kwargs):
        if path.name == "consensus_counters.json.tmp" and failure == "replace":
            raise OSError("synthetic snapshot rename failure")
        return original_replace(path, *args, **kwargs)
    def status():
        raise RuntimeError("synthetic snapshot status failure")
    monkeypatch.setattr(Path, "write_text", write)
    monkeypatch.setattr(Path, "replace", replace)
    if failure == "status":
        monkeypatch.setattr(live.solana, "get_consensus_status", status)
    with caplog.at_level(logging.WARNING, logger="run_live_cycle"):
        live._beat()
    assert (tmp_path / "heartbeat.txt").read_text() == "1000\n"
    assert json.loads(snapshot_file.read_text()) == _snapshot(900)
    assert any(r.levelno == logging.WARNING and "consensus snapshot" in r.message.lower() for r in caplog.records)


@pytest.mark.parametrize("age,code,stale", [(0, 0, False), (120, 0, False), (121, 1, True)])
def test_status_fresh_and_stale_counts(tmp_path, monkeypatch, capsys, age, code, stale):
    from scripts import consensus_status as cli
    (tmp_path / "consensus_counters.json").write_text(json.dumps(_snapshot()))
    monkeypatch.setattr(cli.time, "time", lambda: 1000 + age)
    assert cli.main(["--state-dir", str(tmp_path), "--cycle-interval-seconds", "60"]) == code
    output = capsys.readouterr().out
    assert ("STALE" in output) is stale
    assert f"age_seconds={age:.1f}" in output
    assert "buy attempts=7" in output and "sell attempts=3" in output
    assert "wallet_amount=2" in output and "since_boot_at=1" in output
    assert "SIM_CONSENSUS_REFUSED" in output and "SIM_CONSENSUS_ATTEMPT" in output


def test_status_missing_is_no_data_never_healthy_zeros(tmp_path, capsys):
    from scripts import consensus_status as cli
    assert cli.main(["--state-dir", str(tmp_path)]) == 2
    output = capsys.readouterr().out
    assert "STALE" in output and "no data yet" in output.lower()
    assert "attempts=0" not in output and "refusals=0" not in output
    assert "SIM_CONSENSUS_REFUSED" in output and "SIM_CONSENSUS_ATTEMPT" in output
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("change", ["json", "extra", "category", "side", "bool", "negative", "scope", "nan", "boot"])
def test_status_corrupt_is_invalid_and_does_not_print_values(tmp_path, capsys, change):
    from scripts import consensus_status as cli
    snapshot = copy.deepcopy(_snapshot())
    if change == "extra": snapshot["secret"] = "dummy-sensitive-value"
    if change == "category": snapshot["refused"]["buy"]["secret"] = "dummy-sensitive-value"
    if change == "side": snapshot["attempts"]["extra"] = 1
    if change == "bool": snapshot["attempts"]["buy"] = True
    if change == "negative": snapshot["attempts"]["sell"] = -1
    if change == "scope": snapshot["scope"] = "process"
    if change == "nan": snapshot["written_at"] = float("nan")
    if change == "boot": snapshot["since_boot_at"] = 1001
    (tmp_path / "consensus_counters.json").write_text("{broken" if change == "json" else json.dumps(snapshot))
    assert cli.main(["--state-dir", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert "STALE" in output and "invalid" in output.lower()
    assert "dummy-sensitive-value" not in output and "attempts=0" not in output


def test_status_opens_only_snapshot_read_only(tmp_path, monkeypatch, capsys):
    from scripts import consensus_status as cli
    path = tmp_path / "consensus_counters.json"
    path.write_text(json.dumps(_snapshot()))
    (tmp_path / ".secret_store.key").write_text("dummy-sensitive-value")
    monkeypatch.setattr(cli.time, "time", lambda: 1000)
    opened = []
    original_open, original_io_open = os.open, __import__("io").open
    descriptors = []
    def checked_open(filename, flags, *args, **kwargs):
        opened.append(Path(filename))
        assert Path(filename) == path
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        fd = original_open(filename, flags, *args, **kwargs)
        descriptors.append(fd)
        return fd
    monkeypatch.setattr(os, "open", checked_open)
    def forbidden(*args, **kwargs):
        pytest.fail("consumer must only open the snapshot descriptor")
    monkeypatch.setattr("builtins.open", forbidden)
    def descriptor_only(fd, mode, *args, **kwargs):
        assert fd in descriptors and mode == "r"
        return original_io_open(fd, mode, *args, **kwargs)
    monkeypatch.setattr("io.open", descriptor_only)
    assert cli.main(["--state-dir", str(tmp_path)]) == 0
    assert opened == [path]
    assert "dummy-sensitive-value" not in capsys.readouterr().out


def test_status_unreadable_is_exit_two(tmp_path, monkeypatch, capsys):
    from scripts import consensus_status as cli
    def denied(*args, **kwargs):
        raise PermissionError("synthetic access denial")
    monkeypatch.setattr(os, "open", denied)
    assert cli.main(["--state-dir", str(tmp_path)]) == 2
    assert "STALE" in capsys.readouterr().out


def test_status_does_not_follow_secret_sidecar_symlink(tmp_path, capsys):
    from scripts import consensus_status as cli
    secret = tmp_path / ".secret_store.key"
    secret.write_text("dummy-sensitive-value")
    (tmp_path / "consensus_counters.json").symlink_to(secret)
    assert cli.main(["--state-dir", str(tmp_path)]) == 2
    assert "dummy-sensitive-value" not in capsys.readouterr().out


def test_simulated_live_loop_publishes_snapshot_for_reader(tmp_path, capsys):
    from tests.test_graceful_shutdown import _start, _wait_for_heartbeat
    from scripts import consensus_status as cli
    proc = _start(tmp_path)
    try:
        _wait_for_heartbeat(tmp_path / "state")
        proc.send_signal(signal.SIGTERM)
        output, _ = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    assert proc.returncode == 0, output
    assert cli.main(["--state-dir", str(tmp_path / "state")]) == 0
    assert "scope=live-process" in capsys.readouterr().out
