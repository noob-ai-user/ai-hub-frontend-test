#!/usr/bin/env bash
set -euo pipefail

DATA_ROOT="${DATA_ROOT:-/data}"
ST_USER_DIR="${DATA_ROOT}/sillytavern/data/default-user"
SHARED_CHARS="${DATA_ROOT}/shared/characters"
SHARED_WORLDS="${DATA_ROOT}/shared/world_info"

mkdir -p "${ST_USER_DIR}/characters" "${ST_USER_DIR}/worlds"
mkdir -p "${SHARED_CHARS}" "${SHARED_WORLDS}"

# SillyTavern keeps its own character folder (no symlink).
# Hub sync copies canonical hub_*.png cards in/out — avoids duplicate explosions.
if [[ -L "${ST_USER_DIR}/characters" ]]; then
  rsync -a "${ST_USER_DIR}/characters/" "${SHARED_CHARS}/" 2>/dev/null || true
  rm -f "${ST_USER_DIR}/characters"
  mkdir -p "${ST_USER_DIR}/characters"
fi

if [[ -L "${ST_USER_DIR}/worlds" ]]; then
  rsync -a "${ST_USER_DIR}/worlds/" "${SHARED_WORLDS}/" 2>/dev/null || true
  rm -f "${ST_USER_DIR}/worlds"
  mkdir -p "${ST_USER_DIR}/worlds"
fi

mkdir -p "${DATA_ROOT}/marinara/storage/import-staging/characters"
mkdir -p "${DATA_ROOT}/marinara/storage/import-staging/world_info"
mkdir -p "${DATA_ROOT}/lumiverse/import-staging/characters"
mkdir -p "${DATA_ROOT}/lumiverse/import-staging/world_info"