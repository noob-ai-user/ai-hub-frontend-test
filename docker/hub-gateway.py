#!/usr/bin/env python3
"""HF public gateway on :7860 — parallel apps via /apps/{name}/ + Referer routing."""
from __future__ import annotations

import http.client
import json
import os
import select
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

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

APP_PREFIXES = {
    "sillytavern": "/apps/sillytavern",
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
}

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

SKIP_REQUEST_HEADERS = {"host", "connection", "content-length", "transfer-encoding"}


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


def resolve_route(path: str, referer: str, query: str = "") -> tuple[str, str]:
    """Return (app_name, backend_path)."""
    for app, prefix in APP_PREFIXES.items():
        if path == prefix:
            return app, "/"
        if path.startswith(prefix + "/"):
            return app, path[len(prefix) :] or "/"

    referer_app = app_from_referer(referer)
    if referer_app:
        return referer_app, path

    if path == "/":
        if "logs=" in query:
            return "sillytavern", path + (f"?{query}" if query else "")
        return "hub", path

    return active_app(), path


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "hub-gateway/3"

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
            self._send_json(
                200,
                {
                    "active": active_app(),
                    "routing": "parallel",
                    "apps": {name: prefix for name, prefix in APP_PREFIXES.items()},
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
                    "prefix": APP_PREFIXES[name],
                    "port_open": port_open(port),
                    "http_ready": backend_ready(name),
                }
            self._send_json(
                200,
                {
                    "routing": "parallel (/apps/{app}/ + Referer)",
                    "active_fallback": active_app(),
                    "apps": probes,
                    "shared_characters": str(DATA_ROOT / "shared" / "characters"),
                },
            )
            return True

        if path == "/api/sync" and method == "GET":
            threading.Thread(target=self._run_sync_background, daemon=True).start()
            self._send_json(200, {"ok": True, "message": "sync started in background"})
            return True

        # Legacy shortcuts → prefixed app URLs (new tab friendly).
        legacy = {
            "/sillytavern": "/apps/sillytavern/",
            "/sillytavern/": "/apps/sillytavern/",
            "/lumiverse": "/apps/lumiverse/",
            "/lumiverse/": "/apps/lumiverse/",
            "/marinara": "/apps/marinara/",
            "/marinara/": "/apps/marinara/",
        }
        if path in legacy:
            self._redirect(legacy[path])
            return True

        if path.startswith("/api/switch/") and method == "GET":
            app = path.rsplit("/", 1)[-1].lower()
            if app in APP_PREFIXES:
                self._redirect(f"{APP_PREFIXES[app]}/")
                return True
            self._send_json(400, {"error": "unknown app"})
            return True

        if path == "/" and method == "GET" and "logs=" not in query:
            self._send_html("index.html")
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
        return headers

    def _proxy_http(self, method: str) -> None:
        path, query, referer = self._parsed()
        app, backend_path = resolve_route(path, referer, query)
        if app == "hub":
            self._send_html("index.html")
            return

        if query and "?" not in backend_path:
            backend_path = f"{backend_path}?{query}"

        port = backend_port(app)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3600)
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else None

        try:
            conn.request(method, backend_path, body=body, headers=self._build_forward_headers(app, backend_path))
            resp = conn.getresponse()
            self.send_response(resp.status)
            for key, value in resp.getheaders():
                if key.lower() in HOP_BY_HOP:
                    continue
                self.send_header(key, value)
            data = resp.read()
            if "Content-Length" not in {k for k, _ in resp.getheaders()}:
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
        app, backend_path = resolve_route(path, referer, query)
        if app == "hub":
            self.send_error(400, "WebSocket not supported on hub route")
            return
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
        f"[gateway] starting on 0.0.0.0:{HUB_PORT} mode=parallel "
        f"prefixes={','.join(APP_PREFIXES.values())}",
        flush=True,
    )
    server = ThreadingHTTPServer(("0.0.0.0", HUB_PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()