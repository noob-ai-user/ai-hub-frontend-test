#!/usr/bin/env bash
# Seed SillyTavern user data so /api/settings/get works on first launch.
# Missing settings.json (or preset dirs) causes the UI "settings couldn't be loaded" error.
set -euo pipefail

DATA_ROOT="${DATA_ROOT:-/data}"
ST_DATA_ROOT="${DATA_ROOT}/sillytavern/data"
ST_USER="default-user"
USER_DIR="${ST_DATA_ROOT}/${ST_USER}"
ST_APP="${ST_APP:-/apps/sillytavern}"
CONTENT_DIR="${ST_APP}/default/content"
CONTENT_INDEX="${CONTENT_DIR}/index.json"

if [[ ! -d "${ST_APP}" ]]; then
  echo "[sillytavern] skip data init — app not found at ${ST_APP}" >&2
  exit 0
fi

echo "[sillytavern] ensuring user data under ${USER_DIR}" >&2

mkdir -p \
  "${ST_DATA_ROOT}/_storage" \
  "${ST_DATA_ROOT}/_uploads" \
  "${ST_DATA_ROOT}/_cache" \
  "${ST_DATA_ROOT}/_errors" \
  "${ST_DATA_ROOT}/_css"

USER_SUBDIRS=(
  thumbnails thumbnails/bg thumbnails/avatar thumbnails/persona
  user user/images user/files user/workflows
  "User Avatars" groups "group chats" chats backgrounds
  "NovelAI Settings" "KoboldAI Settings" "OpenAI Settings" "TextGen Settings"
  themes movingUI extensions instruct context QuickReplies
  assets vectors backups sysprompt reasoning
)

mkdir -p "${USER_DIR}"
for sub in "${USER_SUBDIRS[@]}"; do
  mkdir -p "${USER_DIR}/${sub}"
done

for sub in characters worlds chats; do
  if [[ ! -e "${USER_DIR}/${sub}" ]]; then
    mkdir -p "${USER_DIR}/${sub}"
  fi
done


# === Remove alias character duplicates (canonical lives in /data/shared) ===
if [[ -d "${USER_DIR}/characters" ]]; then
  # Remove extensionless character stubs and alias duplicates
  find "${USER_DIR}/characters" -maxdepth 1 -type f ! -name '*.*' -delete 2>/dev/null || true
  # Remove default_* files that are alias duplicates (sync handles canonical)
  find "${USER_DIR}/characters" -maxdepth 1 -name "default_*.png" -delete 2>/dev/null || true
  echo "[sillytavern] cleaned alias character files (canonical in /data/shared)" >&2
fi

# Remove extensionless character stubs (e.g. "Seraphina") — they break chat mkdir.
if [[ -d "${USER_DIR}/characters" ]]; then
  find "${USER_DIR}/characters" -maxdepth 1 -type f ! -name '*.*' -delete 2>/dev/null || true
fi

SETTINGS="${USER_DIR}/settings.json"
CONTENT_LOG="${USER_DIR}/content.log"

if [[ ! -f "${SETTINGS}" && -f "${CONTENT_LOG}" ]]; then
  echo "[sillytavern] settings.json missing — clearing stale content.log entry" >&2
  grep -vxF 'settings.json' "${CONTENT_LOG}" > "${CONTENT_LOG}.tmp" 2>/dev/null || true
  if [[ -s "${CONTENT_LOG}.tmp" ]]; then
    mv "${CONTENT_LOG}.tmp" "${CONTENT_LOG}"
  else
    rm -f "${CONTENT_LOG}.tmp" "${CONTENT_LOG}"
  fi
fi

if [[ ! -f "${SETTINGS}" && -f "${CONTENT_DIR}/settings.json" ]]; then
  cp "${CONTENT_DIR}/settings.json" "${SETTINGS}"
  echo "[sillytavern] seeded settings.json" >&2
fi

if [[ -f "${CONTENT_INDEX}" ]]; then
  python3 - "${CONTENT_INDEX}" "${CONTENT_DIR}" "${USER_DIR}" <<'PY'
import json
import pathlib
import shutil
import sys

index_path, content_dir, user_dir = map(pathlib.Path, sys.argv[1:4])

TYPE_TARGETS = {
    "settings": ".",
    "character": "characters",
    "sprites": "characters",
    "background": "backgrounds",
    "world": "worlds",
    "avatar": "User Avatars",
    "theme": "themes",
    "workflow": "user/workflows",
    "kobold_preset": "KoboldAI Settings",
    "openai_preset": "OpenAI Settings",
    "novel_preset": "NovelAI Settings",
    "textgen_preset": "TextGen Settings",
    "instruct": "instruct",
    "context": "context",
    "moving_ui": "movingUI",
    "quick_replies": "QuickReplies",
    "sysprompt": "sysprompt",
    "reasoning": "reasoning",
    "stylesheet": None,
    "error_page": None,
}

items = json.loads(index_path.read_text(encoding="utf-8"))
seeded = 0

for item in items:
    rel = item.get("filename")
    kind = item.get("type")
    if not rel or not kind:
        continue

    target_rel = TYPE_TARGETS.get(kind)
    if not target_rel:
        continue

    src = content_dir / rel
    if not src.exists():
        continue

    dest_dir = user_dir / target_rel if target_rel != "." else user_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / pathlib.Path(rel).name

    if dest.exists():
        continue

    if src.is_dir():
        shutil.copytree(src, dest)
    else:
        shutil.copy2(src, dest)
    seeded += 1

if seeded:
    print(f"[sillytavern] seeded {seeded} default content file(s)", flush=True)
PY
fi

chmod -R u+rwX "${ST_DATA_ROOT}" 2>/dev/null || true