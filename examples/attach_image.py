#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""attach_image.py — 给本地 Notion AI 代理发「图片 / 文件附件」的完整示例。

只依赖标准库（不需要 pip install），演示三种写法：
  1. OpenAI 协议：image_url（data: URL，也支持 http(s) URL）
  2. OpenAI 协议：file（文本文件 / 代码文件）
  3. Anthropic 协议：image block（Claude Code 走的就是这套）

使用前：先启动代理（start-proxy.cmd 或 notion-proxy.exe），然后：
    python examples\\attach_image.py
    python examples\\attach_image.py --image C:\\path\\to\\photo.jpg
    python examples\\attach_image.py --thread <Notion 会话 id>   # 复用已有会话

注意：
- 单文件上限由 config.json 的 attachmentMaxBytes 控制（默认 14MB）；
- 附件只取「最后一条 user 消息」里的；
- 示例自带一张 1×1 的红色 PNG，没有图片也能直接跑通。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = os.environ.get("NOTION_PROXY_URL", "http://127.0.0.1:8787").rstrip("/")
API_KEY = os.environ.get("NOTION_PROXY_KEY", "")  # 代理没设 apiKey 时留空即可
MODEL = os.environ.get("NOTION_MODEL", "opus-5.5")

# 1×1 红色 PNG（70 字节），用于无图时演示
RED_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

TXT_SAMPLE = "项目代号是 BLUE-FALCON，负责人是 Alice。\n"


def post_json(path: str, payload: dict, timeout: float = 600.0) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE_URL + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if API_KEY:
        req.add_header("Authorization", "Bearer " + API_KEY)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code}: {body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"连不上代理（{BASE_URL}）：{exc.reason}\n"
            "请先启动代理：双击 start-proxy.cmd，或在项目目录运行 node notion-proxy.js"
        ) from exc


def read_image(path: str | None) -> tuple[str, str]:
    """返回 (base64, mime_type)。没传路径时用内置红色小图。"""
    if not path:
        return RED_PNG_B64, "image/png"
    with open(path, "rb") as fh:
        raw = fh.read()
    ext = os.path.splitext(path)[1].lower()
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(ext, "image/png")
    return base64.b64encode(raw).decode(), mime


def demo_openai_image(image_b64: str, mime: str, thread_id: str | None) -> None:
    print("\n=== 1. OpenAI 协议：image_url（data: URL）===")
    payload = {
        "model": MODEL,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "这张图片是什么颜色？一句话回答。"},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
                ],
            }
        ],
    }
    if thread_id:
        payload["thread_id"] = thread_id
    result = post_json("/v1/chat/completions", payload)
    print(result["choices"][0]["message"]["content"])


def demo_openai_file(thread_id: str | None) -> None:
    print("\n=== 2. OpenAI 协议：file（文本文件）===")
    content = base64.b64encode(TXT_SAMPLE.encode("utf-8")).decode()
    payload = {
        "model": MODEL,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "附件里提到的项目代号是什么？只回答代号。"},
                    {
                        "type": "file",
                        "file": {
                            "filename": "note.txt",
                            "file_data": content,
                            "mime_type": "text/plain",
                        },
                    },
                ],
            }
        ],
    }
    if thread_id:
        payload["thread_id"] = thread_id
    result = post_json("/v1/chat/completions", payload)
    print(result["choices"][0]["message"]["content"])


def demo_anthropic_image(image_b64: str, mime: str, thread_id: str | None) -> None:
    print("\n=== 3. Anthropic 协议：image block（Claude Code 同款）===")
    payload = {
        "model": MODEL,
        "max_tokens": 512,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "用一句话描述这张图片。"},
                    {"type": "image", "source": {"type": "base64", "media_type": mime, "data": image_b64}},
                ],
            }
        ],
    }
    if thread_id:
        payload["thread_id"] = thread_id
    result = post_json("/v1/messages", payload)
    print(result["content"][0]["text"])


def main() -> int:
    parser = argparse.ArgumentParser(description="给本地 Notion AI 代理发图片/文件附件")
    parser.add_argument("--image", help="本地图片路径（默认用内置 1×1 红色 PNG）")
    parser.add_argument("--thread", help="复用已有 Notion 会话 id（可选）")
    parser.add_argument(
        "--only",
        choices=["image", "file", "anthropic"],
        help="只跑其中一项（默认三项都跑）",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("Notion AI 本地代理 — 附件示例")
    print("代理地址：%s" % BASE_URL)
    print("模型：    %s" % MODEL)
    print("=" * 60)

    image_b64, mime = read_image(args.image)
    try:
        if args.only in (None, "image"):
            demo_openai_image(image_b64, mime, args.thread)
        if args.only in (None, "file"):
            demo_openai_file(args.thread)
        if args.only in (None, "anthropic"):
            demo_anthropic_image(image_b64, mime, args.thread)
    except KeyboardInterrupt:
        print("\n[已取消]")
        return 130
    except Exception as exc:  # noqa: BLE001 统一转成中文提示
        print("\n[失败] %s" % exc)
        print("请检查：1) 代理已启动；2) 登录态未过期（update-cookie.cmd）；3) 附件不要超过 14MB。")
        return 1

    print("\n全部示例执行完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())