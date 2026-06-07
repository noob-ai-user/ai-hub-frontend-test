#!/usr/bin/env bash
set -uo pipefail

APP="${1:-}"
DATA_ROOT="${DATA_ROOT:-/data}"
PID_DIR="${DATA_ROOT}/.pids"
mkdir -p "${PID_DIR}"

if [[ -z "${APP}" ]]; then
  echo "usage: switch-app.sh <sillytavern|lumiverse|marinara>" >&2
  exit 1
fi

case "${APP}" in
  sillytavern|lumiverse|marinara) ;;
  *) echo "unknown app: ${APP}" >&2; exit 1 ;;
esac

echo "${APP}" > "${DATA_ROOT}/.active_app"

stop_one() {
  local name="$1"
  local pidfile="${PID_DIR}/${name}.pid"
  if [[ -f "${pidfile}" ]]; then
    kill "$(cat "${pidfile}")" 2>/dev/null || true
    rm -f "${pidfile}"
  fi
}

stop_one sillytavern
stop_one lumiverse
stop_one marinara

start_one() {
  local name="$1"
  local script="/opt/hub/docker/run-${name}.sh"
  # Pipe backend logs into HF container logs
  bash "${script}" 2>&1 | while IFS= read -r line; do echo "[${name}] ${line}"; done >&2 &
  echo $! > "${PID_DIR}/${name}.pid"
  echo "[hub] started ${name} wrapper pid $(cat "${PID_DIR}/${name}.pid")" >&2
}

case "${APP}" in
  sillytavern) start_one sillytavern ;;
  lumiverse)   start_one lumiverse ;;
  marinara)    start_one marinara ;;
esac

PORT="8000"
case "${APP}" in
  sillytavern) PORT="${ST_PORT:-8000}" ;;
  lumiverse)   PORT="${LUMIVERSE_PORT:-7861}" ;;
  marinara)    PORT="${MARINARA_PORT:-7862}" ;;
esac

cat > /opt/hub/docker/upstream.conf <<EOF
upstream active_backend {
    server 127.0.0.1:${PORT};
}
EOF

# CRITICAL: nginx caches upstream at load — must reload after every switch
if nginx -s reload -c /opt/hub/docker/nginx.conf 2>/dev/null; then
  echo "[hub] nginx reloaded → ${APP} on :${PORT}" >&2
else
  echo "[hub] nginx reload skipped (not running yet)" >&2
fi

echo "[hub] switched to ${APP} on internal port ${PORT}" >&2
sleep 1