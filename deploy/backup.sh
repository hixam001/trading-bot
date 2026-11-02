#!/usr/bin/env bash
set -euo pipefail

# deploy/backup.sh — Host-side cron wrapper for state backups (§79 Phase E)
#
# Usage:
#   ./deploy/backup.sh [extra options passed to backup_state.py]
#
# Sample crontab (e.g. daily at 03:00 UTC), repo cloned at /opt/trading-bot/app:
#   0 3 * * * cd /opt/trading-bot/app && ./deploy/backup.sh >> /var/log/trading-bot-backup.log 2>&1
#
# Optional Offsite Replication:
#   If OFFSITE_REMOTE is set (e.g. "s3:my-backup-bucket/trading-bot") and rclone
#   is installed on the host, backups will be synchronized offsite automatically.
#   NEVER hardcode credentials or secrets into this file.

# §87 A5: always run from the repo root (where docker-compose.yml lives), no
# matter which cwd cron/operator used.
cd "$(dirname "$0")/.."

echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Starting trading-bot state backup..."

# Execute backup inside the running live container. A failed exec MUST fail
# the wrapper (cron then reports it) instead of printing "finished".
# Absolute path: the container WORKDIR is /app, but operator docs and the
# §88 entrypoint passthrough use /app/scripts/... consistently.
if ! docker compose exec -T live python /app/scripts/backup_state.py "$@"; then
  echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] ERROR: backup failed" >&2
  exit 1
fi

# Optional offsite replication using rclone
if [ -n "${OFFSITE_REMOTE:-}" ]; then
  if command -v rclone >/dev/null 2>&1; then
    echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Syncing backups offsite to ${OFFSITE_REMOTE}..."
    # Copy from host mount / docker volume
    # Assumes backup volume is accessible or copied from container
    SYNC_DIR="/tmp/trading_bot_backups_sync"
    rm -rf "$SYNC_DIR"
    mkdir -p "$SYNC_DIR"
    trap 'rm -rf "$SYNC_DIR"' EXIT
    docker compose cp live:/app/backups/. "$SYNC_DIR/"
    rclone copy "$SYNC_DIR/" "${OFFSITE_REMOTE}"
    rm -rf "$SYNC_DIR"
    trap - EXIT
    echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Offsite sync complete."
  else
    echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] WARNING: OFFSITE_REMOTE is set but 'rclone' was not found on PATH."
  fi
fi

echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Backup finished successfully."

