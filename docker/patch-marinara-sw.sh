#!/usr/bin/env bash
# Marinara PWA can cache stale bundles after hub subpath changes — replace with no-op SW.
set -uo pipefail

DIST="/apps/marinara/packages/client/dist"
SW="${DIST}/sw.js"
INDEX="${DIST}/index.html"

if [[ ! -d "${DIST}" ]]; then
  echo "[hub] marinara dist not found — skip PWA patch" >&2
  exit 0
fi

if [[ -f "${SW}" ]]; then
  cat > "${SW}" <<'EOF'
/* AI Hub: Marinara PWA disabled — prevents stale precached bundles under /apps/marinara/ */
self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(keys.map((key) => caches.delete(key)));
      await self.clients.claim();
    })()
  );
});
EOF
  echo "[hub] replaced marinara sw.js with hub-safe worker" >&2
fi

if [[ -f "${INDEX}" ]]; then
  python3 - "${INDEX}" <<'PY'
import re, sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
original = text
text = re.sub(
    r"<script[^>]*id=[\"']vite-plugin-pwa[^\"']*[\"'][^>]*>.*?</script>",
    "<!-- hub: PWA registration removed -->",
    text,
    flags=re.DOTALL | re.IGNORECASE,
)
if text != original:
    path.write_text(text, encoding="utf-8")
    print("[hub] stripped PWA registration from marinara index.html", flush=True)
PY
fi