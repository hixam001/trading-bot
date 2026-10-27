#!/usr/bin/env python3
"""
scripts/restore_state.py — Decrypt, verify, stage, and safely restore state & SQLite backup (§79 Phase E, §81 Phase E).

Multi-pass restore:
1. Decrypt archive using Fernet with BACKUP_ENCRYPTION_KEY.
2. Pass 1 (In-Memory Validation):
   - Path safety: Reject symlinks, hardlinks, devices, directories; reject absolute paths or traversal '..'
   - Allowed members: MANIFEST.json, trading_bot.db, or state/*
   - Verify every member exists in MANIFEST.json (and all manifest files exist in archive)
   - Verify file sizes, SHA-256 hashes, and JSON validity in memory.
   - Target is 100% untouched during Pass 1.
3. Pre-restore backup:
   - If --force is specified and target is non-empty, copy existing target files into <target_state_dir>/.pre-restore-<UTC timestamp>/.
4. Pass 2 (Atomic Staging & SQLite Integrity):
   - Write files to .staged.<pid>.* in the destination directory, fsync.
   - For SQLite database, run PRAGMA integrity_check ON THE STAGED COPY.
   - If integrity check or staging fails, all staged files are deleted.
5. Pass 3 (Promotion):
   - Atomically rename staged files to final names using os.replace().
6. Pass 4 (Kill Switch Safety Latch):
   - Always trip the kill switch with:
     "restored from <archive_name> at <UTC ts>; run reconcile and clear manually"
   - Overwrites any kill_switch.json that was restored.
   - Print mandatory operator warning and clear instructions.
7. Optional --reconcile: run read-only on-chain reconciliation check.

Exit codes:
- 0 on success (after promote and trip)
- Non-zero on any failure with one-line reason
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tarfile
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken

# Ensure backend package is importable
backend_dir = Path(__file__).resolve().parent.parent / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("restore")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_member_path(member: tarfile.TarInfo, target_state_dir: Path) -> None:
    """Validate archive member path against directory traversal and dangerous file types."""
    if not member.isfile():
        raise RuntimeError(f"Archive member '{member.name}' is not a regular file (type={member.type!r})")

    name = member.name
    # Reject absolute paths or Windows drive letters
    if name.startswith("/") or name.startswith("\\") or (len(name) > 1 and name[1] == ":"):
        raise RuntimeError(f"Archive member '{name}' has an absolute path")

    parts = Path(name).parts
    if ".." in parts:
        raise RuntimeError(f"Archive member '{name}' contains directory traversal components")

    if name == "MANIFEST.json" or name == "trading_bot.db":
        return
    elif name.startswith("state/") and len(name) > len("state/"):
        rel_name = name[len("state/"):]
        target_resolved = target_state_dir.resolve()
        dest = (target_state_dir / rel_name).resolve()
        try:
            dest.relative_to(target_resolved)
        except ValueError:
            raise RuntimeError(f"Archive member '{name}' escapes target state directory")
        return
    else:
        raise RuntimeError(f"Unauthorized member name in archive: '{name}'")


def _remove_sqlite_sidecars(db_path: Optional[Path]) -> None:
    """Delete stray -wal/-shm/-journal sidecars beside db_path (§83).

    api/db.py runs the book in WAL mode, so promoted/rolled-back DB files
    can sit next to sidecars that belong to a DIFFERENT database lineage.
    SQLite may try to recover a foreign WAL; and after a raw copy2 a fresh
    DB inherits the old sidecar names. Best-effort and logged: a sidecar we
    cannot delete must not abort a restore that is otherwise complete.
    """
    if db_path is None:
        return
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            Path(str(db_path) + suffix).unlink(missing_ok=True)
        except OSError as exc:
            log.warning("Could not remove stale SQLite sidecar %s%s: %s",
                        db_path, suffix, exc)


def _attempt_rollback(pre_restore_dir: Path, target_state_dir: Path, target_db_path: Optional[Path]) -> None:
    """Best-effort atomic rollback of pre-restore backup files via temp files in target dir."""
    pid = os.getpid()
    staged_rollback: list[tuple[Path, Path]] = []
    try:
        for p in pre_restore_dir.iterdir():
            if not p.is_file():
                continue
            if target_db_path and p.name == target_db_path.name:
                dest = target_db_path
            else:
                dest = target_state_dir / p.name

            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.parent / f".rollback.{pid}.{dest.name}"
            shutil.copy2(p, tmp)
            staged_rollback.append((tmp, dest))

        for tmp, dest in staged_rollback:
            if target_db_path is not None and dest == target_db_path:
                _remove_sqlite_sidecars(target_db_path)
            os.replace(tmp, dest)
        log.info("Automatic rollback succeeded from %s", pre_restore_dir)
        print(f"ROLLBACK SUCCESS: Restored pre-restore state from {pre_restore_dir}")
    except Exception as rb_exc:
        log.critical("Automatic rollback failed: %s", rb_exc)
        print(f"ROLLBACK FAILED: {rb_exc}", file=sys.stderr)
        for tmp, _ in staged_rollback:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
        raise


def restore_backup(
    archive_path: Path,
    target_state_dir: Path,
    target_db_path: Optional[Path],
    backup_key: str,
    force: bool = False,
) -> dict[str, Any]:
    """
    Safely decrypts, validates, stages, and restores state and database files.
    Always trips the kill switch upon completion.
    """
    if not backup_key or not backup_key.strip():
        raise RuntimeError("BACKUP_ENCRYPTION_KEY is unset or empty; refusing restore")

    if not archive_path.is_file():
        raise FileNotFoundError(f"Backup archive not found: {archive_path}")

    # 1. Decrypt archive in-memory
    encrypted_bytes = archive_path.read_bytes()
    try:
        fernet = Fernet(backup_key.strip().encode("utf-8"))
        decrypted_tar_bytes = fernet.decrypt(encrypted_bytes)
    except InvalidToken:
        raise RuntimeError("Archive decryption failed: InvalidToken (wrong key or corrupted data)")
    except Exception as e:
        raise RuntimeError(f"Archive decryption failed: {e}")

    try:
        tar = tarfile.open(fileobj=io.BytesIO(decrypted_tar_bytes), mode="r:gz")
    except Exception as e:
        raise RuntimeError(f"Corrupted tar.gz archive: {e}")

    # =========================================================================
    # PASS 1: In-Memory Validation (Nothing written to disk)
    # =========================================================================
    try:
        manifest_member = tar.getmember("MANIFEST.json")
        f = tar.extractfile(manifest_member)
        if f is None:
            raise RuntimeError("MANIFEST.json unreadable in archive")
        manifest_data = json.loads(f.read().decode("utf-8"))
    except KeyError:
        raise RuntimeError("MANIFEST.json missing from backup archive")
    except Exception as e:
        raise RuntimeError(f"Failed parsing MANIFEST.json: {e}")

    manifest_map = {item["name"]: item for item in manifest_data.get("files", [])}
    verified_data: dict[str, bytes] = {}

    for member in tar.getmembers():
        if member.name == "MANIFEST.json":
            continue

        validate_member_path(member, target_state_dir)

        if member.name not in manifest_map:
            raise RuntimeError(f"Archive member '{member.name}' not found in MANIFEST.json")

        expected_meta = manifest_map[member.name]
        f = tar.extractfile(member)
        if f is None:
            raise RuntimeError(f"Could not extract member '{member.name}'")
        data = f.read()

        # Size check
        if len(data) != expected_meta["size"]:
            raise RuntimeError(
                f"Size mismatch for '{member.name}': manifest={expected_meta['size']}, got={len(data)}"
            )

        # Hash check
        actual_hash = sha256_bytes(data)
        if actual_hash != expected_meta["sha256"]:
            raise RuntimeError(
                f"SHA-256 mismatch for '{member.name}': manifest={expected_meta['sha256']}, got={actual_hash}"
            )

        # JSON parse validation
        if member.name.endswith(".json"):
            try:
                json.loads(data.decode("utf-8"))
            except Exception as e:
                raise RuntimeError(f"Invalid JSON in '{member.name}': {e}")

        verified_data[member.name] = data

    # Verify that all files listed in manifest were actually in the archive
    for expected_name in manifest_map:
        if expected_name not in verified_data:
            raise RuntimeError(f"File '{expected_name}' listed in manifest missing from archive")

    tar.close()

    # Check target directory emptiness
    has_existing_state = target_state_dir.is_dir() and any(
        p for p in target_state_dir.iterdir() if not p.name.startswith(".pre-restore-")
    )
    has_existing_db = bool(target_db_path and target_db_path.is_file())

    if (has_existing_state or has_existing_db) and not force:
        raise RuntimeError(
            f"Target state directory '{target_state_dir}' is not empty; use --force to overwrite"
        )

    # =========================================================================
    # PRE-RESTORE BACKUP: Preserve what was there
    # =========================================================================
    pre_restore_dir: Optional[Path] = None
    if force and (has_existing_state or has_existing_db):
        now_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        pre_restore_dir = target_state_dir / f".pre-restore-{now_ts}"
        pre_restore_dir.mkdir(parents=True, exist_ok=True)

        if target_state_dir.is_dir():
            for p in target_state_dir.iterdir():
                if p.is_file() and not p.name.startswith(".pre-restore-") and not p.name.startswith(".staged."):
                    shutil.copy2(p, pre_restore_dir / p.name)

            if target_db_path and target_db_path.is_file():
                # §83: copy2 of the main file alone drops committed-but-not-
                # checkpointed rows living in the -wal sidecar (api/db.py runs
                # WAL mode), so the "Existing state preserved" rollback snapshot
                # could silently lose the newest trades. Take a consistent
                # snapshot through SQLite itself, exactly like backup_state.
                pre_db = pre_restore_dir / target_db_path.name
                try:
                    src_conn = sqlite3.connect(str(target_db_path))
                    try:
                        dst_conn = sqlite3.connect(str(pre_db))
                        try:
                            src_conn.backup(dst_conn)
                        finally:
                            dst_conn.close()
                    finally:
                        src_conn.close()
                except sqlite3.DatabaseError as exc:
                    # A non-SQLite file (foreign or corrupt) cannot be read
                    # through the backup API; preserve its raw bytes — the
                    # rollback path copies files back verbatim anyway. Any
                    # other error (locked, IO) stays fatal: never proceed to
                    # overwrite a real book we could not snapshot.
                    if "file is not a database" not in str(exc):
                        raise
                    shutil.copy2(target_db_path, pre_db)

        print(f"PRE-RESTORE BACKUP: Existing state preserved in {pre_restore_dir}")
        log.info("Pre-restore backup created at %s", pre_restore_dir)

    # =========================================================================
    # PASS 2: Staging & SQLite Integrity
    # =========================================================================
    pid = os.getpid()
    staged_files: list[tuple[Path, Path]] = []

    try:
        for name, data in verified_data.items():
            if name.startswith("state/"):
                rel_name = name[len("state/"):]
                dest_path = target_state_dir / rel_name
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                temp_path = dest_path.parent / f".staged.{pid}.{dest_path.name}"
                with open(temp_path, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                staged_files.append((temp_path, dest_path))

            elif name == "trading_bot.db":
                if target_db_path:
                    dest_path = target_db_path
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    temp_path = dest_path.parent / f".staged.{pid}.{dest_path.name}"
                    with open(temp_path, "wb") as f:
                        f.write(data)
                        f.flush()
                        os.fsync(f.fileno())

                    # Track immediately so cleanup will remove it on any failure
                    staged_files.append((temp_path, dest_path))

                    # PRAGMA integrity_check on the STAGED copy
                    conn = sqlite3.connect(str(temp_path))
                    try:
                        cur = conn.cursor()
                        try:
                            cur.execute("PRAGMA integrity_check;")
                            res = cur.fetchall()
                            if not res or res[0][0] != "ok":
                                raise RuntimeError(f"SQLite PRAGMA integrity_check failed: {res}")
                        except sqlite3.DatabaseError as db_err:
                            raise RuntimeError(f"SQLite PRAGMA integrity_check failed: {db_err}")
                    finally:
                        conn.close()
                else:
                    # Never let a DB-bearing archive look fully restored when
                    # the book was silently left behind.
                    print("WARNING: archive contains trading_bot.db but no "
                          "--target-db-path was given — the DATABASE WAS NOT "
                          "RESTORED. Re-run with --target-db-path <path> to "
                          "restore the book.")
                    log.warning("SQLite DB in archive skipped: no target DB path specified")

    except Exception as exc:
        # Clean up any staged files
        for temp_path, _ in staged_files:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass
        if pre_restore_dir:
            print(f"ROLLBACK NOTE: Restore failed. Existing state remains intact and was backed up to {pre_restore_dir}")
            log.warning("Restore failed during staging. Existing state was preserved in %s", pre_restore_dir)
        raise

    # =========================================================================
    # PASS 3: Promotion (os.replace)
    # =========================================================================
    restored_files: list[str] = []
    promoted: list[tuple[Path, Path]] = []
    try:
        for temp_path, dest_path in staged_files:
            if target_db_path is not None and dest_path == target_db_path:
                # §83: the restored DB replaces the whole lineage — stale
                # -wal/-shm/-journal sidecars from the pre-restore book must
                # not survive the swap.
                _remove_sqlite_sidecars(target_db_path)
            os.replace(temp_path, dest_path)
            promoted.append((temp_path, dest_path))
            restored_files.append(str(dest_path))
    except Exception as exc:
        for temp_path, _ in staged_files:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass

        if len(promoted) > 0:
            from live_execution import kill_switch
            trip_reason = (
                f"PARTIAL RESTORE: promoted {len(promoted)} of {len(staged_files)} files "
                f"from {archive_path.name}; state is inconsistent; do not arm"
            )
            try:
                kill_switch.trip(trip_reason, state_dir=target_state_dir)  # Pass 3 partial restore
            except Exception as ks_err:
                log.error("Failed to trip kill switch on partial restore: %s", ks_err)

            if pre_restore_dir and pre_restore_dir.is_dir():
                print(f"ROLLBACK NOTE: Partial restore failure ({len(promoted)}/{len(staged_files)} promoted). Attempting automatic rollback from {pre_restore_dir}")
                log.error("Partial restore failure. Attempting automatic rollback from %s", pre_restore_dir)
                try:
                    _attempt_rollback(pre_restore_dir, target_state_dir, target_db_path)
                except Exception as rb_err:
                    log.error("Automatic rollback failed: %s", rb_err)
            else:
                log.error("No pre-restore backup available for automatic rollback")
        else:
            if pre_restore_dir:
                print(f"ROLLBACK NOTE: Restore failed before any files promoted. Existing state remains intact in {pre_restore_dir}")
                log.warning("Restore failed before promotion. Existing state in %s", pre_restore_dir)
        raise

    # =========================================================================
    # PASS 4: Safety Latch (Always Trip Kill Switch)
    # =========================================================================
    from live_execution import kill_switch
    ts_str = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    trip_reason = f"restored from {archive_path.name} at {ts_str}; run reconcile and clear manually"
    kill_switch.trip(trip_reason, state_dir=target_state_dir)  # Pass 4 safety latch

    notice = f"""
