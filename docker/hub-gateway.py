#!/usr/bin/env python3
"""HF public gateway on :7860 — SillyTavern at /, Lumiverse/Marinara at /apps/{name}/."""
from __future__ import annotations

import gzip
import http.client
import json
import os
import re
import select
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from concurrent.futures import ThreadPoolExecutor
from socketserver import ThreadingMixIn
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import urllib.request
import urllib.error

class ThreadPoolHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    request_queue_size = 512
    executor = ThreadPoolExecutor(max_workers=1024)

    def process_request(self, request, client_address):
        self.executor.submit(self.process_request_thread, request, client_address)

DATA_ROOT = Path(os.environ.get("DATA_ROOT", "/data"))
PUBLIC = Path("/opt/hub/public")
SYNC_SCRIPT = "/opt/hub/scripts/sync-shared-data.sh"
ACTIVE_FILE = DATA_ROOT / ".active_app"
HUB_PORT = int(os.environ.get("HUB_PORT", "7860"))

PORTS = {
    "sillytavern": int(os.environ.get("ST_PORT", "8000")),
    "lumiverse": int(os.environ.get("LUMIVERSE_PORT", "7861")),
    "marinara": int(os.environ.get("MARINARA_PORT", "7862")),
}

# SillyTavern is served at / (native paths). Only Vite SPAs need subpath prefixes.
APP_PREFIXES = {
    "lumiverse": "/apps/lumiverse",
    "marinara": "/apps/marinara",
}

HUB_ONLY_PATHS = {
    "/api/hub",
    "/api/hub/",
    "/api/active",
    "/api/ready",
    "/api/debug",
    "/api/sync",
    "/hub",
    "/hub/",
    "/hub.html",
    "/hub/favicon.ico",
}

# Root-level paths Vite SPAs still request without Referer (dynamic import / PWA).
ORPHAN_APP_PATH_PREFIXES = (
    "/assets/",
    "/logo-",
    "/icon-",
    "/manifest",
    "/registerSW.js",
)

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

SKIP_REQUEST_HEADERS = {
    "host",
    "connection",
    "content-length",
    "transfer-encoding",
    "accept-encoding",
    # Avoid 304 revalidation serving pre-v10 poisoned cached bodies in browsers.
    "if-none-match",
    "if-modified-since",
}

SKIP_RESPONSE_CACHE_HEADERS = {
    "cache-control",
    "etag",
    "last-modified",
    "expires",
}

MAX_JS_REWRITE_BYTES = int(os.environ.get("MAX_JS_REWRITE_BYTES", "524288"))

# Markers that build-time patch scripts (docker/patch-app-subpaths.sh) have run.
BUILD_PATCH_MARKERS: dict[str, tuple[str, ...]] = {
    "lumiverse": ("qs=`/apps/lumiverse/api/v1`", "basename:e=`/apps/lumiverse`"),
    "marinara": ("qs=`/apps/marinara/api/v1`", 'const At="/apps/marinara/api"', "basename:e=`/apps/marinara`"),
}


def active_app() -> str:
    if ACTIVE_FILE.is_file():
        name = ACTIVE_FILE.read_text(encoding="utf-8").strip().lower()
        if name in PORTS:
            return name
    return "sillytavern"


def backend_port(app: str) -> int:
    return PORTS.get(app, PORTS["sillytavern"])


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def backend_ready(app: str) -> bool:
    port = PORTS.get(app)
    if port is None or not port_open(port):
        return False
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        conn.request("GET", "/", headers={"Accept": "text/html,application/json", "User-Agent": "hub-ready-probe"})
        resp = conn.getresponse()
        resp.read()
        return 200 <= resp.status < 500
    except Exception:
        return port_open(port)


def app_from_referer(referer: str) -> str | None:
    if not referer:
        return None
    for app, prefix in APP_PREFIXES.items():
        if f"{prefix}/" in referer or referer.rstrip("/").endswith(prefix):
            return app
    return None


def app_from_origin(origin: str) -> str | None:
    if not origin:
        return None
    origin = origin.rstrip("/")
    for app, prefix in APP_PREFIXES.items():
        if origin.endswith(prefix):
            return app
    return None


def app_from_cookie(cookie_header: str) -> str | None:
    if not cookie_header:
        return None
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith("hub_app="):
            app = part.split("=", 1)[1].strip().lower()
            if app in PORTS:
                return app
    return None


HUB_API_PREFIXES = ("/api/hub", "/api/active", "/api/ready", "/api/debug", "/api/sync")


def decompress_body(data: bytes, encoding: str | None) -> tuple[bytes, bool]:
    """Decompress backend body when possible. Returns (body, was_decompressed)."""
    if not encoding:
        return data, False

    enc = encoding.lower()
    if "gzip" in enc or enc == "x-gzip":
        try:
            return gzip.decompress(data), True
        except OSError:
            return data, False

    if "br" in enc:
        try:
            import brotli  # type: ignore[import-not-found]

            return brotli.decompress(data), True
        except Exception:
            return data, False

    if "deflate" in enc:
        try:
            import zlib

            return zlib.decompress(data), True
        except Exception:
            return data, False

    return data, False


# ── Chub / charhub proxy helpers ──────────────────────────────────────────
# Chub (api.chub.ai) and the avatar CDN (avatars.charhub.io) both sit behind
# Cloudflare. Requests from datacenter IPs (like HF Spaces) succeed as long as
# we send a normal browser User-Agent; the previous "Node fetch gets a 403"
# theory was not reproducible. The important robustness fixes here are:
#   1. Surface the *real* upstream status/body instead of masking everything as
#      a generic 500 (that opaque 500 was the actual "proxy broke" symptom).
#   2. Always decompress, so rewriting avatar URLs as text never silently fails
#      if Chub ever turns on gzip/brotli.
CHUB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
CHUB_CDN_PREFIX = "https://avatars.charhub.io/avatars/"
CHUB_AVATAR_PROXY = "/api/storage/chub/avatar/"


