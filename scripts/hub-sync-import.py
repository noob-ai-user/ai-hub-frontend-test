#!/usr/bin/env python3
"""
Bidirectional hub sync via /data/shared (standard Tavern PNG/JSON cards).

Canonical files: hub_{source}_{slug}.png  (one per character name per source app)
- Never re-import hub_marinara_* into Marinara or hub_lumiverse_* into Lumiverse
- SillyTavern uses its own characters/ folder; sync copies hub_* cards in/out
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from uuid import uuid4

DATA_ROOT = Path(os.environ.get("DATA_ROOT", "/data"))
SHARED = DATA_ROOT / "shared"
ST_CHARS = DATA_ROOT / "sillytavern" / "data" / "default-user" / "characters"
STAGING_MARINARA = DATA_ROOT / "marinara" / "storage" / "import-staging"
STAGING_LUMIVERSE = DATA_ROOT / "lumiverse" / "import-staging"
STATE_DIR = DATA_ROOT / ".hub-sync"
STATE_FILE = STATE_DIR / "import-state.json"
MARINARA_PORT = int(os.environ.get("MARINARA_PORT", "7862"))
LUMIVERSE_PORT = int(os.environ.get("LUMIVERSE_PORT", "7861"))
ST_ROOT = DATA_ROOT / "sillytavern"
MARINARA_BUILTIN_IDS = {"__professor_mari__"}
EXPORT_ONLY = os.environ.get("HUB_SYNC_EXPORT", "").strip().lower()
OWNER_PASSWORD = os.environ.get("OWNER_PASSWORD") or os.environ.get("HUB_SYNC_PASSWORD", "")
HUB_PREFIX_RE = re.compile(r"^hub_(st|marinara|lumiverse)_", re.I)
LEGACY_HUB_RE = re.compile(r"^hub_(st|marinara|lumiverse)_[A-Za-z0-9]{6,8}_", re.I)
CANONICAL_HUB_RE = re.compile(r"^hub_(marinara|lumiverse)_[a-z][a-z0-9_]*\.png$", re.I)
CANONICAL_ST_HUB_RE = re.compile(r"^hub_st_[a-z][a-z0-9_]*\.png$", re.I)
ST_ID_PREFIX_RE = re.compile(r"^[A-Za-z0-9]{6,12}\s+")
ST_RANDOM_ID_SLUG_RE = re.compile(r"^([a-z0-9]{6,12})_")


def is_random_st_id_slug(slug: str) -> bool:
    """True when slug starts with an ST random ID prefix (e.g. a1b2c3d4_name).

    Word slugs like default_seraphina must not match — 'default' has no digits.
    """
    m = ST_RANDOM_ID_SLUG_RE.match(slug)
    if not m:
        return False
    return any(ch.isdigit() for ch in m.group(1))


def resolve_public_origin() -> str:
    origin = (os.environ.get("PUBLIC_ORIGIN") or "").strip().rstrip("/")
    if origin:
        if not origin.startswith(("http://", "https://")):
            origin = f"https://{origin}"
        return origin
    space_host = (os.environ.get("SPACE_HOST") or "").strip().rstrip("/")
    if space_host:
        space_host = space_host.removeprefix("https://").removeprefix("http://")
        return f"https://{space_host}"
    space_id = (os.environ.get("SPACE_ID") or "").strip()
    if space_id:
        return f"https://{space_id.replace('/', '-')}.hf.space"
    return ""


def extract_bearer_token(payload: object, set_cookies: list[str]) -> str | None:
    if isinstance(payload, dict):
        token = payload.get("token")
        if isinstance(token, str) and token.strip():
            return token.strip()
        session = payload.get("session")
        if isinstance(session, dict):
            session_token = session.get("token")
            if isinstance(session_token, str) and session_token.strip():
                return session_token.strip()
    for header in set_cookies:
        for part in header.split(","):
            part = part.strip()
            for marker in (
                "better-auth.session_token=",
                "__Secure-better-auth.session_token=",
            ):
                if marker in part:
                    return part.split(marker, 1)[1].split(";", 1)[0].strip()
    return None


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


def name_slug(name: str) -> str:
    slug = safe_name(name).lower()
    slug = re.sub(r"[^a-z0-9]+", "_", slug).strip("_")
    return slug[:60] or "character"


def canonical_filename(source: str, name: str) -> str:
    return f"hub_{source}_{name_slug(name)}.png"


def canonical_key(source: str, name: str) -> str:
    return f"canonical:{source}:{name_slug(name)}"


def lumiverse_owner_username() -> str:
    cred_path = DATA_ROOT / "lumiverse" / "owner.credentials"
    if cred_path.is_file():
        try:
            data = json.loads(cred_path.read_text(encoding="utf-8"))
            username = data.get("username")
            if isinstance(username, str) and username.strip():
                return username.strip()
        except Exception:
            pass
    return os.environ.get("OWNER_USERNAME", "admin").strip() or "admin"


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
    headers: dict[str, str] | None = None,
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

    req_headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Accept": "application/json",
    }
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(
        url,
        data=body,
        headers=req_headers,
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


def lumiverse_session() -> tuple[urllib.request.OpenerDirector, dict[str, str]] | None:
    if not OWNER_PASSWORD:
        log("lumiverse sync skipped — set OWNER_PASSWORD (your Lumiverse login password) in HF Secrets")
        return None

    username = lumiverse_owner_username()
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    base = f"http://127.0.0.1:{LUMIVERSE_PORT}"
    public_origin = resolve_public_origin()
    sign_in_headers: dict[str, str] = {"Accept": "application/json", "Content-Type": "application/json"}
    if public_origin:
        sign_in_headers["Origin"] = public_origin
        try:
            host = public_origin.split("://", 1)[1]
            sign_in_headers["X-Forwarded-Host"] = host
            sign_in_headers["X-Forwarded-Proto"] = "https"
        except IndexError:
            sign_in_headers["Origin"] = base
    else:
        sign_in_headers["Origin"] = base

    body = json.dumps({"username": username, "password": OWNER_PASSWORD}).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/api/auth/sign-in/username",
        data=body,
        headers=sign_in_headers,
        method="POST",
    )
    try:
        with opener.open(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            payload = json.loads(raw) if raw else {}
            set_cookies = []
            if hasattr(resp.headers, "get_all"):
                set_cookies = list(resp.headers.get_all("Set-Cookie") or [])
            elif resp.headers.get("Set-Cookie"):
                set_cookies = [resp.headers.get("Set-Cookie", "")]
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {"error": raw}
        except json.JSONDecodeError:
            payload = {"error": raw or exc.reason}
        log(f"lumiverse sign-in failed for user '{username}' ({exc.code}): {payload}")
        return None
    except Exception as exc:
        log(f"lumiverse sign-in error for user '{username}': {exc}")
        return None

    auth_headers: dict[str, str] = {}
    token = extract_bearer_token(payload, set_cookies)
    if token:
        auth_headers["Authorization"] = f"Bearer {token}"
    else:
        log(
            f"lumiverse sign-in for '{username}' ok but no bearer token — "
            "falling back to session cookies"
        )
    return opener, auth_headers


def marinara_character_names() -> set[str]:
    if not backend_up(MARINARA_PORT):
        return set()
    status, payload = http_json("GET", f"http://127.0.0.1:{MARINARA_PORT}/api/characters/")
    if status >= 400 or not isinstance(payload, list):
        return set()
    names: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        data = item.get("data")
        name = None
        if isinstance(data, str):
            try:
                parsed = json.loads(data)
                if isinstance(parsed, dict):
                    name = parsed.get("name")
            except json.JSONDecodeError:
                pass
        elif isinstance(data, dict):
            name = data.get("name")
        if name:
            names.add(str(name).strip().lower())
    return names


def char_name_from_path(path: Path) -> str:
    stem = path.stem
    if HUB_PREFIX_RE.match(path.name):
        parts = stem.split("_", 2)
        if len(parts) >= 3:
            slug = parts[2]
            if parts[1] in {"marinara", "lumiverse", "st"} and not is_random_st_id_slug(slug):
                return slug.replace("_", " ")
    return st_display_name(stem)


def st_display_name(stem: str) -> str:
    cleaned = ST_ID_PREFIX_RE.sub("", stem).strip()
    return cleaned or stem


def is_valid_shared_card(name: str) -> bool:
    if re.match(r"^default_.+\.(png|json)$", name, re.I):
        return True
    if CANONICAL_HUB_RE.match(name):
        return True
    if CANONICAL_ST_HUB_RE.match(name):
        slug = name[len("hub_st_") : -4]
        return not is_random_st_id_slug(slug)
    return False


def is_importable_hub_for_st(name: str) -> bool:
    return bool(CANONICAL_HUB_RE.match(name))


def cleanup_shared_junk(state: dict) -> int:
    char_dir = SHARED / "characters"
    if not char_dir.is_dir():
        return 0
    removed = 0
    for path in list(char_dir.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        if is_valid_shared_card(path.name):
            continue
        try:
            path.unlink()
            state["characters"].pop(f"characters/{path.name}", None)
            removed += 1
            log(f"removed junk from shared: characters/{path.name}")
        except OSError as exc:
            log(f"cleanup failed for {path.name}: {exc}")
    return removed


def write_canonical_export(
    state: dict,
    source: str,
    name: str,
    png_bytes: bytes,
    updated: str,
    source_id: str,
) -> bool:
    char_dir = SHARED / "characters"
    char_dir.mkdir(parents=True, exist_ok=True)
    filename = canonical_filename(source, name)
    rel = f"characters/{filename}"
    dest = SHARED / rel
    ckey = canonical_key(source, name)

    prev = state["exports"].get(ckey, {})
    old_rel = prev.get("file")
    if old_rel and old_rel != rel:
        old_path = SHARED / old_rel
        if old_path.is_file():
            try:
                old_path.unlink()
                state["characters"].pop(old_rel, None)
            except OSError:
                pass

    if dest.is_file() and dest.read_bytes() == png_bytes:
        state["exports"][ckey] = {
            "file": rel,
            "filename": filename,
            "updated": updated,
            "name": name,
            "source_id": source_id,
        }
        state["characters"][rel] = file_sig(dest)
        return False

    dest.write_bytes(png_bytes)
    state["exports"][ckey] = {
        "file": rel,
        "filename": filename,
        "updated": updated,
        "name": name,
        "source_id": source_id,
    }
    state["characters"][rel] = file_sig(dest)
    return True


def export_marinara_to_shared(state: dict) -> int:
    if not should_run_export("marinara"):
        return 0
    if not backend_up(MARINARA_PORT):
        return 0

    status, payload = http_json("GET", f"http://127.0.0.1:{MARINARA_PORT}/api/characters/")
    if status >= 400 or not isinstance(payload, list):
        log(f"marinara list failed ({status}): {payload}")
        return 0

    best: dict[str, dict] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        char_id = str(item.get("id") or "")
        if not char_id or char_id in MARINARA_BUILTIN_IDS:
            continue

        updated = str(item.get("updatedAt") or item.get("updated_at") or "")
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

        ckey = canonical_key("marinara", name)
        prev = best.get(ckey)
        if prev and prev["updated"] >= updated:
            continue
        best[ckey] = {"char_id": char_id, "name": name, "updated": updated}

    exported = 0
    for ckey, info in best.items():
        prev = state["exports"].get(ckey, {})
        if prev.get("updated") == info["updated"] and prev.get("file"):
            if (SHARED / prev["file"]).is_file():
                continue

        png_status, png_bytes = http_bytes(
            f"http://127.0.0.1:{MARINARA_PORT}/api/characters/{info['char_id']}/export-png"
        )
        if png_status >= 400 or not png_bytes:
            log(f"marinara export failed for {info['char_id']} ({png_status})")
            continue

        if write_canonical_export(
            state, "marinara", info["name"], png_bytes, info["updated"], info["char_id"]
        ):
            exported += 1
            log(f"exported character → shared: {state['exports'][ckey]['file']}")

    return exported


def export_lumiverse_to_shared(state: dict) -> int:
    if not should_run_export("lumiverse"):
        return 0
    if not backend_up(LUMIVERSE_PORT):
        return 0

    session = lumiverse_session()
    if not session:
        return 0
    opener, auth_headers = session

    base = f"http://127.0.0.1:{LUMIVERSE_PORT}"
    status, payload = http_json(
        "GET",
        f"{base}/api/v1/characters/?limit=500&offset=0",
        headers=auth_headers,
        opener=opener,
    )
    if status >= 400 or not isinstance(payload, dict):
        log(f"lumiverse list failed ({status}): {payload}")
        return 0

    chars = payload.get("data") or []
    if not isinstance(chars, list):
        return 0

    best: dict[str, dict] = {}
    for item in chars:
        if not isinstance(item, dict):
            continue
        char_id = str(item.get("id") or "")
        if not char_id:
            continue

        updated = str(item.get("updated_at") or "")
        name = str(item.get("name") or "character")
        ckey = canonical_key("lumiverse", name)
        prev = best.get(ckey)
        if prev and str(prev["updated"]) >= updated:
            continue
        best[ckey] = {"char_id": char_id, "name": name, "updated": updated}

    exported = 0
    for ckey, info in best.items():
        prev = state["exports"].get(ckey, {})
        if prev.get("updated") == info["updated"] and prev.get("file"):
            if (SHARED / prev["file"]).is_file():
                continue

        png_status, png_bytes = http_bytes(
            f"{base}/api/v1/characters/{info['char_id']}/export?format=png",
            headers=auth_headers,
            opener=opener,
        )
        if png_status >= 400 or not png_bytes:
            log(f"lumiverse export failed for {info['char_id']} ({png_status})")
            continue

        if write_canonical_export(
            state, "lumiverse", info["name"], png_bytes, info["updated"], info["char_id"]
        ):
            exported += 1
            log(f"exported character → shared: {state['exports'][ckey]['file']}")

    return exported


def sync_st_to_shared(state: dict) -> int:
    if not ST_CHARS.is_dir():
        return 0
    copied = 0
    char_dir = SHARED / "characters"
    char_dir.mkdir(parents=True, exist_ok=True)

    for path in sorted(ST_CHARS.iterdir()):
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        if ext not in {".png", ".json"}:
            continue
        if path.name.startswith("hub_"):
            continue

        name = st_display_name(path.stem)
        if ext == ".png":
            dest_name = canonical_filename("st", name)
        else:
            dest_name = path.name

        rel = f"characters/{dest_name}"
        sig = file_sig(path)
        ckey = canonical_key("st", name)
        prev = state["exports"].get(ckey, {})
        if state["characters"].get(rel) == sig and prev.get("source_id") == path.name:
            continue

        try:
            shutil.copy2(path, SHARED / rel)
            state["characters"][f"st_src:{path.name}"] = sig
            state["characters"][rel] = file_sig(SHARED / rel)
            state["exports"][ckey] = {
                "file": rel,
                "filename": dest_name,
                "updated": sig,
                "name": name,
                "source_id": path.name,
            }
            copied += 1
            log(f"exported ST character → shared: {rel}")
        except OSError as exc:
            log(f"ST export failed for {path.name}: {exc}")

    return copied


def sync_shared_to_st(state: dict) -> int:
    char_dir = SHARED / "characters"
    if not char_dir.is_dir():
        return 0
    ST_CHARS.mkdir(parents=True, exist_ok=True)
    copied = 0
    existing = {p.name.lower() for p in ST_CHARS.iterdir() if p.is_file()}

    for path in sorted(char_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() != ".png":
            continue
        if not is_importable_hub_for_st(path.name):
            continue

        name = char_name_from_path(path)
        target_name = f"{safe_name(name)}.png"
        if target_name.lower() in existing:
            st_key = f"st_dst:{target_name}"
            sig = file_sig(path)
            if state["characters"].get(st_key) == sig:
                continue

        target = ST_CHARS / target_name
        try:
            shutil.copy2(path, target)
            state["characters"][f"st_dst:{target_name}"] = file_sig(path)
            existing.add(target_name.lower())
            copied += 1
            log(f"imported character → sillytavern: {target_name}")
        except OSError as exc:
            log(f"ST import failed for {path.name}: {exc}")

    return copied


def import_characters_to_marinara(state: dict) -> int:
    char_dir = SHARED / "characters"
    if not char_dir.is_dir():
        return 0
    if not backend_up(MARINARA_PORT):
        log("marinara not running — skip marinara import")
        return 0

    existing_names = marinara_character_names()
    pending: list[tuple[str, Path]] = []

    for path in sorted(char_dir.iterdir()):
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        if ext not in {".png", ".json", ".charx"}:
            continue
        if path.name.lower().startswith("hub_marinara_"):
            continue

        rel = str(path.relative_to(SHARED))
        sig = file_sig(path)
        if state["characters"].get(rel) == sig:
            continue

        card_name = char_name_from_path(path).strip().lower()
        if card_name and card_name in existing_names:
            state["characters"][rel] = sig
            log(f"marinara skip duplicate name: {path.name}")
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

    session = lumiverse_session()
    if not session:
        return 0
    opener, auth_headers = session

    pending: list[tuple[str, Path]] = []
    for path in sorted(char_dir.iterdir()):
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        if ext not in {".png", ".json", ".charx"}:
            continue
        if path.name.lower().startswith("hub_lumiverse_"):
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
        status, payload = multipart_batch(
            url,
            batch,
            opener=opener,
            fields={"skip_duplicates": "true"},
            headers=auth_headers,
        )
        if status >= 400:
            log(f"lumiverse character batch import failed ({status}): {payload}")
            batch = []
            batch_meta = []
            return
        results = payload.get("results", []) if isinstance(payload, dict) else []
        for rel, result in zip(batch_meta, results):
            if result.get("success"):
                state["characters"][rel] = file_sig(SHARED / rel)
                if not result.get("skipped"):
                    imported += 1
                    log(f"imported character → lumiverse: {rel}")
                else:
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


def main() -> int:
    log(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} syncing shared library")
    state = load_state()
    cleanup_shared_junk(state)

    n_export_marinara = export_marinara_to_shared(state)
    n_export_lumiverse = export_lumiverse_to_shared(state)
    n_export_st = sync_st_to_shared(state)

    if should_run_import():
        rsync_shared()
        n_chars_marinara = import_characters_to_marinara(state)
        n_chars_lumiverse = import_characters_to_lumiverse(state)
        n_chars_st = sync_shared_to_st(state)
        n_worlds = import_lorebooks_to_marinara(state)
    else:
        n_chars_marinara = 0
        n_chars_lumiverse = 0
        n_chars_st = 0
        n_worlds = 0

    save_state(state)
    log(
        "done — "
        f"exported: marinara +{n_export_marinara}, lumiverse +{n_export_lumiverse}, st +{n_export_st}; "
        f"imported: marinara +{n_chars_marinara}, lumiverse +{n_chars_lumiverse}, "
        f"st +{n_chars_st}, lorebooks +{n_worlds}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())