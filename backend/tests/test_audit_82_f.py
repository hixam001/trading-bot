"""
backend/tests/test_audit_82_f.py — Phase F test suite for restore fixes.

Tests:
1. --reconcile-only without archive runs reconcile only (restore_backup not called).
2. Missing archive and missing --reconcile-only exits non-zero with clear error.
3. Partial promotion failure (on 2nd file) trips kill switch with PARTIAL RESTORE and rolls back.
4. Promotion failure on 1st file does not trip PARTIAL RESTORE and leaves state intact.
5. Post-restore banner prints --reconcile-only instruction, not re-restore.
"""

import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.fernet import Fernet

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
from scripts import backup_state, restore_state
from live_execution import kill_switch


@pytest.fixture
def restore_env(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "executions.json").write_text(json.dumps([{"kind": "buy", "status": "OPEN", "mint": "MINT1", "tokens_out": 10.0, "usd_size": 25.0}]), encoding="utf-8")
    (state_dir / "commits.json").write_text(json.dumps([{"hash": "h1"}]), encoding="utf-8")

    db_path = tmp_path / "trading_bot.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE feed_events (id INTEGER PRIMARY KEY, ts TEXT);")
    conn.execute("INSERT INTO feed_events VALUES (1, '2026-10-18T10:00:00Z');")
    conn.commit()
    conn.close()

    key = Fernet.generate_key().decode("utf-8")
    fernet = Fernet(key.encode("utf-8"))

    # Create backup archive
    out_buf = io.BytesIO()
    manifest_files = []
    with tarfile.open(fileobj=out_buf, mode="w:gz") as tout:
        for p in sorted(state_dir.glob("*.json")):
            data = p.read_bytes()
            arcname = f"state/{p.name}"
            ti = tarfile.TarInfo(name=arcname)
            ti.size = len(data)
            tout.addfile(ti, io.BytesIO(data))
            manifest_files.append({"name": arcname, "size": len(data), "sha256": backup_state.sha256_bytes(data)})

        db_data = db_path.read_bytes()
        ti_db = tarfile.TarInfo(name="trading_bot.db")
        ti_db.size = len(db_data)
        tout.addfile(ti_db, io.BytesIO(db_data))
        manifest_files.append({"name": "trading_bot.db", "size": len(db_data), "sha256": backup_state.sha256_bytes(db_data)})

        manifest = {"version": 1, "created_at": "2026-10-18T10:00:00Z", "git_rev": "test", "files": manifest_files}
        m_bytes = json.dumps(manifest).encode("utf-8")
        ti_m = tarfile.TarInfo(name="MANIFEST.json")
        ti_m.size = len(m_bytes)
        tout.addfile(ti_m, io.BytesIO(m_bytes))

    enc_archive = tmp_path / "test_backup.tar.gz.enc"
    enc_archive.write_bytes(fernet.encrypt(out_buf.getvalue()))

    target_state = tmp_path / "target_state"
    target_state.mkdir()
    target_db = tmp_path / "target_trading_bot.db"

    return {
        "tmp_path": tmp_path,
        "archive": enc_archive,
        "key": key,
        "target_state": target_state,
        "target_db": target_db,
    }


def test_reconcile_only_without_archive_runs_reconcile_only(restore_env):
    target_state = restore_env["target_state"]
    with patch("scripts.restore_state.run_reconcile", new_callable=AsyncMock) as mock_rec, \
         patch("scripts.restore_state.restore_backup") as mock_restore:
        mock_rec.return_value = {"checked": True}

        cmd = [
            sys.executable,
            "-m",
            "scripts.restore_state",
            "--reconcile-only",
            "--target-state-dir",
            str(target_state),
        ]
        res = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True)

        assert res.returncode == 0, f"Command failed: {res.stderr}\n{res.stdout}"
        # In a subprocess, check output or call main directly
        with patch("sys.argv", ["restore_state.py", "--reconcile-only", "--target-state-dir", str(target_state)]):
            ret = restore_state.main()
            assert ret == 0
            mock_restore.assert_not_called()
            mock_rec.assert_called_once_with(target_state)


def test_restore_state_requires_archive_or_reconcile_only(restore_env):
    target_state = restore_env["target_state"]
    cmd = [
        sys.executable,
        "-m",
        "scripts.restore_state",
        "--target-state-dir",
        str(target_state),
    ]
    res = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True)
    assert res.returncode != 0
    assert "archive" in res.stderr.lower() or "reconcile-only" in res.stderr.lower()


def test_partial_promotion_failure_trips_kill_switch_and_attempts_rollback(restore_env):
    target_state = restore_env["target_state"]
    target_db = restore_env["target_db"]
    archive = restore_env["archive"]
    key = restore_env["key"]

    # Pre-populate target with initial files
    (target_state / "existing.json").write_text('{"initial": 1}', encoding="utf-8")
    target_db.write_text("INITIAL_DB_CONTENT", encoding="utf-8")
    initial_existing = (target_state / "existing.json").read_text(encoding="utf-8")

    # Patch os.replace so it fails on the 2nd file promoted
    orig_replace = os.replace
    call_count = 0

    def fail_on_second(src, dst):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise OSError("Simulated disk error on 2nd replace")
        return orig_replace(src, dst)

    with patch("os.replace", side_effect=fail_on_second):
        with pytest.raises(OSError, match="Simulated disk error on 2nd replace"):
            restore_state.restore_backup(
                archive_path=archive,
                target_state_dir=target_state,
                target_db_path=target_db,
                backup_key=key,
                force=True,
            )

    # 1. Kill switch tripped with PARTIAL RESTORE
    assert kill_switch.is_tripped(state_dir=target_state)
    reason = kill_switch.trip_reason(state_dir=target_state)
    assert "PARTIAL RESTORE: promoted 1 of" in reason

    # 2. Rollback attempted and pre-restore state restored
    assert (target_state / "existing.json").read_text(encoding="utf-8") == initial_existing
    assert target_db.read_text(encoding="utf-8") == "INITIAL_DB_CONTENT"


