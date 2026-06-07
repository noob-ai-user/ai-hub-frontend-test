#!/usr/bin/env bash
set -uo pipefail

DATA_ROOT="${DATA_ROOT:-/data}"
LUMIVERSE_PORT="${LUMIVERSE_PORT:-7861}"
cd /apps/lumiverse

export NODE_ENV=production
export PORT="${LUMIVERSE_PORT}"
export DATA_DIR="${DATA_ROOT}/lumiverse"
export FRONTEND_DIR=/apps/lumiverse/frontend/dist
export TRUST_ANY_ORIGIN=true

# BetterAuth must use the public HTTPS URL (not localhost) behind HF proxy.
resolved="$(bash /opt/hub/docker/resolve-public-origin.sh || true)"
if [[ -n "${resolved}" ]]; then
  export AUTH_BASE_URL="${resolved}"
  echo "[lumiverse] AUTH_BASE_URL=${AUTH_BASE_URL}" >&2
else
  echo "[lumiverse] WARN: login may fail — set PUBLIC_ORIGIN=https://YOUR-SPACE.hf.space in HF Secrets" >&2
fi

mkdir -p "${DATA_DIR}"
exec bun run src/index.ts