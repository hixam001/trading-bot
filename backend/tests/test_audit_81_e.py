"""
backend/tests/test_audit_81_e.py — Tests for safe state restore (§81 Phase E).

Findings tested:
1. Multi-pass restore: Tampered 3rd file leaves pre-populated target completely untouched (byte-for-byte).
2. Corrupt SQLite inside valid archive fails staged integrity check and leaves existing DB untouched.
3. Success path always trips kill switch with archive name, even if archive had cleared kill switch.
4. --force preserves existing files under .pre-restore-<timestamp>/.
5. Traversal and dangerous members (path traversal .., absolute paths, symlinks) rejected with nothing written.
6. Decryption failure (wrong key), missing manifest, and extra unlisted members rejected.
7. Mutation check: moving promote loop inside validation loop fails Test 1.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import pytest

from cryptography.fernet import Fernet

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import backup_state
import restore_state
from live_execution import kill_switch


@pytest.fixture
def keys():
    key_backup = Fernet.generate_key().decode("utf-8")
    key_secret = Fernet.generate_key().decode("utf-8")
    return key_backup, key_secret


@pytest.fixture
def sample_state_3files(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "executions.json").write_text(json.dumps([{"id": "exec1", "usd_size": 10.0}]), encoding="utf-8")
    (state_dir / "commits.json").write_text(json.dumps([{"id": "commit1", "symbol": "BONK"}]), encoding="utf-8")
    (state_dir / "safety_counters.json").write_text(json.dumps({"guard_refusals": 0}), encoding="utf-8")

    db_path = tmp_path / "trading_bot.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE candidates (id TEXT PRIMARY KEY, symbol TEXT);")
    conn.execute("INSERT INTO candidates VALUES ('c1', 'SOL');")
    conn.commit()
    conn.close()

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    return state_dir, db_path, backup_dir


def test_tampered_third_file_leaves_target_untouched(sample_state_3files, keys):
    """
    Test 1:
    Three-file archive where the THIRD file's hash is tampered.
    Pre-populated target must be left 100% untouched (byte-for-byte identical),
    no temp debris left behind, non-zero exit / exception.

    Mutation check: If promotion/writing happens in the validation loop before all files
    are validated, the first two files would overwrite target files before the third file
    fails validation. Removing the two-pass architecture causes this test to fail.
    """
    state_dir, db_path, backup_dir = sample_state_3files
    key_backup, key_secret = keys

    # Create real backup of 3 state files
    archive = backup_state.create_backup(
        state_dir=state_dir,
        db_path=db_path,
        backup_dir=backup_dir,
        backup_key=key_backup,
        secret_store_key=key_secret,
    )

    # Pre-populate target with distinct initial contents
    target_state = backup_dir / "target_state"
    target_state.mkdir()
    initial_files = {
        "executions.json": b'{"original": "do_not_touch_exec"}',
        "commits.json": b'{"original": "do_not_touch_commits"}',
        "safety_counters.json": b'{"original": "do_not_touch_safety"}',
    }
    for fname, content in initial_files.items():
        (target_state / fname).write_bytes(content)

    # Tamper with the 3rd file in the archive
    fernet = Fernet(key_backup.encode("utf-8"))
    tar_bytes = fernet.decrypt(archive.read_bytes())
    in_buf = io.BytesIO(tar_bytes)
    out_buf = io.BytesIO()

    with tarfile.open(fileobj=in_buf, mode="r:gz") as tin, tarfile.open(fileobj=out_buf, mode="w:gz") as tout:
        for m in tin.getmembers():
            f = tin.extractfile(m)
            data = f.read() if f else b""
            if m.name == "state/safety_counters.json":
                # Tamper data without updating MANIFEST.json
                data = b'{"tampered": true}'
            ti = tarfile.TarInfo(name=m.name)
            ti.size = len(data)
            tout.addfile(ti, io.BytesIO(data))

    tampered_archive = backup_dir / "tampered_third.tar.gz.enc"
    tampered_archive.write_bytes(fernet.encrypt(out_buf.getvalue()))

    # Attempt restore with force=True
    with pytest.raises(RuntimeError, match="mismatch for 'state/safety_counters.json'"):
        restore_state.restore_backup(
            archive_path=tampered_archive,
            target_state_dir=target_state,
            target_db_path=None,
            backup_key=key_backup,
            force=True,
        )

    # Assert byte-for-byte untouched!
    for fname, expected_bytes in initial_files.items():
        actual_bytes = (target_state / fname).read_bytes()
        assert actual_bytes == expected_bytes, f"{fname} was mutated during failed restore!"

    # Assert no staging debris left behind
    staged_debris = list(target_state.glob(".staged.*"))
    assert staged_debris == [], f"Found temporary staging debris: {staged_debris}"


def test_corrupt_sqlite_leaves_existing_db_untouched(sample_state_3files, keys):
    """
    Test 2:
    Corrupt SQLite DB inside an otherwise valid archive (manifest matches corrupted bytes,
    so size and sha256 pass, but PRAGMA integrity_check fails on staged file).
    Pre-existing target DB must remain byte-for-byte untouched, no temp debris.
    """
    state_dir, db_path, backup_dir = sample_state_3files
    key_backup, key_secret = keys

    # Pre-existing valid target DB
    target_state = backup_dir / "target_state_db"
    target_state.mkdir()
    target_db = backup_dir / "target_trading_bot.db"
    conn = sqlite3.connect(str(target_db))
    conn.execute("CREATE TABLE preserved (val TEXT);")
    conn.execute("INSERT INTO preserved VALUES ('intact_content');")
    conn.commit()
    conn.close()
    original_target_db_bytes = target_db.read_bytes()

    # Create a corrupted DB (invalid sqlite header/pages)
    corrupted_db_bytes = b"NOT A SQLITE DATABASE CORRUPT PAGE CONTENT 1234567890" * 20

    # Build an archive containing state files + corrupted DB with valid manifest
    fernet = Fernet(key_backup.encode("utf-8"))
    out_buf = io.BytesIO()
    manifest_files = []

    with tarfile.open(fileobj=out_buf, mode="w:gz") as tout:
        # State files
        for p in sorted(state_dir.glob("*.json")):
            data = p.read_bytes()
            arcname = f"state/{p.name}"
            ti = tarfile.TarInfo(name=arcname)
            ti.size = len(data)
            tout.addfile(ti, io.BytesIO(data))
            manifest_files.append({
                "name": arcname,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            })
        # Corrupted DB
        ti_db = tarfile.TarInfo(name="trading_bot.db")
        ti_db.size = len(corrupted_db_bytes)
        tout.addfile(ti_db, io.BytesIO(corrupted_db_bytes))
        manifest_files.append({
            "name": "trading_bot.db",
            "size": len(corrupted_db_bytes),
            "sha256": hashlib.sha256(corrupted_db_bytes).hexdigest(),
        })

        manifest_data = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "git_revision": "test",
            "files": manifest_files,
        }
        manifest_raw = json.dumps(manifest_data).encode("utf-8")
        ti_m = tarfile.TarInfo(name="MANIFEST.json")
        ti_m.size = len(manifest_raw)
        tout.addfile(ti_m, io.BytesIO(manifest_raw))

    corrupted_archive = backup_dir / "corrupted_db.tar.gz.enc"
    corrupted_archive.write_bytes(fernet.encrypt(out_buf.getvalue()))

    with pytest.raises(RuntimeError, match="SQLite PRAGMA integrity_check failed"):
        restore_state.restore_backup(
            archive_path=corrupted_archive,
            target_state_dir=target_state,
            target_db_path=target_db,
            backup_key=key_backup,
            force=True,
        )

    # Verify target DB is byte-for-byte untouched
    assert target_db.read_bytes() == original_target_db_bytes
    # Verify no staging debris in db directory or state directory
    assert list(target_db.parent.glob(".staged.*")) == []
    assert list(target_state.glob(".staged.*")) == []


def test_success_path_always_trips_kill_switch_with_archive_name(sample_state_3files, keys):
    """
    Test 3:
    Success path: files match archive, and kill_switch.json is tripped EVEN IF
    the archive contained a cleared kill_switch.json. Reason must mention archive name.
    """
    state_dir, db_path, backup_dir = sample_state_3files
    key_backup, key_secret = keys

    # State in archive has cleared kill switch
    (state_dir / "kill_switch.json").write_text(
        json.dumps({"tripped": False, "reason": "", "ts": 123.0}), encoding="utf-8"
    )

    archive = backup_state.create_backup(
        state_dir=state_dir,
        db_path=db_path,
        backup_dir=backup_dir,
        backup_key=key_backup,
        secret_store_key=key_secret,
    )

    target_state = backup_dir / "restored_state_ks"
    target_db = backup_dir / "restored_db_ks.sqlite"

    res = restore_state.restore_backup(
        archive_path=archive,
        target_state_dir=target_state,
        target_db_path=target_db,
        backup_key=key_backup,
    )

    assert len(res["restored_files"]) >= 4

    # Check kill switch state
    ks_path = target_state / "kill_switch.json"
    assert ks_path.is_file()
    ks_data = json.loads(ks_path.read_text(encoding="utf-8"))
    assert ks_data.get("tripped") is True
    assert archive.name in ks_data.get("reason", "")
    assert "run reconcile and clear manually" in ks_data.get("reason", "")

    # Also check through live_execution.kill_switch API
    assert kill_switch.is_tripped(target_state) is True
    assert archive.name in kill_switch.trip_reason(target_state)


def test_force_preserves_old_files_in_pre_restore_backup(sample_state_3files, keys):
    """
    Test 4:
    With --force on non-empty target, existing files that will be replaced
    are preserved under <target_state_dir>/.pre-restore-<timestamp>/.
    """
    state_dir, db_path, backup_dir = sample_state_3files
    key_backup, key_secret = keys

    archive = backup_state.create_backup(
        state_dir=state_dir,
        db_path=db_path,
        backup_dir=backup_dir,
        backup_key=key_backup,
        secret_store_key=key_secret,
    )

    target_state = backup_dir / "target_state_force"
    target_state.mkdir()
    target_db = backup_dir / "target_force_db.sqlite"

    # Pre-populate target
    (target_state / "executions.json").write_text('{"old_exec": true}', encoding="utf-8")
    (target_state / "pre_existing_note.txt").write_text("important historical note", encoding="utf-8")
    conn = sqlite3.connect(str(target_db))
    conn.execute("CREATE TABLE old_table (x INT);")
    conn.commit()
    conn.close()

    res = restore_state.restore_backup(
        archive_path=archive,
        target_state_dir=target_state,
        target_db_path=target_db,
        backup_key=key_backup,
        force=True,
    )

    # Check that a .pre-restore-<ts> directory was created
    pre_restore_dirs = list(target_state.glob(".pre-restore-*"))
    assert len(pre_restore_dirs) == 1
    pre_dir = pre_restore_dirs[0]

    # Verify old files exist in pre-restore directory
    assert (pre_dir / "executions.json").read_text(encoding="utf-8") == '{"old_exec": true}'
    assert (pre_dir / "pre_existing_note.txt").read_text(encoding="utf-8") == "important historical note"
    assert (pre_dir / target_db.name).is_file()

    # And current target_state has restored contents
    restored_exec = json.loads((target_state / "executions.json").read_text(encoding="utf-8"))
    assert restored_exec == [{"id": "exec1", "usd_size": 10.0}]


def test_path_traversal_and_dangerous_members_rejected(sample_state_3files, keys):
    """
    Test 5:
    Archive members with path traversal (state/../../etc/x), absolute path (/etc/shadow),
    or symlinks are rejected without writing anything to target.
    """
    state_dir, db_path, backup_dir = sample_state_3files
    key_backup, key_secret = keys
    fernet = Fernet(key_backup.encode("utf-8"))

    # Case A: Traversal with ..
    target_state_a = backup_dir / "target_traversal_a"
    target_state_a.mkdir()
    buf_a = io.BytesIO()
    with tarfile.open(fileobj=buf_a, mode="w:gz") as tar:
        ti = tarfile.TarInfo(name="state/../../etc/passwd")
        data = b"malicious content"
        ti.size = len(data)
        tar.addfile(ti, io.BytesIO(data))
        m_raw = json.dumps({"files": [{"name": "state/../../etc/passwd", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}]}).encode("utf-8")
        ti_m = tarfile.TarInfo(name="MANIFEST.json")
        ti_m.size = len(m_raw)
        tar.addfile(ti_m, io.BytesIO(m_raw))
    enc_a = backup_dir / "traversal_a.tar.gz.enc"
    enc_a.write_bytes(fernet.encrypt(buf_a.getvalue()))

    with pytest.raises(RuntimeError, match="(traversal|unauthorized|regular file)"):
        restore_state.restore_backup(enc_a, target_state_a, None, key_backup)
    assert not any(target_state_a.iterdir())

    # Case B: Absolute path
    target_state_b = backup_dir / "target_traversal_b"
    target_state_b.mkdir()
    buf_b = io.BytesIO()
    with tarfile.open(fileobj=buf_b, mode="w:gz") as tar:
        ti = tarfile.TarInfo(name="/etc/evil")
        data = b"malicious content"
        ti.size = len(data)
        tar.addfile(ti, io.BytesIO(data))
        m_raw = json.dumps({"files": [{"name": "/etc/evil", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}]}).encode("utf-8")
        ti_m = tarfile.TarInfo(name="MANIFEST.json")
        ti_m.size = len(m_raw)
        tar.addfile(ti_m, io.BytesIO(m_raw))
    enc_b = backup_dir / "traversal_b.tar.gz.enc"
    enc_b.write_bytes(fernet.encrypt(buf_b.getvalue()))

    with pytest.raises(RuntimeError, match="(absolute|unauthorized|regular file)"):
        restore_state.restore_backup(enc_b, target_state_b, None, key_backup)
    assert not any(target_state_b.iterdir())

    # Case C: Symlink member
    target_state_c = backup_dir / "target_traversal_c"
    target_state_c.mkdir()
    buf_c = io.BytesIO()
    with tarfile.open(fileobj=buf_c, mode="w:gz") as tar:
        ti = tarfile.TarInfo(name="state/symlink.json")
        ti.type = tarfile.SYMTYPE
        ti.linkname = "/etc/passwd"
        tar.addfile(ti)
        m_raw = json.dumps({"files": [{"name": "state/symlink.json", "size": 0, "sha256": hashlib.sha256(b"").hexdigest()}]}).encode("utf-8")
        ti_m = tarfile.TarInfo(name="MANIFEST.json")
        ti_m.size = len(m_raw)
        tar.addfile(ti_m, io.BytesIO(m_raw))
    enc_c = backup_dir / "traversal_c.tar.gz.enc"
    enc_c.write_bytes(fernet.encrypt(buf_c.getvalue()))

    with pytest.raises(RuntimeError, match="not a regular file"):
        restore_state.restore_backup(enc_c, target_state_c, None, key_backup)
    assert not any(target_state_c.iterdir())


def test_wrong_key_missing_manifest_and_extra_member_rejected(sample_state_3files, keys):
    """
    Test 6:
    Wrong key, missing manifest, and unlisted extra member are all rejected fail-closed.
    """
    state_dir, db_path, backup_dir = sample_state_3files
    key_backup, key_secret = keys
    other_key = Fernet.generate_key().decode("utf-8")

    archive = backup_state.create_backup(
        state_dir=state_dir,
        db_path=db_path,
        backup_dir=backup_dir,
        backup_key=key_backup,
        secret_store_key=key_secret,
    )

    target_state = backup_dir / "target_rejections"
    target_state.mkdir()

    # 1. Wrong key
    with pytest.raises(RuntimeError, match="Archive decryption failed"):
        restore_state.restore_backup(archive, target_state, None, other_key)

    # 2. Missing manifest
    fernet = Fernet(key_backup.encode("utf-8"))
    buf_no_m = io.BytesIO()
    with tarfile.open(fileobj=buf_no_m, mode="w:gz") as tar:
        ti = tarfile.TarInfo(name="state/executions.json")
        data = b'[]'
        ti.size = len(data)
        tar.addfile(ti, io.BytesIO(data))
    enc_no_m = backup_dir / "no_manifest.tar.gz.enc"
    enc_no_m.write_bytes(fernet.encrypt(buf_no_m.getvalue()))

    with pytest.raises(RuntimeError, match="MANIFEST.json missing"):
        restore_state.restore_backup(enc_no_m, target_state, None, key_backup)

    # 3. Extra unlisted member
    buf_extra = io.BytesIO()
    with tarfile.open(fileobj=buf_extra, mode="w:gz") as tar:
        ti = tarfile.TarInfo(name="state/unlisted.json")
        data = b'[]'
        ti.size = len(data)
        tar.addfile(ti, io.BytesIO(data))
        m_raw = json.dumps({"files": []}).encode("utf-8")
        ti_m = tarfile.TarInfo(name="MANIFEST.json")
        ti_m.size = len(m_raw)
        tar.addfile(ti_m, io.BytesIO(m_raw))
    enc_extra = backup_dir / "extra_member.tar.gz.enc"
    enc_extra.write_bytes(fernet.encrypt(buf_extra.getvalue()))

    with pytest.raises(RuntimeError, match="not found in MANIFEST.json"):
        restore_state.restore_backup(enc_extra, target_state, None, key_backup)


def test_cli_exit_codes(sample_state_3files, keys):
    """
    Test 7:
    CLI runner: returns non-zero on failure, returns 0 on success.
    """
    state_dir, db_path, backup_dir = sample_state_3files
    key_backup, key_secret = keys

    archive = backup_state.create_backup(
        state_dir=state_dir,
        db_path=db_path,
        backup_dir=backup_dir,
        backup_key=key_backup,
        secret_store_key=key_secret,
    )

    target_state = backup_dir / "cli_target_state"
    target_state.mkdir()

    # Success run with --force
    cmd_success = [
        sys.executable,
        str(SCRIPTS_DIR / "restore_state.py"),
        str(archive),
        "--target-state-dir",
        str(target_state),
        "--key",
        key_backup,
        "--force",
    ]
    proc = subprocess.run(cmd_success, capture_output=True, text=True)
    assert proc.returncode == 0, f"CLI restore failed: {proc.stderr}"
    assert "MANDATORY OPERATOR WARNING" in proc.stdout

    # Failure run with invalid key
    cmd_failure = [
        sys.executable,
        str(SCRIPTS_DIR / "restore_state.py"),
        str(archive),
        "--target-state-dir",
        str(target_state),
        "--key",
        "invalid_key_12345",
        "--force",
    ]
    proc_fail = subprocess.run(cmd_failure, capture_output=True, text=True)
    assert proc_fail.returncode != 0

