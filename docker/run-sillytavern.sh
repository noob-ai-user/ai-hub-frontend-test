#!/usr/bin/env bash
# Logs go to stdout/stderr — visible in HF Logs tab
set -uo pipefail

DATA_ROOT="${DATA_ROOT:-/data}"
ST_PORT="${ST_PORT:-8000}"

echo "[sillytavern] starting on port ${ST_PORT}" >&2
cd /apps/sillytavern

export NODE_ENV=production
export SILLYTAVERN_LISTEN=true
export SILLYTAVERN_WHITELISTMODE=false
export SILLYTAVERN_ENABLEFORWARDEDWHITELIST=false
export SILLYTAVERN_HOSTWHITELIST_ENABLED=false
export SILLYTAVERN_LISTENADDRESS_IPV4=0.0.0.0
export SILLYTAVERN_PORT="${ST_PORT}"

mkdir -p "${DATA_ROOT}/sillytavern/config" "${DATA_ROOT}/sillytavern/data/default-user"

# Always apply HF-safe config template
cp /opt/hub/config/sillytavern-config.yaml "${DATA_ROOT}/sillytavern/config/config.yaml"

rm -rf config data 2>/dev/null || true
ln -sfn "${DATA_ROOT}/sillytavern/config" config
ln -sfn "${DATA_ROOT}/sillytavern/data" data
rm -f config.yaml 2>/dev/null || true
ln -sfn "${DATA_ROOT}/sillytavern/config/config.yaml" config.yaml

echo "[sillytavern] running npm init..." >&2
npm run init 2>&1 || echo "[sillytavern] warn: npm init" >&2

echo "[sillytavern] launching server.js" >&2
exec node server.js --listen --port "${ST_PORT}"