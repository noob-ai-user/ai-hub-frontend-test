#!/usr/bin/env bash
set -uo pipefail

DATA_ROOT="${DATA_ROOT:-/data}"
LUMIVERSE_PORT="${LUMIVERSE_PORT:-7861}"
cd /apps/lumiverse

export PATH="/usr/local/bin:/usr/bin:/bin:${PATH:-}"
export GIT_EXEC_PATH="${GIT_EXEC_PATH:-/usr/lib/git-core}"
export NODE_ENV=production
export PORT="${LUMIVERSE_PORT}"
export DATA_DIR="${DATA_ROOT}/lumiverse"
export FRONTEND_DIR=/apps/lumiverse/frontend/dist
export TRUST_ANY_ORIGIN=true

# BetterAuth must use the public HTTPS URL (not localhost) behind HF proxy.
resolved="$(bash /opt/hub/docker/resolve-public-origin.sh || true)"
if [[ -n "${resolved}" ]]; then
  # Root public origin (no /apps/lumiverse) — BetterAuth handler rewrites to
  # https://host/api/auth/... which matches baseURL; subpath in baseURL caused 404.
  export AUTH_BASE_URL="${resolved%/}"
  echo "[lumiverse] AUTH_BASE_URL=${AUTH_BASE_URL}" >&2
else
  echo "[lumiverse] WARN: login may fail — set PUBLIC_ORIGIN=https://YOUR-SPACE.hf.space in HF Secrets" >&2
fi

mkdir -p "${DATA_DIR}"

bash /opt/hub/docker/patch-lumiverse-auth.sh || echo "[lumiverse] warn: optional auth patch skipped" >&2
bash /opt/hub/docker/patch-lumiverse-sw.sh || echo "[lumiverse] warn: PWA patch failed" >&2

exec bun run src/index.ts