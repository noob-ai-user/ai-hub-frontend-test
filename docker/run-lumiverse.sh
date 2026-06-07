#!/usr/bin/env bash
set -euo pipefail

DATA_ROOT="${DATA_ROOT:-/data}"
LUMIVERSE_PORT="${LUMIVERSE_PORT:-7861}"

if [[ -f "${DATA_ROOT}/lumiverse/.built" ]]; then
  APP_DIR="${DATA_ROOT}/lumiverse-app"
else
  APP_DIR="/apps/lumiverse"
fi

cd "${APP_DIR}"

export NODE_ENV=production
export PORT="${LUMIVERSE_PORT}"
export DATA_DIR="${DATA_ROOT}/lumiverse"
export FRONTEND_DIR="${APP_DIR}/frontend/dist"
export TRUST_ANY_ORIGIN=true

mkdir -p "${DATA_DIR}"
exec bun run src/index.ts