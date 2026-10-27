"""
backend/tests/test_audit_79_e.py — Tests for backup and restore capabilities (§79 Phase E).

Covers:
1. Round-trip in temp dir (backup -> restore -> equal JSON & DB integrity check).
2. Tampered archive refuses (decryption failure or hash/manifest mismatch).
3. Wrong decryption key refuses.
4. Refusal when BACKUP_ENCRYPTION_KEY is missing or identical to SECRET_STORE_KEY.
5. Secret files excluded via denylist.
6. Non-empty target directory refused unless --force is specified.
7. Live SQLite with active connection / WAL mode backs up consistently.
8. Retention pruning keeps 14 daily + 8 weekly archives across fake dates.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tarfile
import pytest

from cryptography.fernet import Fernet

# Ensure scripts and backend are importable
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import backup_state
import restore_state


@pytest.fixture
def keys():
    key_backup = Fernet.generate_key().decode("utf-8")
    key_secret = Fernet.generate_key().decode("utf-8")
    return key_backup, key_secret


@pytest.fixture
def sample_state(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "executions.json").write_text(json.dumps([{"id": "exec1", "usd_size": 10.0}]), encoding="utf-8")
    (state_dir / "commits.json").write_text(json.dumps([{"id": "commit1", "symbol": "BONK"}]), encoding="utf-8")
    (state_dir / "kill_switch.json").write_text(json.dumps({"latched": False}), encoding="utf-8")

    db_path = tmp_path / "trading_bot.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE candidates (id TEXT PRIMARY KEY, symbol TEXT);")
    conn.execute("INSERT INTO candidates VALUES ('c1', 'SOL');")
    conn.commit()
    conn.close()

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    return state_dir, db_path, backup_dir


def test_backup_restore_round_trip(sample_state, keys):
    """
    Round-trip verification:
    State JSONs and SQLite DB are snapshotted, encrypted, restored to a fresh target,
    and verified for JSON equality and SQLite PRAGMA integrity_check.
    """
    state_dir, db_path, backup_dir = sample_state
    key_backup, key_secret = keys

    archive = backup_state.create_backup(
        state_dir=state_dir,
        db_path=db_path,
        backup_dir=backup_dir,
        backup_key=key_backup,
        secret_store_key=key_secret,
    )
    assert archive.is_file()

    target_state = backup_dir / "restored_state"
    target_db = backup_dir / "restored_db.sqlite"

    res = restore_state.restore_backup(
        archive_path=archive,
        target_state_dir=target_state,
        target_db_path=target_db,
        backup_key=key_backup,
    )

    assert len(res["restored_files"]) >= 4

    # Verify JSON content matches
    orig_exec = json.loads((state_dir / "executions.json").read_text(encoding="utf-8"))
    rest_exec = json.loads((target_state / "executions.json").read_text(encoding="utf-8"))
    assert orig_exec == rest_exec

    orig_commits = json.loads((state_dir / "commits.json").read_text(encoding="utf-8"))
    rest_commits = json.loads((target_state / "commits.json").read_text(encoding="utf-8"))
    assert orig_commits == rest_commits

    # Verify DB content and integrity
    conn = sqlite3.connect(str(target_db))
    cur = conn.cursor()
    cur.execute("PRAGMA integrity_check;")
    assert cur.fetchall() == [("ok",)]

    cur.execute("SELECT symbol FROM candidates WHERE id = 'c1';")
    assert cur.fetchone() == ("SOL",)
    conn.close()


def test_tampered_archive_refused(sample_state, keys):
    """
    Mutation check: If line `fernet.decrypt(...)` or `sha256 mismatch` check
    in restore_state.py is removed, corrupted or forged archives would be accepted.
    """
    state_dir, db_path, backup_dir = sample_state
    key_backup, key_secret = keys

    archive = backup_state.create_backup(
        state_dir=state_dir,
        db_path=db_path,
        backup_dir=backup_dir,
        backup_key=key_backup,
        secret_store_key=key_secret,
    )

    # 1. Flip raw bytes -> decryption failure
    raw = archive.read_bytes()
    tampered_bytes = raw[:50] + b"\x00\xff" + raw[52:]
    tampered_archive = backup_dir / "tampered.tar.gz.enc"
    tampered_archive.write_bytes(tampered_bytes)

    target_state = backup_dir / "restored_state_tampered"
    with pytest.raises(RuntimeError, match="Archive decryption failed"):
        restore_state.restore_backup(
            archive_path=tampered_archive,
            target_state_dir=target_state,
            target_db_path=None,
            backup_key=key_backup,
        )

    # 2. Tampered content inside valid Fernet envelope (hash mismatch)
    fernet = Fernet(key_backup.encode("utf-8"))
    decrypted_tar = fernet.decrypt(raw)
    in_buf = io.BytesIO(decrypted_tar)
    out_buf = io.BytesIO()

    with tarfile.open(fileobj=in_buf, mode="r:gz") as tin, tarfile.open(fileobj=out_buf, mode="w:gz") as tout:
        for m in tin.getmembers():
            f = tin.extractfile(m)
            data = f.read() if f else b""
            if m.name == "state/executions.json":
                data = b'[{"forged": true}]'
            ti = tarfile.TarInfo(name=m.name)
            ti.size = len(data)
            tout.addfile(ti, io.BytesIO(data))

    tampered_inner = fernet.encrypt(out_buf.getvalue())
    tampered_inner_archive = backup_dir / "tampered_inner.tar.gz.enc"
    tampered_inner_archive.write_bytes(tampered_inner)

    with pytest.raises(RuntimeError, match="mismatch for 'state/executions.json'"):
        restore_state.restore_backup(
            archive_path=tampered_inner_archive,
            target_state_dir=target_state,
            target_db_path=None,
            backup_key=key_backup,
        )


def test_wrong_key_refused(sample_state, keys):
    """
    Mutation check: If Fernet key check is removed, any key would attempt
    decryption and crash unhandled or permit wrong keys.
    """
    state_dir, db_path, backup_dir = sample_state
    key_backup, key_secret = keys
    other_key = Fernet.generate_key().decode("utf-8")

    archive = backup_state.create_backup(
        state_dir=state_dir,
        db_path=db_path,
        backup_dir=backup_dir,
        backup_key=key_backup,
        secret_store_key=key_secret,
    )

    target_state = backup_dir / "restored_state_wrong_key"
    with pytest.raises(RuntimeError, match="Archive decryption failed"):
        restore_state.restore_backup(
            archive_path=archive,
            target_state_dir=target_state,
            target_db_path=None,
            backup_key=other_key,
        )


def test_key_separation_and_missing_key_refusal(sample_state, keys):
    """
    Refusal rules:
    - Empty or unset BACKUP_ENCRYPTION_KEY must fail closed.
    - BACKUP_ENCRYPTION_KEY == SECRET_STORE_KEY must be refused.
    Mutation check: Removing `if backup_key_clean == secret_store_key.strip(): raise`
    allows reuse of secret store keys for backups.
    """
    state_dir, db_path, backup_dir = sample_state
    key_backup, _ = keys

    # Missing key
    with pytest.raises(RuntimeError, match="BACKUP_ENCRYPTION_KEY is unset or empty"):
        backup_state.create_backup(
            state_dir=state_dir,
            db_path=db_path,
            backup_dir=backup_dir,
            backup_key="",
        )

    # Identical keys
    with pytest.raises(RuntimeError, match="BACKUP_ENCRYPTION_KEY must NOT match SECRET_STORE_KEY"):
        backup_state.create_backup(
            state_dir=state_dir,
            db_path=db_path,
            backup_dir=backup_dir,
            backup_key=key_backup,
            secret_store_key=key_backup,
        )


def test_secret_files_excluded_from_archive(sample_state, keys):
    """
    Denylist check:
    Wallet keypairs, secret keys, sidecars, and .env files in state dir
    must NEVER be packaged into the backup archive.
    Mutation check: If `if is_denylisted(p.name): continue` in backup_state.py
    is removed, sensitive keypairs are archived.
    """
    state_dir, db_path, backup_dir = sample_state
    key_backup, key_secret = keys

    # Plant sensitive files in state dir
    (state_dir / "wallet_keypair.json").write_text('{"secret": "sensitive_wallet_key"}', encoding="utf-8")
    (state_dir / "secret_store.key").write_text("sensitivestorekey123", encoding="utf-8")
    (state_dir / "privy_sidecar.json").write_text('{"token": "secret"}', encoding="utf-8")
    (state_dir / "prod.env.json").write_text('{"env": "secret"}', encoding="utf-8")
    (state_dir / "private.pem").write_text("---PEM---", encoding="utf-8")

    archive = backup_state.create_backup(
        state_dir=state_dir,
        db_path=db_path,
        backup_dir=backup_dir,
        backup_key=key_backup,
        secret_store_key=key_secret,
    )

    fernet = Fernet(key_backup.encode("utf-8"))
    tar_bytes = fernet.decrypt(archive.read_bytes())

    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
        names = tar.getnames()
        manifest_f = tar.extractfile("MANIFEST.json")
        assert manifest_f is not None
        manifest = json.loads(manifest_f.read().decode("utf-8"))

    # Assert no denylisted filenames in tar members
    for name in names:
        assert "wallet_keypair" not in name.lower()
        assert "secret_store" not in name.lower()
        assert "sidecar" not in name.lower()
        assert ".key" not in name.lower()
        assert ".pem" not in name.lower()
        assert ".env" not in name.lower()

    # Assert no denylisted filenames in MANIFEST.json
    for f in manifest["files"]:
        assert not backup_state.is_denylisted(f["name"])


def test_restore_refuses_non_empty_target_without_force(sample_state, keys):
    """
    Safety rule:
    Refuse to restore into an existing non-empty directory unless --force is given.
    Mutation check: If `if target_state_dir.is_dir() and any(...): raise` is removed,
    existing states will be silently overwritten.
    """
    state_dir, db_path, backup_dir = sample_state
    key_backup, key_secret = keys

    archive = backup_state.create_backup(
        state_dir=state_dir,
        db_path=db_path,
        backup_dir=backup_dir,
        backup_key=key_backup,
        secret_store_key=key_secret,
    )

    target_dir = backup_dir / "occupied_state"
    target_dir.mkdir()
    (target_dir / "existing.txt").write_text("don't overwrite me", encoding="utf-8")

    # Without force -> refuses
    with pytest.raises(RuntimeError, match="is not empty; use --force to overwrite"):
        restore_state.restore_backup(
            archive_path=archive,
            target_state_dir=target_dir,
            target_db_path=None,
            backup_key=key_backup,
            force=False,
        )

    # With force -> succeeds
    res = restore_state.restore_backup(
        archive_path=archive,
        target_state_dir=target_dir,
        target_db_path=None,
        backup_key=key_backup,
        force=True,
    )
    assert len(res["restored_files"]) >= 3


def test_live_sqlite_open_writer_backup_consistency(tmp_path, keys):
    """
    Consistency test:
    An active SQLite connection with WAL mode and pending/uncommitted records
    is safely backed up using sqlite3.Connection.backup() without corruption.
    """
    key_backup, key_secret = keys
    db_path = tmp_path / "live_wal.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT);")
    conn.execute("INSERT INTO orders VALUES (1, 'FILLED');")
    conn.commit()

    # Leave a reader/writer open during backup
    conn2 = sqlite3.connect(str(db_path))
    conn2.execute("INSERT INTO orders VALUES (2, 'PENDING');")
    conn2.commit()

    state_dir = tmp_path / "empty_state"
    state_dir.mkdir()
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    archive = backup_state.create_backup(
        state_dir=state_dir,
        db_path=db_path,
        backup_dir=backup_dir,
        backup_key=key_backup,
        secret_store_key=key_secret,
    )

    conn.close()
    conn2.close()

    restored_db = tmp_path / "restored_wal.db"
    restore_state.restore_backup(
        archive_path=archive,
        target_state_dir=tmp_path / "restored_state",
        target_db_path=restored_db,
        backup_key=key_backup,
    )

    r_conn = sqlite3.connect(str(restored_db))
    cur = r_conn.cursor()
    cur.execute("PRAGMA integrity_check;")
    assert cur.fetchall() == [("ok",)]
    cur.execute("SELECT COUNT(*) FROM orders;")
    assert cur.fetchone()[0] == 2
    r_conn.close()


def test_retention_pruning_daily_and_weekly(tmp_path):
    """
    Retention policy test:
    Keeps newest for up to 14 distinct calendar days and 8 distinct calendar weeks.
    Prunes older archives.
    """
    backup_dir = tmp_path / "retention_backups"
    backup_dir.mkdir()

    base_time = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)

    # Generate 40 daily files going back 40 days
    created_files = []
    for days_ago in range(40):
        dt = base_time - timedelta(days=days_ago)
        ts_str = dt.strftime("%Y%m%dT%H%M%SZ")
        p = backup_dir / f"state-{ts_str}.tar.gz.enc"
        p.write_bytes(b"dummy")
        # Set file mtime to fake historical time
        os.utime(p, (dt.timestamp(), dt.timestamp()))
        created_files.append(p)

    deleted = backup_state.prune_retention(backup_dir, now_dt=base_time)

    remaining = sorted(backup_dir.glob("state-*.tar.gz.enc"))
    # 14 days will cover first 14 days.
    # 8 weeks will cover some additional older weeks.
    # Total kept should be strictly less than 40 and >= 14
    assert len(deleted) > 0
    assert 14 <= len(remaining) < 40

    # Oldest file (40 days ago) should be pruned
    oldest = created_files[-1]
    assert oldest not in remaining
    assert oldest in deleted


# ---------------------------------------------------------------------------
# §83: backup must never silently omit the database
# ---------------------------------------------------------------------------

def test_create_backup_refuses_missing_db(sample_state, keys):
    """A missing DB used to be skipped without a word — the archive looked
    like a SUCCESS while containing no tick history at all."""
    state_dir, _, backup_dir = sample_state
    key_backup, key_secret = keys
    with pytest.raises(RuntimeError, match="database file not found"):
        backup_state.create_backup(
            state_dir=state_dir,
            db_path=state_dir.parent / "does-not-exist.db",
            backup_dir=backup_dir,
            backup_key=key_backup,
            secret_store_key=key_secret,
        )


def test_backup_cli_resolves_db_path_through_backend_config(
        tmp_path, monkeypatch):
    """Bare `python scripts/backup_state.py` must resolve the SAME DB the app
    uses (backend/config.DB_PATH), not a CWD-relative data/trading_bot.db,
    and the script must load .env like the app does."""
    captured = {}

    def fake_create_backup(state_dir, db_path, backup_dir, backup_key,
                           secret_store_key=None, now_dt=None):
        captured["db_path"] = db_path
        raise RuntimeError("stop-after-capture")

    monkeypatch.setattr(backup_state, "create_backup", fake_create_backup)
    monkeypatch.setattr(sys, "argv", [
        "backup_state.py", "--state-dir", str(tmp_path / "state")])
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "irrelevant-for-capture")

    rc = backup_state.main()
    assert rc == 1  # create_backup's sentinel error is handled by main

    import config as backend_config
    assert captured["db_path"] == Path(backend_config.DB_PATH)


# ---------------------------------------------------------------------------
# §83: restore must not leave/copy stale SQLite sidecars
# ---------------------------------------------------------------------------

def test_restore_removes_stale_sqlite_sidecars(sample_state, keys, tmp_path):
    """A promoted DB must not sit next to -wal/-shm/-journal files that
    belong to the pre-restore lineage (SQLite could try to recover a foreign
    WAL against the restored file)."""
    state_dir, db_path, backup_dir = sample_state
    key_backup, _ = keys
    archive = backup_state.create_backup(
        state_dir=state_dir, db_path=db_path, backup_dir=backup_dir,
        backup_key=key_backup)

    target_db = tmp_path / "live" / "trading_bot.db"
    target_db.parent.mkdir(parents=True)
    seed = sqlite3.connect(str(target_db))
    seed.execute("CREATE TABLE old (x TEXT);")
    seed.commit()
    seed.close()

    sidecars = [Path(str(target_db) + s)
                for s in ("-wal", "-shm", "-journal")]
    for s in sidecars:
        s.write_bytes(b"stale sidecar")

    restore_state.restore_backup(
        archive_path=archive,
        target_state_dir=tmp_path / "restored_state",
        target_db_path=target_db,
        backup_key=key_backup,
        force=True,
    )

    for s in sidecars:
        assert not s.exists(), f"stale sidecar survived restore: {s}"


def test_pre_restore_snapshot_includes_uncheckpointed_wal_rows(
        sample_state, keys, tmp_path):
    """The rollback snapshot is advertised as "Existing state preserved",
    but a raw copy2 of the main file loses committed rows still living in
    the -wal sidecar (WAL mode). The snapshot must go through SQLite."""
    state_dir, db_path, backup_dir = sample_state
    key_backup, _ = keys
    archive = backup_state.create_backup(
        state_dir=state_dir, db_path=db_path, backup_dir=backup_dir,
        backup_key=key_backup)

    target_dir = tmp_path / "live"
    target_dir.mkdir()
    target_db = target_dir / "trading_bot.db"
    conn = sqlite3.connect(str(target_db))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("CREATE TABLE orders (id TEXT PRIMARY KEY);")
    conn.execute("INSERT INTO orders VALUES ('wal-only-row');")
    conn.commit()
    # Connection stays OPEN: the committed row lives in the -wal sidecar and
    # the main DB file alone does not contain it.
    assert Path(str(target_db) + "-wal").exists()

    try:
        restore_state.restore_backup(
            archive_path=archive,
            target_state_dir=tmp_path / "restored_state",
            target_db_path=target_db,
            backup_key=key_backup,
            force=True,
        )
    finally:
        conn.close()

    pre_dirs = list((tmp_path / "restored_state").glob(".pre-restore-*"))
    assert len(pre_dirs) == 1
    snapshot = pre_dirs[0] / "trading_bot.db"
    assert snapshot.is_file()

    sconn = sqlite3.connect(str(snapshot))
    try:
        count = sconn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    finally:
        sconn.close()
    assert count == 1, "pre-restore snapshot lost the uncheckpointed WAL row"



def test_backup_refuses_corrupt_state_file(tmp_path, keys):
    """A corrupt state JSON must abort the backup — silently omitting it
    would produce a 'successful' archive missing part of the book."""
    key_backup, key_secret = keys
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "executions.json").write_text("{not valid json", encoding="utf-8")
    db_path = tmp_path / "trading_bot.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (id INTEGER);")
    conn.commit()
    conn.close()
    backup_dir = tmp_path / "backups"

    with pytest.raises(RuntimeError, match="unreadable or invalid JSON"):
        backup_state.create_backup(
            state_dir=state_dir,
            db_path=db_path,
            backup_dir=backup_dir,
            backup_key=key_backup,
            secret_store_key=key_secret,
        )


def test_restore_without_target_db_warns_loudly(sample_state, keys, capsys):
    """A DB-bearing archive restored without --target-db-path must say the
    database was skipped instead of looking like a complete restore."""
    state_dir, db_path, backup_dir = sample_state
    key_backup, key_secret = keys
    archive = backup_state.create_backup(
        state_dir=state_dir, db_path=db_path, backup_dir=backup_dir,
        backup_key=key_backup, secret_store_key=key_secret,
    )
    target_state = backup_dir / "state_only_restore"
    target_state.mkdir()

    restore_state.restore_backup(
        archive_path=archive, target_state_dir=target_state,
        target_db_path=None, backup_key=key_backup, force=True,
    )

    out = capsys.readouterr().out
    assert "DATABASE WAS NOT RESTORED" in out