def chub_fetch(
    url: str,
    *,
    accept: str = "application/json",
    referer: str | None = "https://chub.ai/",
    timeout: int = 20,
) -> tuple[int, bytes, str]:
    """Fetch a Chub/charhub URL with browser-like headers.

    Returns ``(status, body, content_type)``. The body is always decompressed.
    On an HTTP error the upstream status code and error body are returned rather
    than raising, so the gateway can pass Chub's real status through to the
    browser (visible in the network tab) instead of an opaque 500.
    """
    headers = {
        "User-Agent": CHUB_UA,
        "Accept": accept,
        # We always decompress below, so advertising gzip is safe.
        "Accept-Encoding": "gzip, deflate",
    }
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        status = 200
    except urllib.error.HTTPError as exc:  # 4xx / 5xx from Chub
        resp = exc
        status = exc.code
    with resp:
        raw = resp.read()
        enc = resp.headers.get("Content-Encoding")
        ctype = resp.headers.get_content_type() or "application/octet-stream"
    body, _ = decompress_body(raw, enc)
    return status, body, ctype


def rewrite_chub_avatars(body: bytes) -> bytes:
    """Route avatar CDN URLs in a Chub JSON body through our avatar proxy."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return body
    return text.replace(CHUB_CDN_PREFIX, CHUB_AVATAR_PROXY).encode("utf-8")


def build_chub_search_url(query: str) -> str:
    """Translate incoming search params (storage.html *and* Marinara frontend)
    into the parameter names api.chub.ai actually expects.

    Marinara's own backend (packages/server/src/routes/bot-browser.routes.ts)
    forwards a rich filter set; the gateway used to drop all of it, so tag /
    token / time filters silently did nothing. This keeps parity.
    """
    from urllib.parse import urlencode

    qs = parse_qs(query)

    def first(key: str, default: str | None = None) -> str | None:
        vals = qs.get(key)
        return vals[0] if vals else default

    out: dict[str, str] = {}
    out["search"] = first("search") or first("q") or ""
    out["first"] = first("first", "48")
    out["page"] = first("page", "1")

    nsfw = first("nsfw", "true")
    out["nsfw"] = nsfw
    out["nsfl"] = nsfw

    # Chub defaults Marinara always sends.
    out["include_forks"] = first("include_forks", "true")
    out["venus"] = first("venus", "false")
    out["min_tokens"] = first("min_tokens", "50")

    sort = first("sort", "download_count")
    if sort == "downloads":  # legacy storage.html value
        sort = "download_count"
    if sort and sort != "default":
        out["sort"] = sort

    if first("asc") == "true":
        out["asc"] = "true"

    max_days = first("max_days_ago")
    if max_days and max_days != "0":
        out["max_days_ago"] = max_days

    for key in ("special_mode", "username", "max_tokens"):
        val = first(key)
        if val:
            out[key] = val

    # Tag filters use Chub's "topics" / "excludetopics" names.
    tags = first("tags") or first("topics")
    if tags:
        out["topics"] = tags
    extags = first("excludeTags") or first("excludetopics")
    if extags:
        out["excludetopics"] = extags

    for key in (
        "require_images",
        "require_lore",
        "require_expressions",
        "require_alternate_greetings",
    ):
        if first(key) == "true":
            out[key] = "true"

    return "https://api.chub.ai/search?" + urlencode(out)


# ── DataCat (datacat.run) — JanitorAI character aggregator ─────────────────
# DataCat exposes a clean REST API (search + downloadable V2 cards) but, unlike
# Chub, sends NO `Access-Control-Allow-Origin`, so the browser cannot call it
# cross-origin. It also requires a session token and Origin/Referer headers the
# browser can't forge. So we proxy it server-side. Unlike api.chub.ai, DataCat
# is not behind aggressive Cloudflare bot-mitigation and serves datacenter IPs,
# so the gateway can reach it. Avatars live on ella.janitorai.com (CORS:*) and
# are loaded directly by the browser — not proxied here.
DATACAT_BASE = "https://datacat.run"
DATACAT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_datacat_token = {"value": "", "ts": 0.0}
_datacat_lock = threading.Lock()


def _datacat_mint_token() -> str:
    import uuid
    body = json.dumps({"deviceToken": str(uuid.uuid4())}).encode("utf-8")
    req = urllib.request.Request(
        f"{DATACAT_BASE}/api/liberator/identify",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": DATACAT_UA,
            "Origin": DATACAT_BASE,
            "Referer": DATACAT_BASE + "/",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
    token = data.get("sessionToken")
    if not token:
        raise RuntimeError("datacat: response missing sessionToken")
    return token


def datacat_token(force: bool = False) -> str:
    import time
    with _datacat_lock:
        if (not force and _datacat_token["value"]
                and time.time() - _datacat_token["ts"] < 1800):
            return _datacat_token["value"]
        token = _datacat_mint_token()
        _datacat_token["value"] = token
        _datacat_token["ts"] = time.time()
        return token


def datacat_get(path: str) -> tuple[int, bytes]:
    """GET a DataCat API path with the cached session token, decompressing the
    body and re-minting the token once on a 401. Returns (status, body)."""
    def _do(token: str) -> tuple[int, bytes]:
        req = urllib.request.Request(
            f"{DATACAT_BASE}{path}",
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip, deflate",
                "User-Agent": DATACAT_UA,
                "Origin": DATACAT_BASE,
                "Referer": DATACAT_BASE + "/",
                "X-Session-Token": token,
            },
        )
        try:
            resp = urllib.request.urlopen(req, timeout=25)
            status = 200
        except urllib.error.HTTPError as exc:
            resp = exc
            status = exc.code
        with resp:
            raw = resp.read()
            enc = resp.headers.get("Content-Encoding")
        body, _ = decompress_body(raw, enc)
        return status, body

    status, body = _do(datacat_token())
    if status == 401:
        status, body = _do(datacat_token(force=True))
    return status, body


def fix_base_href(text: str, prefix: str) -> str:
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


def _skip_path(path: str, prefix: str) -> bool:
    return path.startswith(prefix + "/") or path.startswith("//") or any(
        path.startswith(h) for h in HUB_API_PREFIXES
    )


def strip_lumiverse_pwa_html(text: str) -> str:
    """Remove inline PWA registration — stale SW breaks subpath loading."""
    text = re.sub(
        r"<script[^>]*vite-plugin-pwa[^>]*>.*?</script>",
        "<!-- hub: lumiverse PWA removed -->",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    return text


def rewrite_root_paths(text: str, prefix: str) -> str:
    """Rewrite root-absolute URLs in HTML/CSS/JSON — <base> does NOT affect paths starting with /."""

    def repl_quoted(match: re.Match[str]) -> str:
        quote, path = match.group(1), match.group(2)
        if _skip_path(path, prefix):
            return match.group(0)
        return f"{quote}{prefix}{path}{quote}"

    def repl_backtick(match: re.Match[str]) -> str:
        path = match.group(1)
        if not path.startswith("/") or _skip_path(path, prefix):
            return match.group(0)
        return f"`{prefix}{path}`"

    text = re.sub(r'(["\'])(/(?!/)[^"\'\\]*)\1', repl_quoted, text)
    text = re.sub(r"`(/(?!/)[^`\\]+)`", repl_backtick, text)
    text = re.sub(
        r'(\bimport\s*\(\s*)(["\'])(/(?!/)[^"\'\\]*)\2',
        lambda m: (
            f"{m.group(1)}{m.group(2)}{prefix}{m.group(3)}{m.group(2)}"
            if not _skip_path(m.group(3), prefix)
            else m.group(0)
        ),
        text,
    )
    text = re.sub(
        r'(\bnew URL\s*\(\s*)(["\'])(/(?!/)[^"\'\\]*)\2',
        lambda m: (
            f"{m.group(1)}{m.group(2)}{prefix}{m.group(3)}{m.group(2)}"
            if not _skip_path(m.group(3), prefix)
            else m.group(0)
        ),
        text,
    )
    return text


def strip_erroneous_app_prefix(text: str, prefix: str) -> str:
    """Undo legacy v5 gateway rewriting of API endpoint suffixes in JS bundles."""

    def repl_quoted(match: re.Match[str]) -> str:
        quote, path = match.group(1), match.group(2)
        if not path.startswith(prefix + "/") or path.startswith(prefix + "/api/"):
            return match.group(0)
        return f"{quote}{path[len(prefix):]}{quote}"

    return re.sub(r'(["\'])(/[^"\'\\]+)\1', repl_quoted, text)


def rewrite_js_api_paths(text: str, prefix: str) -> str:
    """Rewrite only /api* URLs in JS.

    Do NOT prefix bare endpoint suffixes like "/chats" — Marinara composes
    fetch(`${API_BASE}${endpoint}`) and double-prefixing breaks every API call.
    """
    text = strip_erroneous_app_prefix(text, prefix)

    def repl_quoted(match: re.Match[str]) -> str:
        quote, path = match.group(1), match.group(2)
        if not path.startswith("/api") or _skip_path(path, prefix):
            return match.group(0)
        return f"{quote}{prefix}{path}{quote}"

    def repl_backtick(match: re.Match[str]) -> str:
        path = match.group(1)
        if not path.startswith("/api") or _skip_path(path, prefix):
            return match.group(0)
        return f"`{prefix}{path}`"

    text = text.replace('const At="/api"', f'const At="{prefix}/api"')
    text = text.replace("const At='/api'", f"const At='{prefix}/api'")
    text = text.replace("qs=`/api/v1`", f"qs=`{prefix}/api/v1`")
    text = re.sub(r'(["\'])(/api[^"\'\\]*)\1', repl_quoted, text)
    text = re.sub(r"`(/api[^`\\]*)`", repl_backtick, text)
    text = re.sub(
        r'(\bimport\s*\(\s*)(["\'])(/api[^"\'\\]*)\2',
        lambda m: (
            f"{m.group(1)}{m.group(2)}{prefix}{m.group(3)}{m.group(2)}"
            if not _skip_path(m.group(3), prefix)
            else m.group(0)
        ),
        text,
    )
    text = re.sub(
        r'(\bnew URL\s*\(\s*)(["\'])(/api[^"\'\\]*)\2',
        lambda m: (
            f"{m.group(1)}{m.group(2)}{prefix}{m.group(3)}{m.group(2)}"
            if not _skip_path(m.group(3), prefix)
            else m.group(0)
        ),
        text,
    )
    return text


def js_already_build_patched(text: str, app: str) -> bool:
    markers = BUILD_PATCH_MARKERS.get(app)
    if not markers:
        return False
    return any(marker in text for marker in markers)


def patch_lumiverse_router_basename(text: str, prefix: str) -> str:
    """React Router defaults to basename=/ — routes fail under /apps/lumiverse/."""
    marker = f"basename:e=`{prefix}`"
    if marker in text:
        return text
    replacements = (
        ("basename:e=`/`", marker),
        ("e.basename||`/`", f"e.basename||`{prefix}`"),
        ("S=e.basename||`/`", f"S=e.basename||`{prefix}`"),
        ("c=e.basename||`/`", f"c=e.basename||`{prefix}`"),
    )
    for old, new in replacements:
        text = text.replace(old, new)
    return text


def patch_lumiverse_js(text: str, prefix: str) -> str:
    """Apply basename + /api* prefixing for Lumiverse entry/lazy chunks."""
    text = patch_lumiverse_router_basename(text, prefix)
    api_marker = f"qs=`{prefix}/api/v1`"
    if api_marker not in text:
        text = rewrite_js_api_paths(text, prefix)
        # Interpolated CSS url() templates: url(${q}/api/v1/theme-assets/...)
        text = text.replace("/api/v1/theme-assets", f"{prefix}/api/v1/theme-assets")
        text = text.replace("/api/v1/image-gen", f"{prefix}/api/v1/image-gen")
    return text


def inject_marinara_chub_proxy(html: str) -> str:
    if "<head>" not in html:
        return html
    script = """<script>
