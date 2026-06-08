#!/usr/bin/env python3
"""Hub launcher API — only /hub and /api/switch|health|active|ready."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

DATA_ROOT = os.environ.get("DATA_ROOT", "/data")
PUBLIC = Path("/opt/hub/public")
SWITCH_SCRIPT = "/opt/hub/docker/switch-app.sh"
PORT = int(os.environ.get("HUB_API_PORT", "7870"))

APP_PORTS = {
    "sillytavern": int(os.environ.get("ST_PORT", "8000")),
    "lumiverse": int(os.environ.get("LUMIVERSE_PORT", "7861")),
    "marinara": int(os.environ.get("MARINARA_PORT", "7862")),
}


def active_app() -> str:
    path = os.path.join(DATA_ROOT, ".active_app")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip() or "sillytavern"
    return "sillytavern"


def backend_ready(app: str | None = None) -> bool:
    app = app or active_app()
    port = APP_PORTS.get(app)
    if port is None:
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print(f"[hub-api] {self.address_string()} - {fmt % args}", flush=True)

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, filename: str) -> None:
        path = PUBLIC / filename
        if not path.is_file():
            self._json(404, {"error": "hub page missing"})
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _run_switch_async(self, app: str) -> None:
        try:
            subprocess.run([SWITCH_SCRIPT, app], check=False, timeout=600)
        except Exception as exc:
            print(f"[hub-api] switch to {app} failed: {exc}", flush=True)

    def do_GET(self) -> None:
        path = urlparse(self.path).path

        if path in {"/hub", "/hub/", "/api/hub", "/api/hub/"}:
            self._html("index.html")
            return

        if path == "/api/health":
            self._json(200, {"status": "ok", "active": active_app()})
            return

        if path == "/api/ready":
            app = active_app()
            ready = backend_ready(app)
            self._json(200, {"active": app, "ready": ready})
            return

        if path == "/api/active":
            self._json(200, {"active": active_app()})
            return

        if path == "/api/sync":
            try:
                proc = subprocess.run(
                    ["/opt/hub/scripts/sync-shared-data.sh"],
                    capture_output=True,
                    text=True,
                    timeout=300,
                    check=False,
                )
                lines = (proc.stdout or proc.stderr or "").strip().splitlines()
                tail = lines[-8:] if lines else []
                self._json(
                    200,
                    {
                        "ok": proc.returncode == 0,
                        "exit_code": proc.returncode,
                        "log": tail,
                    },
                )
            except Exception as exc:
                self._json(500, {"ok": False, "error": str(exc)})
            return

        if path.startswith("/api/switch/"):
            app = path.rsplit("/", 1)[-1].lower()
            if app not in APP_PORTS:
                self._json(400, {"error": "unknown app"})
                return
            # Always-on mode: switch is nginx reload only — run synchronously.
            try:
                proc = subprocess.run(
                    [SWITCH_SCRIPT, app],
                    capture_output=True,
                    text=True,
                    timeout=600,
                    check=False,
                )
                if proc.returncode != 0:
                    tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
                    print(f"[hub-api] switch to {app} exit {proc.returncode}: {tail}", flush=True)
            except Exception as exc:
                print(f"[hub-api] switch to {app} failed: {exc}", flush=True)
            self._html("switching.html")
            return

        self._json(404, {"error": "not found"})

    def do_HEAD(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/api/switch/"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            return
        if path in {"/hub", "/hub/", "/api/hub", "/api/hub/"}:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            return
        self.do_GET()


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[hub-api] listening on 127.0.0.1:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()