#!/usr/bin/env python3
"""Read Notion auth cookies from a running Chromium browser via CDP.

Why: Chrome/Edge 127+ encrypt cookies with App-Bound Encryption (v20), which is
not decryptable offline without elevation. If a browser was launched with
--remote-debugging-port, the browser itself hands us decrypted cookies via CDP
(it serves HttpOnly cookies too, unlike document.cookie). This is the local,
non-destructive path used by this project.

Reads only Notion-related cookies, writes credentials to
work/ai-proxy/secret/credential.json, never prints secret values.

Usage: python cdp_cookie_extract.py [--port 9222] [--verbose]
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import unquote

import requests
import websocket

HOME = Path(os.environ["USERPROFILE"])
SECRET_DIR = Path(__file__).resolve().parent.parent / "secret"
SECRET_DIR.mkdir(parents=True, exist_ok=True)

USER_DATA_DIRS = [
    HOME / "AppData/Local/Microsoft/Edge/User Data",
    HOME / "AppData/Local/Google/Chrome/User Data",
    HOME / "AppData/Local/BraveSoftware/Brave-Browser/User Data",
]

NO_PROXY = {"http": None, "https": None}


def discover_endpoints():
    """Yield (label, port, browser_info, ws_url) for browsers with a DevTools port.

    Newer Edge hides the legacy /json HTTP routes (they 404) but still writes
    the browser WS path into DevToolsActivePort, so we can connect directly.
    """
    seen = set()
    for ud in USER_DATA_DIRS:
        f = ud / "DevToolsActivePort"
        if not f.exists():
            continue
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").strip().splitlines()
            port = int(lines[0].strip())
            ws_path = lines[1].strip() if len(lines) > 1 else ""
        except Exception:  # noqa: BLE001
            continue
        if port in seen:
            continue
        seen.add(port)
        info: dict = {}
        try:
            r = requests.get(
                f"http://127.0.0.1:{port}/json/version", timeout=3, proxies=NO_PROXY
            )
            if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
                info = r.json()
        except Exception:  # noqa: BLE001
            pass
        ws_url = info.get("webSocketDebuggerUrl") or (
            f"ws://127.0.0.1:{port}{ws_path}" if ws_path.startswith("/devtools/") else None
        )
        if ws_url:
            yield ud.name, port, info, ws_url


def cdp_call(ws, method, params=None, msg_id=1):
    ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == msg_id:
            return msg


def get_all_cookies(ws_url: str):
    ws = websocket.create_connection(ws_url, timeout=10, suppress_origin=True)
    try:
        resp = cdp_call(ws, "Storage.getCookies")
        if "result" in resp:
            return resp["result"].get("cookies", []), "Storage.getCookies"
        err = resp.get("error", {}).get("message", "")
        print(f"  Storage.getCookies failed ({err}); trying Network.getAllCookies")
        resp = cdp_call(ws, "Network.getAllCookies", msg_id=2)
        if "result" in resp:
            return resp["result"].get("cookies", []), "Network.getAllCookies"
        return [], "none"
    finally:
        ws.close()


def is_notion(c: dict) -> bool:
    name = (c.get("name") or "").lower()
    domain = (c.get("domain") or "").lower()
    return "notion" in domain or name in {
        "token_v2",
        "notion_user_id",
        "notion_browser_id",
        "notion_users",
        "notion_signal",
    }


def main() -> int:
    verbose = "--verbose" in sys.argv
    cli_ports = []
    if "--port" in sys.argv:
        cli_ports = [int(p) for p in sys.argv[sys.argv.index("--port") + 1].split(",")]

    versions = list(discover_endpoints())
    for port in cli_ports:
        try:
            r = requests.get(
                f"http://127.0.0.1:{port}/json/version", timeout=3, proxies=NO_PROXY
            )
            info = r.json() if r.status_code == 200 else {}
        except Exception:  # noqa: BLE001
            # DevToolsActivePort may already have registered it; try bare WS route.
            info = {}
        if not info:
            dp = None
            for ud in USER_DATA_DIRS:
                if (ud / "DevToolsActivePort").exists():
                    dp = ud / "DevToolsActivePort"
            ws = f"ws://127.0.0.1:{port}/devtools/browser"
            print(f"[port{port}] direct HTTP endpoint unavailable; trying raw WS {ws}")
            versions.append((f"port{port}", port, info, ws))
        else:
            versions.append((f"port{port}", port, info, info.get("webSocketDebuggerUrl")))

    if not versions:
        print("RESULT: no browser with an active DevTools port found.")
        print("  (Chrome/Edge must be started with --remote-debugging-port=9222)")
        return 2

    all_cookies = []
    source = ""
    for label, port, info, ws_url in versions:
        if not ws_url:
            continue
        print(f"[{label}] CDP port {port}: {info.get('Browser', 'ws-only')}")
        try:
            cookies, how = get_all_cookies(ws_url)
        except Exception as e:  # noqa: BLE001
            print(f"  CDP error: {type(e).__name__}: {e}")
            continue
        notion = [c for c in cookies if is_notion(c)]
        print(f"  total cookies: {len(cookies)}, notion: {len(notion)} ({how})")
        if notion:
            all_cookies.extend(notion)
            source = f"{label}:{port}"
            break

    if not all_cookies:
        print("\nRESULT: no Notion cookies found over CDP (not logged in in that browser?)")
        return 3

    tokens = [c for c in all_cookies if c["name"] == "token_v2" and c.get("value")]
    if not tokens:
        print("\nRESULT: Notion cookies present but token_v2 missing/empty.")
        if verbose:
            print("  names:", sorted({c["name"] for c in all_cookies}))
        return 4

    # app.notion.com token first, then .notion.com, then anything.
    def tok_rank(c):
        d = c.get("domain", "")
        return (1 if d == ".app.notion.com" else 0, "notion.com" in d)

    tokens.sort(key=tok_rank, reverse=True)
    tok = tokens[0]
    raw = tok["value"]
    decoded = unquote(raw)

    def first(name: str) -> str:
        vals = [c.get("value", "") for c in all_cookies if c["name"] == name and c.get("value")]
        return vals[0] if vals else ""

    user_id = first("notion_user_id")
    browser_id = first("notion_browser_id")

    def build_header(token: str) -> str:
        parts = [f"token_v2={token}"]
        if user_id:
            parts.append(f"notion_user_id={user_id}")
        if browser_id:
            parts.append(f"notion_browser_id={browser_id}")
        return "; ".join(parts)

    cred = {
        "token_v2": raw,
        "token_v2_decoded": decoded,
        "token_was_url_encoded": raw != decoded,
        "notion_user_id": user_id,
        "notion_browser_id": browser_id,
        "token_domain": tok.get("domain", ""),
        "httpOnly": tok.get("httpOnly", False),
        "source": source,
        "extracted_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "cookie_header": build_header(raw),
        "all_cookie_names": sorted({c["name"] for c in all_cookies}),
    }
    path = SECRET_DIR / "credential.json"
    path.write_text(json.dumps(cred, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass

    print("\nRESULT: OK")
    print(f"  source: {source}")
    print(f"  token_v2: domain={cred['token_domain']!r} len={len(raw)} url_encoded={cred['token_was_url_encoded']} httpOnly={cred['httpOnly']}")
    print(f"  notion_user_id: {'present' if user_id else 'MISSING'}")
    print(f"  notion_browser_id: {'present' if browser_id else 'MISSING'}")
    if verbose:
        print(f"  notion cookie names: {sorted({c['name'] for c in all_cookies})}")
    print(f"  written: {path} (never printed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
