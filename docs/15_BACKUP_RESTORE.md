# 15. Backup and Restore Guide (§79 Phase E)

> [!CAUTION]
> **OPERATOR NOTICE:** This backup and restore mechanism was developed and verified in test environments;
> it was **NOT exercised against production**. The operator must perform a drill in a staging/scratch
> environment before relying on it for disaster recovery.

---

## 1. Overview

The trading engine maintains state in two primary locations:
1. `STATE_DIR/*.json`:
   - `executions.json` (idempotency ledger and cost basis tracking)
   - `commits.json` (published thesis commits)
   - `kill_switch.json` (latch state)
   - `safety_counters.json` (simulation and guard refusal tallies)
2. `trading_bot.db`:
   - SQLite database for candidates, feed events, and rule evaluations (unless `USE_SUPABASE_DB=1` is active).

Losing the ledger risks duplicate fills, lost positions, and inaccurate cost basis. `backup_state.py` and `restore_state.py` provide encrypted snapshots and deterministic restores.

Both scripts resolve the database path through the backend config (`backend/config.py` → `DB_PATH`, with `.env` loaded like the app), never from the current working directory (§83 Phase G). A backup whose database file is missing is **refused** (`database file not found`) rather than silently producing an archive with no tick history.

---

## 2. Key Generation & Setup

Backups are encrypted using **Fernet** (symmetric authenticated encryption).
The backup key **MUST** be distinct from `SECRET_STORE_KEY`.

