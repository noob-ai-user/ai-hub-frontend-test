#!/usr/bin/env bash
set -uo pipefail

echo "[hub] HF start $(date -Is)" >&2

DATA_ROOT="${DATA_ROOT:-/data}"
export DATA_ROOT
export PATH="/usr/local/bin:/usr/bin:/bin:${PATH:-}"

if [[ -z "${PUBLIC_ORIGIN:-}" ]]; then
  resolved="$(bash /opt/hub/docker/resolve-public-origin.sh 2>/dev/null || true)"
  if [[ -n "${resolved}" ]]; then
    export PUBLIC_ORIGIN="${resolved}"
  fi
fi

mkdir -p "${DATA_ROOT}" "${DATA_ROOT}/.pids" \
  /tmp/nginx/body /tmp/nginx/proxy /tmp/nginx/fastcgi /tmp/nginx/uwsgi /tmp/nginx/scgi 2>/dev/null || true
chmod -R u+rwX "${DATA_ROOT}" /tmp/nginx 2>/dev/null || true

/opt/hub/docker/init-data-dirs.sh 2>&1 || echo "[hub] warn: init-data-dirs" >&2
/opt/hub/docker/link-shared-data.sh 2>&1 || true
/opt/hub/docker/init-sillytavern-data.sh 2>&1 || echo "[hub] warn: init-sillytavern-data" >&2
/opt/hub/scripts/sync-shared-data.sh 2>&1 || true

ACTIVE="${ACTIVE_APP:-sillytavern}"
if [[ -f "${DATA_ROOT}/.active_app" ]]; then
  saved="$(cat "${DATA_ROOT}/.active_app")"
  case "${saved}" in
    sillytavern|lumiverse|marinara) ACTIVE="${saved}" ;;
  esac
fi
echo "${ACTIVE}" > "${DATA_ROOT}/.active_app"

echo "[hub] starting all frontends (always-on)" >&2
/opt/hub/docker/start-all-apps.sh 2>&1 || echo "[hub] warn: start-all-apps" >&2

echo "[hub] routing traffic to: ${ACTIVE}" >&2
HUB_SKIP_SYNC=1 /opt/hub/docker/switch-app.sh "${ACTIVE}" 2>&1 || echo "[hub] warn: switch-app" >&2

case "${ACTIVE}" in
  sillytavern) BACKEND_PORT="${ST_PORT:-8000}" ;;
  lumiverse)   BACKEND_PORT="${LUMIVERSE_PORT:-7861}" ;;
  marinara)    BACKEND_PORT="${MARINARA_PORT:-7862}" ;;
  *)           BACKEND_PORT="${ST_PORT:-8000}" ;;
esac

port_up() {
  (echo >/dev/tcp/127.0.0.1/"${BACKEND_PORT}") >/dev/null 2>&1
}

WAIT_MAX=120
if [[ "${ACTIVE}" == "sillytavern" ]]; then
  WAIT_MAX=300
fi

echo "[hub] waiting for backend :${BACKEND_PORT} (up to ${WAIT_MAX}s)" >&2
ready=0
for i in $(seq 1 "${WAIT_MAX}"); do
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

echo "[hub] gateway on :${HUB_PORT:-7860} (ST=/ hub=/hub lumiverse|marinara=/apps/.../)" >&2
( sleep 8; /opt/hub/scripts/sync-shared-data.sh 2>&1 || echo "[hub] warn: post-boot sync" >&2 ) >&2 &
exec python3 /opt/hub/docker/hub-gateway.py