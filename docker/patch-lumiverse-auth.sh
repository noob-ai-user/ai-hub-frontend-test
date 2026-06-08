#!/usr/bin/env bash
# BetterAuth baseURL includes /apps/lumiverse — auth handler must rewrite requests
# with X-Forwarded-Prefix or sign-in returns 404 behind the hub gateway.
set -euo pipefail

APP_TS="/apps/lumiverse/src/app.ts"
if [[ ! -f "${APP_TS}" ]]; then
  echo "[hub] lumiverse app.ts not found — skip auth patch" >&2
  exit 0
fi

python3 - "${APP_TS}" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")

if "fwdPrefix" in text and "x-forwarded-prefix" in text:
    print("[hub] lumiverse auth patch already applied", flush=True)
    raise SystemExit(0)

replacement = (
    'const fwdPrefix = (c.req.header("x-forwarded-prefix") || "").replace(/\\/$/, "");\n'
    '    const rewritten = new URL((fwdPrefix + url.pathname).replace(/\\/\\/+/g, "/") + url.search, `${proto}://${host}`);'
)

patterns = [
    r"const rewritten = new URL\(url\.pathname \+ url\.search, `\$\{proto\}://\$\{host\}`\);",
    r"const rewritten=new URL\(url\.pathname\+url\.search,`\$\{proto\}://\$\{host\}`\);",
]

patched = False
for pattern in patterns:
    new_text, n = re.subn(pattern, replacement, text, count=1)
    if n:
        text = new_text
        patched = True
        break

if not patched:
    print("[hub] ERROR: lumiverse auth rewrite pattern not found in app.ts", flush=True)
    raise SystemExit(1)

path.write_text(text, encoding="utf-8")
print("[hub] patched lumiverse BetterAuth URL rewrite for subpath hub", flush=True)
PY