#!/usr/bin/env python3
"""Live probe of the Notion AI internal API using the extracted session cookie.

Uses curl.exe (Windows Schannel TLS) because Notion's edge rejects Node/OpenSSL
TLS fingerprints with HTTP-200 in-band "temporarily-unavailable" empty streams.
Flow: getSpaces -> getAvailableModels -> runInferenceTranscript("PONG").
Writes discovered workspace info to secret/workspace.json on success.

Usage: python notion_probe.py [--host https://app.notion.com] [--verbose]
"""
from __future__ import annotations

import collections
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRET = ROOT / "secret"
CRED_PATH = SECRET / "credential.json"

HOSTS = ["https://app.notion.com", "https://www.notion.so"]
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
CLIENT_VERSION = "23.13.20260720.1949"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def load_cred():
    if not CRED_PATH.exists():
        sys.exit(f"missing {CRED_PATH}; run cdp_cookie_extract.py first")
    return json.loads(CRED_PATH.read_text(encoding="utf-8"))


def cookie_header(cred, token: str) -> str:
    parts = [f"token_v2={token}"]
    if cred.get("notion_user_id"):
        parts.append(f"notion_user_id={cred['notion_user_id']}")
    if cred.get("notion_browser_id"):
        parts.append(f"notion_browser_id={cred['notion_browser_id']}")
    return "; ".join(parts)


def base_headers(cred: dict, host: str, token: str, space_id: str = "") -> dict:
    h = {
        "Content-Type": "application/json",
        "User-Agent": UA,
        "Origin": host,
        "Referer": f"{host}/chat",
        "notion-client-version": CLIENT_VERSION,
        "notion-audit-log-platform": "web",
        "sec-ch-ua": '"Chromium";v="146", "Not?A_Brand";v="24", "Google Chrome";v="146"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "Cookie": cookie_header(cred, token),
    }
    if cred.get("notion_user_id"):
        h["x-notion-active-user-header"] = cred["notion_user_id"]
    if space_id:
        h["x-notion-space-id"] = space_id
    return h


def curl_post(host: str, path: str, payload: dict | str, headers: dict, accept: str,
              max_time: int = 180):
    url = host + path
    body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    headers = dict(headers)
    headers["Accept"] = accept
    with tempfile.TemporaryDirectory(prefix="notion-probe-") as td:
        bodyf = Path(td) / "body.json"
        bodyf.write_bytes(body.encode("utf-8"))
        out = Path(td) / "out.bin"
        hdr = Path(td) / "out.hdr"
        args = [
            "curl.exe", "-sS", "--max-time", str(max_time), "-X", "POST", url,
            "--data-binary", "@" + str(bodyf), "-D", str(hdr), "-o", str(out), "-w", "%{http_code}",
        ]
        for k, v in headers.items():
            args += ["-H", f"{k}: {v}"]
        try:
            p = subprocess.run(args, capture_output=True, timeout=max_time + 20)
        except subprocess.TimeoutExpired:
            return "TIMEOUT", "", b""
        status = p.stdout.decode("utf-8", "replace").strip()
        text = out.read_bytes() if out.exists() else b""
        hdrs = hdr.read_text(encoding="utf-8", errors="replace") if hdr.exists() else ""
        if p.stderr:
            print("    curl stderr:", p.stderr.decode("utf-8", "replace").strip()[:300])
        return status, hdrs, text


def find_space(cred, host: str, token: str):
    """Call getSpaces and return (space_id, user_id, space_view_id, raw)."""
    for body in ("{}", ""):
        status, _, raw = curl_post(host, "/api/v3/getSpaces", body, base_headers(cred, host, token), "application/json")
        if status != "200":
            print(f"  getSpaces -> HTTP {status} ({len(raw)} bytes)")
            if status in ("401", "403"):
                snippet = raw[:200].decode("utf-8", "replace")
                print("    body:", snippet)
            continue
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            print("  getSpaces: unparseable JSON")
            continue
        space_id = user_id = space_view_id = ""

        def nested_value(record):
            """getSpaces nests records as {id: {value: {value: {...}}}}."""
            if not isinstance(record, dict):
                return {}
            outer = record.get("value") or {}
            return (outer.get("value") or {}) if isinstance(outer, dict) else {}

        for _k, v in data.items():
            if not isinstance(v, dict):
                continue
            for sk, sv in (v.get("space") or {}).items():
                if not isinstance(sv, dict):
                    continue
                val = nested_value(sv)
                space_id = val.get("id") or (sk if UUID_RE.match(sk) else space_id)
                if space_id:
                    break
            for vk, vv in (v.get("space_view") or {}).items():
                if not isinstance(vv, dict):
                    continue
                val = nested_value(vv)
                space_view_id = val.get("id") or (vk if UUID_RE.match(vk) else space_view_id)
                if space_view_id:
                    break
            for _uk, uv in (v.get("notion_user") or {}).items():
                val = nested_value(uv)
                user_id = val.get("id") or user_id
        if not space_id:
            for k, v in data.items():
                if UUID_RE.match(k) and isinstance(v, dict) and "space" in v:
                    space_id = k
                    break
        return space_id, user_id or cred.get("notion_user_id", ""), space_view_id, data
    return "", cred.get("notion_user_id", ""), "", None


