#!/usr/bin/env bash
# Bake subpath prefixes into static bundles at image build time (faster + safer than
# rewriting every endpoint suffix like "/chats" at runtime).
set -uo pipefail

patch_tree() {
  local dir="$1"
  local prefix="$2"
  if [[ ! -d "${dir}" ]]; then
    echo "[hub] skip subpath patch — missing ${dir}" >&2
    return 0
  fi

  python3 - "${dir}" "${prefix}" <<'PY'
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
prefix = sys.argv[2].rstrip("/")
hub_api = (
    "/api/hub",
    "/api/active",
    "/api/ready",
    "/api/debug",
    "/api/sync",
)


def skip(path: str) -> bool:
    return path.startswith(prefix + "/") or path.startswith("//") or any(
        path.startswith(h) for h in hub_api
    )


def rewrite_api_js(text: str) -> str:
    text = text.replace('const At="/api"', f'const At="{prefix}/api"')
    text = text.replace("const At='/api'", f"const At='{prefix}/api'")
    text = text.replace("qs=`/api/v1`", f"qs=`{prefix}/api/v1`")
    # CSS url() templates and other interpolated /api/v1 paths in minified bundles.
    text = text.replace("/api/v1/theme-assets", f"{prefix}/api/v1/theme-assets")
    text = text.replace("/api/v1/image-gen", f"{prefix}/api/v1/image-gen")

    def repl_quoted(match: re.Match[str]) -> str:
        quote, path = match.group(1), match.group(2)
        if not path.startswith("/api") or skip(path):
            return match.group(0)
        return f"{quote}{prefix}{path}{quote}"

    def repl_backtick(match: re.Match[str]) -> str:
        path = match.group(1)
        if not path.startswith("/api") or skip(path):
            return match.group(0)
        return f"`{prefix}{path}`"

    text = re.sub(r'(["\'])(/api[^"\'\\]*)\1', repl_quoted, text)
    text = re.sub(r"`(/api[^`\\]*)`", repl_backtick, text)
    return text


def rewrite_router_basename(text: str) -> str:
    """React Router defaults to basename=/ — breaks SPA routing under /apps/{app}/."""
    basename_default = f"basename:e=`{prefix}`"
    replacements = (
        ("basename:e=`/`", basename_default),
        ("e.basename||`/`", f"e.basename||`{prefix}`"),
        ("S=e.basename||`/`", f"S=e.basename||`{prefix}`"),
        ("c=e.basename||`/`", f"c=e.basename||`{prefix}`"),
    )
    for old, new in replacements:
        text = text.replace(old, new)
    return text


def rewrite_static(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        quote, path = match.group(1), match.group(2)
        if skip(path):
            return match.group(0)
        return f"{quote}{prefix}{path}{quote}"

    return re.sub(r'(["\'])(/(?!/)[^"\'\\]*)\1', repl, text)


def fix_base_href(text: str) -> str:
    tag = f'<base href="{prefix}/">'
    if re.search(r"<base\s", text, re.I):
        return re.sub(
            r"<base\s+href=[\"'][^\"']*[\"']\s*/?\s*>",
            tag,
            text,
            count=1,
            flags=re.I,
        )
    head = re.search(r"<head([^>]*)>", text, re.I)
    if head:
        pos = head.end()
        return text[:pos] + f"\n  {tag}" + text[pos:]
    return tag + text


changed = 0
for path in root.rglob("*"):
    if not path.is_file():
        continue
    name = path.name.lower()
    if name.endswith((".html", ".css", ".json", ".js", ".mjs")):
        try:
            original = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        text = original
        if name.endswith(".html"):
            text = fix_base_href(text)
            text = rewrite_static(text)
        elif name.endswith((".css", ".json")):
            text = rewrite_static(text)
        elif name.endswith((".js", ".mjs")):
            text = rewrite_api_js(text)
            text = rewrite_static(text)
            if prefix.endswith("/lumiverse"):
                text = rewrite_router_basename(text)
        if text != original:
            path.write_text(text, encoding="utf-8")
            changed += 1

print(f"[hub] subpath patch {root} prefix={prefix}/ files_changed={changed}", flush=True)
PY
}

patch_tree "/apps/marinara/packages/client/dist" "/apps/marinara"
patch_tree "/apps/lumiverse/frontend/dist" "/apps/lumiverse"