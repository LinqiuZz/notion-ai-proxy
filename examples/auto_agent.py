#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""auto_agent.py —— 让 Notion AI 自动改本地代码的简易 agent（基于 notion-proxy）。

背景
----
notion-proxy 只提供纯文本对话（上游 Notion 没有原生 tool use / function calling），
所以本脚本用「文本 JSON 工具协议」模拟工具调用：

    模型每一轮只输出一个 ```json {"tool": "...", ...}``` 代码块
    脚本解析后在本地执行（列目录 / 读文件 / 覆盖写 / 精确替换）
    把执行结果作为下一条 user 消息回传给模型
    如此循环，直到模型调用 {"tool": "finish", "summary": "..."}

安全设计
--------
* 默认 dry-run：只打印将要做的修改，不写盘；加 --apply 才真正修改文件。
* 每次写盘前自动备份到 <dir>/.autoedit-backup/<时间戳>/ 下，可随时还原。
* 只允许操作 --dir 目录内的文件；拒绝 .git / secret / node_modules 等目录。
* 命令执行默认关闭：只有显式加 --allow-exec 才允许模型跑测试/命令；带破坏性命令黑名单、
  超时（默认 120 秒）和输出截断（4000 字符），命令在 --dir 目录下执行。
* 自动测试：加 --test-cmd "python -m pytest -q" 后，脚本会在启动时先跑一次基线，
  之后**每次改完文件自动再跑一遍**，把结果回传给模型；这条命令由用户写死，模型改不了。
* 单文件读取上限 512KB、读取行数上限 400 行，避免上下文爆炸。
* 默认 thread_mode=single（复用同一个 Notion 会话），减少 Notion 里新建对话的数量。
  注意：复用会话时每次请求都会把整个会话上下文重读一遍，输入 token 大约每轮涨 3~4 万；
  代理在输入超过 30 万 token 时会自动换新会话。因此本脚本默认 --max-steps 8，
  并且每轮都重复任务描述，万一中途换了新会话模型也能接着做。

用法
----
    python auto_agent.py --task "把 greet.py 的返回值改成 hi" --dir ./src --apply
    python auto_agent.py --task "给这个目录的 Python 文件都加上类型注解" --dir . --max-steps 20
    python auto_agent.py --task "解释 main.py 的作用" --dir .
    python auto_agent.py --task "修到测试全绿" --dir . --apply --test-cmd "python -m pytest -q"
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

DEFAULT_BASE_URL = "http://127.0.0.1:8787/v1"
DEFAULT_MODEL = "opus-5.5"
DEFAULT_API_KEY = "sk-local"
MAX_FILE_BYTES = 512 * 1024
MAX_READ_LINES = 400
MAX_LIST_FILES = 300
MAX_DIFF_LINES = 120
EXEC_MAX_OUTPUT = 4000

# 命令执行的兜底黑名单（只是减少误操作，不是安全沙箱；请只对自己信任的目录开 --allow-exec）
DANGEROUS_COMMAND_RE = re.compile(
    r"(rm\s+-[a-z]*r[a-z]*f|rm\s+-[a-z]*f[a-z]*r|rmdir\s+/s|del\s+/[fsq]|"
    r"format\s+[a-z]:|shutdown|reboot|diskpart|mkfs|"
    r"reg\s+(delete|add)|"
    r"git\s+(push|reset\s+--hard|clean\s+-[a-z]*f)|"
    r"curl\s+[^|]*\|\s*(ba)?sh|wget\s+[^|]*\|\s*(ba)?sh|"
    r"invoke-expression|\biex\b|remove-item\s+-recurse|"
    r"start-process|stop-process|taskkill)",
    re.I,
)

IGNORE_DIRS = {
    ".git",
    ".autoedit-backup",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "secret",
    "ref",
    "out",
    ".tmp",
    "dist",
    "build",
}