================================================================================
MANDATORY OPERATOR WARNING: State restored from backup.
NEVER arm after restoring until reconcile reports clean.
The kill switch has been automatically ENGAGED.
Reason: {trip_reason}

To inspect state and clear the kill switch:
  1. Run reconciliation:
     python scripts/restore_state.py --reconcile-only --target-state-dir {target_state_dir}
  2. Clear the kill switch manually after verification:
     python backend/live_execution/scripts/confirm_trade.py resume
================================================================================
"""
    print(notice)
    log.info("Successfully restored %d file(s) from %s", len(restored_files), archive_path.name)

    return {
        "restored_files": restored_files,
        "manifest": manifest_data,
        "pre_restore_backup": str(pre_restore_dir) if pre_restore_dir else None,
    }


async def run_reconcile(state_dir: Path) -> dict[str, Any]:
    """Run read-only reconciliation check against chain truth."""
    log.info("Running read-only reconciliation check against chain...")
    meta: dict[str, Any] = {}
    executions_file = state_dir / "executions.json"

    if executions_file.is_file():
        try:
            data = json.loads(executions_file.read_text(encoding="utf-8"))
            # Real ledgers write {"records": [...]}; older fixtures wrote a
            # bare list. Anything else cannot be reconciled — fail loudly
            # instead of reporting a clean, empty book.
            records = data.get("records") if isinstance(data, dict) else data
            if not isinstance(records, list):
                raise ValueError("expected a record list or a {'records': [...]} object")
            open_statuses = {
                "recorded", "sent", "confirmed",       # ExecutionLedger._OPEN
                "OPEN", "PARTIAL", "open", "partial",  # legacy operators
            }
            for r in records:
                if not isinstance(r, dict):
                    raise ValueError("execution record is not an object")
                if r.get("kind") != "buy" or r.get("status") not in open_statuses:
                    continue
                mint = r.get("mint")
                if not mint:
                    raise ValueError("open buy record has no mint")
                tokens = float(r.get("tokens_out") or 0.0)
                cost = float(r.get("usd_size") or 0.0)
                if mint in meta:
                    meta[mint]["tokens"] += tokens
                    meta[mint]["cost"] += cost
                else:
                    meta[mint] = {
                        "tokens": tokens,
                        "cost": cost,
                        "price_usd": float(r.get("price_usd") or 0.0),
                        "opened_ts": float(r.get("ts") or 0.0),
                    }
        except Exception as e:
            log.error("Failed loading executions.json for reconcile: %s", e)
            return {"checked": False, "error": f"executions.json unreadable: {e}"}

    try:
        from live_execution import wallet, solana
        from live_execution.reconcile import reconcile

        try:
            payer = wallet.load_keypair()
            wallet_pubkey = wallet.pubkey_string(payer)
            balances = await solana.get_token_balances(wallet_pubkey)
        except Exception as e:
            log.warning("Cannot read on-chain balances: %s", e)
            balances = None

        report = reconcile(meta, balances, exclude_mints=frozenset({"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"}))
        log.info("Reconciliation check completed: %s", report)
        if not report.get("checked"):
            # RPC outage is not a clean bill of health.
            print("RECONCILE SKIPPED: on-chain balances unavailable — "
                  "the restored journal was NOT verified against chain.")
        elif report.get("discrepancies"):
            print("RECONCILE WARNING: Found discrepancies between restored journal and chain:")
            for d in report["discrepancies"]:
                print(f"  - {d}")
        else:
            print("RECONCILE SUCCESS: Restored state matches chain balances.")
        return report
    except Exception as e:
        log.warning("Could not complete reconciliation: %s", e)
        return {"checked": False, "error": str(e)}


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
    parser = argparse.ArgumentParser(description="Restore encrypted state backup")
    parser.add_argument("archive", type=Path, nargs="?", default=None, help="Path to state-*.tar.gz.enc archive")
    parser.add_argument("--target-state-dir", type=Path, required=True, help="Target state directory")
    parser.add_argument("--target-db-path", type=Path, default=None, help="Target SQLite DB path")
    parser.add_argument("--key", type=str, default=None, help="Backup encryption key")
    parser.add_argument("--force", action="store_true", help="Force overwrite non-empty target directory")
    parser.add_argument("--reconcile", action="store_true", help="Run read-only reconciliation check")
    parser.add_argument("--reconcile-only", action="store_true", help="Run read-only reconciliation check without restoring backup")
    args = parser.parse_args(_normalize_argv(sys.argv[1:]))

    if args.reconcile_only:
        report = asyncio.run(run_reconcile(args.target_state_dir))
        # An operator script must not report success when reconciliation
        # found drift or could not read the journal.
        if report.get("error"):
            return 1
        return 2 if report.get("discrepancies") else 0

    if not args.archive:
        parser.error("archive is required unless --reconcile-only is specified")

    backup_key = args.key or os.getenv("BACKUP_ENCRYPTION_KEY", "")

    try:
        restore_backup(
            archive_path=args.archive,
            target_state_dir=args.target_state_dir,
            target_db_path=args.target_db_path,
            backup_key=backup_key,
            force=args.force,
        )

        if args.reconcile:
            asyncio.run(run_reconcile(args.target_state_dir))

        return 0
    except Exception as e:
        log.error("Restore failed: %s", e)
        print(f"RESTORE FAILED: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
