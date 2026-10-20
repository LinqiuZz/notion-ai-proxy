#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""capture_attachment.py —— 抓一次「带附件的 AI 对话请求」，用来逆向附件在 transcript 里的格式。

背景
----
Notion 网页 AI 支持在对话里附带图片/文件，代理目前只实现了纯文本。上传接口已经找到了
（getUploadFileUrlForAssistantChatTranscriptUpload），但"附件在 runInferenceTranscript 请求体里
怎么引用"还没抓到。这个脚本用 CDP 监听浏览器网络请求，把相关请求体保存下来。

用法
----
1. 完全关闭 Edge/Chrome，然后用调试端口启动并登录 Notion：

       "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe" --remote-debugging-port=9222

2. 打开 https://app.notion.com 的 AI 对话，**先不要发消息**。

3. 运行本脚本：

       python tools\\capture_attachment.py

4. 在浏览器里：附件随便选一张小图片（或一个 txt），发一条消息（比如"这张图里有什么？"）。

5. 等模型开始回复后，回到脚本按 Ctrl+C 停止；它会保存到：
       .tmp/capture/attachment-capture-<时间戳>.json

脚本只读本地调试端口，不会修改任何东西；抓到的内容可能含你的对话文字，注意不要外传。
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
    "getUploadFileUrlForAssistantChatTranscriptUpload",
    "submitTransaction",
    "saveTransactions",
    "syncRecordValues",
)
API_HINT = "/api/v3/"


def find_debug_port() -> int | None:
    for ud in BROWSER_DIRS:
        f = ud / "DevToolsActivePort"
        if not f.is_file():
            continue
        try:
            lines = f.read_text(encoding="utf-8").strip().splitlines()
            port = int(lines[0])
            if port > 0:
                return port
        except (ValueError, IndexError, OSError):
            continue
    return None


def http_json(url: str):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def pick_target(port: int) -> dict | None:
    try:
        targets = http_json(f"http://127.0.0.1:{port}/json")
    except Exception as exc:
        print(f"读取 http://127.0.0.1:{port}/json 失败：{exc}")
        return None
    pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    notion = [t for t in pages if "notion" in (t.get("url") or "").lower()]
    if notion:
        return notion[0]
    if pages:
        print("没找到 Notion 标签页，使用第一个标签页：", pages[0].get("url"))
        return pages[0]
    return None


class CdpCapture:
    def __init__(self, ws_url: str) -> None:
        self.ws = websocket.create_connection(ws_url, timeout=5, suppress_origin=True)
        self.next_id = 0
        self.pending: dict[int, dict] = {}
        self.records: list[dict] = []
        self.by_request: dict[str, dict] = {}

    def send(self, method: str, params: dict | None = None) -> int:
        self.next_id += 1
        msg = {"id": self.next_id, "method": method}
        if params:
            msg["params"] = params
        self.ws.send(json.dumps(msg))
        self.pending[self.next_id] = {"method": method, "params": params or {}}
        return self.next_id

    def handle(self, msg: dict) -> None:
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "Network.requestWillBeSent":
            request = params.get("request") or {}
            url = request.get("url") or ""
            if API_HINT in url and any(h in url for h in CAPTURE_HINTS):
                record = {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "method": request.get("method"),
                    "url": url,
                    "requestId": params.get("requestId"),
                    "postData": None,
                    "responseStatus": None,
                    "responseBody": None,
                }
                self.records.append(record)
                self.by_request[params["requestId"]] = record
                self.send("Network.getRequestPostData", {"requestId": params["requestId"]})
                print(f"[请求] {record['method']} {url.split('?')[0]}")
        elif method == "Network.responseReceived":
            request_id = params.get("requestId")
            record = self.by_request.get(request_id)
            if record:
                response = params.get("response") or {}
                record["responseStatus"] = response.get("status")
                record["mimeType"] = response.get("mimeType")
                print(f"[响应] {record['responseStatus']} {record['url'].split('?')[0]}")
                if "UploadFileUrl" in record["url"]:
                    self.send("Network.getResponseBody", {"requestId": request_id})
        elif "id" in msg and msg["id"] in self.pending:
            info = self.pending.pop(msg["id"])
            method = info["method"]
            result = msg.get("result") or {}
            if method == "Network.getRequestPostData":
                for record in self.records:
                    if record.get("requestId") == info["params"].get("requestId"):
                        record["postData"] = result.get("postData")
            elif method == "Network.getResponseBody":
                for record in self.records:
                    if record.get("requestId") == info["params"].get("requestId"):
                        record["responseBody"] = result.get("body")

    def run(self, seconds: float) -> None:
        self.send("Network.enable")
        deadline = time.time() + seconds
        self.ws.settimeout(1.0)
        while time.time() < deadline:
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except KeyboardInterrupt:
                break
            if not raw:
                continue
            try:
                self.handle(json.loads(raw))
            except json.JSONDecodeError:
                continue
        self.ws.close()


def main() -> int:
    port = find_debug_port()
    if not port:
        print("没找到 DevToolsActivePort。请先完全关掉 Edge/Chrome，再用调试端口启动：")
        print('  msedge.exe --remote-debugging-port=9222')
        return 1
    target = pick_target(port)
    if not target:
        print("没有可用的页面标签页。请先打开 https://app.notion.com 的 AI 对话。")
        return 1

    print(f"浏览器调试端口: {port}")
    print(f"目标标签页: {target.get('url')}")
    print()
    print("现在请去浏览器里：附一张小图片（或 txt）→ 发一条消息 → 等模型开始回复。")
    print("完成后回到这里按 Ctrl+C 停止抓包。")
    print("-" * 70)

    cap = CdpCapture(target["webSocketDebuggerUrl"])
    try:
        cap.run(600)
    except KeyboardInterrupt:
        pass

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"attachment-capture-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(
        json.dumps(
            {
                "capturedAt": datetime.now().isoformat(timespec="seconds"),
                "target": target.get("url"),
                "records": cap.records,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    interesting = [r for r in cap.records if r.get("postData")]
    print()
    print(f"共抓 {len(cap.records)} 个相关请求，其中 {len(interesting)} 个有请求体")
    print(f"已保存：{out}")
    for record in cap.records:
        print(f"  {record.get('responseStatus')} {record.get('method')} {record['url'].split('?')[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())