(function() {
  const originalFetch = window.fetch;
  window.fetch = async function(...args) {
    let url = args[0];
    if (typeof url === 'string') {
      if (url.includes('/api/bot-browser/chub/search')) {
        const urlObj = new URL(url, window.location.origin);
        const chubUrl = '/api/storage/chub/search' + urlObj.search;
        try { return await originalFetch(chubUrl); } catch (e) { console.warn("Chub search proxy failed", e); }
      } else if (url.includes('/api/bot-browser/chub/character/')) {
        const id = url.split('/character/')[1].split('?')[0];
        const chubUrl = '/api/storage/chub/character/' + id;
        try { return await originalFetch(chubUrl); } catch (e) { console.warn("Chub char proxy failed", e); }
      }
    }
    return originalFetch.apply(this, args);
  };
})();
</script>"""
    return html.replace("<head>", f"<head>\n{script}\n", 1)


def rewrite_app_body(data: bytes, content_type: str, prefix: str, app: str = "") -> bytes:
    if not prefix:
        return data
    ct = content_type.lower()
    if not any(
        token in ct
        for token in ("text/html", "javascript", "text/css", "json", "manifest")
    ):
        return data
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    if "javascript" in ct:
        # Vite/Marinara/Lumiverse bundles are patched at image build — do not
        # re-decode multi-MB chunks on every request (slow + risks corruption).
        if app in ("lumiverse", "marinara") and js_already_build_patched(text, app):
            return data
        if app == "lumiverse":
            patched = patch_lumiverse_js(text, prefix)
            if patched != text:
                return patched.encode("utf-8")
            return data
        if js_already_build_patched(text, app):
            return data
        if len(data) > MAX_JS_REWRITE_BYTES:
            return data
        text = rewrite_js_api_paths(text, prefix)
    else:
        if "text/html" in ct:
            text = fix_base_href(text, prefix)
            if app == "lumiverse":
                text = strip_lumiverse_pwa_html(text)
            if app == "marinara":
                text = inject_marinara_chub_proxy(text)
        text = rewrite_root_paths(text, prefix)
    return text.encode("utf-8")


def proxy_cache_headers(app: str, content_type: str) -> dict[str, str]:
    """Override backend cache headers for subpath SPAs (avoid stale gzip in browser cache)."""
    ct = content_type.lower()
    if "javascript" in ct:
        return {"Cache-Control": "no-cache"}
    return {}


def rewrite_location(location: str, prefix: str) -> str:
    if not location.startswith("/") or location.startswith("//"):
        return location
    if location == prefix or location.startswith(prefix + "/"):
        return location
    return prefix + location


def resolve_route(
    path: str,
    referer: str,
    query: str = "",
    origin: str = "",
    cookie: str = "",
) -> tuple[str, str]:
    """Return (app_name, backend_path)."""
    for app, prefix in APP_PREFIXES.items():
        if path == prefix:
            return app, "/"
        if path.startswith(prefix + "/"):
            return app, path[len(prefix) :] or "/"

    context_app = (
        app_from_referer(referer)
        or app_from_origin(origin)
        or app_from_cookie(cookie)
    )
    if context_app and (
        any(path.startswith(prefix) for prefix in ORPHAN_APP_PATH_PREFIXES)
        or path == "/manifest.webmanifest"
    ):
        return context_app, path

    # SillyTavern owns / and all root paths not claimed by hub or subpath SPAs.
    return "sillytavern", path


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 15
    server_version = "hub-gateway/16"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[gateway] {self.address_string()} - {fmt % args}", flush=True)

    def _parsed(self) -> tuple[str, str, str]:
        parsed = urlparse(self.path)
        return parsed.path or "/", parsed.query, self.headers.get("Referer", "")

    def _send_bytes(self, code: int, body: bytes, content_type: str, extra_headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, payload: dict) -> None:
        self._send_bytes(code, json.dumps(payload).encode("utf-8"), "application/json")

    def _send_html(self, filename: str, cache_control: str = "no-cache") -> None:
        path = PUBLIC / filename
        if not path.is_file():
            self._send_json(404, {"error": f"{filename} missing"})
            return
        self._send_bytes(
            200,
            path.read_bytes(),
            "text/html; charset=utf-8",
            {"Cache-Control": cache_control},
        )

    def _send_public_file(self, filename: str, content_type: str) -> None:
        path = PUBLIC / filename
        if not path.is_file():
            self._send_json(404, {"error": f"{filename} missing"})
            return
        self._send_bytes(
            200,
            path.read_bytes(),
            content_type,
            {"Cache-Control": "public, max-age=86400"},
        )

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _run_sync_background(self) -> None:
        try:
            subprocess.run([SYNC_SCRIPT], capture_output=True, text=True, timeout=300, check=False)
        except Exception as exc:
            print(f"[gateway] background sync failed: {exc}", flush=True)

    def _handle_hub_route(self, method: str) -> bool:
        path, query, _referer = self._parsed()

        if path in HUB_ONLY_PATHS:
            if path in {"/api/hub", "/api/hub/", "/hub/", "/hub.html"}:
                filename = "hub.html" if path == "/hub.html" else "index.html"
                self._send_html(filename)
                return True
            if path == "/hub":
                self._send_html("hub-redirect.html")
                return True

        if path == "/api/active":
            apps = {"sillytavern": "/"}
            apps.update(APP_PREFIXES)
            self._send_json(
                200,
                {
                    "active": active_app(),
                    "routing": "st-root+subpath-spas",
                    "hub_launcher": "/hub",
                    "apps": apps,
                },
            )
            return True

        if path == "/api/ready":
            probes = {name: backend_ready(name) for name in PORTS}
            self._send_json(200, {"routing": "parallel", "ready": probes})
            return True

        if path == "/api/debug":
            probes = {}
            for name, port in PORTS.items():
                probes[name] = {
                    "port": port,
                    "prefix": APP_PREFIXES.get(name, "/"),
                    "port_open": port_open(port),
                    "http_ready": backend_ready(name),
                }
            shared_chars = DATA_ROOT / "shared" / "characters"
            hub_cards = sorted(
                p.name
                for p in shared_chars.glob("hub_*.png")
                if p.is_file()
            ) if shared_chars.is_dir() else []

            # Count characters per app for debug visibility
            st_chars_dir = DATA_ROOT / "sillytavern" / "data" / "default-user" / "characters"
            st_char_count = len([
                p for p in st_chars_dir.iterdir()
                if p.is_file() and p.suffix.lower() in {".png", ".json"} and not p.name.startswith("hub_")
            ]) if st_chars_dir.is_dir() else 0
            import subprocess as _sp
            ma_char_count = 0
            lu_char_count = 0
            try:
                if port_open(PORTS["marinara"]):
                    import urllib.request as _ur
                    _resp = _ur.urlopen(f"http://127.0.0.1:{PORTS['marinara']}/api/characters/", timeout=3)
                    _data = json.loads(_resp.read())
                    ma_char_count = len(_data) if isinstance(_data, list) else 0
            except Exception:
                pass
            try:
                if port_open(PORTS["lumiverse"]):
                    import urllib.request as _ur2
                    _resp2 = _ur2.urlopen(f"http://127.0.0.1:{PORTS['lumiverse']}/api/v1/characters?limit=1", timeout=3)
                    _data2 = json.loads(_resp2.read())
                    if isinstance(_data2, dict) and "data" in _data2:
                        # Try to get total count
                        lu_char_count = len(_data2.get("data", []))
            except Exception:
                pass
            sync_state = DATA_ROOT / ".hub-sync" / "import-state.json"
            sync_hint = {
                "st_character_count": st_char_count,
                "marinara_character_count": ma_char_count,
                "lumiverse_character_count": lu_char_count,
                "owner_password_set": bool(
                    os.environ.get("OWNER_PASSWORD") or os.environ.get("HUB_SYNC_PASSWORD")
                ),
                "lumiverse_import_requires": "OWNER_PASSWORD in HF Secrets (Lumiverse login password)",
                "canonical_cards": hub_cards,
                "st_storage": str(DATA_ROOT / "sillytavern" / "data" / "default-user"),
                "lumiverse_storage": str(DATA_ROOT / "lumiverse"),
                "marinara_storage": str(DATA_ROOT / "marinara"),
                "sync_state_file": str(sync_state) if sync_state.is_file() else None,
            }
            self._send_json(
                200,
                {
                    "routing": "ST at / ; lumiverse+marinara at /apps/{app}/",
                    "gateway_version": self.server_version,
                    "hub_launcher": "/hub",
                    "active_fallback": active_app(),
                    "apps": probes,
                    "shared_characters": str(shared_chars),
                    "sync": sync_hint,
                },
            )
            return True

        if path == "/api/sync" and method == "GET":
            # Run sync synchronously so the caller gets actual results
            import subprocess as _sp
            try:
                proc = _sp.run(
                    [SYNC_SCRIPT], capture_output=True, text=True, timeout=300, check=False,
                )
                lines = (proc.stdout or proc.stderr or "").strip().splitlines()
                tail = lines[-12:] if lines else []
                self._send_json(200, {
                    "ok": proc.returncode == 0,
                    "exit_code": proc.returncode,
                    "log": tail,
                })
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
            return True

        if path in {"/storage", "/storage/", "/api/storage", "/api/storage/"} and method == "GET":
            self._send_public_file("storage.html", "text/html; charset=utf-8")
            return True

        if path == "/api/storage/list" and method == "GET":
            shared_dir = DATA_ROOT / "shared"
            chars = []
            lores = []
            
            import base64
            import json
            import urllib.parse
            import struct

            def parse_metadata(fpath):
                ext = fpath.suffix.lower()
                if ext == ".json":
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            return json.load(f)
                    except:
                        pass
                elif ext == ".png":
                    try:
                        with open(fpath, "rb") as f:
                            if f.read(8) != b'\x89PNG\r\n\x1a\n': return None
                            while True:
                                length_bytes = f.read(4)
                                if len(length_bytes) < 4: break
                                length = struct.unpack('>I', length_bytes)[0]
                                chunk_type = f.read(4)
                                if chunk_type == b'tEXt':
                                    data = f.read(length)
                                    f.read(4) # crc
                                    if data.startswith(b'chara\0'):
                                        b64_data = data[6:]
                                        try:
                                            json_str = urllib.parse.unquote(base64.b64decode(b64_data).decode('utf-8', 'ignore'))
                                            return json.loads(json_str)
                                        except:
                                            pass
                                    elif data.startswith(b'ccv3\0'):
                                        b64_data = data[5:]
                                        try:
                                            json_str = urllib.parse.unquote(base64.b64decode(b64_data).decode('utf-8', 'ignore'))
                                            return json.loads(json_str)
                                        except:
                                            pass
                                else:
                                    f.seek(length + 4, 1) # skip data and crc
                    except:
                        pass
                return None

            for t_dir, lst in [("characters", chars), ("world_info", lores)]:
                d = shared_dir / t_dir
                if d.is_dir():
                    for f in d.iterdir():
                        if f.is_file():
                            st = f.stat()
                            meta = parse_metadata(f)
                            lst.append({
                                "name": f.name,
                                "path": str(f.resolve()),
                                "size": st.st_size,
                                "mtime": st.st_mtime,
                                "metadata": meta
                            })
            self._send_json(200, {"characters": chars, "lorebooks": lores})
            return True

        if path == "/api/storage/upload" and method == "POST":
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                if content_length > 10 * 1024 * 1024:
                    self._send_json(413, {"error": "File too large (max 10MB)"})
                    return True

                ctype = self.headers.get('Content-Type', '')
                if not ctype.startswith('multipart/form-data'):
                    self._send_json(400, {"error": "Must be multipart/form-data"})
                    return True
                
                # Simple multipart parser for exactly 1 file part
                import cgi
                form = cgi.FieldStorage(
                    fp=self.rfile,
                    headers=self.headers,
                    environ={'REQUEST_METHOD': 'POST', 'CONTENT_TYPE': ctype}
                )
                
                if 'file' not in form:
                    self._send_json(400, {"error": "No file uploaded"})
                    return True
                
                file_item = form['file']
                filename = file_item.filename
                file_data = file_item.file.read()

                if not filename or not file_data:
                    self._send_json(400, {"error": "Empty file"})
                    return True

                ext = filename.lower()
                is_char = ext.endswith('.png') or ext.endswith('.webp')
                is_lore = ext.endswith('.json')
                
                if not (is_char or is_lore):
                    self._send_json(400, {"error": "Only .png, .webp, or .json allowed"})
                    return True

                folder = "characters" if is_char else "world_info"
                # Canonicalize to hub_{slug}.{ext} so the sync treats it as a
                # shared card and propagates it to every frontend. Non-canonical
                # names (e.g. "character.png") are pruned as junk and vanish.
                import re as _re
                stem, dot, fext = filename.rpartition(".")
                stem = stem or filename
                if not stem.lower().startswith("hub_"):
                    slug = _re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_")[:60] or "character"
                    filename = f"hub_{slug}.{fext.lower()}"
                dest_path = DATA_ROOT / "shared" / folder / filename

                with open(dest_path, "wb") as f:
                    f.write(file_data)

                self._send_json(200, {"success": True, "path": str(dest_path), "name": filename})
                return True
            except Exception as e:
                self._send_json(500, {"error": str(e)})
                return True

        if path == "/api/storage/download" and method == "GET":
            qs = parse_qs(query)
            target = qs.get("path", [""])[0]
            if not target:
                self._send_json(400, {"error": "path required"})
                return True
            try:
                target_path = Path(target).resolve()
                shared_dir = (DATA_ROOT / "shared").resolve()
                if not str(target_path).startswith(str(shared_dir)) or not target_path.is_file():
                    self._send_json(403, {"error": "Invalid path"})
                    return True
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", f'attachment; filename="{target_path.name}"')
                self.send_header("Content-Length", str(target_path.stat().st_size))
                self.end_headers()
                with open(target_path, "rb") as fh:
                    self.wfile.write(fh.read())
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return True

        if path == "/api/storage/delete" and method == "POST":
            qs = parse_qs(query)
            target = qs.get("path", [""])[0]
            if not target:
                self._send_json(400, {"error": "path required"})
                return True
            try:
                target_path = Path(target).resolve()
                shared_dir = (DATA_ROOT / "shared").resolve()
                # Only allow deleting regular files inside /data/shared.
                if not str(target_path).startswith(str(shared_dir) + os.sep) or not target_path.is_file():
                    self._send_json(403, {"error": "Invalid path"})
                    return True
                target_path.unlink()
                self._send_json(200, {"success": True})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return True

        if path in ("/api/storage/chub/search", "/apps/marinara/api/bot-browser/chub/search") and method == "GET":
            # Cloudflare blocks the HF Space's datacenter IP with a 403 when the
            # gateway fetches Chub server-side. Instead we 302-redirect the
            # browser to Chub directly: the user's residential IP + real browser
            # TLS pass Cloudflare, and Chub sends `Access-Control-Allow-Origin: *`
            # so the cross-origin JSON read is allowed. No server-side fetch.
            self._redirect(build_chub_search_url(query))
            return True

        if (path.startswith("/api/storage/chub/character/") or path.startswith("/apps/marinara/api/bot-browser/chub/character/")) and method == "GET":
            from urllib.parse import quote
            char_id = path.split("/character/", 1)[1]
            # Re-encode path segments (Author/name) without touching the slash.
            safe_id = quote(char_id, safe="/")
            self._redirect(f"https://api.chub.ai/api/characters/{safe_id}?full=true")
            return True

        if (path.startswith("/api/storage/chub/avatar/") or path.startswith("/apps/marinara/api/bot-browser/chub/avatar/")) and method == "GET":
            char_id = path.split("/avatar/", 1)[1]
            url = f"https://avatars.charhub.io/avatars/{char_id}"
            if not url.endswith((".webp", ".png", ".jpg", ".jpeg")):
                url += "/avatar.webp"
            # <img>/fetch follows the redirect from the browser's own IP.
            self._redirect(url)
            return True

        if (path.startswith("/api/storage/chub/download/") or path.startswith("/apps/marinara/api/bot-browser/chub/download/")) and method == "GET":
            char_id = path.split("/download/", 1)[1]
            self._redirect(f"https://avatars.charhub.io/avatars/{char_id}/chara_card_v2.png")
            return True

        if path == "/api/storage/datacat/search" and method == "GET":
            from urllib.parse import urlencode, quote
            qs = parse_qs(query)
            params = {
                "limit": qs.get("limit", ["48"])[0],
                "offset": qs.get("offset", ["0"])[0],
                "summary": "1",
                "minTotalTokens": qs.get("min_tokens", ["200"])[0],
            }
            qstr = urlencode(params)
            q = (qs.get("q", [""])[0] or qs.get("search", [""])[0]).strip()
            if q:
                qstr += "&search=" + quote(q)
            try:
                status, body = datacat_get(f"/api/characters/recent-public?{qstr}")
                self._send_bytes(status, body, "application/json")
            except Exception as e:
                self._send_json(502, {"error": f"datacat unreachable: {e}"})
            return True

        if path.startswith("/api/storage/datacat/download/") and method == "GET":
            from urllib.parse import quote
            import time
            cid = path.split("/download/", 1)[1]
            try:
                status, body = datacat_get(
                    f"/api/characters/{quote(cid, safe='')}/download?t={int(time.time())}"
                )
                self._send_bytes(status, body, "application/json")
            except Exception as e:
                self._send_json(502, {"error": f"datacat download failed: {e}"})
            return True

        if path == "/api/storage/catbox" and method == "POST":
            qs = parse_qs(query)
            target = qs.get("path", [""])[0]
            if not target:
                self._send_json(400, {"error": "path required"})
                return True
            try:
                target_path = Path(target).resolve()
                shared_dir = (DATA_ROOT / "shared").resolve()
                if not str(target_path).startswith(str(shared_dir)) or not target_path.is_file():
                    self._send_json(403, {"error": "Invalid path"})
                    return True
                
                import uuid
                boundary = uuid.uuid4().hex
                body = []
                body.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"reqtype\"\r\n\r\nfileupload\r\n".encode())
                body.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"fileToUpload\"; filename=\"{target_path.name}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode())
                with open(target_path, "rb") as fh:
                    body.append(fh.read())
                body.append(f"\r\n--{boundary}--\r\n".encode())
                data = b"".join(body)
                
                req = urllib.request.Request(
                    "https://catbox.moe/user/api.php",
                    data=data,
                    headers={
                        "Content-Type": f"multipart/form-data; boundary={boundary}",
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 AIHub/1.0"
                    },
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    catbox_url = resp.read().decode("utf-8").strip()
                    self._send_json(200, {"url": catbox_url})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return True

        if path == "/hub/favicon.ico" and method == "GET":
            self._send_public_file("favicon.ico", "image/x-icon")
            return True

        # Legacy shortcuts → canonical app URLs.
        legacy = {
            "/sillytavern": "/",
            "/sillytavern/": "/",
            "/apps/sillytavern": "/",
            "/apps/sillytavern/": "/",
            "/lumiverse": "/apps/lumiverse/",
            "/lumiverse/": "/apps/lumiverse/",
            "/marinara": "/apps/marinara/",
            "/marinara/": "/apps/marinara/",
        }
        if path in legacy:
            self._redirect(legacy[path])
            return True

        if path.startswith("/apps/sillytavern/"):
            self._redirect(path[len("/apps/sillytavern") :] or "/")
            return True

        if path.startswith("/api/switch/") and method == "GET":
            app = path.rsplit("/", 1)[-1].lower()
            if app == "sillytavern":
                self._redirect("/")
                return True
            if app in APP_PREFIXES:
                self._redirect(f"{APP_PREFIXES[app]}/")
                return True
            self._send_json(400, {"error": "unknown app"})
            return True

        return False

    def _build_forward_headers(self, app: str, backend_path: str) -> dict[str, str]:
        headers: dict[str, str] = {}
        for key, value in self.headers.items():
            lower = key.lower()
            if lower in SKIP_REQUEST_HEADERS:
                continue
            headers[key] = value

        host = self.headers.get("Host", "")
        prefix = APP_PREFIXES.get(app, "")
        if host and prefix:
            headers["X-Forwarded-Host"] = host
            headers["X-Forwarded-Prefix"] = prefix
            headers["X-Hub-App"] = app
        headers["X-Forwarded-Proto"] = os.environ.get("FORWARDED_PROTO", "https")
        headers["X-Real-IP"] = self.client_address[0]
        prior = self.headers.get("X-Forwarded-For", "")
        client_ip = self.client_address[0]
        headers["X-Forwarded-For"] = f"{prior}, {client_ip}" if prior else client_ip
        # Never ask backends for br/gzip — we rewrite bodies as text and must not
        # forward compressed bytes after stripping Content-Encoding.
        headers["Accept-Encoding"] = "identity"
        return headers

    def _proxy_http(self, method: str) -> None:
        path, query, referer = self._parsed()
        origin = self.headers.get("Origin", "")
        cookie = self.headers.get("Cookie", "")
        app, backend_path = resolve_route(path, referer, query, origin, cookie)

        if query and "?" not in backend_path:
            backend_path = f"{backend_path}?{query}"

        prefix = APP_PREFIXES.get(app, "")
        port = backend_port(app)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else None

        try:
            conn.request(method, backend_path, body=body, headers=self._build_forward_headers(app, backend_path))
            resp = conn.getresponse()
            data = resp.read()
            content_type = resp.getheader("Content-Type", "")
            content_encoding = resp.getheader("Content-Encoding")
            data, decompressed = decompress_body(data, content_encoding)
            if prefix:
                data = rewrite_app_body(data, content_type, prefix, app)

            self.send_response(resp.status)
            for key, value in resp.getheaders():
                lower = key.lower()
                if lower in HOP_BY_HOP or lower == "content-length":
                    continue
                if prefix and lower in SKIP_RESPONSE_CACHE_HEADERS:
                    continue
                if lower == "content-encoding":
                    # Drop encoding only when we successfully decoded; otherwise keep
                    # header + compressed bytes intact (avoids binary garbage in browser).
                    if decompressed:
                        continue
                    self.send_header(key, value)
                    continue
                if lower == "location" and prefix:
                    value = rewrite_location(value, prefix)
                self.send_header(key, value)
            if prefix:
                for key, value in proxy_cache_headers(app, content_type).items():
                    self.send_header(key, value)
            else:
                # ST (root) static assets: let the browser cache them so a refresh
                # doesn't re-download ~200 JS/CSS/font files every time. HTML stays
                # uncached. Cache-Control was stripped from the backend above.
                _p = self.path.split("?", 1)[0]
                if (_p.endswith((".js", ".mjs", ".css", ".woff2", ".woff", ".ttf",
                                 ".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif",
                                 ".mp3", ".ico"))
                        or _p.startswith(("/scripts/", "/lib/", "/css/", "/webfonts/",
                                          "/fonts/", "/img/", "/sounds/", "/locales/"))):
                    self.send_header("Cache-Control", "public, max-age=604800")
            if "text/html" in content_type.lower() and app in PORTS:
                self.send_header(
                    "Set-Cookie",
                    f"hub_app={app}; Path=/; SameSite=Lax; Max-Age=86400",
                )
            # Hub service worker registration is NOT injected into SillyTavern pages.
            # The root-scope SW (sw.js) clears stale PWA caches from Lumiverse/Marinara
            # subpath apps, but injecting it into ST causes black-screen on Chrome incognito
            # because skipWaiting + clients.claim steals fetches mid-page-load.
            # SW is registered by subpath SPAs themselves (via their own PWA manifests).
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as exc:
            print(f"[gateway] proxy {method} {app} → :{port}{backend_path} failed: {exc}", flush=True)
            self._send_json(502, {"error": "backend unavailable", "app": app, "port": port})
        finally:
            conn.close()

    def _proxy_websocket(self) -> None:
        path, query, referer = self._parsed()
        origin = self.headers.get("Origin", "")
        cookie = self.headers.get("Cookie", "")
        app, backend_path = resolve_route(path, referer, query, origin, cookie)
        if query and "?" not in backend_path:
            backend_path = f"{backend_path}?{query}"

        port = backend_port(app)
        lines = [f"{self.command} {backend_path} {self.request_version}"]
        for key, value in self.headers.items():
            lower = key.lower()
            if lower == "host":
                value = f"127.0.0.1:{port}"
            lines.append(f"{key}: {value}")
        lines.extend(["", ""])
        payload = "\r\n".join(lines).encode("latin-1", errors="replace")

        client = self.connection
        backend = socket.create_connection(("127.0.0.1", port), timeout=60)
        try:
            backend.sendall(payload)
            sockets = [client, backend]
            while True:
                readable, _, _ = select.select(sockets, [], [], 3600)
                if not readable:
                    break
                for sock in readable:
                    chunk = sock.recv(65536)
                    if not chunk:
                        return
                    other = backend if sock is client else client
                    other.sendall(chunk)
        except Exception as exc:
            print(f"[gateway] websocket {app} → :{port}{backend_path} failed: {exc}", flush=True)
        finally:
            backend.close()

    def handle(self) -> None:
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if not self.raw_requestline:
                return
            if not self.parse_request():
                return

            global_pwd = os.environ.get("GLOBAL_PASSWORD")
            if global_pwd:
                import base64
                expected = f"Basic {base64.b64encode(f'admin:{global_pwd}'.encode()).decode()}"
                if self.headers.get("Authorization") != expected:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="AI Hub"')
                    self.end_headers()
                    self.wfile.write(b"Unauthorized")
                    return

            if self._handle_hub_route(self.command):
                return

            if self.headers.get("Upgrade", "").lower() == "websocket":
                self._proxy_websocket()
                return

            mname = f"do_{self.command}"
            if not hasattr(self, mname):
                self.send_error(501, "Unsupported method")
                return
            getattr(self, mname)()
        except (ConnectionResetError, BrokenPipeError):
            pass

    def do_GET(self) -> None:
        self._proxy_http("GET")

    def do_HEAD(self) -> None:
        self._proxy_http("HEAD")

    def do_POST(self) -> None:
        self._proxy_http("POST")

    def do_PUT(self) -> None:
        self._proxy_http("PUT")

    def do_PATCH(self) -> None:
        self._proxy_http("PATCH")

    def do_DELETE(self) -> None:
        self._proxy_http("DELETE")

    def do_OPTIONS(self) -> None:
        self._proxy_http("OPTIONS")

def main() -> None:
    print(
        f"[gateway] starting on 0.0.0.0:{HUB_PORT} mode=st-root+subpath-spas "
        f"st=/ prefixes={','.join(APP_PREFIXES.values())} hub=/hub",
        flush=True,
    )
    server = ThreadPoolHTTPServer(("0.0.0.0", HUB_PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()