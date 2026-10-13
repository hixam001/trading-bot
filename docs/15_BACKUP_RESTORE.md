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

## 6. Disaster Recovery & Restore Drill

> [!WARNING]
> **MANDATORY OPERATOR WARNING:** State restored from backup.
> **NEVER arm after restoring until reconcile reports clean.**

### Restore Drill Checklist:

1. **Stop the engine** before restoring production state:
   ```bash
   docker compose stop live api
   ```
2. **Select the archive** to restore from `/app/backups/`:
   ```bash
   ls -lat /app/backups/state-*.tar.gz.enc
   ```
3. **Execute restore into target location**:
   ```bash
   docker compose run --rm live python scripts/restore_state.py /app/backups/state-YYYYMMDDTHHMMSSZ.tar.gz.enc \
       --target-state-dir /app/state \
       --target-db-path /app/data/trading_bot.db \
       --force \
       --reconcile
   ```
4. **Inspect Reconciliation Output**:
   - The restore script runs `PRAGMA integrity_check` on the SQLite database.
   - It parses and validates every restored `.json` file against the manifest SHA-256 hashes.
   - With `--reconcile`, it reads current on-chain token accounts and cross-checks them against the restored journal.
   - If any `vanished` or `unjournaled` positions appear:
     - Review on-chain Explorer transactions.
     - Ensure no manual or out-of-band swaps were executed while the bot was offline.
5. **Resume operation**:
   ```bash
   docker compose up -d live api
   ```