def list_models(cred, host: str, token: str, space_id: str):
    headers = base_headers(cred, host, token, space_id)
    status, _, raw = curl_post(
        host, "/api/v3/getAvailableModels", {"spaceId": space_id}, headers, "application/json"
    )
    if status != "200":
        print(f"  getAvailableModels -> HTTP {status} ({len(raw)} bytes)")
        if raw:
            print("    body:", raw[:300].decode("utf-8", "replace"))
        return []
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        print("  getAvailableModels: unparseable JSON")
        return []
    models = data.get("models") or data.get("availableModels") or []
    rows = []
    for m in models:
        if not isinstance(m, dict):
            continue
        rows.append(
            {
                "model": m.get("model"),
                "display": m.get("modelMessage") or m.get("modelName") or m.get("name"),
                "family": m.get("modelFamily") or m.get("family"),
                "disabled": bool(m.get("isDisabled")),
                "efforts": (m.get("modelConfiguration") or {}).get("supportedReasoningEfforts") or [],
            }
        )
    print(f"  getAvailableModels -> {len(rows)} models")
    for r in rows[:30]:
        flag = " (disabled)" if r["disabled"] else ""
        print(f"    - {r['model']:<32} {r['display'] or '':<28} family={r['family']} efforts={r['efforts']}{flag}")
    return rows


def strip_tags(s: str) -> str:
    s = re.sub(r"<lang\b[^>]*>(.*?)</lang>", r"\1", s, flags=re.S)
    s = re.sub(r"</?lang\b[^>]*>", "", s)
    s = re.sub(r'<lang\b[^>]*/>', "", s)
    return s


def extract_record_map_final(rec: dict) -> str:
    rm = rec.get("recordMap") or {}
    tm = rm.get("thread_message") or {}
    best = []
    for _mid, m in tm.items():
        try:
            step = m["value"]["value"]["step"]
        except Exception:  # noqa: BLE001
            continue
        typ = step.get("type") or ""
        items = step.get("value") or []
        txt = "".join(i.get("content", "") for i in items if isinstance(i, dict) and isinstance(i.get("content"), str))
        txt = strip_tags(txt)
        if txt:
            prio = 0 if typ in ("text", "markdown-chat") else 1
            best.append((prio, -len(txt), txt, typ))
    if best:
        best.sort()
        return best[0][2]
    return ""


def parse_stream(raw: bytes):
    text = raw.decode("utf-8", "replace")
    types = collections.Counter()
    errors = []
    record_map_final = ""
    legacy_last = ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        try:
            rec = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(rec, dict):
            continue
        t = rec.get("type")
        types[t] += 1
        val = rec.get("value")
        if t == "record-map":
            f = extract_record_map_final(rec)
            if f:
                record_map_final = f
        elif t in (None, "") and isinstance(val, list):
            legacy = "".join(
                seg[0] for seg in val if isinstance(seg, list) and seg and isinstance(seg[0], str)
            )
            if legacy:
                legacy_last = legacy
        elif t == "markdown-chat" and isinstance(val, str):
            legacy_last = strip_tags(val) or legacy_last
        elif t == "error":
            errors.append((rec.get("subType"), rec.get("message")))
        elif t == "patch-start":
            for item in (rec.get("data") or {}).get("s") or []:
                if isinstance(item, dict) and item.get("type") == "error":
                    errors.append((item.get("subType"), item.get("message")))
    return {
        "types": dict(types),
        "errors": errors,
        "content": record_map_final or legacy_last,
        "legacy_text": legacy_last,
        "record_map_text": record_map_final,
    }


