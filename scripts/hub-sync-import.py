#!/usr/bin/env python3
"""
Bidirectional hub sync via /data/shared (standard Tavern PNG/JSON cards).

Flow:
  1. Export running backends (Marinara, Lumiverse) → /data/shared/characters
  2. Mirror shared → per-app import-staging folders
  3. Import shared → Marinara + Lumiverse when those backends are up

SillyTavern symlinks characters/worlds directly to shared — no import step.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from uuid import uuid4

DATA_ROOT = Path(os.environ.get("DATA_ROOT", "/data"))
SHARED = DATA_ROOT / "shared"
STAGING_MARINARA = DATA_ROOT / "marinara" / "storage" / "import-staging"
STAGING_LUMIVERSE = DATA_ROOT / "lumiverse" / "import-staging"
STATE_DIR = DATA_ROOT / ".hub-sync"
STATE_FILE = STATE_DIR / "import-state.json"
MARINARA_PORT = int(os.environ.get("MARINARA_PORT", "7862"))
LUMIVERSE_PORT = int(os.environ.get("LUMIVERSE_PORT", "7861"))
ST_ROOT = DATA_ROOT / "sillytavern"
MARINARA_BUILTIN_IDS = {"__professor_mari__"}
EXPORT_ONLY = os.environ.get("HUB_SYNC_EXPORT", "").strip().lower()
OWNER_USERNAME = os.environ.get("OWNER_USERNAME", "admin")
OWNER_PASSWORD = os.environ.get("OWNER_PASSWORD") or os.environ.get("HUB_SYNC_PASSWORD", "")


def log(msg: str) -> None:
    print(f"[sync] {msg}", flush=True)


def load_state() -> dict:
    if not STATE_FILE.is_file():
        return {"characters": {}, "world_info": {}, "exports": {}}
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"characters": {}, "world_info": {}, "exports": {}}
    state.setdefault("characters", {})
    state.setdefault("world_info", {})
    state.setdefault("exports", {})
    return state


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def file_sig(path: Path) -> str:
    st = path.stat()
    return f"{st.st_mtime_ns}:{st.st_size}"


def safe_name(value: str, fallback: str = "character") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\u0000-\u001f]+', " ", value or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned[:80] if cleaned else fallback
    return cleaned or fallback


def shared_filename(source: str, char_id: str, name: str) -> str:
    short_id = re.sub(r"[^a-zA-Z0-9]", "", char_id)[:8] or "id"
    return f"hub_{source}_{short_id}_{safe_name(name)}.png"


def rsync_shared() -> None:
    import subprocess

    pairs = [
        (SHARED / "characters", STAGING_MARINARA / "characters"),
        (SHARED / "characters", STAGING_LUMIVERSE / "characters"),
        (SHARED / "world_info", STAGING_MARINARA / "world_info"),
        (SHARED / "world_info", STAGING_LUMIVERSE / "world_info"),
        (SHARED / "connections", STAGING_MARINARA / "connections"),
        (SHARED / "connections", STAGING_LUMIVERSE / "connections"),
    ]
    for src, dst in pairs:
        src.mkdir(parents=True, exist_ok=True)
        dst.mkdir(parents=True, exist_ok=True)
        delete = "--delete" if src.name in {"characters", "world_info"} else ""
        cmd = ["rsync", "-a"]
        if delete:
            cmd.append(delete)
        cmd.extend([f"{src}/", f"{dst}/"])
        subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def backend_up(port: int) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def should_run_export(source: str) -> bool:
    if not EXPORT_ONLY:
        return True
    return EXPORT_ONLY in {source, "all", "export", "exports"}


def should_run_import() -> bool:
    if EXPORT_ONLY and EXPORT_ONLY not in {"all", "import", "imports"}:
        return False
    return True


def http_json(
    method: str,
    url: str,
    body: dict | None = None,
    headers: dict | None = None,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[int, object]:
    data = None
    hdrs = {"Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    open_fn = opener.open if opener else urllib.request.urlopen
    try:
        with open_fn(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {"error": raw}
        except json.JSONDecodeError:
            payload = {"error": raw or exc.reason}
        return exc.code, payload


def http_bytes(
    url: str,
    headers: dict | None = None,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[int, bytes]:
    hdrs = headers or {}
    req = urllib.request.Request(url, headers=hdrs, method="GET")
    open_fn = opener.open if opener else urllib.request.urlopen
    try:
        with open_fn(req, timeout=300) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def multipart_batch(
    url: str,
    files: list[tuple[str, bytes]],
    opener: urllib.request.OpenerDirector | None = None,
    fields: dict[str, str] | None = None,
) -> tuple[int, object]:
    boundary = f"hubsync-{uuid4().hex}"
    parts: list[bytes] = []

    for key, value in (fields or {}).items():
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        parts.append(f"{value}\r\n".encode())

    for name, content in files:
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="files"; filename="{name}"\r\n'.encode())
        parts.append(f"Content-Type: {mime}\r\n\r\n".encode())
        parts.append(content)
        parts.append(b"\r\n")

    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)

    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Accept": "application/json"},
        method="POST",
    )
    open_fn = opener.open if opener else urllib.request.urlopen
    try:
        with open_fn(req, timeout=300) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {"error": raw}
        except json.JSONDecodeError:
            payload = {"error": raw or exc.reason}
        return exc.code, payload


def lumiverse_opener() -> urllib.request.OpenerDirector | None:
    if not OWNER_PASSWORD:
        log("lumiverse auto-import skipped — set OWNER_PASSWORD in HF Secrets")
        return None

    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    base = f"http://127.0.0.1:{LUMIVERSE_PORT}"
    status, payload = http_json(
        "POST",
        f"{base}/api/auth/sign-in/username",
        {"username": OWNER_USERNAME, "password": OWNER_PASSWORD},
        headers={"Origin": base},
        opener=opener,
    )
    if status >= 400:
        log(f"lumiverse sign-in failed ({status}): {payload}")
        return None
    return opener


def export_marinara_to_shared(state: dict) -> int:
    if not should_run_export("marinara"):
        return 0
    if not backend_up(MARINARA_PORT):
        return 0

    status, payload = http_json("GET", f"http://127.0.0.1:{MARINARA_PORT}/api/characters/")
    if status >= 400 or not isinstance(payload, list):
        log(f"marinara list failed ({status}): {payload}")
        return 0

    out_dir = SHARED / "characters"
    out_dir.mkdir(parents=True, exist_ok=True)
    exported = 0

    for item in payload:
        if not isinstance(item, dict):
            continue
        char_id = str(item.get("id") or "")
        if not char_id or char_id in MARINARA_BUILTIN_IDS:
            continue

        updated = str(item.get("updatedAt") or item.get("updated_at") or "")
        export_key = f"marinara:{char_id}"
        prev = state["exports"].get(export_key, {})
        if prev.get("updated") == updated and prev.get("file"):
            shared_rel = prev["file"]
            if (SHARED / shared_rel).is_file():
                continue

        name = "character"
        data = item.get("data")
        if isinstance(data, str):
            try:
                parsed = json.loads(data)
                if isinstance(parsed, dict):
                    name = str(parsed.get("name") or name)
            except json.JSONDecodeError:
                pass
        elif isinstance(data, dict):
            name = str(data.get("name") or name)

        filename = prev.get("filename") or shared_filename("marinara", char_id, name)
        rel = f"characters/{filename}"
        dest = SHARED / rel

        png_status, png_bytes = http_bytes(f"http://127.0.0.1:{MARINARA_PORT}/api/characters/{char_id}/export-png")
        if png_status >= 400 or not png_bytes:
            log(f"marinara export failed for {char_id} ({png_status})")
            continue

        dest.write_bytes(png_bytes)
        state["exports"][export_key] = {
            "file": rel,
            "filename": filename,
            "updated": updated,
            "name": name,
        }
        state["characters"][rel] = file_sig(dest)
        exported += 1
        log(f"exported character → shared: {rel}")

    return exported


def export_lumiverse_to_shared(state: dict) -> int:
    if not should_run_export("lumiverse"):
        return 0
    if not backend_up(LUMIVERSE_PORT):
        return 0

    opener = lumiverse_opener()
    if not opener:
        return 0

    base = f"http://127.0.0.1:{LUMIVERSE_PORT}"
    status, payload = http_json("GET", f"{base}/api/v1/characters/?limit=500&offset=0", opener=opener)
    if status >= 400 or not isinstance(payload, dict):
        log(f"lumiverse list failed ({status}): {payload}")
        return 0

    chars = payload.get("data") or []
    if not isinstance(chars, list):
        return 0

    out_dir = SHARED / "characters"
    out_dir.mkdir(parents=True, exist_ok=True)
    exported = 0

    for item in chars:
        if not isinstance(item, dict):
            continue
        char_id = str(item.get("id") or "")
        if not char_id:
            continue

        updated = str(item.get("updated_at") or "")
        export_key = f"lumiverse:{char_id}"
        prev = state["exports"].get(export_key, {})
        if prev.get("updated") == updated and prev.get("file"):
            shared_rel = prev["file"]
            if (SHARED / shared_rel).is_file():
                continue

        name = str(item.get("name") or "character")
        filename = prev.get("filename") or shared_filename("lumiverse", char_id, name)
        rel = f"characters/{filename}"
        dest = SHARED / rel

        png_status, png_bytes = http_bytes(
            f"{base}/api/v1/characters/{char_id}/export?format=png",
            opener=opener,
        )
        if png_status >= 400 or not png_bytes:
            log(f"lumiverse export failed for {char_id} ({png_status})")
            continue

        dest.write_bytes(png_bytes)
        state["exports"][export_key] = {
            "file": rel,
            "filename": filename,
            "updated": updated,
            "name": name,
        }
        state["characters"][rel] = file_sig(dest)
        exported += 1
        log(f"exported character → shared: {rel}")

    return exported


def import_characters_to_marinara(state: dict) -> int:
    char_dir = SHARED / "characters"
    if not char_dir.is_dir():
        return 0
    if not backend_up(MARINARA_PORT):
        log("marinara not running — skip auto-import (switch to Marinara first)")
        return 0

    pending: list[tuple[str, Path]] = []
    for path in sorted(char_dir.iterdir()):
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        if ext not in {".png", ".json", ".charx"}:
            continue
        rel = str(path.relative_to(SHARED))
        sig = file_sig(path)
        if state["characters"].get(rel) == sig:
            continue
        pending.append((rel, path))

    if not pending:
        return 0

    imported = 0
    batch: list[tuple[str, bytes]] = []
    batch_meta: list[str] = []

    def flush_batch() -> None:
        nonlocal imported, batch, batch_meta
        if not batch:
            return
        url = f"http://127.0.0.1:{MARINARA_PORT}/api/import/st-character/batch"
        status, payload = multipart_batch(url, batch)
        if status >= 400:
            log(f"marinara character batch import failed ({status}): {payload}")
            batch = []
            batch_meta = []
            return
        results = payload.get("results", []) if isinstance(payload, dict) else []
        for rel, result in zip(batch_meta, results):
            if result.get("success"):
                state["characters"][rel] = file_sig(SHARED / rel)
                imported += 1
                log(f"imported character → marinara: {rel}")
            else:
                log(f"marinara import failed for {rel}: {result.get('error', 'unknown')}")
        batch = []
        batch_meta = []

    for rel, path in pending:
        try:
            content = path.read_bytes()
        except OSError as exc:
            log(f"read failed {rel}: {exc}")
            continue
        batch.append((path.name, content))
        batch_meta.append(rel)
        if len(batch) >= 10:
            flush_batch()
    flush_batch()
    return imported


def import_characters_to_lumiverse(state: dict) -> int:
    char_dir = SHARED / "characters"
    if not char_dir.is_dir():
        return 0
    if not backend_up(LUMIVERSE_PORT):
        return 0

    opener = lumiverse_opener()
    if not opener:
        return 0

    pending: list[tuple[str, Path]] = []
    for path in sorted(char_dir.iterdir()):
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        if ext not in {".png", ".json", ".charx"}:
            continue
        rel = str(path.relative_to(SHARED))
        sig = file_sig(path)
        if state["characters"].get(rel) == sig:
            continue
        pending.append((rel, path))

    if not pending:
        return 0

    imported = 0
    batch: list[tuple[str, bytes]] = []
    batch_meta: list[str] = []

    def flush_batch() -> None:
        nonlocal imported, batch, batch_meta
        if not batch:
            return
        url = f"http://127.0.0.1:{LUMIVERSE_PORT}/api/v1/characters/import-bulk"
        status, payload = multipart_batch(url, batch, opener=opener, fields={"skip_duplicates": "true"})
        if status >= 400:
            log(f"lumiverse character batch import failed ({status}): {payload}")
            batch = []
            batch_meta = []
            return
        results = payload.get("results", []) if isinstance(payload, dict) else []
        for rel, result in zip(batch_meta, results):
            if result.get("success") and not result.get("skipped"):
                state["characters"][rel] = file_sig(SHARED / rel)
                imported += 1
                log(f"imported character → lumiverse: {rel}")
            elif result.get("skipped"):
                state["characters"][rel] = file_sig(SHARED / rel)
                log(f"lumiverse skipped duplicate: {rel}")
            else:
                log(f"lumiverse import failed for {rel}: {result.get('error', 'unknown')}")
        batch = []
        batch_meta = []

    for rel, path in pending:
        try:
            content = path.read_bytes()
        except OSError as exc:
            log(f"read failed {rel}: {exc}")
            continue
        batch.append((path.name, content))
        batch_meta.append(rel)
        if len(batch) >= 10:
            flush_batch()
    flush_batch()
    return imported


def import_lorebooks_to_marinara(state: dict) -> int:
    world_dir = SHARED / "world_info"
    if not world_dir.is_dir():
        return 0
    if not backend_up(MARINARA_PORT):
        return 0

    imported = 0
    for path in sorted(world_dir.glob("*.json")):
        rel = str(path.relative_to(SHARED))
        sig = file_sig(path)
        if state["world_info"].get(rel) == sig:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            log(f"skip lorebook {rel}: {exc}")
            continue
        payload["__filename"] = path.name
        url = f"http://127.0.0.1:{MARINARA_PORT}/api/import/st-lorebook"
        status, result = http_json("POST", url, payload)
        if status < 400 and isinstance(result, dict) and result.get("success", True):
            state["world_info"][rel] = sig
            imported += 1
            log(f"imported lorebook → marinara: {rel}")
        else:
            log(f"marinara lorebook import failed for {rel} ({status}): {result}")
    return imported


def bulk_import_from_sillytavern_tree(state: dict) -> int:
    """Fallback: Marinara ST bulk scan expects data/default-user/ layout under ST root."""
    if not ST_ROOT.is_dir() or not backend_up(MARINARA_PORT):
        return 0
    url = f"http://127.0.0.1:{MARINARA_PORT}/api/import/st-bulk/scan"
    status, scan = http_json("POST", url, {"folderPath": str(ST_ROOT)})
    if status >= 400 or not isinstance(scan, dict) or not scan.get("success"):
        return 0
    chars = scan.get("characters") or []
    if not chars:
        return 0
    new_ids = []
    for item in chars:
        rel = item.get("path", "")
        try:
            p = Path(rel)
            if not p.is_file():
                continue
            key = f"st:{p.name}"
            sig = file_sig(p)
            if state["characters"].get(key) == sig:
                continue
            new_ids.append(item.get("id"))
        except OSError:
            continue
    if not new_ids:
        return 0
    run_url = f"http://127.0.0.1:{MARINARA_PORT}/api/import/st-bulk/run"
    body = {
        "folderPath": str(ST_ROOT),
        "options": {
            "characters": new_ids,
            "chats": False,
            "groupChats": False,
            "presets": False,
            "lorebooks": False,
            "backgrounds": False,
            "personas": False,
        },
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        run_url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        log(f"marinara bulk run failed ({exc.code})")
        return 0
    imported = 0
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        if "imported" in event and isinstance(event["imported"], dict):
            imported = int(event["imported"].get("characters") or 0)
    if imported:
        log(f"marinara bulk imported {imported} character(s) from sillytavern tree")
    return imported


def main() -> int:
    log(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} syncing shared library")
    state = load_state()

    n_export_marinara = export_marinara_to_shared(state)
    n_export_lumiverse = export_lumiverse_to_shared(state)

    if should_run_import():
        rsync_shared()
        n_chars_marinara = import_characters_to_marinara(state)
        n_chars_lumiverse = import_characters_to_lumiverse(state)
        n_worlds = import_lorebooks_to_marinara(state)
        if n_chars_marinara == 0:
            n_chars_marinara = bulk_import_from_sillytavern_tree(state)
    else:
        n_chars_marinara = 0
        n_chars_lumiverse = 0
        n_worlds = 0

    save_state(state)
    log(
        "done — "
        f"exported: marinara +{n_export_marinara}, lumiverse +{n_export_lumiverse}; "
        f"imported: marinara +{n_chars_marinara}, lumiverse +{n_chars_lumiverse}, "
        f"lorebooks +{n_worlds}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())