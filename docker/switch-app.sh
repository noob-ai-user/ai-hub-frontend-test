#!/usr/bin/env bash
# Switch active frontend — only ONE runs at a time (HF free-tier RAM).
set -uo pipefail

APP="${1:-}"
DATA_ROOT="${DATA_ROOT:-/data}"
PID_DIR="${DATA_ROOT}/.pids"
LOG_DIR="${DATA_ROOT}/.logs"
LOCK_FILE="${DATA_ROOT}/.switch.lock"
mkdir -p "${PID_DIR}" "${LOG_DIR}"

if [[ -z "${APP}" ]]; then
  echo "usage: switch-app.sh <sillytavern|lumiverse|marinara>" >&2
  exit 1
fi

case "${APP}" in
  sillytavern|lumiverse|marinara) ;;
  *) echo "unknown app: ${APP}" >&2; exit 1 ;;
esac

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "[hub] switch already in progress — skip" >&2
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

wait_for() {
  local name="$1"
  local port="$2"
  local max="$3"
  echo "[hub] waiting for ${name} on :${port} (up to ${max}s)..." >&2
  for i in $(seq 1 "${max}"); do
    if port_up "${port}"; then
      echo "[hub] ${name} ready on :${port} (after ${i}s)" >&2
      return 0
    fi
    if (( i % 15 == 0 )); then
      echo "[hub] still waiting for ${name} :${port} (${i}s)..." >&2
    fi
    sleep 1
  done
  echo "[hub] ERROR: ${name} never opened :${port} after ${max}s" >&2
  return 1
}

stop_one() {
  local name="$1"
  local pidfile="${PID_DIR}/${name}.pid"
  local logpidfile="${PID_DIR}/${name}-log.pid"

  if [[ -f "${logpidfile}" ]]; then
    kill "$(cat "${logpidfile}")" 2>/dev/null || true
    rm -f "${logpidfile}"
  fi

  if [[ -f "${pidfile}" ]]; then
    local pid
    pid="$(cat "${pidfile}")"
    kill -- -"${pid}" 2>/dev/null || kill "${pid}" 2>/dev/null || true
    rm -f "${pidfile}"
  fi

  case "${name}" in
    sillytavern) pkill -f "node server.js" 2>/dev/null || true ;;
    lumiverse)   pkill -f "bun run src/index.ts" 2>/dev/null || true ;;
    marinara)    pkill -f "packages/server/dist/index.js" 2>/dev/null || true ;;
  esac
}

start_one() {
  local name="$1"
  local script="/opt/hub/docker/run-${name}.sh"
  local log="${LOG_DIR}/${name}.log"

  : > "${log}"
  setsid bash -c "exec bash \"${script}\"" >> "${log}" 2>&1 &
  echo $! > "${PID_DIR}/${name}.pid"

  tail -n 0 -f "${log}" 2>/dev/null | sed -u "s/^/[${name}] /" >&2 &
  echo $! > "${PID_DIR}/${name}-log.pid"

  echo "[hub] started ${name} pid $(cat "${PID_DIR}/${name}.pid")" >&2
}

PREV_APP=""
if [[ -f "${DATA_ROOT}/.active_app" ]]; then
  PREV_APP="$(cat "${DATA_ROOT}/.active_app")"
fi

PORT="$(port_for "${APP}")"

# Already on this app and port is up — just ensure nginx points correctly.
if [[ "${PREV_APP}" == "${APP}" ]] && port_up "${PORT}"; then
  echo "${APP}" > "${DATA_ROOT}/.active_app"
  cat > /opt/hub/docker/upstream.conf <<EOF
upstream active_backend {
    server 127.0.0.1:${PORT};
}
EOF
  nginx -s reload -c /opt/hub/docker/nginx.conf 2>/dev/null || true
  echo "[hub] already on ${APP} :${PORT}" >&2
  exit 0
fi

# Export from the app we're leaving while it is still running.
if [[ -n "${PREV_APP}" && "${PREV_APP}" != "${APP}" ]]; then
  HUB_SYNC_EXPORT="${PREV_APP}" python3 /opt/hub/scripts/hub-sync-import.py 2>&1 || true
fi

echo "${APP}" > "${DATA_ROOT}/.active_app"

stop_one sillytavern
stop_one lumiverse
stop_one marinara
sleep 2

start_one "${APP}"

WAIT_MAX=120
case "${APP}" in
  sillytavern) WAIT_MAX=300 ;;
esac

if ! wait_for "${APP}" "${PORT}" "${WAIT_MAX}"; then
  echo "[hub] switch to ${APP} failed — backend did not start" >&2
  exit 1
fi

cat > /opt/hub/docker/upstream.conf <<EOF
upstream active_backend {
    server 127.0.0.1:${PORT};
}
EOF

if nginx -s reload -c /opt/hub/docker/nginx.conf 2>/dev/null; then
  echo "[hub] nginx reloaded → ${APP} on :${PORT}" >&2
else
  echo "[hub] nginx reload skipped (not running yet)" >&2
fi

/opt/hub/scripts/sync-shared-data.sh 2>&1 || echo "[hub] warn: sync-shared-data" >&2

echo "[hub] switched to ${APP} on internal port ${PORT}" >&2