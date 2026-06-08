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
# Re-apply dist subpath patches at boot (basename + /api/v1) in case image build skipped them.
python3 - /apps/lumiverse/frontend/dist /apps/lumiverse <<'PY' || echo "[lumiverse] warn: dist subpath patch failed" >&2
import re, sys
from pathlib import Path

root = Path(sys.argv[1])
prefix = sys.argv[2].rstrip("/")
if not root.is_dir():
    raise SystemExit(0)

def patch_js(text: str) -> str:
    text = text.replace("qs=`/api/v1`", f"qs=`{prefix}/api/v1`")
    text = text.replace("/api/v1/theme-assets", f"{prefix}/api/v1/theme-assets")
    text = text.replace("/api/v1/image-gen", f"{prefix}/api/v1/image-gen")
    for old, new in (
        ("basename:e=`/`", f"basename:e=`{prefix}`"),
        ("e.basename||`/`", f"e.basename||`{prefix}`"),
        ("S=e.basename||`/`", f"S=e.basename||`{prefix}`"),
        ("c=e.basename||`/`", f"c=e.basename||`{prefix}`"),
    ):
        text = text.replace(old, new)

    def repl_q(m: re.Match[str]) -> str:
        q, path = m.group(1), m.group(2)
        if path.startswith("/api") and not path.startswith(prefix + "/"):
            return f"{q}{prefix}{path}{q}"
        return m.group(0)

    text = re.sub(r'(["\'])(/api[^"\'\\]*)\1', repl_q, text)
    text = re.sub(r"`(/api[^`\\]*)`", lambda m: f"`{prefix}{m.group(1)}`" if not m.group(1).startswith(prefix + "/") else m.group(0), text)
    return text

changed = 0
for path in root.rglob("*.js"):
    try:
        original = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        continue
    text = patch_js(original)
    if text != original:
        path.write_text(text, encoding="utf-8")
        changed += 1
print(f"[lumiverse] boot-time dist patch files_changed={changed}", flush=True)
PY

exec bun run src/index.ts