### Generate a dedicated backup encryption key:
```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### Configure environment:
Add to `.env` on your deployment host:
```bash
BACKUP_ENCRYPTION_KEY=<generated-fernet-key>
# Ensure it is NOT identical to SECRET_STORE_KEY
```

---

## 3. Scheduled Automated Backups (Cron)

A Docker volume `engine_backups` is mounted inside the `live` container at `/app/backups`.

Use the host-side wrapper `deploy/backup.sh` to trigger backups via cron.

### Sample Crontab Entry (`crontab -e`):
```bash
# Run backup daily at 03:00 UTC
0 3 * * * cd /opt/trading-bot && ./deploy/backup.sh >> /var/log/trading-bot-backup.log 2>&1
```

### Retention Policy:
The backup script automatically retains:
- The newest backup for each of the last **14 distinct calendar days**.
- The newest backup for each of the last **8 distinct calendar weeks**.
Older backup archives are automatically pruned.

---

## 4. Offsite Archival (rclone)

To prevent total loss in the event of host failure, replicate `/app/backups` offsite using `rclone`.

1. Configure `rclone` on the host:
   ```bash
   rclone config
   ```
2. Set the destination remote in your environment or crontab:
   ```bash
   export OFFSITE_REMOTE="s3:my-secure-trading-backups/bot-backups"
   ```
   `deploy/backup.sh` will automatically sync backups to your remote.
   *Never store cloud credentials in the repository or unencrypted config files.*

---

## 5. Secret Exclusion (Denylist)

To prevent wallet private keys or decryption secrets from leaking into state backups, `backup_state.py` strictly excludes files matching:
- `wallet_keypair`
- `keypair`
- `secret_store`
- `sidecar`
- `.key`, `.pem`, `.env`

If any such file exists in `STATE_DIR`, it is logged and excluded from the archive.

---

## 6. Disaster Recovery & Safe Restore Architecture (§81 Phase E)

The restore process (`scripts/restore_state.py`) follows a strict multi-pass fail-closed architecture:

1. **Pass 1: In-Memory Validation & Path Safety**:
   - The archive is decrypted in memory.
   - Every file member is checked for path safety: only regular files are accepted (`member.isfile()`); symlinks, hardlinks, devices, and directories are rejected. Absolute paths and directory traversal (`..`) are strictly forbidden. Allowed prefixes/names are `state/*`, `trading_bot.db`, and `MANIFEST.json`.
   - File sizes and SHA-256 hashes are verified against `MANIFEST.json` in memory.
   - All `.json` state files are parsed and validated.
   - **Nothing is written to the destination disk during Pass 1.**

2. **Pre-Restore Rollback Snapshot**:
   - If `--force` is used or the target directory is non-empty, existing state files and database are preserved under `<target-state-dir>/.pre-restore-<UTC-timestamp>/` before staging or promotion.
   - The database snapshot goes through SQLite's `Connection.backup()` (§83 Phase H), so committed rows still living in the `-wal` sidecar (WAL mode) are included. A raw file copy is used only when the target is genuinely not a SQLite database (`file is not a database`); any other failure (locked, IO) stays fatal, so a real book that could not be snapshotted is never overwritten.

3. **Pass 2: Atomic Staging & SQLite Integrity**:
   - Files are staged to temporary names (`.staged.<pid>.*`) in their target directory on the same filesystem.
   - `PRAGMA integrity_check;` is executed directly on the **staged** SQLite database file. If the integrity check fails, all staged temporary files are deleted immediately and the existing database remains untouched.

4. **Pass 3: Atomic Promotion & Partial Failure Containment (§82 Phase F, §83 Phase H)**:
   - Staged files are promoted to their final paths via atomic `os.replace`.
   - Immediately before the restored database is promoted, stale `-wal`/`-shm`/`-journal` sidecars beside it are deleted (§83 Phase H): the restored DB replaces the whole lineage, and SQLite must never try to recover a foreign WAL against it. The same cleanup runs before the rollback `os.replace`, and is best-effort/logged — an undeletable sidecar must not abort an otherwise complete restore.
   - If `os.replace` fails after ≥1 files have been promoted:
     - The kill switch is immediately tripped: `PARTIAL RESTORE: promoted N of M files from <archive>; state is inconsistent; do not arm`.
     - Automatic best-effort rollback restores the pre-restore backup files via atomic `os.replace`.
     - Staged temporary files are unlinked and the exception is re-raised.
   - If zero files were promoted, the original state remains intact and the partial restore trip is not engaged.

5. **Pass 4: Mandatory Safety Latch**:
   - The kill switch is **always tripped** upon restore, overwriting whatever `kill_switch.json` was present in the archive:
     `kill_switch.trip("restored from <archive> at <UTC ts>; run reconcile and clear manually", state_dir=target_state_dir)`
   - The operator must explicitly clear the kill switch after verifying reconciliation.

---

## 7. Operator Restore Drill (Staging Verification)

Operators should periodically practice a restore drill in a scratch directory to verify backup integrity without affecting production state.

### Drill Procedure:

1. **Select an archive**:
   ```bash
   LATEST_BACKUP=$(ls -t /app/backups/state-*.tar.gz.enc | head -n 1)
   ```

2. **Restore into a scratch directory**:
   ```bash
   SCRATCH_DIR=$(mktemp -d /tmp/restore-drill-XXXXXX)
   python scripts/restore_state.py "$LATEST_BACKUP" \
       --target-state-dir "$SCRATCH_DIR/state" \
       --target-db-path "$SCRATCH_DIR/trading_bot.db" \
       --reconcile
   ```

3. **Verify the drill results**:
   - Confirm all files restored: `ls -la "$SCRATCH_DIR/state"`
   - Confirm kill switch was engaged:
     ```bash
     python -c "import sys; sys.path.insert(0, 'backend'); from live_execution import kill_switch; from pathlib import Path; assert kill_switch.is_tripped(Path('$SCRATCH_DIR/state')), 'Kill switch should be tripped'"
     ```
   - Review reconciliation report printed to console.

4. **Clean up scratch directory**:
   ```bash
   rm -rf "$SCRATCH_DIR"
   ```

---

## 8. Production Disaster Recovery Workflow

> [!WARNING]
> **MANDATORY OPERATOR WARNING:** State restored from backup.
> **NEVER arm after restoring until reconcile reports clean.**

1. **Stop the engine**:
   ```bash
   docker compose stop live api
   ```
2. **Execute restore**:
   ```bash
   docker compose run --rm live python scripts/restore_state.py /app/backups/state-YYYYMMDDTHHMMSSZ.tar.gz.enc \
       --target-state-dir /app/state \
       --target-db-path /app/data/trading_bot.db \
       --force \
       --reconcile
   ```
3. **Inspect reconciliation report**:
   - Verify on-chain token accounts match the restored ledger journal.
   - If discrepancies appear, resolve manually before clearing the kill switch.
   - To re-run reconciliation at any time without re-restoring the archive, use `--reconcile-only`:
     ```bash
     docker compose run --rm live python scripts/restore_state.py --reconcile-only --target-state-dir /app/state
     ```
4. **Clear the kill switch** (only after human verification):
   ```bash
   docker compose run --rm live python backend/live_execution/scripts/confirm_trade.py resume
   ```
5. **Resume operations**:
   ```bash
   docker compose up -d live api
   ```