def build_system_prompt(allow_exec: bool, test_cmd: str = "") -> str:
    tools = [
        '1. 列文件：{"tool": "list_files", "path": ".", "pattern": "*.py"}',
        '2. 读文件：{"tool": "read_file", "path": "a.py", "start": 1, "end": 200}',
        '3. 覆盖写：{"tool": "write_file", "path": "a.py", "content": "文件的完整新内容"}',
        '4. 精确替换：{"tool": "replace_in_file", "path": "a.py", "old": "原文片段", "new": "新片段"}',
    ]
    if allow_exec:
        tools.append(
            '5. 执行命令：{"tool": "exec", "command": "pytest -q"}   在 --dir 目录下执行，可用于跑测试/编译检查'
        )
    tools.append('%d. 完成：{"tool": "finish", "summary": "改了什么"}' % (len(tools) + 1))

    rules = [
        "- 先 list_files / read_file 了解真实内容，绝对不要凭空猜文件内容。",
        "- 小改动优先用 replace_in_file（old 必须是文件里逐字出现的唯一片段）；整文件重写才用 write_file。",
        "- 不要修改 secret/、.git/、node_modules/、.autoedit-backup/ 下的任何文件。",
        "- 不要删除文件，只做代码编辑。",
    ]
    if allow_exec:
        rules.append(
            "- 可以用 exec 跑测试/编译来验证修改（如 pytest、python -m py_compile）；不要执行破坏性命令（删除、格式化、git push 等）。"
        )
        rules.append("- exec 一次只跑一条命令，命令在 --dir 目录下执行，输出过长会被截断。")
    else:
        rules.append("- 你不能执行命令，只能读写文件。")
    if test_cmd:
        rules.append(
            "- 脚本已配置自动测试（%s）：每次你修改文件后会自动执行它，并把结果发给你。"
            "测试失败就继续修，测试通过后尽快调用 finish。" % test_cmd
        )
    rules.extend(
        [
            "- 如果信息不足，继续 read_file 或 list_files；任务完成后立刻调用 finish。",
            "- 输出必须是合法 JSON（字符串里的换行写成 \\n），代码块语言标记写 json。",
        ]
    )
    return (
        "你是一个代码修改助手，运行在一个本地 Python 脚本里。\n\n"
        "你没有原生的工具调用能力，必须通过下面的 JSON 协议操作文件。\n"
        "每次回复**只输出一个 ```json 代码块**，不要输出解释、不要输出多个代码块。\n\n"
        "可用工具：\n" + "\n".join(tools) + "\n\n规则：\n" + "\n".join(rules)
    )

TOOL_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


# ───────────────────────────── 输出与参数 ─────────────────────────────


