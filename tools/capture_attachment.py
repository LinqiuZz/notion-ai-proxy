#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""capture_attachment.py —— 抓一次「带附件的 AI 对话请求」，用来逆向附件在 transcript 里的格式。

背景
----
Notion 网页 AI 支持在对话里附带图片/文件，代理目前只实现了纯文本。上传接口已经找到了
（getUploadFileUrlForAssistantChatTranscriptUpload），但"附件在 runInferenceTranscript 请求体里
怎么引用"还没抓到。这个脚本用 CDP 监听浏览器网络请求，把相关请求体保存下来。

连接方式
--------
Edge/Chrome 有时会关掉 HTTP 的 /json 接口（返回 404），所以这里**直连浏览器 WebSocket**
（从 DevToolsActivePort 读端口和路径），用 Target.getTargets / attachToTarget 找到 Notion 标签页。

用法
----
1. 浏览器用调试端口启动（Edge 需完全关闭后重启）：

       "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe" --remote-debugging-port=9222

2. 打开 https://app.notion.com 的 AI 对话，先别发消息。

3. 运行：python tools\\capture_attachment.py

4. 在浏览器里：附一张小图片（或 txt）→ 发一条消息（例如"这张图里有什么？"）。

5. 等模型开始回复，回到脚本按 Ctrl+C 停止；结果写到：
       .tmp/capture/attachment-capture-<时间戳>.json

注意：抓到的请求体可能含你的对话文字，只在本机 .tmp 目录（已 gitignore），不要外传。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    import websocket  # websocket-client
except ImportError:
    print("需要 websocket-client：pip install websocket-client")
    sys.exit(1)

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / ".tmp" / "capture"
BROWSER_DIRS = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "User Data",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data",
]
CAPTURE_HINTS = (
    "runInferenceTranscript",
    "getUploadFileUrl",
    "submitTransaction",
    "saveTransactions",
    "syncRecordValues",
    "loadPageChunk",
)
API_HINT = "/api/v3/"


def find_debug_port() -> tuple[int, str] | None:
    for ud in BROWSER_DIRS:
        f = ud / "DevToolsActivePort"
        if not f.is_file():
            continue
        try:
            lines = f.read_text(encoding="utf-8").strip().splitlines()
            port = int(lines[0])
            ws_path = lines[1].strip() if len(lines) > 1 else ""
            if port > 0 and ws_path.startswith("/devtools/"):
                return port, ws_path
        except (ValueError, IndexError, OSError):
            continue
    return None


class BrowserCdp:
    """直连浏览器级 WebSocket（Target 域），再 attach 到具体页面。"""

    def __init__(self, ws_url: str) -> None:
        self.ws = websocket.create_connection(ws_url, timeout=10, suppress_origin=True)
        self.next_id = 0
        self.pending: dict[int, dict] = {}
        self.session_id: str | None = None

    def send(self, method: str, params: dict | None = None, session_id: str | None = None) -> int:
        self.next_id += 1
        msg = {"id": self.next_id, "method": method}
        if params:
            msg["params"] = params
        if session_id:
            msg["sessionId"] = session_id
        self.ws.send(json.dumps(msg))
        self.pending[self.next_id] = {"method": method, "params": params or {}}
        return self.next_id

    def wait_result(self, msg_id: int, timeout: float = 10.0) -> dict:
        deadline = time.time() + timeout
        self.ws.settimeout(1.0)
        while time.time() < deadline:
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not raw:
                continue
            msg = json.loads(raw)
            if msg.get("id") == msg_id:
                return msg.get("result") or {}
        return {}

    def pick_notion_target(self) -> dict | None:
        msg_id = self.send("Target.getTargets")
        result = self.wait_result(msg_id)
        pages = [t for t in result.get("targetInfos", []) if t.get("type") == "page"]
        notion = [t for t in pages if "notion" in (t.get("url") or "").lower()]
        if notion:
            # 优先 /chat 页面（AI 对话）
            notion.sort(key=lambda t: 0 if "/chat" in (t.get("url") or "") else 1)
            return notion[0]
        return pages[0] if pages else None

    def attach(self, target_id: str) -> str | None:
        msg_id = self.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        return (self.wait_result(msg_id).get("sessionId")) or None


