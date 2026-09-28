#!/bin/bash
# Operator-invoked backup. No changes to scheduler, rclone or user accounts.
set -euo pipefail
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${PINAK_DATA_ROOT:-$BASE_DIR/data}"
BACKUP_ROOT="${PINAK_BACKUP_ROOT:-$BASE_DIR/../pinak-memory-backups}"
PYTHON="${PINAK_BACKUP_PYTHON:-python3}"
cd "$BASE_DIR"
exec "$PYTHON" -m scripts.backup_verified create --data-dir "$DATA_DIR" --backup-root "$BACKUP_ROOT"
