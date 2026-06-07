#!/usr/bin/env bash
set -uo pipefail

echo "[hub] HF start $(date -Is)" >&2

DATA_ROOT="${DATA_ROOT:-/data}"
export DATA_ROOT

mkdir -p "${DATA_ROOT}" "${DATA_ROOT}/.pids" \
  /tmp/nginx/body /tmp/nginx/proxy /tmp/nginx/fastcgi /tmp/nginx/uwsgi /tmp/nginx/scgi 2>/dev/null || true
chmod -R u+rwX "${DATA_ROOT}" /tmp/nginx 2>/dev/null || true

/opt/hub/docker/init-data-dirs.sh 2>&1 || echo "[hub] warn: init-data-dirs" >&2
/opt/hub/docker/link-shared-data.sh 2>&1 || true
/opt/hub/scripts/sync-shared-data.sh 2>&1 || true

ACTIVE="${ACTIVE_APP:-sillytavern}"
[[ -f "${DATA_ROOT}/.active_app" ]] && ACTIVE="$(cat "${DATA_ROOT}/.active_app")"
echo "${ACTIVE}" > "${DATA_ROOT}/.active_app"

echo "[hub] starting hub-api on :7870" >&2
python3 /opt/hub/docker/hub-api.py >&2 &

echo "[hub] booting frontend: ${ACTIVE}" >&2
/opt/hub/docker/switch-app.sh "${ACTIVE}" 2>&1 || echo "[hub] warn: switch-app" >&2

case "${ACTIVE}" in
  sillytavern) BACKEND_PORT="${ST_PORT:-8000}" ;;
  lumiverse)   BACKEND_PORT="${LUMIVERSE_PORT:-7861}" ;;
  marinara)    BACKEND_PORT="${MARINARA_PORT:-7862}" ;;
  *)           BACKEND_PORT="${ST_PORT:-8000}" ;;
esac

port_up() {
  (echo >/dev/tcp/127.0.0.1/"${BACKEND_PORT}") >/dev/null 2>&1
}

echo "[hub] waiting for backend :${BACKEND_PORT}" >&2
ready=0
for i in $(seq 1 90); do
  if port_up; then
    echo "[hub] backend ready on :${BACKEND_PORT} (after ${i}s)" >&2
    ready=1
    break
  fi
  if (( i % 10 == 0 )); then
    echo "[hub] still waiting for :${BACKEND_PORT} (${i}s)..." >&2
  fi
  sleep 1
done

if [[ "${ready}" -eq 0 ]]; then
  echo "[hub] ERROR: backend :${BACKEND_PORT} never opened — check [sillytavern] lines above" >&2
fi

(while true; do sleep 300; /opt/hub/scripts/sync-shared-data.sh || true; done) >&2 &

echo "[hub] nginx on :${HUB_PORT:-7860}" >&2
exec nginx -c /opt/hub/docker/nginx.conf -g 'daemon off;'