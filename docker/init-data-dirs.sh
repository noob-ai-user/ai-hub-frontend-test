#!/usr/bin/env bash
set -euo pipefail

DATA_ROOT="${DATA_ROOT:-/data}"

mkdir -p \
  "${DATA_ROOT}/shared/characters" \
  "${DATA_ROOT}/shared/world_info" \
  "${DATA_ROOT}/shared/connections" \
  "${DATA_ROOT}/sillytavern/config" \
  "${DATA_ROOT}/sillytavern/data/default-user" \
  "${DATA_ROOT}/sillytavern/plugins" \
  "${DATA_ROOT}/sillytavern/extensions" \
  "${DATA_ROOT}/lumiverse" \
  "${DATA_ROOT}/marinara/storage" \
  "${DATA_ROOT}/marinara/uploads" \
  "${DATA_ROOT}/marinara/.env.d"

# Seed SillyTavern config on first run
if [[ ! -f "${DATA_ROOT}/sillytavern/config/config.yaml" ]]; then
  if [[ -f /apps/sillytavern/default/config.yaml ]]; then
    cp /apps/sillytavern/default/config.yaml "${DATA_ROOT}/sillytavern/config/config.yaml"
  elif [[ -f /opt/hub/config/sillytavern-config.yaml ]]; then
    cp /opt/hub/config/sillytavern-config.yaml "${DATA_ROOT}/sillytavern/config/config.yaml"
  fi
fi

# Seed Marinara .env for HF / remote access
MARINARA_ENV="${DATA_ROOT}/marinara/.env"
if [[ ! -f "${MARINARA_ENV}" ]]; then
  cat > "${MARINARA_ENV}" <<EOF
HOST=0.0.0.0
PORT=${MARINARA_PORT:-7862}
DATA_DIR=${DATA_ROOT}/marinara
FILE_STORAGE_DIR=${DATA_ROOT}/marinara/storage
ALLOW_UNAUTHENTICATED_REMOTE=true
BYPASS_AUTH_DOCKER=true
BYPASS_AUTH_TAILSCALE=true
IMPORT_ALLOWED_ROOTS=${DATA_ROOT}/shared
CSRF_TRUSTED_ORIGINS=*
CORS_ORIGINS=*
LOG_LEVEL=warn
AUTO_CREATE_DEFAULT_CONNECTION=true
EOF
fi

# Lumiverse first-run marker (setup wizard skipped in hub mode)
touch "${DATA_ROOT}/lumiverse/.hub-initialized"

# Fix ownership for node (SillyTavern + Marinara) and world-readable shared lib
if id node >/dev/null 2>&1; then
  chown -R node:node "${DATA_ROOT}/sillytavern" "${DATA_ROOT}/marinara" 2>/dev/null || true
fi
chmod -R a+rX "${DATA_ROOT}/shared" 2>/dev/null || true