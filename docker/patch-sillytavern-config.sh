#!/usr/bin/env bash
# Force HF-safe SillyTavern settings (persisted /data config may have whitelist on).
set -euo pipefail

DATA_ROOT="${DATA_ROOT:-/data}"
CONFIG="${DATA_ROOT}/sillytavern/config/config.yaml"

mkdir -p "$(dirname "${CONFIG}")"

if [[ ! -f "${CONFIG}" ]]; then
  cp /opt/hub/config/sillytavern-config.yaml "${CONFIG}"
fi

# Patch in place — nginx proxies as 127.0.0.1 with forwarded HF internal IPs
sed -i \
  -e 's/whitelistMode: true/whitelistMode: false/g' \
  -e 's/enableForwardedWhitelist: true/enableForwardedWhitelist: false/g' \
  -e 's/^listen: false/listen: true/g' \
  "${CONFIG}" 2>/dev/null || true

# hostWhitelist block — only flip the enabled line under hostWhitelist if present
python3 - <<'PY' "${CONFIG}" 2>/dev/null || true
import pathlib, re, sys
p = pathlib.Path(sys.argv[1])
text = p.read_text(encoding="utf-8")
text = re.sub(r"(?m)^listen: false", "listen: true", text)
text = re.sub(r"(?m)^whitelistMode: true", "whitelistMode: false", text)
text = re.sub(r"(?m)^  enabled: true(?=\n(?:  [a-z].*\n)*?hostWhitelist:)", "  enabled: false", text, count=0)
# simpler: hostWhitelist enabled
text = re.sub(
    r"(hostWhitelist:\n(?:  .*\n)*?  enabled:) true",
    r"\1 false",
    text,
)
p.write_text(text, encoding="utf-8")
PY

echo "[hub] SillyTavern config patched for reverse proxy" >&2