#!/usr/bin/env python3
"""Extract Notion auth cookies from local Chromium profiles (Chrome / Edge).

Local-only utility for the user's own machine. Reads Chromium's cookie SQLite DB,
decrypts values with the profile's DPAPI-protected AES key (v10/v11) and writes
only the Notion credentials to work/ai-proxy/secret/credential.json.
Never prints full secret values; prints masked diagnostics only.

Usage: python extract_cookie.py [--verbose]
"""
import base64
import ctypes
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path

HOME = Path(os.environ["USERPROFILE"])
SECRET_DIR = Path(__file__).resolve().parent.parent / "secret"
SECRET_DIR.mkdir(parents=True, exist_ok=True)

BROWSERS = {
    "chrome": HOME / "AppData/Local/Google/Chrome/User Data",
    "edge": HOME / "AppData/Local/Microsoft/Edge/User Data",
    "brave": HOME / "AppData/Local/BraveSoftware/Brave-Browser/User Data",
}

NOTION_NAMES = {
    "token_v2",
    "notion_user_id",
    "notion_browser_id",
    "notion_check_cookie_consent",
    "notion_users",
    "notion_signal",
}


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def dpapi_unprotect(data: bytes) -> bytes:
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise OSError(f"CryptUnprotectData failed (winerr={ctypes.GetLastError()})")
    out = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return out


def load_master_key(user_data: Path):
    ls_path = user_data / "Local State"
    if not ls_path.exists():
        return None, "no Local State"
    try:
        state = json.loads(ls_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return None, f"Local State unreadable: {e}"
    os_crypt = state.get("os_crypt") or {}
    enc = os_crypt.get("encrypted_key")
    if not enc:
        return None, "no os_crypt.encrypted_key (app-bound only?)"
    raw = base64.b64decode(enc)
    if raw[:5] != b"DPAPI":
        return None, f"unexpected key prefix {raw[:5]!r}"
    try:
        return dpapi_unprotect(raw[5:]), "DPAPI"
    except OSError as e:
        return None, f"DPAPI decrypt failed: {e}"


def decrypt_value(enc: bytes, key: bytes | None):
    """Return (plaintext, scheme) or (None, scheme)."""
    if not enc:
        return b"", "empty"
    prefix = enc[:3]
    if prefix in (b"v10", b"v11"):
        if not key:
            return None, "v10-no-key"
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            nonce, payload = enc[3:15], enc[15:]
            return AESGCM(key).decrypt(nonce, payload, None), "v10"
        except Exception as e:  # noqa: BLE001
            return None, f"v10-fail:{type(e).__name__}"
    if prefix == b"v20":
        return None, "v20-app-bound"
    # Chromium <80 plaintext
    try:
        return enc, "plain"
    except Exception:  # noqa: BLE001
        return None, "unknown"


def profile_dirs(user_data: Path):
    yield user_data / "Default"
    for p in sorted(user_data.glob("Profile *")):
        yield p


def read_cookies(db_path: Path):
    """Copy DB (+WAL/SHM) to temp, fall back to read-only live connection."""
    tmp = Path(tempfile.mkdtemp(prefix="notion-cookie-"))
    try:
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(db_path) + suffix)
            if src.exists():
                try:
                    shutil.copy2(src, tmp / (db_path.name + suffix))
                except PermissionError:
                    pass
        if not (tmp / db_path.name).exists():
            raise PermissionError("could not copy cookie DB")
        conn = sqlite3.connect(str(tmp / db_path.name))
        try:
            return conn.execute(
                "SELECT host_key, name, encrypted_value, value FROM cookies"
            ).fetchall()
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    verbose = "--verbose" in sys.argv
    found = []
    for browser, user_data in BROWSERS.items():
        if not user_data.exists():
            continue
        key, key_note = load_master_key(user_data)
        for prof in profile_dirs(user_data):
            db = prof / "Network" / "Cookies"
            if not db.exists():
                continue
            try:
                rows = read_cookies(db)
            except Exception as e:  # noqa: BLE001
                print(f"[{browser}/{prof.name}] cannot read cookie DB: {type(e).__name__}: {e}")
                continue
            hits = []
            for host_key, name, enc, plain in rows:
                if name in NOTION_NAMES or "notion" in name.lower():
                    hits.append((host_key, name, enc))
            if not hits:
                continue
            print(f"[{browser}/{prof.name}] notion cookies: {len(hits)} ({key_note})")
            for host_key, name, enc in hits:
                plain, scheme = decrypt_value(enc, key)
                if plain is None:
                    print(f"    {name} @ {host_key}: [undecryptable: {scheme}]")
                    continue
                text = plain.decode("utf-8", "replace")
                masked = text[:6] + "…" + text[-4:] if len(text) > 14 else "…"
                print(f"    {name} @ {host_key}: len={len(text)} scheme={scheme} {masked}")
                if name in ("token_v2", "notion_user_id", "notion_browser_id", "notion_users"):
                    found.append(
                        {
                            "browser": browser,
                            "profile": prof.name,
                            "host_key": host_key,
                            "name": name,
                            "value": text,
                        }
                    )

    if not found:
        print("\nRESULT: no decryptable Notion credential found.")
        return 2

    # Pick the best token_v2: prefer notion.com host, prefer non-Default-Chrome? keep all.
    tokens = [f for f in found if f["name"] == "token_v2" and len(f["value"]) > 20]
    if not tokens:
        print("\nRESULT: notion cookies found but no token_v2.")
        return 3
    tokens.sort(key=lambda f: (".notion.com" in f["host_key"], f["browser"] == "chrome"), reverse=True)
    token = tokens[0]
    by_name = {}
    for f in found:
        if f["name"] and f["name"] != "token_v2" and f["name"] not in by_name:
            by_name[f["name"]] = f["value"]

    cred = {
        "token_v2": token["value"],
        "notion_user_id": by_name.get("notion_user_id", ""),
        "notion_browser_id": by_name.get("notion_browser_id", ""),
        "source": f'{token["browser"]}/{token["profile"]}',
        "host_key": token["host_key"],
        "extracted_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
    }
    cred_path = SECRET_DIR / "credential.json"
    cred_path.write_text(json.dumps(cred, ensure_ascii=False, indent=2), encoding="utf-8")
    cred_path.chmod(0o600)

    print("\nRESULT: OK")
    print(f"  source: {cred['source']} (cookie host {cred['host_key']})")
    print(f"  token_v2: len={len(cred['token_v2'])}")
    print(f"  notion_user_id: {'present' if cred['notion_user_id'] else 'MISSING'}")
    print(f"  notion_browser_id: {'present' if cred['notion_browser_id'] else 'MISSING'}")
    print(f"  written: {cred_path} (git-ignored, never printed)")
    if verbose:
        print(f"  discovered cookie names: {sorted({f['name'] for f in found})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
