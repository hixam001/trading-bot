#!/usr/bin/env python3
"""
scripts/restore_state.py — Decrypt, verify, and restore state & SQLite backup (§79 Phase E).

Steps:
1. Decrypt archive using Fernet with BACKUP_ENCRYPTION_KEY.
2. Read MANIFEST.json and verify all file SHA-256 hashes and sizes.
3. Validate every extracted .json file parses cleanly.
4. Refuse to restore into a non-empty target directory unless --force is given.
5. Restore SQLite database and execute PRAGMA integrity_check.
6. Print mandatory operator warning.
7. Optional --reconcile: run read-only on-chain reconciliation check.

Usage:
    python scripts/restore_state.py <archive_path> --target-state-dir <dir> [--target-db-path <db>] [--force] [--reconcile]
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import tarfile
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("restore")

MANDATORY_WARNING = """
================================================================================
MANDATORY OPERATOR WARNING: State restored from backup.
NEVER arm after restoring until reconcile reports clean.
================================================================================
"""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def restore_backup(
    archive_path: Path,
    target_state_dir: Path,
    target_db_path: Optional[Path],
    backup_key: str,
    force: bool = False,
) -> dict[str, Any]:
    """
    Decrypts, validates, and restores state and database files from an encrypted backup archive.
    """
    if not backup_key or not backup_key.strip():
        raise RuntimeError("BACKUP_ENCRYPTION_KEY is unset or empty; refusing restore")

    if not archive_path.is_file():
        raise FileNotFoundError(f"Backup archive not found: {archive_path}")

    # Check target state directory emptiness
    if target_state_dir.is_dir() and any(target_state_dir.iterdir()):
        if not force:
            raise RuntimeError(
                f"Target state directory '{target_state_dir}' is not empty; use --force to overwrite"
            )

    # Check target DB existence
    if target_db_path and target_db_path.is_file() and not force:
        raise RuntimeError(
            f"Target database file '{target_db_path}' already exists; use --force to overwrite"
        )

    # 1. Decrypt archive
    encrypted_bytes = archive_path.read_bytes()
    try:
        fernet = Fernet(backup_key.strip().encode("utf-8"))
        decrypted_tar_bytes = fernet.decrypt(encrypted_bytes)
    except InvalidToken:
        raise RuntimeError("Archive decryption failed: InvalidToken (wrong key or corrupted data)")
    except Exception as e:
        raise RuntimeError(f"Archive decryption failed: {e}")

    # 2. Inspect tar contents
    try:
        tar = tarfile.open(fileobj=io.BytesIO(decrypted_tar_bytes), mode="r:gz")
    except Exception as e:
        raise RuntimeError(f"Corrupted tar.gz archive: {e}")

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

    restored_files: list[str] = []

    # 3. Validate and extract all files
    for member in tar.getmembers():
        if member.name == "MANIFEST.json":
            continue

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

        # Destination routing
        if member.name.startswith("state/"):
            rel_name = member.name[len("state/"):]
            out_p = target_state_dir / rel_name
            target_state_dir.mkdir(parents=True, exist_ok=True)
            out_p.write_bytes(data)
            restored_files.append(str(out_p))
        elif member.name == "trading_bot.db":
            if target_db_path:
                target_db_path.parent.mkdir(parents=True, exist_ok=True)
                target_db_path.write_bytes(data)
                # Verify SQLite integrity
                conn = sqlite3.connect(str(target_db_path))
                try:
                    cur = conn.cursor()
                    cur.execute("PRAGMA integrity_check;")
                    res = cur.fetchall()
                    if not res or res[0][0] != "ok":
                        raise RuntimeError(f"SQLite PRAGMA integrity_check failed: {res}")
                finally:
                    conn.close()
                restored_files.append(str(target_db_path))
            else:
                log.info("SQLite DB in archive skipped (no target DB path specified)")
        else:
            log.warning("Unknown file in archive skipped: %s", member.name)

    tar.close()

    print(MANDATORY_WARNING)
    log.info("Successfully restored %d file(s) from %s", len(restored_files), archive_path.name)

    return {
        "restored_files": restored_files,
        "manifest": manifest_data,
    }


async def run_reconcile(state_dir: Path) -> dict[str, Any]:
    """Run read-only reconciliation check against chain truth."""
    log.info("Running read-only reconciliation check against chain...")
    meta: dict[str, Any] = {}
    executions_file = state_dir / "executions.json"

    if executions_file.is_file():
        try:
            records = json.loads(executions_file.read_text(encoding="utf-8"))
            for r in records:
                if r.get("kind") == "buy" and r.get("status") in ("OPEN", "PARTIAL", "open", "partial"):
                    mint = r.get("mint")
                    if not mint:
                        continue
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

    backend_dir = Path(__file__).resolve().parent.parent / "backend"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))

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
        if report.get("discrepancies"):
            print("RECONCILE WARNING: Found discrepancies between restored journal and chain:")
            for d in report["discrepancies"]:
                print(f"  - {d}")
        else:
            print("RECONCILE SUCCESS: Restored state matches chain balances or balances unavailable.")
        return report
    except Exception as e:
        log.warning("Could not complete reconciliation: %s", e)
        return {"checked": False, "error": str(e)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore encrypted state backup")
    parser.add_argument("archive", type=Path, help="Path to state-*.tar.gz.enc archive")
    parser.add_argument("--target-state-dir", type=Path, required=True, help="Target state directory")
    parser.add_argument("--target-db-path", type=Path, default=None, help="Target SQLite DB path")
    parser.add_argument("--key", type=str, default=None, help="Backup encryption key")
    parser.add_argument("--force", action="store_true", help="Force overwrite non-empty target directory")
    parser.add_argument("--reconcile", action="store_true", help="Run read-only reconciliation check")
    args = parser.parse_args()

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
        return 1


if __name__ == "__main__":
    sys.exit(main())