def setup_stdio() -> None:
    """Windows 控制台默认是 GBK，强制 UTF-8，避免中文日志报 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def say(message: str) -> None:
    print(message, flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="auto_agent.py",
        description="让 Notion AI 自动修改本地代码文件（文本 JSON 工具协议 agent）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python auto_agent.py --task \"把 greet.py 的返回值改成 hi\" --dir ./src --apply\n"
            "  python auto_agent.py --task \"给所有 py 文件加类型注解\" --dir . --max-steps 20\n"
            "  python auto_agent.py --task \"修复失败的测试并跑 pytest 验证\" --dir . --apply --allow-exec\n"
            "  python auto_agent.py --task \"修到测试全绿\" --dir . --apply --test-cmd \"python -m pytest -q\"\n"
            "\n注意: 默认只预览不写盘，确认无误后加 --apply。\n"
        ),
    )
    parser.add_argument("--task", required=True, help="要模型完成的任务，越具体越好")
    parser.add_argument("--dir", default=".", help="工作目录（只允许修改这个目录内的文件），默认当前目录")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="模型 id，默认 %s" % DEFAULT_MODEL)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="代理地址，默认 %s" % DEFAULT_BASE_URL)
    parser.add_argument("--api-key", default=DEFAULT_API_KEY, help="本地代理密钥，任意非空即可")
    parser.add_argument("--max-steps", type=int, default=8, help="最多多少轮工具调用，默认 8")
    parser.add_argument("--timeout", type=float, default=600.0, help="单次请求超时秒数，默认 600")
    parser.add_argument(
        "--thread-mode",
        choices=("single", "new"),
        default="single",
        help="Notion 会话模式：single=复用同一会话（默认，省对话数）；new=每步新建会话",
    )
    parser.add_argument("--apply", action="store_true", help="真正写盘；不加则只预览修改（dry-run）")
    parser.add_argument(
        "--allow-exec",
        action="store_true",
        help="允许模型在 --dir 目录下执行命令（跑测试/编译检查）。默认关闭，有破坏性命令黑名单和超时限制",
    )
    parser.add_argument("--exec-timeout", type=float, default=120.0, help="单条命令超时秒数，默认 120")
    parser.add_argument(
        "--test-cmd",
        default="",
        help=(
            "自动测试命令（固定字符串，如 \"python -m pytest -q\"）：启动时先跑一次记录基线，"
            "之后每次改完文件自动再跑，并把结果回传给模型。模型不能修改它"
        ),
    )
    parser.add_argument(
        "--test-timeout", type=float, default=300.0, help="自动测试命令的超时秒数，默认 300"
    )
    return parser


# ───────────────────────────── 代理调用 ─────────────────────────────


def build_opener() -> urllib.request.OpenerDirector:
    # 显式禁用系统代理，确保 localhost 直连
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def chat(
    opener: urllib.request.OpenerDirector,
    url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_text: str,
    thread_mode: str,
    timeout: float,
) -> tuple[str, dict]:
    """调本地代理的 /v1/chat/completions，返回 (正文, usage)。"""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "stream": False,
        "thread_mode": thread_mode,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
        method="POST",
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:600]
        except Exception:
            pass
        raise SystemExit("代理返回 HTTP %s：%s" % (exc.code, detail)) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise SystemExit(
            "连不上本地代理（%s）。确认 notion-proxy 正在 8787 端口运行。" % reason
        ) from exc

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise SystemExit("代理返回格式异常：%s" % json.dumps(data, ensure_ascii=False)[:600])
    usage = data.get("usage") or {}
    return content, usage


# ───────────────────────────── 工具执行 ─────────────────────────────


class Toolbox:
    def __init__(
        self,
        root: Path,
        apply: bool,
        allow_exec: bool = False,
        exec_timeout: float = 120.0,
        test_cmd: str = "",
        test_timeout: float = 300.0,
    ) -> None:
        self.root = root
        self.apply = apply
        self.allow_exec = allow_exec
        self.exec_timeout = max(5.0, float(exec_timeout))
        self.test_cmd = (test_cmd or "").strip()
        self.test_timeout = max(5.0, float(test_timeout))
        self.mutated = False  # 自上次自动测试后是否有文件写入成功
        self.stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.changed: list[str] = []
        self.backup_dir = root / ".autoedit-backup" / self.stamp

    # ---- 路径与备份 ----

    def resolve(self, raw_path: str) -> Path:
        if not raw_path or not isinstance(raw_path, str):
            raise ToolError("缺少 path 参数")
        candidate = (self.root / raw_path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            raise ToolError("路径越界：只允许操作 %s 内的文件" % self.root)
        parts = set(part.lower() for part in candidate.relative_to(self.root).parts)
        blocked = parts & {d.lower() for d in IGNORE_DIRS}
        if blocked:
            raise ToolError("这个目录不允许修改：%s" % ", ".join(sorted(blocked)))
        return candidate

    def backup(self, path: Path) -> None:
        rel = path.relative_to(self.root)
        target = self.backup_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(path, target)
        except OSError as exc:
            raise ToolError("备份失败：%s" % exc)

    # ---- 工具 ----

    def list_files(self, args: dict) -> str:
        raw = args.get("path", ".")
        base = self.resolve(raw)
        if not base.is_dir():
            raise ToolError("%s 不是目录" % raw)
        pattern = args.get("pattern") or "*"
        found: list[str] = []
        for path in sorted(base.rglob(pattern)):
            if len(found) >= MAX_LIST_FILES:
                found.append("...（文件太多，只列前 %d 个）" % MAX_LIST_FILES)
                break
            if not path.is_file():
                continue
            rel = path.relative_to(self.root)
            if set(part.lower() for part in rel.parts) & {d.lower() for d in IGNORE_DIRS}:
                continue
            size = path.stat().st_size
            found.append("%s  (%d 字节)" % (rel.as_posix(), size))
        if not found:
            return "（没有匹配的文件）"
        return "\n".join(found)

    def read_file(self, args: dict) -> str:
        path = self.resolve(args.get("path", ""))
        if not path.is_file():
            raise ToolError("%s 不存在或不是文件" % args.get("path"))
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ToolError("文件太大（%d 字节），超过 %d 上限" % (size, MAX_FILE_BYTES))
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        start = max(1, int(args.get("start") or 1))
        end = int(args.get("end") or (start + MAX_READ_LINES - 1))
        end = min(end, len(lines), start + MAX_READ_LINES - 1)
        chunk = lines[start - 1 : end]
        numbered = "\n".join("%4d| %s" % (start + i, line) for i, line in enumerate(chunk))
        header = "%s（共 %d 行，显示 %d-%d）\n" % (args.get("path"), len(lines), start, end)
        if end < len(lines):
            numbered += "\n...（还有 %d 行，可继续 read_file）" % (len(lines) - end)
        return header + numbered

    def write_file(self, args: dict) -> str:
        path = self.resolve(args.get("path", ""))
        content = args.get("content")
        if not isinstance(content, str):
            raise ToolError("write_file 需要字符串 content")
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise ToolError("要写入的内容太大（>%d 字节）" % MAX_FILE_BYTES)
        old = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        diff = make_diff(old, content, path)
        if not self.apply:
            return "dry-run：将覆盖写入 %s（未写盘）\n%s" % (path.relative_to(self.root).as_posix(), diff)
        if path.exists():
            self.backup(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.changed.append(path.relative_to(self.root).as_posix())
        self.mutated = True
        return "已写入 %s\n%s" % (path.relative_to(self.root).as_posix(), diff)

    def replace_in_file(self, args: dict) -> str:
        path = self.resolve(args.get("path", ""))
        if not path.is_file():
            raise ToolError("%s 不存在" % args.get("path"))
        old_snippet = args.get("old")
        new_snippet = args.get("new")
        if not isinstance(old_snippet, str) or not old_snippet:
            raise ToolError("replace_in_file 需要非空字符串 old")
        if not isinstance(new_snippet, str):
            raise ToolError("replace_in_file 需要字符串 new")
        text = path.read_text(encoding="utf-8", errors="replace")
        count = text.count(old_snippet)
        if count == 0:
            raise ToolError(
                "old 片段在文件里找不到。请先 read_file 确认原文（注意缩进和换行必须完全一致）"
            )
        new_text = text.replace(old_snippet, new_snippet, 1) if count > 1 else text.replace(old_snippet, new_snippet)
        diff = make_diff(text, new_text, path)
        note = "" if count == 1 else "（old 出现 %d 次，只替换了第 1 处，请检查是否需要继续）" % count
        if not self.apply:
            return "dry-run：将修改 %s（未写盘）%s\n%s" % (
                path.relative_to(self.root).as_posix(),
                note,
                diff,
            )
        self.backup(path)
        path.write_text(new_text, encoding="utf-8")
        self.changed.append(path.relative_to(self.root).as_posix())
        self.mutated = True
        return "已修改 %s%s\n%s" % (path.relative_to(self.root).as_posix(), note, diff)

    def run_auto_test(self) -> str:
        """执行 --test-cmd 指定的固定测试命令，返回给模型看的结果文本。

        命令是用户在启动时写死的，模型无法修改；改完文件后由主循环自动触发。
        """
        if not self.test_cmd:
            return "（未配置 --test-cmd）"
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        started = time.time()
        try:
            proc = subprocess.run(
                self.test_cmd,
                shell=True,
                cwd=str(self.root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.test_timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return "自动测试超时（> %.0f 秒），已终止：%s" % (self.test_timeout, self.test_cmd)
        except OSError as exc:
            return "自动测试启动失败：%s" % exc

        output = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if len(output) > EXEC_MAX_OUTPUT:
            output = output[:EXEC_MAX_OUTPUT] + "\n...（输出过长，已截断）"
        verdict = "通过" if proc.returncode == 0 else "失败"
        return "命令: %s\n结果: %s（退出码 %s）\n耗时: %.1fs\n输出:\n%s" % (
            self.test_cmd,
            verdict,
            proc.returncode,
            time.time() - started,
            output or "（无输出）",
        )

    def run_command(self, args: dict) -> str:
        if not self.allow_exec:
            raise ToolError("命令执行未开启（默认关闭）。需要跑测试请用 --allow-exec 启动")
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ToolError("exec 需要非空字符串 command")
        if DANGEROUS_COMMAND_RE.search(command):
            raise ToolError("命令被安全规则拒绝（疑似破坏性操作）：%s" % command[:120])

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        say("▶ 执行命令: %s" % command)
        started = time.time()
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(self.root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.exec_timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return "命令超时（> %.0f 秒），已终止：%s" % (self.exec_timeout, command)
        except OSError as exc:
            raise ToolError("命令启动失败：%s" % exc)

        output = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if len(output) > EXEC_MAX_OUTPUT:
            output = output[:EXEC_MAX_OUTPUT] + "\n...（输出过长，已截断）"
        return "命令: %s\n退出码: %s\n耗时: %.1fs\n输出:\n%s" % (
            command,
            proc.returncode,
            time.time() - started,
            output or "（无输出）",
        )

    def dispatch(self, call: dict) -> str:
        tool = call.get("tool")
        handlers = {
            "list_files": self.list_files,
            "read_file": self.read_file,
            "write_file": self.write_file,
            "replace_in_file": self.replace_in_file,
            "exec": self.run_command,
        }
        if tool == "finish":
            summary = call.get("summary") or "（模型未提供 summary）"
            return str(summary)
        handler = handlers.get(tool)
        if not handler:
            raise ToolError("未知工具：%r" % tool)
        return handler(call)


class ToolError(Exception):
    pass


def make_diff(old: str, new: str, path: Path) -> str:
    diff_lines = list(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=path.as_posix() + "（原）",
            tofile=path.as_posix() + "（新）",
            lineterm="",
            n=2,
        )
    )
    if not diff_lines:
        return "（内容无变化）"
    if len(diff_lines) > MAX_DIFF_LINES:
        diff_lines = diff_lines[:MAX_DIFF_LINES] + ["...（diff 太长，已截断）"]
    return "\n".join(diff_lines)


# ───────────────────────────── 解析模型输出 ─────────────────────────────


def parse_tool_call(text: str) -> dict | None:
    """从模型回复里抠出 JSON 工具调用；容忍前后有解释文字。"""
    if not text:
        return None
    for block in reversed(TOOL_FENCE_RE.findall(text)):
        try:
            obj = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("tool"):
            return obj
    # 兜底 1：解码任意位置的 JSON 对象
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("tool"):
            return obj
    return None


# ───────────────────────────── 主循环 ─────────────────────────────


def main(argv: list[str] | None = None) -> int:
    setup_stdio()
    args = build_parser().parse_args(argv)
    root = Path(args.dir).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit("--dir 不是目录：%s" % root)
    if args.max_steps < 1:
        raise SystemExit("--max-steps 必须 >= 1")

    url = args.base_url.rstrip("/") + "/chat/completions"
    opener = build_opener()
    toolbox = Toolbox(
        root=root,
        apply=args.apply,
        allow_exec=args.allow_exec,
        exec_timeout=args.exec_timeout,
        test_cmd=args.test_cmd,
        test_timeout=args.test_timeout,
    )

    mode = "写盘模式" if args.apply else "dry-run（只预览，不写盘）"
    say("=" * 70)
    say("任务 : %s" % args.task)
    say("目录 : %s" % root)
    say("模型 : %s    会话: %s    模式: %s" % (args.model, args.thread_mode, mode))
    say("命令 : %s" % ("允许执行（--allow-exec）" if args.allow_exec else "禁止执行（默认）"))
    say("测试 : %s" % ("改完自动跑 -> %s" % args.test_cmd if args.test_cmd else "未配置（加 --test-cmd 可自动跑测试）"))
    say("上限 : %d 轮工具调用" % args.max_steps)
    say("=" * 70)
    system_prompt = build_system_prompt(args.allow_exec, args.test_cmd)

    def make_observation(step: int, payload: str) -> str:
        # 每轮都重复任务，防止会话轮换后模型丢失目标
        return "[第 %d 轮] 任务：%s\n\n%s" % (step, args.task, payload)

    # 自动测试：先跑一次记录基线（让模型知道初始哪些用例本来就是坏的）
    baseline_note = ""
    if args.test_cmd:
        say("\n──── 自动测试（基线，改动前）────")
        say("▶ %s" % args.test_cmd)
        baseline = toolbox.run_auto_test()
        say(baseline[:2000])
        baseline_note = "\n\n改动前的自动测试基线结果：\n\n%s" % baseline

    observation = make_observation(
        1,
        "工作目录：%s\n请先列目录，再读相关文件，然后修改。完成后调用 finish。" % root
        + baseline_note,
    )

    finished = False
    summary = ""
    last_error = ""
    for step in range(1, args.max_steps + 1):
        if last_error:
            observation = make_observation(
                step,
                "上一步工具调用失败了：%s\n请修正后重新输出一个 ```json 工具调用代码块。" % last_error,
            )
            last_error = ""
        started = time.time()
        say("\n──── 第 %d/%d 轮 ────" % (step, args.max_steps))
        content, usage = chat(
            opener,
            url,
            args.api_key,
            args.model,
            system_prompt,
            observation,
            args.thread_mode,
            args.timeout,
        )
        elapsed = time.time() - started
        prompt_tokens = usage.get("prompt_tokens")
        say(
            "（模型回复 %d 字，耗时 %.1fs，输入 %s tokens）"
            % (len(content), elapsed, prompt_tokens if prompt_tokens is not None else "?")
        )
        if isinstance(prompt_tokens, int) and prompt_tokens > 240000:
            say("⚠ 输入 tokens 已接近 30 万轮换阈值，继续可能触发新 Notion 会话（好在每轮都带任务描述，可续做）")

        call = parse_tool_call(content)
        if not call:
            say("解析不到工具调用，内容如下：\n%s" % content.strip()[:800])
            last_error = "回复里没有合法的 ```json {\"tool\": ...}``` 代码块"
            continue

        tool_name = call.get("tool")
        say("工具: %s %s" % (tool_name, json.dumps({k: v for k, v in call.items() if k != "tool"}, ensure_ascii=False)[:300]))
        try:
            result = toolbox.dispatch(call)
        except ToolError as exc:
            say("执行失败: %s" % exc)
            last_error = str(exc)
            continue

        if tool_name == "finish":
            finished = True
            summary = result
            say("\n任务完成：%s" % summary)
            break
        say(result[:1500])
        payload = "工具结果（%s）：\n\n%s" % (tool_name, result)
        # 改完文件后自动跑测试，把结果一起喂给模型（模型自己不能改这条命令）
        if toolbox.test_cmd and toolbox.mutated:
            toolbox.mutated = False
            say("\n──── 自动测试（改动后）────")
            say("▶ %s" % toolbox.test_cmd)
            test_result = toolbox.run_auto_test()
            say(test_result[:2000])
            payload += "\n\n自动测试结果（%s）：\n\n%s" % (toolbox.test_cmd, test_result)
        observation = make_observation(step + 1, payload)

    say("\n" + "=" * 70)
    if finished:
        say("完成：%s" % summary)
    else:
        say("达到 %d 轮上限，任务未显式结束（可加大 --max-steps 重跑）" % args.max_steps)

    if toolbox.changed:
        say("\n修改的文件（共 %d 个）：" % len(toolbox.changed))
        for rel in toolbox.changed:
            say("  - %s" % rel)
        if args.apply:
            say("\n备份目录：%s" % toolbox.backup_dir)
    else:
        say("\n本次没有文件被修改%s" % ("（dry-run 模式，加 --apply 才会写盘）" if not args.apply else ""))

    return 0 if finished else 1


if __name__ == "__main__":
    sys.exit(main())