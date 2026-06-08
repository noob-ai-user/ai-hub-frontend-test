#!/usr/bin/env bash
# Lumiverse registers a PWA service worker that hijacks navigations to /hub
# and serves cached index.html → React Router 404. In hub mode we replace it.
set -euo pipefail

DIST="/apps/lumiverse/frontend/dist"
SW="${DIST}/sw.js"
INDEX="${DIST}/index.html"

if [[ ! -d "${DIST}" ]]; then
  echo "[hub] lumiverse dist not found — skip PWA patch" >&2
  exit 0
fi

if [[ -f "${SW}" ]]; then
  cat > "${SW}" <<'EOF'
/* AI Hub: Lumiverse PWA disabled — prevents /hub navigation hijack */
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
  echo "[hub] replaced lumiverse sw.js with hub-safe worker" >&2
fi

if [[ -f "${INDEX}" ]]; then
  python3 - "${INDEX}" <<'PY'
import re, sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
original = text

# Vite PWA inline registration (id may be vite-plugin-pwa:inline-sw)
text = re.sub(
    r"<script[^>]*vite-plugin-pwa[^>]*>.*?</script>",
    "<!-- hub: PWA registration removed -->",
    text,
    flags=re.DOTALL | re.IGNORECASE,
)

# Any serviceWorker.register(...) one-liner left in HTML
text = re.sub(
    r"<script[^>]*>\s*if\s*\(\s*['\"]serviceWorker['\"]\s+in\s+navigator\s*\)[^<]*</script>",
    "<!-- hub: service worker registration removed -->",
    text,
    flags=re.DOTALL | re.IGNORECASE,
)

if text != original:
    path.write_text(text, encoding="utf-8")
    print("[hub] stripped PWA registration from lumiverse index.html", flush=True)
else:
    print("[hub] lumiverse index.html unchanged (no PWA block found)", flush=True)
PY
fi