def main() -> int:
    found = find_debug_port()
    if not found:
        print("没找到 DevToolsActivePort。请完全关掉 Edge/Chrome 后用调试端口启动：")
        print('  msedge.exe --remote-debugging-port=9222')
        return 1
    port, ws_path = found
    browser_url = f"ws://127.0.0.1:{port}{ws_path}"
    print(f"浏览器调试端口: {port}")
    print(f"连接: {browser_url}")

    try:
        cdp = BrowserCdp(browser_url)
    except Exception as exc:
        print(f"连不上浏览器 WebSocket：{exc}")
        return 1

    target = cdp.pick_notion_target()
    if not target:
        print("没找到可用的页面标签页，请先打开 https://app.notion.com 的 AI 对话。")
        return 1
    print(f"目标标签页: {target.get('url')}")
    session_id = cdp.attach(target["targetId"])
    if not session_id:
        print("attachToTarget 失败（无法接管该标签页）。")
        return 1
    print(f"已接管标签页 session={session_id[:12]}…")

    records: list[dict] = []
    by_request: dict[str, dict] = {}

    def on_message(msg: dict) -> None:
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "Network.requestWillBeSent":
            request = params.get("request") or {}
            url = request.get("url") or ""
            if API_HINT in url and any(hint in url for hint in CAPTURE_HINTS):
                record = {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "method": request.get("method"),
                    "url": url,
                    "requestId": params.get("requestId"),
                    "postData": None,
                    "responseStatus": None,
                    "responseBody": None,
                }
                records.append(record)
                by_request[params["requestId"]] = record
                cdp.send("Network.getRequestPostData", {"requestId": params["requestId"]}, session_id)
                print(f"[请求] {record['method']} {url.split('?')[0]}")
        elif method == "Network.responseReceived":
            record = by_request.get(params.get("requestId"))
            if record:
                response = params.get("response") or {}
                record["responseStatus"] = response.get("status")
                record["mimeType"] = response.get("mimeType")
                print(f"[响应] {record['responseStatus']} {record['url'].split('?')[0]}")
                if "UploadFileUrl" in record["url"]:
                    cdp.send("Network.getResponseBody", {"requestId": params.get("requestId")}, session_id)
        elif "id" in msg and msg["id"] in cdp.pending:
            info = cdp.pending.pop(msg["id"])
            result = msg.get("result") or {}
            request_id = info["params"].get("requestId")
            if info["method"] == "Network.getRequestPostData":
                for record in records:
                    if record.get("requestId") == request_id:
                        record["postData"] = result.get("postData")
            elif info["method"] == "Network.getResponseBody":
                for record in records:
                    if record.get("requestId") == request_id:
                        record["responseBody"] = result.get("body")

    cdp.send("Network.enable", {}, session_id)
    print()
    print("现在请去浏览器里：附一张小图片（或 txt）→ 发一条消息 → 等模型开始回复。")
    print("完成后回到这里按 Ctrl+C 停止抓包。")
    print("-" * 70)

    cdp.ws.settimeout(1.0)
    try:
        while True:
            try:
                raw = cdp.ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not raw:
                continue
            try:
                on_message(json.loads(raw))
            except json.JSONDecodeError:
                continue
    except KeyboardInterrupt:
        pass
    finally:
        try:
            cdp.ws.close()
        except Exception:
            pass

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"attachment-capture-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(
        json.dumps(
            {
                "capturedAt": datetime.now().isoformat(timespec="seconds"),
                "target": target.get("url"),
                "records": records,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    with_body = [r for r in records if r.get("postData")]
    print()
    print(f"共抓 {len(records)} 个相关请求，其中 {len(with_body)} 个有请求体")
    print(f"已保存：{out}")
    for record in records:
        print(f"  {record.get('responseStatus')} {record.get('method')} {record['url'].split('?')[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())