#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""openai_python.py — 用官方 openai SDK 调用本地 Notion AI 代理的完整小例子。

使用前：
  1. 双击项目根目录的 start-proxy.cmd 启动代理，等窗口里出现“就绪”；
  2. 安装官方 SDK（只需一次）：
         pip install openai
  3. 运行本脚本：
         python examples\\openai_python.py

可以用环境变量自定义（cmd 里先 set 再运行）：
  OPENAI_BASE_URL  默认 http://127.0.0.1:8787/v1
  OPENAI_API_KEY   默认 sk-local（本地代理不校验，随便填一个非空值）
  NOTION_MODEL     默认 claude-sonnet4.6（可用 /v1/models 查看全部模型）
"""

from __future__ import annotations

import os
from typing import NoReturn

BASE_URL = os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:8787/v1")
API_KEY = os.environ.get("OPENAI_API_KEY", "sk-local")
MODEL = os.environ.get("NOTION_MODEL", "claude-sonnet4.6")


def fail(message: str, code: int = 1) -> NoReturn:
    print(message)
    raise SystemExit(code)


try:
    from openai import OpenAI
except ImportError:
    fail(
        "未安装 openai SDK，无法运行本示例。\n"
        "请在命令行执行（只需一次）：\n"
        "    pip install openai\n"
        "安装完成后再运行：python examples\\openai_python.py"
    )


def demo_list_models(client: "OpenAI") -> None:
    """先看看代理到底提供了哪些模型。"""
    print("\n=== 0. 查看可用模型（GET /v1/models）===")
    try:
        models = client.models.list()
        ids = [model.id for model in models.data]
        print("代理返回 %d 个模型，前 10 个：" % len(ids))
        print("  " + ", ".join(ids[:10]) + (" ..." if len(ids) > 10 else ""))
        print("提示：把上面任意一个 id 填到客户端「模型」里即可使用。")
    except Exception as exc:  # noqa: BLE001 模型列表失败不影响后面的对话演示
        print("获取模型列表失败（不影响后续对话）：%s: %s" % (type(exc).__name__, exc))


def demo_non_stream(client: "OpenAI") -> None:
    """非流式调用：一次性拿到完整回答。"""
    print("\n=== 1. 非流式调用（stream=False）===")
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "user",
                "content": "用三句话介绍一下你自己，并说明你正在通过本地代理工作。",
            }
        ],
        stream=False,
    )
    answer = response.choices[0].message.content or ""
    print(answer)

    usage = getattr(response, "usage", None)
    if usage is not None:
        print(
            "\n[token 用量] 输入 %s，输出 %s，合计 %s"
            % (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens)
        )


def demo_stream(client: "OpenAI") -> None:
    """流式调用：边生成边打印，适合聊天客户端的效果。"""
    print("\n=== 2. 流式调用（stream=True）===")
    stream = client.chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "user",
                "content": "用一句简短的中文欢迎语，欢迎用户使用这个本地 AI 代理，并提到三个可用模型的名字。",
            }
        ],
        stream=True,
    )
    pieces = []
    for chunk in stream:
        choices = getattr(chunk, "choices", None)
        if not choices:
            continue
        delta = getattr(choices[0], "delta", None)
        piece = getattr(delta, "content", None) if delta is not None else None
        if piece:
            pieces.append(piece)
            print(piece, end="", flush=True)
    print()
    if not pieces:
        print("（流式响应为空，可能是上游返回异常，请查看代理窗口的日志）")


def main() -> int:
    print("=" * 60)
    print("Notion AI 本地代理 — openai SDK 示例")
    print("代理地址：%s" % BASE_URL)
    print("模型：    %s" % MODEL)
    print("=" * 60)

    client = OpenAI(base_url=BASE_URL, api_key=API_KEY, timeout=600.0)
    try:
        demo_list_models(client)
        demo_non_stream(client)
        demo_stream(client)
    except KeyboardInterrupt:
        print("\n[已取消]")
        return 130
    except Exception as exc:  # noqa: BLE001 这里把所有常见 SDK 异常统一转成中文提示
        print("\n[失败] %s: %s" % (type(exc).__name__, exc))
        print("请依次检查：")
        print("  1) 代理是否已经启动（双击 start-proxy.cmd，看到“就绪”字样）")
        print("  2) 地址是否是 http://127.0.0.1:8787/v1")
        print("  3) 登录态是否过期：双击 update-cookie.cmd 刷新后重试")
        print("  4) 模型 id 是否存在（运行本脚本看第 0 步的模型列表）")
        return 1

    print("\n全部示例执行完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
