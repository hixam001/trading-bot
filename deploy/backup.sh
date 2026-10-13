#!/usr/bin/env bash
set -euo pipefail

# deploy/backup.sh — Host-side cron wrapper for state backups (§79 Phase E)
#
# Usage:
#   ./deploy/backup.sh [extra options passed to backup_state.py]
#
# Sample crontab (e.g. daily at 03:00 UTC):
#   0 3 * * * cd /opt/trading-bot && ./deploy/backup.sh >> /var/log/trading-bot-backup.log 2>&1
#
# Optional Offsite Replication:
#   If OFFSITE_REMOTE is set (e.g. "s3:my-backup-bucket/trading-bot") and rclone
#   is installed on the host, backups will be synchronized offsite automatically.
#   NEVER hardcode credentials or secrets into this file.

echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Starting trading-bot state backup..."

# Execute backup inside the running live container
docker compose exec -T live python scripts/backup_state.py "$@"

# Optional offsite replication using rclone
if [ -n "${OFFSITE_REMOTE:-}" ]; then
  if command -v rclone >/dev/null 2>&1; then
    echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Syncing backups offsite to ${OFFSITE_REMOTE}..."
    # Copy from host mount / docker volume
    # Assumes backup volume is accessible or copied from container
    docker compose cp live:/app/backups/. /tmp/trading_bot_backups_sync/
    rclone copy /tmp/trading_bot_backups_sync/ "${OFFSITE_REMOTE}"
    rm -rf /tmp/trading_bot_backups_sync/
    echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Offsite sync complete."
  else
    echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] WARNING: OFFSITE_REMOTE is set but 'rclone' was not found on PATH."
  fi
fi

echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Backup finished successfully."

