#!/usr/bin/env bash
set -euo pipefail

DATA_ROOT="${DATA_ROOT:-/data}"
MARINARA_PORT="${MARINARA_PORT:-7862}"
cd /apps/marinara

export NODE_ENV=production
export PORT="${MARINARA_PORT}"
export HOST=0.0.0.0
export DATA_DIR="${DATA_ROOT}/marinara"
export FILE_STORAGE_DIR="${DATA_ROOT}/marinara/storage"
export MARINARA_ENV_FILE="${DATA_ROOT}/marinara/.env"
export MARINARA_DOCKER=true
export ALLOW_UNAUTHENTICATED_REMOTE=true
export IMPORT_ALLOWED_ROOTS="${DATA_ROOT}/shared"

# Re-apply dist subpath patches at boot (/assets/ + /api in Vite chunks).
bash /opt/hub/docker/patch-app-subpaths.sh \
  || echo "[marinara] warn: subpath patch failed" >&2

# Skip root-only docker entrypoint on HF (uid 1000)
exec node packages/server/dist/index.js