#!/usr/bin/env python3
"""
scripts/backup_state.py — Encrypted state & SQLite backup with retention pruning (§79 Phase E).

Snapshots:
1. STATE_DIR/*.json (executions.json, commits.json, kill_switch.json, etc.):
   Validates JSON before copying.
2. SQLite database:
   Uses sqlite3.Connection.backup() API (never copies raw live WAL files).
   Skips if USE_SUPABASE_DB=1.
3. Excludes secrets via strict denylist (wallet keypairs, secret store keys, sidecars).
4. Packages into tar.gz with MANIFEST.json (SHA-256 hashes, sizes, git revision).
5. Encrypts archive using Fernet with BACKUP_ENCRYPTION_KEY (must differ from SECRET_STORE_KEY).
6. Retention pruning: preserves the newest 14 daily + 8 weekly archives in BACKUP_DIR.

Usage:
    python scripts/backup_state.py [--state-dir DIR] [--db-path PATH] [--backup-dir DIR]
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from typing import Any, Optional

from cryptography.fernet import Fernet

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backup")

DENYLIST_SUBSTRINGS = (
    "wallet_keypair",
    "keypair",
    "secret_store",
    "sidecar",
    ".key",
    ".pem",
    ".env",
)


def is_denylisted(filename: str) -> bool:
    """Return True if filename matches any secret or key pattern."""
    fn_lower = filename.lower()
    return any(pattern in fn_lower for pattern in DENYLIST_SUBSTRINGS)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def get_git_revision() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except Exception:
        return "unknown"


def prune_retention(backup_dir: Path, now_dt: Optional[datetime] = None) -> list[Path]:
    """
    Retain the newest archive for each of the last 14 distinct calendar days
    and newest for each of the last 8 distinct calendar weeks (Sunday to Saturday).
    Deletes older archives. Returns list of deleted paths.
    """
    files = sorted(backup_dir.glob("state-*.tar.gz.enc"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return []

    kept: set[Path] = set()
    daily_seen: set[str] = set()
    weekly_seen: set[str] = set()

    for p in files:
        mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        day_key = mtime.strftime("%Y-%m-%d")
        week_key = mtime.strftime("%Y-W%U")

        keep_this = False
        if len(daily_seen) < 14 and day_key not in daily_seen:
            daily_seen.add(day_key)
            keep_this = True

        if len(weekly_seen) < 8 and week_key not in weekly_seen:
            weekly_seen.add(week_key)
            keep_this = True

        if keep_this:
            kept.add(p)

    deleted = []
    for p in files:
        if p not in kept:
            try:
                p.unlink()
                deleted.append(p)
                log.info("Retention prune deleted: %s", p.name)
            except OSError as e:
                log.warning("Failed to prune %s: %s", p, e)
    return deleted


def create_backup(
    state_dir: Path,
    db_path: Optional[Path],
    backup_dir: Path,
    backup_key: str,
    secret_store_key: Optional[str] = None,
    now_dt: Optional[datetime] = None,
) -> Path:
    """Create an encrypted state and DB backup archive."""
    if not backup_key or not backup_key.strip():
        raise RuntimeError("BACKUP_ENCRYPTION_KEY is unset or empty; refusing unencrypted backup")

    backup_key_clean = backup_key.strip()
    if secret_store_key and backup_key_clean == secret_store_key.strip():
        raise RuntimeError("BACKUP_ENCRYPTION_KEY must NOT match SECRET_STORE_KEY; separate keys required")

    fernet = Fernet(backup_key_clean.encode("utf-8"))

    dt = now_dt or datetime.now(timezone.utc)
    ts_str = dt.strftime("%Y%m%dT%H%M%SZ")

    manifest_files = []
    tar_buf = io.BytesIO()

    with tarfile.open(fileobj=tar_buf, mode="w:gz") as tar:
        # 1. State files
        if state_dir.is_dir():
            for p in sorted(state_dir.glob("*.json")):
                if is_denylisted(p.name):
                    log.warning("Denylist skipping secret file: %s", p.name)
                    continue
                try:
                    raw_text = p.read_text(encoding="utf-8")
                    json.loads(raw_text)  # Validate JSON parses
                    data = raw_text.encode("utf-8")
                except Exception as e:
                    # Fail closed: silently skipping a corrupt state file
                    # produces a "successful" backup with holes in the book.
                    raise RuntimeError(
                        f"state file {p} is unreadable or invalid JSON ({e}) — "
                        f"refusing to write an incomplete backup"
                    ) from e

                arcname = f"state/{p.name}"
                ti = tarfile.TarInfo(name=arcname)
                ti.size = len(data)
                ti.mtime = int(p.stat().st_mtime)
                tar.addfile(ti, io.BytesIO(data))

                manifest_files.append({
                    "name": arcname,
                    "size": len(data),
                    "sha256": sha256_bytes(data),
                })

        # 2. SQLite Database backup
        use_supabase = os.getenv("USE_SUPABASE_DB", "").lower() in ("1", "true", "yes")
        if use_supabase:
            log.info("USE_SUPABASE_DB=1: skipping local SQLite backup (managed by Supabase)")
        elif db_path is not None and not db_path.is_file():
            # §83: a backup that silently lacks the book is worse than a
            # failed backup. The old code skipped a missing DB without a
            # word — an archive with no tick history looked "SUCCESS".
            raise RuntimeError(
                f"database file not found at '{db_path}' — refusing to write "
                f"a backup without the book's SQLite database. Pass an "
                f"explicit --db-path or set DB_PATH so it matches the app "
                f"(default backend/trading_bot.db)."
            )
        elif db_path and db_path.is_file():
            if is_denylisted(db_path.name):
                log.warning("Denylist skipping DB: %s", db_path.name)
            else:
                src_conn = sqlite3.connect(str(db_path))
                try:
                    # Write a consistent raw SQLite image via the backup API
                    # (never copy live WAL files).
                    temp_backup_path = backup_dir / f".tmp_db_{ts_str}.sqlite"
                    temp_backup_path.parent.mkdir(parents=True, exist_ok=True)
                    file_dst = sqlite3.connect(str(temp_backup_path))
                    src_conn.backup(file_dst)
                    file_dst.close()

                    db_data = temp_backup_path.read_bytes()
                    temp_backup_path.unlink(missing_ok=True)

                    arcname = "trading_bot.db"
                    ti = tarfile.TarInfo(name=arcname)
                    ti.size = len(db_data)
                    ti.mtime = int(dt.timestamp())
                    tar.addfile(ti, io.BytesIO(db_data))

                    manifest_files.append({
                        "name": arcname,
                        "size": len(db_data),
                        "sha256": sha256_bytes(db_data),
                    })
                finally:
                    src_conn.close()

        # 3. MANIFEST.json
        manifest = {
            "created_at_utc": dt.isoformat(),
            "git_revision": get_git_revision(),
            "supabase_db_skipped": use_supabase,
            "files": manifest_files,
        }
        manifest_raw = json.dumps(manifest, indent=2).encode("utf-8")
        m_ti = tarfile.TarInfo(name="MANIFEST.json")
        m_ti.size = len(manifest_raw)
        m_ti.mtime = int(dt.timestamp())
        tar.addfile(m_ti, io.BytesIO(manifest_raw))

    tar_bytes = tar_buf.getvalue()
    encrypted_bytes = fernet.encrypt(tar_bytes)

    backup_dir.mkdir(parents=True, exist_ok=True)
    out_file = backup_dir / f"state-{ts_str}.tar.gz.enc"
    tmp_out = out_file.with_suffix(".tmp")
    tmp_out.write_bytes(encrypted_bytes)
    tmp_out.replace(out_file)

    log.info("Backup successfully written: %s (%d bytes)", out_file, len(encrypted_bytes))

    # 4. Prune retention
    prune_retention(backup_dir, dt)

    return out_file


def _normalize_argv(argv: list[str]) -> list[str]:
    """Attach the value to ``--key`` so Fernet keys starting with ``-`` parse.

    argparse treats a token beginning with ``-`` as an option, so a valid
    urlsafe-base64 Fernet key such as ``-opUFe...`` was rejected with
    "expected one argument". The value is known to belong to ``--key``
    whenever it is not already in ``--key=VALUE`` form.
    """
    out: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--key" and i + 1 < len(argv):
            out.append(f"--key={argv[i + 1]}")
            i += 2
        else:
            out.append(argv[i])
            i += 1
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Create encrypted state backup")
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--db-path", type=Path, default=None)
    parser.add_argument("--backup-dir", type=Path, default=None)
    parser.add_argument("--key", type=str, default=None)
    args = parser.parse_args(_normalize_argv(sys.argv[1:]))

    # Resolve default paths
    backend_dir = Path(__file__).resolve().parent.parent / "backend"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))

    state_dir = args.state_dir
    if not state_dir:
        try:
            from live_execution import config as live_config
            state_dir = Path(live_config.STATE_DIR)
        except Exception:
            state_dir = Path("backend/live_execution/state")

    db_path = args.db_path
    if not db_path:
        # §83: single source of truth — the app resolves DB_PATH as
        # backend/ + $DB_PATH (backend/config.py) AND loads .env. This
        # script used to fall back to CWD-relative data/trading_bot.db with
        # no dotenv, so a host-run backup silently archived NO database.
        try:
            import config as backend_config
            db_path = Path(backend_config.DB_PATH)
        except Exception:
            db_path = backend_dir / os.getenv("DB_PATH", "trading_bot.db")

    backup_dir = args.backup_dir or Path(os.getenv("BACKUP_DIR", "/app/backups"))
    backup_key = args.key or os.getenv("BACKUP_ENCRYPTION_KEY", "")
    secret_store_key = os.getenv("SECRET_STORE_KEY", "")

    try:
        archive_path = create_backup(
            state_dir=state_dir,
            db_path=db_path,
            backup_dir=backup_dir,
            backup_key=backup_key,
            secret_store_key=secret_store_key,
        )
        print(f"SUCCESS: {archive_path}")
        return 0
    except Exception as e:
        log.error("Backup failed: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())

