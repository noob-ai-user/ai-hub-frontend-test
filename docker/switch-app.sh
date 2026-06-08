#!/usr/bin/env bash
# Switch active frontend — all three backends stay running (needs ~16GB RAM).
# Routing is dynamic: hub-gateway reads /data/.active_app per request (no nginx reload).
set -uo pipefail

APP="${1:-}"
DATA_ROOT="${DATA_ROOT:-/data}"
LOCK_FILE="${DATA_ROOT}/.switch.lock"

if [[ -z "${APP}" ]]; then
  echo "usage: switch-app.sh <sillytavern|lumiverse|marinara>" >&2
  exit 1
fi

case "${APP}" in
  sillytavern|lumiverse|marinara) ;;
  *) echo "unknown app: ${APP}" >&2; exit 1 ;;
esac

PREV_APP=""
if [[ -f "${DATA_ROOT}/.active_app" ]]; then
  PREV_APP="$(cat "${DATA_ROOT}/.active_app")"
fi

# Update routing target immediately — hub-gateway reads this per request.
echo "${APP}" > "${DATA_ROOT}/.active_app"
echo "[hub] routing → ${APP}" >&2

if [[ "${HUB_ROUTING_ONLY:-}" == "1" ]]; then
  exit 0
fi

# Serialize heavy work (export / restart / sync) but never block routing updates.
exec 9>"${LOCK_FILE}"
if ! flock -w 15 9; then
  echo "[hub] sync queue busy — routing already ${APP}" >&2
  exit 0
fi

port_for() {
  case "$1" in
    sillytavern) echo "${ST_PORT:-8000}" ;;
    lumiverse)   echo "${LUMIVERSE_PORT:-7861}" ;;
    marinara)    echo "${MARINARA_PORT:-7862}" ;;
  esac
}

port_up() {
  local port="$1"
  (echo >/dev/tcp/127.0.0.1/"${port}") >/dev/null 2>&1
}

PORT="$(port_for "${APP}")"

# Export from the app we're leaving.
if [[ -n "${PREV_APP}" && "${PREV_APP}" != "${APP}" ]]; then
  HUB_SYNC_EXPORT="${PREV_APP}" python3 /opt/hub/scripts/hub-sync-import.py 2>&1 || true
fi

# Ensure all backends are up (start any that crashed).
/opt/hub/docker/start-all-apps.sh 2>&1 || true

if ! port_up "${PORT}"; then
  echo "[hub] ERROR: ${APP} not listening on :${PORT} after start-all" >&2
  exit 1
fi

# Legacy upstream.conf for diagnostics / optional nginx fallback.
cat > /opt/hub/docker/upstream.conf <<EOF
upstream active_backend {
    server 127.0.0.1:${PORT};
}
EOF

if [[ "${HUB_SKIP_SYNC:-}" != "1" ]]; then
  /opt/hub/scripts/sync-shared-data.sh 2>&1 || echo "[hub] warn: sync-shared-data" >&2
fi

echo "[hub] switched to ${APP} on internal port ${PORT} (dynamic gateway)" >&2