def test_promotion_failure_on_first_file_leaves_state_intact_and_does_not_trip_partial(restore_env):
    target_state = restore_env["target_state"]
    target_db = restore_env["target_db"]
    archive = restore_env["archive"]
    key = restore_env["key"]

    (target_state / "existing.json").write_text('{"initial": 1}', encoding="utf-8")
    target_db.write_text("INITIAL_DB_CONTENT", encoding="utf-8")
    initial_existing = (target_state / "existing.json").read_text(encoding="utf-8")

    # Untrip kill switch if tripped
    kill_switch.clear(state_dir=target_state)

    orig_replace = os.replace
    call_count = 0

    def fail_on_first(src, dst):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise OSError("Simulated disk error on 1st replace")
        return orig_replace(src, dst)

    with patch("os.replace", side_effect=fail_on_first):
        with pytest.raises(OSError, match="Simulated disk error on 1st replace"):
            restore_state.restore_backup(
                archive_path=archive,
                target_state_dir=target_state,
                target_db_path=target_db,
                backup_key=key,
                force=True,
            )

    # Kill switch must NOT be tripped with PARTIAL RESTORE reason
    reason = kill_switch.trip_reason(state_dir=target_state)
    assert not reason or "PARTIAL RESTORE" not in reason

    # Existing files remain intact
    assert (target_state / "existing.json").read_text(encoding="utf-8") == initial_existing
    assert target_db.read_text(encoding="utf-8") == "INITIAL_DB_CONTENT"


def test_post_restore_banner_prints_reconcile_only(restore_env, capsys):
    target_state = restore_env["target_state"]
    target_db = restore_env["target_db"]
    archive = restore_env["archive"]
    key = restore_env["key"]

    restore_state.restore_backup(
        archive_path=archive,
        target_state_dir=target_state,
        target_db_path=target_db,
        backup_key=key,
        force=True,
    )
    captured = capsys.readouterr()

    # Must contain `--reconcile-only --target-state-dir`
    assert f"python scripts/restore_state.py --reconcile-only --target-state-dir {target_state}" in captured.out
    # Must NOT contain `<archive> ... --reconcile`
    assert f"{archive} --target-state-dir {target_state} --reconcile" not in captured.out


# ---------------------------------------------------------------------------
# Reconcile must understand the REAL ExecutionLedger shape, not the legacy
# bare-list fixture, and must never print a clean bill of health for drift.
# ---------------------------------------------------------------------------

MINT_OPEN = "MintOpen11111111111111111111111111111111111"


def _real_ledger(state_dir, status="confirmed", tokens=10.0):
    (state_dir / "executions.json").write_text(json.dumps({
        "records": [{
            "kind": "buy", "idempotency_key": "k1", "mint": MINT_OPEN,
            "usd_size": 25.0, "tokens_out": tokens, "price_usd": 2.5,
            "signature": "SIG", "status": status, "ts": 100.0,
        }]
    }), encoding="utf-8")


def _arm_chain(monkeypatch, balances):
    import live_execution.solana as solana_mod
    import live_execution.wallet as wallet_mod

    monkeypatch.setattr(wallet_mod, "load_keypair", lambda: object())
    monkeypatch.setattr(wallet_mod, "pubkey_string", lambda p: "WALLET")

    async def fake_balances(addr):
        return balances

    monkeypatch.setattr(solana_mod, "get_token_balances", fake_balances)


@pytest.mark.asyncio
async def test_reconcile_reads_real_ledger_records_shape(tmp_path, monkeypatch):
    _real_ledger(tmp_path)
    _arm_chain(monkeypatch, {MINT_OPEN: 10.0})

    report = await restore_state.run_reconcile(tmp_path)

    assert report["checked"] is True
    assert report["discrepancies"] == []
    assert not report.get("error")


@pytest.mark.asyncio
async def test_reconcile_flags_chain_below_real_ledger(tmp_path, monkeypatch):
    _real_ledger(tmp_path)
    _arm_chain(monkeypatch, {MINT_OPEN: 4.0})

    report = await restore_state.run_reconcile(tmp_path)

    assert report["checked"] is True
    assert [d["kind"] for d in report["discrepancies"]] == ["chain_below_journal"]


@pytest.mark.asyncio
async def test_reconcile_unreadable_journal_is_an_error(tmp_path, monkeypatch):
    (tmp_path / "executions.json").write_text('{"unexpected": []}', encoding="utf-8")
    _arm_chain(monkeypatch, {})

    report = await restore_state.run_reconcile(tmp_path)

    assert report["checked"] is False
    assert "unreadable" in report["error"]


def test_reconcile_only_exits_nonzero_on_discrepancies(tmp_path):
    with patch.object(restore_state, "run_reconcile", new_callable=AsyncMock) as rec, \
         patch("sys.argv", ["restore_state.py", "--reconcile-only",
                            "--target-state-dir", str(tmp_path)]):
        rec.return_value = {"checked": True,
                            "discrepancies": [{"kind": "vanished"}]}
        assert restore_state.main() == 2


def test_reconcile_only_exits_nonzero_on_unreadable_journal(tmp_path):
    with patch.object(restore_state, "run_reconcile", new_callable=AsyncMock) as rec, \
         patch("sys.argv", ["restore_state.py", "--reconcile-only",
                            "--target-state-dir", str(tmp_path)]):
        rec.return_value = {"checked": False, "error": "bad journal"}
        assert restore_state.main() == 1
