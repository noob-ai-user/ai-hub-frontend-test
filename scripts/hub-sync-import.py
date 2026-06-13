#!/usr/bin/env python3
"""
Bidirectional hub sync via /data/shared (standard Tavern PNG/JSON cards).

Canonical files: hub_{slug}.png — ONE global file per character name (shared database).
- Legacy hub_{source}_{slug}.png files are merged into hub_{slug}.png on sync
- SillyTavern reads shared cards via symlinks (no duplicate PNG copies)
- Marinara/Lumiverse import once per character name (skip duplicates)
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
HUB_SOURCE_PREFIX_RE = re.compile(r"^hub_(st|marinara|lumiverse)_", re.I)
GLOBAL_CANONICAL_RE = re.compile(r"^hub_[a-z][a-z0-9_]+\.png$", re.I)
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


def extract_bearer_token(
    payload: object,
    set_cookies: list[str],
    response_headers: object | None = None,
) -> str | None:
    if response_headers is not None:
        for header_name in ("set-auth-token", "Set-Auth-Token"):
            try:
                token = response_headers.get(header_name)
            except Exception:
                token = None
            if isinstance(token, str) and token.strip():
                return token.strip()
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


def lumiverse_api_headers(auth_headers: dict[str, str]) -> dict[str, str]:
    hdrs = dict(auth_headers)
    hdrs.setdefault("Accept", "application/json")
    hdrs["Accept-Encoding"] = "identity"
    public_origin = resolve_public_origin()
    if public_origin:
        try:
            host = public_origin.split("://", 1)[1]
            hdrs["Host"] = host
            hdrs["X-Forwarded-Host"] = host
            hdrs["X-Forwarded-Proto"] = "https"
            hdrs["X-Forwarded-Prefix"] = "/apps/lumiverse"
        except IndexError:
            pass
    hdrs["X-Forwarded-For"] = "127.0.0.1"
    hdrs["X-Real-IP"] = "127.0.0.1"
    return hdrs


def decode_json_response(raw: str, status: int) -> object:
    if not raw or not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        preview = raw[:240].replace("\n", " ").replace("\r", " ")
        return {"error": "non-json response", "preview": preview, "status": status}


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
    # Drop a trailing file extension so a name that accidentally carries one
    # (e.g. "Eldoria.json" coming back from an app that used the filename as the
    # lorebook name) slugs to "eldoria", not "eldoria_json".
    slug = re.sub(r"\.(json|png|webp|charx|card)$", "", slug)
    slug = re.sub(r"[^a-z0-9]+", "_", slug).strip("_")
    return slug[:60] or "character"


def canonical_filename(_source: str, name: str) -> str:
    return f"hub_{name_slug(name)}.png"


def canonical_key(_source: str, name: str) -> str:
    return f"canonical:{name_slug(name)}"


def is_global_canonical(name: str) -> bool:
    return bool(GLOBAL_CANONICAL_RE.match(name)) and not HUB_SOURCE_PREFIX_RE.match(name)


def is_legacy_source_canonical(name: str) -> bool:
    return bool(HUB_SOURCE_PREFIX_RE.match(name)) and name.lower().endswith(".png")


def global_slug_from_filename(name: str) -> str | None:
    if is_global_canonical(name):
        return name[len("hub_") : -4]
    m = re.match(r"^hub_(?:st|marinara|lumiverse)_([a-z][a-z0-9_]*)\.png$", name, re.I)
    if m and not is_random_st_id_slug(m.group(1)):
        return m.group(1)
    return None


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
            return resp.status, decode_json_response(raw, resp.status)
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
            return resp.status, decode_json_response(raw, resp.status)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return exc.code, decode_json_response(raw, exc.code)


def lumiverse_session() -> tuple[urllib.request.OpenerDirector, dict[str, str]] | None:
    if not OWNER_PASSWORD:
        log("lumiverse sync skipped — set OWNER_PASSWORD (your Lumiverse login password) in HF Secrets")
        return None

    username = lumiverse_owner_username()
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    base = f"http://127.0.0.1:{LUMIVERSE_PORT}"

    # ALWAYS authenticate directly against Lumiverse's port — never through the
    # public gateway.  The gateway rewrites paths and adds forwarded headers that
    # confuse BetterAuth.  Direct-port auth with explicit forwarded-prefix is the
    # reliable path for server-to-server sync.
    sign_in_url = f"{base}/api/auth/sign-in/username"
    sign_in_headers: dict[str, str] = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": base,
        "X-Forwarded-Prefix": "/apps/lumiverse",
        "X-Forwarded-Proto": "https",
    }
    # Add X-Forwarded-Host from public origin if available (BetterAuth needs it)
    public_origin = resolve_public_origin()
    if public_origin:
        try:
            host = public_origin.split("://", 1)[1]
            sign_in_headers["X-Forwarded-Host"] = host
        except IndexError:
            pass

    body = json.dumps({"username": username, "password": OWNER_PASSWORD}).encode("utf-8")
    req = urllib.request.Request(
        sign_in_url,
        data=body,
        headers=sign_in_headers,
        method="POST",
    )
    response_headers = None
    set_cookies: list[str] = []
    payload: object = {}
    try:
        with opener.open(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            payload = decode_json_response(raw, resp.status)
            response_headers = resp.headers
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
    token = extract_bearer_token(payload, set_cookies, response_headers)
    if token:
        auth_headers["Authorization"] = f"Bearer {token}"
        log(f"lumiverse authenticated as '{username}' (token obtained)")
    else:
        log(
            f"lumiverse sign-in for '{username}' ok but no bearer token "
            "(missing set-auth-token header) — sync may fail; check OWNER_PASSWORD"
        )
    return opener, lumiverse_api_headers(auth_headers)


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
    slug = global_slug_from_filename(path.name)
    if slug:
        return slug.replace("_", " ")
    stem = path.stem
    if HUB_SOURCE_PREFIX_RE.match(path.name):
        parts = stem.split("_", 2)
        if len(parts) >= 3:
            legacy_slug = parts[2]
            if not is_random_st_id_slug(legacy_slug):
                return legacy_slug.replace("_", " ")
    return st_display_name(stem)


def st_display_name(stem: str) -> str:
    cleaned = ST_ID_PREFIX_RE.sub("", stem).strip()
    return cleaned or stem


def is_valid_shared_card(name: str) -> bool:
    if is_global_canonical(name):
        return True
    if is_legacy_source_canonical(name):
        slug = global_slug_from_filename(name)
        return slug is not None and not is_random_st_id_slug(slug)
    return False


def is_importable_global(name: str) -> bool:
    if not is_global_canonical(name):
        return False
    # A hub_default_* asset that has a canonical hub_* sibling is redundant
    slug = global_slug_from_filename(name)
    if slug:
        canon = canonical_slug(slug)
        if canon != slug:
            # This is an aliased form (e.g. hub_default_seraphina) —
            # only importable if the canonical form is missing.
            return False  # skip alias — canonical carries the authority
    return True


SLUG_ALIASES = {"default_seraphina": "seraphina"}

# Aliases in reversed direction for lookup: canonical → aliased forms
ALIAS_SLUGS = {v: k for k, v in SLUG_ALIASES.items()}


def normalize_slug(slug: str) -> str:
    return SLUG_ALIASES.get(slug, slug)


def canonical_slug(slug: str) -> str:
    """Return the canonical slug for a given slug (resolve aliases)."""
    return SLUG_ALIASES.get(slug, slug)


def all_alias_slugs(slug: str) -> set[str]:
    """Return {slug} plus any alternate slugs that resolve to the same canonical."""
    canonical = canonical_slug(slug)
    result = {canonical}
    for alias, canon in SLUG_ALIASES.items():
        if canon == canonical:
            result.add(alias)
    return result


def migrate_legacy_canonicals(state: dict) -> int:
    """Merge hub_{source}_{slug}.png → hub_{slug}.png (newest file wins)."""
    char_dir = SHARED / "characters"
    if not char_dir.is_dir():
        return 0

    best_by_slug: dict[str, tuple[Path, float]] = {}
    for path in char_dir.iterdir():
        if not path.is_file() or path.suffix.lower() != ".png":
            continue
        slug = global_slug_from_filename(path.name)
        if not slug or is_random_st_id_slug(slug):
            continue
        slug = normalize_slug(slug)
        mtime = path.stat().st_mtime_ns
        prev = best_by_slug.get(slug)
        if not prev or mtime > prev[1]:
            best_by_slug[slug] = (path, mtime)

    merged = 0
    for slug, (src_path, _) in best_by_slug.items():
        dest = char_dir / f"hub_{slug}.png"
        if dest.is_file() and dest.resolve() == src_path.resolve():
            continue
        try:
            if dest.is_file():
                if dest.stat().st_mtime_ns >= src_path.stat().st_mtime_ns and dest.read_bytes() == src_path.read_bytes():
                    pass
                else:
                    shutil.copy2(src_path, dest)
            else:
                shutil.copy2(src_path, dest)
            name = char_name_from_path(dest)
            ckey = canonical_key("shared", name)
            state["exports"][ckey] = {
                "file": f"characters/{dest.name}",
                "filename": dest.name,
                "updated": file_sig(dest),
                "name": name,
                "source_id": src_path.name,
            }
            state["characters"][f"characters/{dest.name}"] = file_sig(dest)
            merged += 1
            log(f"canonical character: characters/{dest.name} (from {src_path.name})")
        except OSError as exc:
            log(f"canonical merge failed for {src_path.name}: {exc}")

    removed = 0
    for path in list(char_dir.iterdir()):
        if not path.is_file():
            continue
        if is_legacy_source_canonical(path.name):
            slug = global_slug_from_filename(path.name)
            global_path = char_dir / f"hub_{slug}.png" if slug else None
            if global_path and global_path.is_file():
                try:
                    path.unlink()
                    state["characters"].pop(f"characters/{path.name}", None)
                    removed += 1
                    log(f"removed legacy duplicate: characters/{path.name}")
                except OSError as exc:
                    log(f"legacy cleanup failed for {path.name}: {exc}")
    return merged + removed


def cleanup_shared_junk(state: dict) -> int:
    char_dir = SHARED / "characters"
    if not char_dir.is_dir():
        return 0
    removed = 0

    # Step 1 — remove alias cards when their canonical counterpart exists
    canonical_files: dict[str, Path] = {}
    for path in char_dir.iterdir():
        if not path.is_file() or path.name.startswith("."):
            continue
        if is_global_canonical(path.name):
            slug = global_slug_from_filename(path.name)
            if slug:
                canon = canonical_slug(slug)
                if canon == slug:
                    canonical_files[canon] = path

    for path in list(char_dir.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        if is_global_canonical(path.name):
            slug = global_slug_from_filename(path.name)
            if slug:
                canon = canonical_slug(slug)
                if canon != slug and canon in canonical_files:
                    # This is an alias card — canonical already exists.
                    # BEFORE deleting, repoint any ST symlinks to the canonical file.
                    canonical_path = canonical_files[canon]
                    st_dir = DATA_ROOT / "sillytavern" / "data" / "default-user" / "characters"
                    if st_dir.is_dir():
                        alias_abs = path.resolve()
                        for st_entry in st_dir.iterdir():
                            if st_entry.is_symlink():
                                try:
                                    if st_entry.resolve() == alias_abs:
                                        st_entry.unlink()
                                        st_entry.symlink_to(canonical_path.resolve())
                                        log(f"repointed ST symlink: {st_entry.name} → hub_{canon}.png")
                                except OSError:
                                    pass
                    try:
                        path.unlink()
                        state["characters"].pop(f"characters/{path.name}", None)
                        removed += 1
                        log(f"removed alias duplicate from shared: characters/{path.name} (canonical: hub_{canon}.png)")
                    except OSError as exc:
                        log(f"alias cleanup failed for {path.name}: {exc}")
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

    # Step 3 — remove malformed duplicate lorebooks. Older syncs named a
    # round-tripped lorebook "hub_eldoria_json.json" (the source filename's
    # ".json" got slugged into "_json"). Drop that artifact when the correct
    # "hub_eldoria.json" canonical exists.
    world_dir = SHARED / "world_info"
    if world_dir.is_dir():
        existing = {p.name for p in world_dir.iterdir() if p.is_file()}
        for path in list(world_dir.iterdir()):
            if not path.is_file() or not path.name.startswith("hub_"):
                continue
            if path.name.endswith("_json.json"):
                canonical = path.name[: -len("_json.json")] + ".json"
                if canonical != path.name and canonical in existing:
                    try:
                        path.unlink()
                        state.get("world_info", {}).pop(f"world_info/{path.name}", None)
                        removed += 1
                        log(f"removed duplicate lorebook from shared: world_info/{path.name} (canonical: {canonical})")
                    except OSError as exc:
                        log(f"lorebook cleanup failed for {path.name}: {exc}")

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

    # Atomic write: write to temp first, then rename — prevents
    # SillyTavern from reading a partially-written file mid-sync.
    tmp = dest.with_suffix(dest.suffix + ".hubsync-tmp")
    try:
        tmp.write_bytes(png_bytes)
        tmp.replace(dest)
    except OSError:
        # Fallback: direct write if atomic rename fails (e.g. cross-device)
        dest.write_bytes(png_bytes)
        if tmp.is_file():
            try:
                tmp.unlink()
            except OSError:
                pass
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
        log(f"marinara list failed ({status}): {str(payload)[:200]}")
        return 0
    if not payload:
        log("marinara returned empty character list — no characters to export")
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
            log(f"marinara export failed for {info['char_id']} '{info.get('name','?')}' (HTTP {png_status}, {len(png_bytes) if png_bytes else 0} bytes)")
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
        f"{base}/api/v1/characters?limit=500&offset=0",
        headers=auth_headers,
        opener=opener,
    )
    if status >= 400 or not isinstance(payload, dict):
        log(f"lumiverse list failed ({status}): {payload}")
        return 0
    if payload.get("error") == "non-json response":
        log(f"lumiverse list returned HTML/non-JSON ({status}): {payload.get('preview', '')[:120]}")
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
        # Skip symlinks — these point BACK to shared and must not be re-exported
        # (would create circular copies and duplicate characters).
        if path.is_symlink():
            # Clean up dangling symlinks (target was deleted)
            try:
                path.resolve(strict=True)
            except (OSError, FileNotFoundError):
                try:
                    path.unlink()
                    log(f"removed dangling ST symlink during export sweep: {path.name}")
                except OSError:
                    pass
            continue
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        if ext not in {".png", ".json"}:
            continue
        if path.name.startswith("hub_"):
            continue

        name = st_display_name(path.stem)
        # Resolve alias: default_seraphina → seraphina (canonical)
        canon_name = canonical_slug(name_slug(name)).replace("_", " ")
        if ext == ".png":
            dest_name = canonical_filename("st", canon_name)
        else:
            dest_name = path.name

        rel = f"characters/{dest_name}"
        sig = file_sig(path)
        ckey = canonical_key("st", canon_name)
        prev = state["exports"].get(ckey, {})

        # If canonical card already exists in shared, convert to symlink
        if canon_name != name:
            canon_rel = f"characters/hub_{name_slug(canon_name)}.png"
            canon_path = SHARED / canon_rel
            if canon_path.is_file():
                try:
                    path.unlink()
                    path.symlink_to(canon_path.resolve())
                    state["characters"][f"st_link:{path.name}"] = file_sig(canon_path)
                    log(f"converted ST alias to symlink: {path.name} → hub_{name_slug(canon_name)}.png")
                except OSError as exc:
                    log(f"ST alias symlink failed for {path.name}: {exc}")
                continue

        if state["characters"].get(rel) == sig and prev.get("source_id") == path.name:
            continue

        dest = SHARED / rel
        try:
            if path.resolve() == dest.resolve():
                state["characters"][f"st_src:{path.name}"] = sig
                state["characters"][rel] = file_sig(dest)
                state["exports"][ckey] = {
                    "file": rel,
                    "filename": dest_name,
                    "updated": sig,
                    "name": name,
                    "source_id": path.name,
                }
                continue
            shutil.copy2(path, dest)
            state["characters"][f"st_src:{path.name}"] = sig
            state["characters"][rel] = file_sig(dest)
            state["exports"][ckey] = {
                "file": rel,
                "filename": dest_name,
                "updated": sig,
                "name": canon_name,
                "source_id": path.name,
            }
            copied += 1
            log(f"exported ST character → shared: {rel}")
            # After exporting the canonical, replace local file with symlink
            if path.is_file() and not path.is_symlink():
                try:
                    path.unlink()
                    path.symlink_to(dest.resolve())
                    state["characters"][f"st_link:{path.name}"] = file_sig(dest)
                    log(f"replaced ST local with symlink: {path.name} → {dest_name}")
                except OSError:
                    pass  # fallthrough — file remains as local copy
        except OSError as exc:
            log(f"ST export failed for {path.name}: {exc}")

    # Export ST worlds to shared/world_info
    st_worlds_dir = DATA_ROOT / "sillytavern" / "data" / "default-user" / "worlds"
    world_info_dir = SHARED / "world_info"
    world_info_dir.mkdir(parents=True, exist_ok=True)
    if st_worlds_dir.is_dir():
        for path in sorted(st_worlds_dir.glob("*.json")):
            if not path.is_file() or path.name.startswith("hub_"):
                continue
            # Canonicalize the shared filename so ST, Marinara and Lumiverse all
            # converge on ONE file per lorebook (hub_{slug}.json) instead of ST
            # keeping "Eldoria.json" while the others write "hub_eldoria.json".
            canon_name = f"hub_{name_slug(path.stem)}.json"
            rel = f"world_info/{canon_name}"
            sig = file_sig(path)
            prev = state.get("world_info", {}).get(rel)
            if prev == sig:
                continue
            dest = world_info_dir / canon_name
            try:
                if path.resolve() != dest.resolve():
                    shutil.copy2(path, dest)
                    state.setdefault("world_info", {})[rel] = sig
                    copied += 1
                    log(f"exported lorebook → shared: {rel}")
                    # Remove the legacy non-canonical copy this routine used to
                    # write (e.g. shared/world_info/Eldoria.json). The source of
                    # truth is ST's own worlds dir, so the shared copy is safe to
                    # drop once the canonical file exists.
                    legacy = world_info_dir / path.name
                    if legacy.name != canon_name and legacy.is_file():
                        try:
                            legacy.unlink()
                            state.get("world_info", {}).pop(f"world_info/{path.name}", None)
                            log(f"removed duplicate lorebook ← shared: world_info/{path.name}")
                        except OSError:
                            pass
            except OSError as exc:
                log(f"ST export lorebook failed {path.name}: {exc}")

    return copied


def st_character_names() -> set[str]:
    """Return display names of all ST characters (native + symlinked from shared)."""
    names: set[str] = set()
    if not ST_CHARS.is_dir():
        return names
    for path in ST_CHARS.iterdir():
        # Include both real files AND symlinks (symlinks = shared cards linked in)
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".png", ".json"}:
            continue
        if path.name.startswith("hub_"):
            continue
        raw = st_display_name(path.stem)
        canon = canonical_slug(name_slug(raw))
        names.add(canon.replace("_", " "))
    return names


def sync_shared_symlinks_to_st(state: dict) -> int:
    """Link shared hub_{slug}.png into ST as {Name}.png — single on-disk copy."""
    char_dir = SHARED / "characters"
    if not char_dir.is_dir():
        return 0
    ST_CHARS.mkdir(parents=True, exist_ok=True)
    linked = 0
    existing_names = st_character_names()

    # First, clean up any dangling ST symlinks (target was deleted by cleanup)
    for st_entry in list(ST_CHARS.iterdir()):
        if st_entry.is_symlink():
            try:
                st_entry.resolve(strict=True)
            except (OSError, FileNotFoundError):
                try:
                    st_entry.unlink()
                    log(f"removed dangling ST symlink: {st_entry.name}")
                except OSError:
                    pass

    for path in sorted(char_dir.iterdir()):
        if not path.is_file() or not is_importable_global(path.name):
            continue
        # Skip files that aren't real PNGs (e.g. a webp saved as .png by an older
        # import). SillyTavern's strict PNG parser errors on them ("no IEND").
        if path.suffix.lower() == ".png":
            try:
                with open(path, "rb") as _fh:
                    if _fh.read(8) != b"\x89PNG\r\n\x1a\n":
                        continue
            except OSError:
                continue

        target_name = f"{safe_name(name)}.png"
        card_key = name.strip().lower()
        if card_key in existing_names:
            target = ST_CHARS / target_name
            if not target.exists() and not target.is_symlink():
                # Name exists in ST under a different filename. Skip creating duplicate.
                state["characters"][f"st_link:{target_name}"] = file_sig(path)
                continue
            
            st_key = f"st_link:{target_name}"
            if state["characters"].get(st_key) == file_sig(path):
                continue

        target = ST_CHARS / target_name
        shared_abs = path.resolve()
        try:
            if target.is_symlink():
                if target.resolve() == shared_abs:
                    state["characters"][f"st_link:{target_name}"] = file_sig(path)
                    continue
                target.unlink()
            elif target.is_file():
                # Real local ST card with same filename — do not overwrite.
                state["characters"][f"st_link:{target_name}"] = file_sig(path)
                existing_names.add(card_key)
                log(f"ST keep local card (not replaced): {target_name}")
                continue

            target.symlink_to(shared_abs)
            state["characters"][f"st_link:{target_name}"] = file_sig(path)
            existing_names.add(card_key)
            linked += 1
            log(f"linked shared → sillytavern: {target_name} → {path.name}")
        except OSError as exc:
            log(f"ST symlink failed for {path.name}: {exc}")

    return linked


def import_characters_to_marinara(state: dict) -> int:
    char_dir = SHARED / "characters"
    if not char_dir.is_dir():
        return 0
    if not backend_up(MARINARA_PORT):
        log("marinara not running — skip marinara import")
        return 0

    existing_names = marinara_character_names()
    log(f"marinara has {len(existing_names)} characters: {sorted(existing_names)[:10]}")
    # Match on slug so punctuation/spacing differences don't cause re-imports
    # (e.g. Marinara name "little sister, big theft" vs file slug
    # "little_sister_big_theft"). Mismatches here used to re-import every cycle,
    # duplicating the character in Marinara.
    existing_slugs = {name_slug(n) for n in existing_names}
    pending: list[tuple[str, Path]] = []

    for path in sorted(char_dir.iterdir()):
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        if ext not in {".png", ".json", ".charx"}:
            continue
        if not is_importable_global(path.name):
            continue

        rel = str(path.relative_to(SHARED))
        sig = file_sig(path)

        # Check if character already exists in Marinara by name (most reliable)
        card_name = canonical_slug(char_name_from_path(path).strip().lower())
        card_slug = name_slug(card_name) if card_name else ""
        if card_slug and card_slug in existing_slugs:
            # Character exists — update state sig so we don't re-check next cycle
            state["characters"][rel] = sig
            continue

        # State says imported but character is gone from Marinara — re-import
        if state["characters"].get(rel) == sig:
            # Double-check: maybe the name changed or dedup was wrong
            if card_slug and card_slug not in existing_slugs:
                log(f"marinara re-import (missing despite state): {card_name} ({path.name})")
            else:
                continue

        pending.append((rel, path, card_name))

    if not pending:
        return 0

    imported = 0

    # Try batch import first
    batch: list[tuple[str, bytes]] = []
    batch_meta: list[tuple[str, str]] = []  # (rel, card_name)

    def flush_batch() -> None:
        nonlocal imported, batch, batch_meta
        if not batch:
            return
        url = f"http://127.0.0.1:{MARINARA_PORT}/api/import/st-character/batch"
        status, payload = multipart_batch(url, batch)
        if status >= 400:
            log(f"marinara batch import failed ({status}) — falling back to single-file")
            # Fallback: try single-file imports
            for i, (name, data) in enumerate(batch):
                rel, card_name = batch_meta[i]
                single_url = f"http://127.0.0.1:{MARINARA_PORT}/api/import/st-character"
                s_status, s_payload = multipart_batch(single_url, [(name, data)])
                if s_status < 400:
                    result = s_payload if isinstance(s_payload, dict) else {}
                    if result.get("success", True):
                        state["characters"][rel] = file_sig(SHARED / rel)
                        imported += 1
                        log(f"imported character → marinara (single): {rel}")
                    else:
                        log(f"marinara single import failed for {rel}: {result}")
                else:
                    log(f"marinara single import HTTP {s_status} for {rel}")
            batch = []
            batch_meta = []
            return
        results = payload.get("results", []) if isinstance(payload, dict) else []
        for i, result in enumerate(results):
            if i >= len(batch_meta):
                break
            rel, card_name = batch_meta[i]
            if isinstance(result, dict) and result.get("success"):
                state["characters"][rel] = file_sig(SHARED / rel)
                imported += 1
                log(f"imported character → marinara: {rel}")
            else:
                err = result.get("error", "unknown") if isinstance(result, dict) else "unknown"
                log(f"marinara import failed for {rel}: {err}")
        batch = []
        batch_meta = []

    for rel, path, card_name in pending:
        try:
            file_content = path.read_bytes()
        except OSError as exc:
            log(f"read failed {rel}: {exc}")
            continue
        batch.append((path.name, file_content))
        batch_meta.append((rel, card_name))
        if len(batch) >= 10:
            flush_batch()
    flush_batch()

    # Verify import: refresh Marinara names and confirm
    if imported > 0:
        new_names = marinara_character_names()
        newly_visible = new_names - existing_names
        if newly_visible:
            log(f"marinara now has {len(newly_visible)} new characters: {sorted(newly_visible)}")

    return imported


def lumiverse_character_names(opener: urllib.request.OpenerDirector, auth_headers: dict[str, str]) -> set[str]:
    base = f"http://127.0.0.1:{LUMIVERSE_PORT}"
    status, payload = http_json(
        "GET",
        f"{base}/api/v1/characters?limit=500&offset=0",
        headers=auth_headers,
        opener=opener,
    )
    if status >= 400 or not isinstance(payload, dict):
        return set()
    chars = payload.get("data") or []
    names: set[str] = set()
    if isinstance(chars, list):
        for item in chars:
            if isinstance(item, dict) and item.get("name"):
                names.add(str(item["name"]).strip().lower())
    return names


def import_characters_to_lumiverse(state: dict) -> int:
    char_dir = SHARED / "characters"
    if not char_dir.is_dir():
        return 0
    if not backend_up(LUMIVERSE_PORT):
        log("lumiverse not running — skip lumiverse import")
        return 0

    session = lumiverse_session()
    if not session:
        log("lumiverse import skipped — OWNER_PASSWORD required for Lumiverse API access")
        return 0
    opener, auth_headers = session
    existing_names = lumiverse_character_names(opener, auth_headers)
    log(f"lumiverse has {len(existing_names)} characters: {sorted(existing_names)[:10]}")
    existing_slugs = {name_slug(n) for n in existing_names}

    pending: list[tuple[str, Path, str]] = []
    for path in sorted(char_dir.iterdir()):
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        if ext not in {".png", ".json", ".charx"}:
            continue
        if not is_importable_global(path.name):
            continue

        rel = str(path.relative_to(SHARED))
        sig = file_sig(path)
        card_name = char_name_from_path(path).strip().lower()

        # Normalize through aliases before checking (e.g. default_seraphina → seraphina)
        card_name_norm = canonical_slug(card_name) if card_name else card_name
        card_slug = name_slug(card_name_norm) if card_name_norm else ""
        # Character already in Lumiverse — mark as synced
        if card_slug and card_slug in existing_slugs:
            state["characters"][rel] = sig
            continue

        # State says imported but character gone — re-import
        if state["characters"].get(rel) == sig:
            if card_slug and card_slug not in existing_slugs:
                log(f"lumiverse re-import (missing despite state): {card_name_norm} ({path.name})")
            else:
                continue

        pending.append((rel, path, card_name))

    if not pending:
        return 0

    imported = 0

    # Try batch import first, fall back to single-file
    batch: list[tuple[str, bytes]] = []
    batch_meta: list[tuple[str, str]] = []

    def flush_batch() -> None:
        nonlocal imported, batch, batch_meta
        if not batch:
            return

        # Try bulk endpoint first
        url = f"http://127.0.0.1:{LUMIVERSE_PORT}/api/v1/characters/import-bulk"
        status, payload = multipart_batch(
            url, batch, opener=opener,
            fields={"skip_duplicates": "true"},
            headers=auth_headers,
        )

        if status >= 400:
            log(f"lumiverse batch import failed ({status}) — trying single-file imports")
            # Fallback: single-file import endpoint
            single_url = f"http://127.0.0.1:{LUMIVERSE_PORT}/api/v1/characters/import"
            for i, (name, data) in enumerate(batch):
                rel, card_name = batch_meta[i]
                s_status, s_payload = multipart_batch(
                    single_url, [(name, data)],
                    opener=opener,
                    headers=auth_headers,
                )
                if s_status < 400:
                    state["characters"][rel] = file_sig(SHARED / rel)
                    imported += 1
                    log(f"imported character → lumiverse (single): {rel}")
                else:
                    log(f"lumiverse single import HTTP {s_status} for {rel}")
            batch = []
            batch_meta = []
            return

        results = payload.get("results", []) if isinstance(payload, dict) else []
        # If no results array, treat whole batch as success if status < 400
        if not results and isinstance(payload, dict) and not payload.get("error"):
            for rel, card_name in batch_meta:
                state["characters"][rel] = file_sig(SHARED / rel)
                imported += 1
                log(f"imported character → lumiverse: {rel}")
            batch = []
            batch_meta = []
            return

        for i, result in enumerate(results):
            if i >= len(batch_meta):
                break
            rel, card_name = batch_meta[i]
            if isinstance(result, dict) and result.get("success"):
                state["characters"][rel] = file_sig(SHARED / rel)
                if not result.get("skipped"):
                    imported += 1
                    log(f"imported character → lumiverse: {rel}")
                else:
                    log(f"lumiverse skipped duplicate: {rel}")
            else:
                err = result.get("error", "unknown") if isinstance(result, dict) else "unknown"
                log(f"lumiverse import failed for {rel}: {err}")
        batch = []
        batch_meta = []

    for rel, path, card_name in pending:
        try:
            file_content = path.read_bytes()
        except OSError as exc:
            log(f"read failed {rel}: {exc}")
            continue
        batch.append((path.name, file_content))
        batch_meta.append((rel, card_name))
        if len(batch) >= 10:
            flush_batch()
    flush_batch()

    # Verify
    if imported > 0:
        new_names = lumiverse_character_names(opener, auth_headers)
        newly_visible = new_names - existing_names
        if newly_visible:
            log(f"lumiverse now has {len(newly_visible)} new characters: {sorted(newly_visible)}")

    return imported


def export_marinara_lorebooks_to_shared(state: dict) -> int:
    if not should_run_export("marinara"):
        return 0
    if not backend_up(MARINARA_PORT):
        return 0

    status, payload = http_json("GET", f"http://127.0.0.1:{MARINARA_PORT}/api/lorebooks/")
    if status >= 400 or not isinstance(payload, list):
        return 0

    world_dir = SHARED / "world_info"
    world_dir.mkdir(parents=True, exist_ok=True)
    exported = 0

    for item in payload:
        if not isinstance(item, dict):
            continue
        lb_id = str(item.get("id") or "")
        if not lb_id:
            continue

        updated = str(item.get("updatedAt") or item.get("updated_at") or "")
        name = str(item.get("name") or "lorebook")
        
        canon_name = f"hub_{name_slug(name)}.json"
        rel = f"world_info/{canon_name}"

        ckey = f"marinara_lb:{lb_id}"
        prev = state.get("exports", {}).get(ckey)
        if prev and prev.get("updated") >= updated:
            continue

        status_exp, exp_json = http_json("GET", f"http://127.0.0.1:{MARINARA_PORT}/api/lorebooks/{lb_id}/export?format=compatible")
        if status_exp >= 400 or not exp_json:
            continue

        dest = world_dir / canon_name
        try:
            dest.write_text(json.dumps(exp_json, indent=2), encoding="utf-8")
            state.setdefault("world_info", {})[rel] = file_sig(dest)
            state.setdefault("exports", {})[ckey] = {"updated": updated, "file": rel}
            exported += 1
            log(f"exported lorebook → shared: {rel}")
        except Exception as exc:
            log(f"failed to write exported marinara lorebook {rel}: {exc}")

    return exported


def export_lumiverse_lorebooks_to_shared(state: dict) -> int:
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
        f"{base}/api/v1/world_books?limit=500&offset=0",
        headers=auth_headers,
        opener=opener,
    )
    if status >= 400 or not isinstance(payload, dict):
        return 0

    items = payload.get("items", payload) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return 0

    world_dir = SHARED / "world_info"
    world_dir.mkdir(parents=True, exist_ok=True)
    exported = 0

    for item in items:
        if not isinstance(item, dict):
            continue
        lb_id = str(item.get("id") or "")
        if not lb_id:
            continue

        updated = str(item.get("updatedAt") or item.get("updated_at") or "")
        name = str(item.get("name") or "lorebook")
        
        canon_name = f"hub_{name_slug(name)}.json"
        rel = f"world_info/{canon_name}"

        ckey = f"lumiverse_lb:{lb_id}"
        prev = state.get("exports", {}).get(ckey)
        if prev and prev.get("updated") >= updated:
            continue

        status_exp, exp_json = http_json(
            "GET",
            f"{base}/api/v1/world_books/{lb_id}/export?format=sillytavern",
            headers=auth_headers,
            opener=opener,
        )
        if status_exp >= 400 or not exp_json:
            continue

        dest = world_dir / canon_name
        try:
            dest.write_text(json.dumps(exp_json, indent=2), encoding="utf-8")
            state.setdefault("world_info", {})[rel] = file_sig(dest)
            state.setdefault("exports", {})[ckey] = {"updated": updated, "file": rel}
            exported += 1
            log(f"exported lorebook → shared: {rel}")
        except Exception as exc:
            log(f"failed to write exported lumiverse lorebook {rel}: {exc}")

    return exported


def import_lorebooks_to_marinara(state: dict) -> int:
    world_dir = SHARED / "world_info"
    if not world_dir.is_dir():
        return 0
    if not backend_up(MARINARA_PORT):
        return 0

    imported = 0
    all_jsons = sorted(world_dir.glob("*.json"))
    # Set of canonical slugs present (hub_{slug}.json). If both "Eldoria.json"
    # and "hub_eldoria.json" exist, import only the canonical one.
    canonical_slugs = {
        p.name[len("hub_") : -len(".json")]
        for p in all_jsons
        if p.name.startswith("hub_") and p.name.endswith(".json")
    }
    for path in all_jsons:
        if not path.name.startswith("hub_") and name_slug(path.stem) in canonical_slugs:
            continue  # canonical sibling carries this lorebook
        rel = str(path.relative_to(SHARED))
        sig = file_sig(path)
        if state["world_info"].get(rel) == sig:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            log(f"skip lorebook {rel}: {exc}")
            continue
        # Marinara names the lorebook from the JSON's internal `name`, else from
        # this fallback. Hand it a clean display name (no hub_ prefix, no .json)
        # so the round-trip re-export stays stable as hub_{slug}.json instead of
        # drifting to hub_eldoria_json.json.
        clean = path.stem[len("hub_"):] if path.stem.startswith("hub_") else path.stem
        payload["__filename"] = clean.replace("_", " ").strip() or path.stem
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
    try:
        log(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} syncing shared library")
        state = load_state()
        cleanup_shared_junk(state)
        migrate_legacy_canonicals(state)

        n_export_marinara = export_marinara_to_shared(state)
        n_export_lumiverse = export_lumiverse_to_shared(state)
        n_export_st = sync_st_to_shared(state)
        
        n_export_marinara_lb = export_marinara_lorebooks_to_shared(state)
        n_export_lumiverse_lb = export_lumiverse_lorebooks_to_shared(state)

        if should_run_import():
            rsync_shared()
            n_chars_marinara = import_characters_to_marinara(state)
            n_chars_lumiverse = import_characters_to_lumiverse(state)
            n_chars_st = sync_shared_symlinks_to_st(state)
            n_worlds = import_lorebooks_to_marinara(state)
        else:
            n_chars_marinara = 0
            n_chars_lumiverse = 0
            n_chars_st = 0
            n_worlds = 0

        save_state(state)
        log(
            "done — "
            f"exported: marinara +{n_export_marinara}, lumiverse +{n_export_lumiverse}, st +{n_export_st}, "
            f"lorebooks (marinara +{n_export_marinara_lb}, lumiverse +{n_export_lumiverse_lb}); "
            f"imported: marinara +{n_chars_marinara}, lumiverse +{n_chars_lumiverse}, "
            f"st +{n_chars_st}, lorebooks +{n_worlds}"
        )
        return 0
    except Exception as exc:
        log(f"sync fatal error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())