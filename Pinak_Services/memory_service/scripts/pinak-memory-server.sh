#!/bin/bash

set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$BASE_DIR"

if [ -z "${PINAK_JWT_SECRET:-}" ] || [ "$PINAK_JWT_SECRET" = "secret" ] || [ "$PINAK_JWT_SECRET" = "dev-secret-change-me" ]; then
  echo "PINAK_JWT_SECRET must be set to a non-default signing secret" >&2
  exit 1
fi
export PINAK_EMBEDDING_BACKEND="${PINAK_EMBEDDING_BACKEND:-dummy}"
export PINAK_EMBEDDING_TIMEOUT_MS="${PINAK_EMBEDDING_TIMEOUT_MS:-50}"
export PINAK_VERIFY_IN_BACKGROUND="${PINAK_VERIFY_IN_BACKGROUND:-1}"
export PINAK_BIND_HOST="${PINAK_BIND_HOST:-127.0.0.1}"
export PINAK_BIND_PORT="${PINAK_BIND_PORT:-8000}"

exec "$BASE_DIR/.venv/bin/python" -m uvicorn app.main:app --host "$PINAK_BIND_HOST" --port "$PINAK_BIND_PORT"