def build_inference_payload(space_id: str, user_id: str, model_code: str, prompt: str,
                            thread_type: str = "workflow", use_web_search: bool = False):
    now = datetime.now().astimezone().isoformat()
    return {
        "traceId": str(uuid.uuid4()),
        "spaceId": space_id,
        "threadId": str(uuid.uuid4()),
        "threadType": thread_type,
        "createThread": True,
        "generateTitle": False,
        "saveAllThreadOperations": True,
        "setUnreadState": True,
        "isPartialTranscript": False,
        "asPatchResponse": True,
        "isUserInAnySalesAssistedSpace": False,
        "isSpaceSalesAssisted": False,
        "threadParentPointer": {"table": "space", "id": space_id, "spaceId": space_id},
        "transcript": [
            {
                "id": str(uuid.uuid4()),
                "type": "config",
                "value": {"type": thread_type, "model": model_code, "modelFromUser": True, "useWebSearch": use_web_search},
            },
            {
                "id": str(uuid.uuid4()),
                "type": "context",
                "value": {"timezone": "Asia/Shanghai", "currentDatetime": now, "userId": user_id, "spaceId": space_id},
            },
            {
                "id": str(uuid.uuid4()),
                "type": "user",
                "value": [[prompt]],
                "userId": user_id,
                "createdAt": now,
            },
        ],
        "debugOverrides": {
            "emitAgentSearchExtractedResults": True,
            "cachedInferences": {},
            "annotationInferences": {},
            "emitInferences": False,
        },
    }


def main() -> int:
    verbose = "--verbose" in sys.argv
    cred = load_cred()
    tokens = [cred["token_v2"]]
    if cred.get("token_v2_decoded") and cred["token_v2_decoded"] != cred["token_v2"]:
        tokens.append(cred["token_v2_decoded"])

    hosts = HOSTS
    if "--host" in sys.argv:
        hosts = [sys.argv[sys.argv.index("--host") + 1]]

    space_id = user_id = space_view_id = None
    chosen = None
    for host in hosts:
        for token in tokens:
            enc = token != cred["token_v2"]
            print(f"== getSpaces host={host} token={'decoded' if enc else 'raw'}")
            sid, uid, svid, data = find_space(cred, host, token)
            if sid:
                space_id, user_id, space_view_id = sid, uid, svid
                chosen = (host, token)
                print(f"  OK spaceId={sid[:8]}... userId={uid[:8]}...")
                break
        if chosen:
            break
    if not chosen or not space_id:
        print("\nRESULT: could not authenticate with Notion (check token/subscription).")
        return 2
    host, token = chosen

    print("\n== getAvailableModels")
    models = list_models(cred, host, token, space_id)

    preferred = "claude-sonnet4.6"
    static_fallback = {
        "claude-sonnet4.6": "almond-croissant-low",
        "gpt-5.6-sol": "orange-mousse",
        "gemini-3.1pro": "galette-medium-thinking",
    }
    coded = [m for m in models if not m["disabled"] and m["model"]]
    model_code = static_fallback.get(preferred)
    thread_type = "workflow"
    if coded:
        pick = next((m for m in coded if preferred in (str(m["display"] or "").lower().replace(" ", "-"))), coded[0])
        model_code = pick["model"]
        if pick["model"] == "vertex-gemini-3.5-flash":
            thread_type = "markdown-chat"
    print(f"\n== runInferenceTranscript (model={model_code}, threadType={thread_type})")
    payload = build_inference_payload(space_id, user_id, model_code, "Reply with exactly one word: PONG")
    headers = base_headers(cred, host, token, space_id)
    status, hdrs, raw = curl_post(host, "/api/v3/runInferenceTranscript", payload, headers, "application/x-ndjson")
    print(f"  HTTP {status}, {len(raw)} bytes")
    if status != "200":
        print("  body:", raw[:500].decode("utf-8", "replace"))
        if status in ("403", "429", "000"):
            print("RESULT: upstream rejected the request (TLS/Cloudflare/rate-limit or subscription).")
        return 3
    parsed = parse_stream(raw)
    print("  record types:", parsed["types"])
    if parsed["errors"]:
        print("  in-band errors:", parsed["errors"])
    print("  final content:", repr(parsed["content"][:200]))
    (SECRET / "probe.ndjson").write_bytes(raw)
    print(f"  raw stream saved to {SECRET / 'probe.ndjson'}")

    if not parsed["content"]:
        print("RESULT: 200 but no text extracted (endpoint drift or in-band error).")
        return 4

    workspace = {
        "host": host,
        "spaceId": space_id,
        "userId": user_id,
        "spaceViewId": space_view_id or "",
        "clientVersion": CLIENT_VERSION,
        "userAgent": UA,
        "defaultModel": model_code,
        "models": models,
        "probedAt": datetime.now().isoformat(timespec="seconds"),
    }
    wf = SECRET / "workspace.json"
    wf.write_text(json.dumps(workspace, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nRESULT: OK - workspace info written to {wf}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

