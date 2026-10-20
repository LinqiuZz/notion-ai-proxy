#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""batch_process.py — 用本地 Notion AI 代理批量处理文本 / 代码文件。

只依赖 Python 标准库，不需要安装任何第三方包。
使用前请先启动代理（双击项目根目录的 start-proxy.cmd）。

功能：
  * 输入可以是单个文件、一个目录（递归），或 glob 通配符；
  * 每个文件按 --prompt 模板发给本地代理（模板必须包含 {content}，可选 {path}）；
  * 结果写到 --out-dir，文件名 = 原文件名 + ".md"，并保留相对目录结构；
  * 支持并发（最多 3 个）；单个文件失败不影响其他文件；
  * 按 Ctrl+C 可以随时优雅退出，已经写出的结果会保留。

示例（Windows cmd，在 ai-proxy 目录下运行）：
  python examples\\batch_process.py --input src --prompt "请用中文解释这个文件的作用和改进建议：{content}" --out-dir out
  python examples\\batch_process.py --input "src/**/*.py" --prompt "审查以下代码（{path}）：{content}" --concurrency 2
  python examples\\batch_process.py --input README.md --prompt "请总结成 5 条要点：{content}" --model claude-opus5
  python examples\\batch_process.py --help

退出码：0 = 全部成功；1 = 有文件失败 / 没有找到文件；2 = 启动前检查失败；130 = 用户按 Ctrl+C。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DEFAULT_EXTS = ".py,.js,.ts,.java,.go,.rs,.c,.cpp,.h,.md,.txt,.json,.yaml,.yml"
DEFAULT_BASE_URL = "http://127.0.0.1:8787/v1"
DEFAULT_MODEL = "claude-sonnet4.6"
DEFAULT_API_KEY = "sk-local"
DEFAULT_MAX_CHARS = 120000
DEFAULT_CONCURRENCY = 1
MAX_CONCURRENCY = 3
MAX_FILE_BYTES = 64 * 1024 * 1024  # 超过 64MB 的文本文件直接跳过，避免误读大文件
MAX_ATTEMPTS = 3  # 单个文件最多尝试次数（含第一次）
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

# 目录扫描时自动跳过的目录名（避免把依赖、缓存、密钥、参考源码、上次输出也扫进去）
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "node_modules",
    ".venv",
    "venv",
    ".tmp",
    "secret",
    "ref",
    "out",
}

# 所有线程共用一把打印锁，保证并发时日志不串行错乱
PRINT_LOCK = threading.RLock()


def say(message: str = "") -> None:
    """线程安全地打印一行日志（并发处理时不会串行错乱）。"""
    with PRINT_LOCK:
        print(message, flush=True)

EXAMPLES_TEXT = """\
示例（Windows cmd，在 ai-proxy 目录下运行）：
  python examples\\batch_process.py --input src --prompt "请用中文解释这个文件的作用和改进建议：{content}" --out-dir out
  python examples\\batch_process.py --input "src/**/*.py" --prompt "审查以下代码（{path}）：{content}" --model claude-opus5 --concurrency 2
  python examples\\batch_process.py --input README.md --prompt "请总结成 5 条要点：{content}"

--prompt 必须包含 {content}（文件内容），可选 {path}（相对路径）。
输出：<out-dir>/<原相对路径>.md，例如 out/src/app.py.md
"""


class BatchUserError(Exception):
    """可以直接展示给用户的错误（不打印 traceback）。"""


class SkipFile(Exception):
    """该文件应被跳过（二进制 / 空文件 / 过大等）。"""


@dataclass
class FileResult:
    path: Path
    rel: str
    ok: bool = False
    skipped: bool = False
    error: str = ""
    seconds: float = 0.0
    chars: int = 0
    out_path: Path | None = None


# ---------------------------------------------------------------------------
# 命令行参数
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="batch_process.py",
        description="用本地 Notion AI 代理批量处理文本 / 代码文件，结果保存为 .md 文件。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EXAMPLES_TEXT,
    )
    parser.add_argument("--input", default=".", help="输入：文件、目录（递归）或 glob；默认当前目录")
    parser.add_argument("--prompt", required=True, help="提示词模板，必须包含 {content}，可选 {path}")
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="模型 id，默认 %s（可用 /v1/models 查看全部）" % DEFAULT_MODEL,
    )
    parser.add_argument("--out-dir", default="./out", help="输出目录，默认 ./out")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help="并发数，默认 %d，最大 %d" % (DEFAULT_CONCURRENCY, MAX_CONCURRENCY),
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help="代理的 OpenAI 兼容地址，默认 %s" % DEFAULT_BASE_URL,
    )
    parser.add_argument(
        "--api-key",
        default=DEFAULT_API_KEY,
        help="本地代理不校验 key，填任意非空值即可，默认 %s" % DEFAULT_API_KEY,
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=DEFAULT_MAX_CHARS,
        help="单个文件最多读取的字符数，默认 %d；超长会截断并在输出里标注" % DEFAULT_MAX_CHARS,
    )
    parser.add_argument(
        "--exts",
        default=DEFAULT_EXTS,
        help="目录扫描时处理的扩展名，逗号分隔；传空字符串表示不限制。默认：%s" % DEFAULT_EXTS,
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="单次请求超时秒数，默认 600",
    )
    parser.add_argument(
        "--thread-mode",
        choices=("single", "new"),
        default="single",
        help=(
            "Notion 会话模式：single=复用同一个 Notion 会话（默认，避免每个文件都在 Notion 里新建对话）；"
            "new=每个文件新建会话（文件之间完全隔离，但会堆积很多对话）"
        ),
    )
    return parser


def normalize_exts(raw: str) -> set[str] | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    exts: set[str] = set()
    for part in raw.split(","):
        part = part.strip().lower()
        if not part:
            continue
        if not part.startswith("."):
            part = "." + part
        exts.add(part)
    return exts or None


def normalize_base_url(raw: str) -> str:
    url = (raw or DEFAULT_BASE_URL).strip().rstrip("/")
    if url.endswith("/chat/completions"):
        url = url[: -len("/chat/completions")]
        say("[提示] --base-url 填的是完整接口地址，已自动改为：%s" % url)
    parts = urllib.parse.urlsplit(url)
    if parts.path in ("", "/"):
        url = url + "/v1"
        say("[提示] --base-url 未包含 /v1，已自动改为：%s" % url)
    elif not parts.path.endswith("/v1"):
        say("[提示] --base-url 通常应以 /v1 结尾（当前：%s）；如代理配置不同请忽略。" % url)
    return url


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not re.search(r"\{content\}", args.prompt):
        parser.error(
            '--prompt 必须包含 {content} 占位符，例如：--prompt "请总结下面的内容：{content}"'
        )

    if args.concurrency < 1:
        say("[提示] --concurrency 小于 1，已按 1 处理。")
        args.concurrency = 1
    elif args.concurrency > MAX_CONCURRENCY:
        say("[提示] --concurrency 最大为 %d，已按 %d 处理。" % (MAX_CONCURRENCY, MAX_CONCURRENCY))
        args.concurrency = MAX_CONCURRENCY

    if args.max_chars <= 0:
        parser.error("--max-chars 必须大于 0")
    if args.timeout <= 0:
        parser.error("--timeout 必须大于 0")

    args.exts_set = normalize_exts(args.exts)
    args.base_url = normalize_base_url(args.base_url)
    args.api_key = (args.api_key or "").strip() or DEFAULT_API_KEY
    args.out_dir = args.out_dir or "./out"
    return args


# ---------------------------------------------------------------------------
# 输入文件收集
# ---------------------------------------------------------------------------


def collect_files(
    input_arg: str, exts: set[str] | None, out_dir: Path
) -> tuple[list[Path], Path]:
    """把 --input 解析成 (文件列表, 相对路径基准目录)。"""
    raw = (input_arg or ".").strip().strip('"').strip("'") or "."
    path = Path(raw).expanduser()

    # 1) 明确的单个文件：直接处理，不再按扩展名过滤
    if path.is_file():
        resolved = path.resolve()
        return [resolved], resolved.parent

    # 2) 目录：递归扫描，按 --exts 过滤
    if path.is_dir():
        base = path.resolve()
        if base == out_dir:
            raise SystemExit("[错误] --out-dir 不能与 --input 目录相同，请换一个输出目录。")
        files: list[Path] = []
        for root, dirs, names in os.walk(base):
            root_path = Path(root)
            dirs[:] = sorted(
                d
                for d in dirs
                if d not in SKIP_DIRS and (root_path / d).resolve() != out_dir
            )
            for name in sorted(names):
                file_path = root_path / name
                if exts is not None and file_path.suffix.lower() not in exts:
                    continue
                try:
                    if not file_path.is_file():
                        continue
                except OSError:
                    continue
                files.append(file_path)
        return files, base

    # 3) glob 通配符：按用户给定的范围处理（不再按 --exts 过滤）
    if any(ch in raw for ch in "*?["):
        matches: list[Path] = []
        for item in glob.glob(raw, recursive=True):
            matched = Path(item)
            try:
                if matched.is_file():
                    matches.append(matched.resolve())
            except OSError:
                continue
        if not matches:
            raise SystemExit(
                "[错误] glob 没有匹配到任何文件：%s\n       请检查 --input 写法（例如 \"src/**/*.py\"）。" % raw
            )
        unique = sorted(set(matches))
        try:
            base = Path(os.path.commonpath([str(m.parent) for m in unique]))
        except ValueError:
            base = Path.cwd()
        return unique, base

    raise SystemExit(
        "[错误] 找不到输入路径：%s\n       请检查 --input（可以是文件、目录或带通配符的 glob）。" % raw
    )


def safe_relative(path: Path, base: Path) -> str:
    try:
        rel = path.relative_to(base)
    except ValueError:
        rel = Path(path.name)
    return rel.as_posix()


# ---------------------------------------------------------------------------
# 读取文件
# ---------------------------------------------------------------------------


def read_source(path: Path, max_chars: int) -> tuple[str, bool, str]:
    """读取文本文件，返回 (内容, 是否截断, 实际编码)。

    优先 UTF-8；失败回退 GBK。二进制 / 空文件 / 超大文件抛 SkipFile。
    """
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise BatchUserError("无法读取文件信息：%s" % exc) from exc
    if size > MAX_FILE_BYTES:
        raise SkipFile("文件过大（%.1f MB），已跳过" % (size / 1024 / 1024))
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise BatchUserError("读取文件失败：%s" % exc) from exc

    if b"\x00" in raw[:8192]:
        raise SkipFile("看起来是二进制文件，已跳过")

    text: str | None = None
    encoding = ""
    for enc in ("utf-8-sig", "gbk"):
        try:
            text = raw.decode(enc)
            encoding = enc
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")
        encoding = "utf-8(容错)"

    if not text.strip():
        raise SkipFile("空文件，已跳过")

    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    return text, truncated, encoding


# ---------------------------------------------------------------------------
# 构造 prompt / 调用代理
# ---------------------------------------------------------------------------


def build_user_prompt(template: str, content: str, rel: str) -> str:
    """用单次替换实现 {content}/{path} 占位符，避免文件内容里的花括号被二次替换。"""

    def replace(match: re.Match[str]) -> str:
        return content if match.group(1) == "content" else rel

    return re.sub(r"\{(content|path)\}", replace, template)


def make_opener() -> urllib.request.OpenerDirector:
    """构造禁用系统代理的 opener：128.0.0.0/127.0.0.1 请求不应走 HTTP_PROXY。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _post_chat(
    opener: urllib.request.OpenerDirector,
    url: str,
    api_key: str,
    payload: dict,
    timeout: float,
) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": "Bearer " + api_key,
        "Accept": "application/json",
        "User-Agent": "ai-proxy-batch-process/1.0",
    }
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with opener.open(request, timeout=timeout) as response:
        raw = response.read()
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        preview = raw[:300].decode("utf-8", errors="replace")
        raise BatchUserError("代理返回的不是合法 JSON，响应开头：%r" % preview) from exc


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        text = exc.read().decode("utf-8", errors="replace").strip()
    except Exception:  # noqa: BLE001
        text = ""
    if not text:
        return ""
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                message = error.get("message") or error.get("code")
                if message:
                    return "：" + str(message)[:200]
            elif isinstance(error, str):
                return "：" + error[:200]
            if data.get("message"):
                return "：" + str(data["message"])[:200]
    except (ValueError, AttributeError):
        pass
    return "：" + text[:200]


def _retry_wait(exc: urllib.error.HTTPError, attempt: int) -> float:
    try:
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        if retry_after:
            value = float(str(retry_after).strip())
            if 0 < value <= 30:
                return value
    except (TypeError, ValueError, AttributeError):
        pass
    return min(2.0 * attempt, 8.0)


def extract_text(data: dict) -> str:
    """从 OpenAI 兼容响应里取出文字内容。"""
    if not isinstance(data, dict):
        raise BatchUserError("代理返回格式异常（不是 JSON 对象）")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        error = data.get("error")
        if error is not None:
            detail = error if isinstance(error, str) else json.dumps(error, ensure_ascii=False)
            raise BatchUserError("上游返回错误：%s" % str(detail)[:300])
        raise BatchUserError("响应缺少 choices 字段")

    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    content = message.get("content")

    if isinstance(content, list):  # 少数兼容实现返回分段内容
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        content = "".join(parts)

    if not isinstance(content, str) or not content.strip():
        reasoning = message.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning.strip():
            content = reasoning

    if not isinstance(content, str) or not content.strip():
        finish_reason = choice.get("finish_reason")
        raise BatchUserError("模型返回了空内容（finish_reason=%r）" % (finish_reason,))

    return content.strip()


def call_model(
    opener: urllib.request.OpenerDirector, args: argparse.Namespace, user_text: str
) -> str:
    """把一条 prompt 发给本地代理，返回模型生成的文字；失败抛 BatchUserError。"""
    payload = {
        "model": args.model,
        "messages": [{"role": "user", "content": user_text}],
        "stream": False,
        "thread_mode": getattr(args, "thread_mode", "single"),
    }
    url = args.base_url.rstrip("/") + "/chat/completions"
    last_error = "请求失败"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            data = _post_chat(opener, url, args.api_key, payload, args.timeout)
            return extract_text(data)
        except urllib.error.HTTPError as exc:
            last_error = "HTTP %s%s" % (exc.code, _http_error_detail(exc))
            if exc.code in RETRYABLE_STATUS and attempt < MAX_ATTEMPTS:
                wait = _retry_wait(exc, attempt)
                say("  [重试 %d/%d] %s，%.1f 秒后重试" % (attempt + 1, MAX_ATTEMPTS, last_error, wait))
                time.sleep(wait)
                continue
            raise BatchUserError(last_error) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            last_error = "无法连接本地代理（%s）" % reason
            if attempt < MAX_ATTEMPTS:
                wait = min(1.5 * attempt, 5.0)
                say("  [重试 %d/%d] %s，%.1f 秒后重试" % (attempt + 1, MAX_ATTEMPTS, last_error, wait))
                time.sleep(wait)
                continue
            raise BatchUserError(last_error) from exc
    raise BatchUserError(last_error)


# ---------------------------------------------------------------------------
# 写输出
# ---------------------------------------------------------------------------


def write_output(
    out_path: Path,
    answer: str,
    rel: str,
    args: argparse.Namespace,
    truncated: bool,
    encoding: str,
) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    head = ["> 源文件：`%s` ｜ 模型：`%s` ｜ 生成时间：%s" % (rel, args.model, stamp)]
    if truncated:
        head.append(
            "> [注意] 源文件超过 %d 字符，已截断，仅处理前 %d 字符。" % (args.max_chars, args.max_chars)
        )
    if encoding == "gbk":
        head.append("> [注意] 源文件不是 UTF-8，已按 GBK 解码。")
    elif encoding.startswith("utf-8("):
        head.append("> [注意] 源文件编码无法严格解码，已按 UTF-8 容错解码（可能有少量乱码）。")
    text = "\n".join(head) + "\n\n" + answer.rstrip() + "\n"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # 先写临时文件再原子替换，避免 Ctrl+C / 异常时留下半截结果
    tmp_path = out_path.with_name(out_path.name + ".tmp%d-%d" % (os.getpid(), threading.get_ident()))
    try:
        tmp_path.write_text(text, encoding="utf-8")
        os.replace(tmp_path, out_path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 单个文件处理
# ---------------------------------------------------------------------------


def process_one(
    opener: urllib.request.OpenerDirector,
    args: argparse.Namespace,
    path: Path,
    rel: str,
    out_dir: Path,
) -> FileResult:
    started = time.monotonic()
    try:
        content, truncated, encoding = read_source(path, args.max_chars)
        user_text = build_user_prompt(args.prompt, content, rel)
        answer = call_model(opener, args, user_text)
        out_path = out_dir / Path(rel).parent / (path.name + ".md")
        write_output(out_path, answer, rel, args, truncated, encoding)
        return FileResult(
            path=path,
            rel=rel,
            ok=True,
            seconds=time.monotonic() - started,
            chars=len(answer),
            out_path=out_path,
        )
    except SkipFile as exc:
        return FileResult(
            path=path, rel=rel, skipped=True, error=str(exc), seconds=time.monotonic() - started
        )
    except BatchUserError as exc:
        return FileResult(
            path=path, rel=rel, error=str(exc), seconds=time.monotonic() - started
        )
    except Exception as exc:  # noqa: BLE001 单个文件失败不能影响整批
        return FileResult(
            path=path,
            rel=rel,
            error="%s: %s" % (type(exc).__name__, exc),
            seconds=time.monotonic() - started,
        )


# ---------------------------------------------------------------------------
# 批量调度
# ---------------------------------------------------------------------------


def progress_bar(done: int, total: int, width: int = 20) -> str:
    if total <= 0:
        return "-" * width
    filled = int(round(min(done / total, 1.0) * width))
    return "#" * filled + "-" * (width - filled)


def build_hints(failures: list[FileResult]) -> list[str]:
    joined = "\n".join(result.error for result in failures).lower()
    hints: list[str] = []
    if "401" in joined or "unauthorized" in joined:
        hints.append(
            "出现 401：登录态可能已过期 → 双击 update-cookie.cmd 刷新后重试；"
            "若 config.json 设置了 apiKey，请核对 --api-key 是否一致。"
        )
    if "403" in joined:
        hints.append("出现 403：模型可能不可用（套餐不支持）或触发风控 → 换模型、降低并发后重试。")
    if "429" in joined:
        hints.append("出现 429：请求过快被限流 → 降低 --concurrency（如 1），等几分钟再试。")
    if "无法连接" in joined or "refused" in joined or "timed out" in joined:
        hints.append("连接代理失败 → 确认 start-proxy.cmd 窗口还开着，且 --base-url 端口正确。")
    return hints


def run_batch(
    items: list[tuple[Path, str]],
    args: argparse.Namespace,
    out_dir: Path,
    opener: urllib.request.OpenerDirector,
) -> int:
    total = len(items)
    task_queue: queue.Queue[tuple[Path, str]] = queue.Queue()
    for item in items:
        task_queue.put(item)

    results: list[FileResult] = []
    counts = {"ok": 0, "fail": 0, "skip": 0}
    stop = threading.Event()
    started_at = time.monotonic()

    print("=" * 68)
    print("[批量处理] 共 %d 个文件 ｜ 模型：%s ｜ 并发：%d" % (total, args.model, args.concurrency))
    print("[批量处理] 输出目录：%s" % out_dir)
    print("[提示] 处理期间请保持代理窗口开着；按 Ctrl+C 可随时停止。")
    print("=" * 68)

    def worker() -> None:
        while not stop.is_set():
            try:
                path, rel = task_queue.get_nowait()
            except queue.Empty:
                return
            with PRINT_LOCK:
                print("  -> 请求中：%s" % rel, flush=True)
            result = process_one(opener, args, path, rel, out_dir)
            with PRINT_LOCK:
                results.append(result)
                done = len(results)
                if result.skipped:
                    counts["skip"] += 1
                    tag = "跳过"
                elif result.ok:
                    counts["ok"] += 1
                    tag = "成功"
                else:
                    counts["fail"] += 1
                    tag = "失败"
                pct = int(done * 100 / total) if total else 100
                detail = "，%d 字符" % result.chars if result.ok else ""
                print(
                    "[%s] %3d%%  %d/%d  %s  %s  (%.1fs%s)"
                    % (progress_bar(done, total), pct, done, total, tag, rel, result.seconds, detail),
                    flush=True,
                )
                if result.skipped:
                    print("        跳过原因：%s" % result.error, flush=True)
                elif not result.ok:
                    print("        原因：%s" % result.error, flush=True)

    worker_count = max(1, min(args.concurrency, total))
    threads = [
        threading.Thread(target=worker, name="batch-worker-%d" % (index + 1), daemon=True)
        for index in range(worker_count)
    ]
    for thread in threads:
        thread.start()

    interrupted = False
    try:
        while True:
            alive = [thread for thread in threads if thread.is_alive()]
            if not alive:
                break
            alive[0].join(timeout=0.2)
    except KeyboardInterrupt:
        interrupted = True
        stop.set()
        print("\n[中断] 收到 Ctrl+C，正在安全退出（已经写出的结果会保留）...", flush=True)
        deadline = time.monotonic() + 1.0
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)

    elapsed = time.monotonic() - started_at
    with PRINT_LOCK:
        done = len(results)
        ok_count = counts["ok"]
        fail_count = counts["fail"]
        skip_count = counts["skip"]
        unfinished = total - done
        failures = [result for result in results if not result.ok and not result.skipped]

        print("")
        print("=" * 68)
        print(
            "[汇总] 共 %d 个文件：成功 %d 个，失败 %d 个，跳过 %d 个"
            % (total, ok_count, fail_count, skip_count)
        )
        print("[汇总] 用时 %.1f 秒 ｜ 输出目录：%s" % (elapsed, out_dir))
        if interrupted and unfinished > 0:
            print("[汇总] 已中断：还有 %d 个文件未处理。" % unfinished)
        if failures:
            print("")
            print("[失败清单]")
            for result in failures[:50]:
                print("  - %s：%s" % (result.rel, result.error))
            if len(failures) > 50:
                print("  ...（共 %d 个失败，只列出前 50 个）" % len(failures))
        hints = build_hints(failures)
        if hints:
            print("")
            print("[建议]")
            for hint in hints:
                print("  - " + hint)
        print("=" * 68)

    if interrupted:
        return 130
    return 0 if fail_count == 0 else 1


# ---------------------------------------------------------------------------
# 启动前检查 / main
# ---------------------------------------------------------------------------


def check_proxy(opener: urllib.request.OpenerDirector, args: argparse.Namespace) -> bool:
    """用 GET /models 快速确认本地代理是否在线；连不上时给出可操作的提示。"""
    url = args.base_url.rstrip("/") + "/models"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": "Bearer " + args.api_key,
            "Accept": "application/json",
            "User-Agent": "ai-proxy-batch-process/1.0",
        },
        method="GET",
    )
    try:
        with opener.open(request, timeout=8) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            say(
                "[警告] 代理返回 HTTP 401：如果 config.json 里设置了 apiKey，"
                "请把 --api-key 改成一致的值；否则多半是登录态过期。"
            )
            say("[警告] 仍会尝试继续处理；若全部失败，请先双击 update-cookie.cmd 刷新登录态。")
        else:
            say("[警告] 代理有响应，但 %s 返回 HTTP %s；仍会尝试继续处理。" % (url, exc.code))
        return True
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        say("")
        say("[错误] 连不上本地代理：%s" % args.base_url)
        say("       原因：%s" % reason)
        say("       1) 请先双击 start-proxy.cmd 启动代理，并确认窗口里出现“就绪”字样；")
        say("       2) 检查 --base-url 是否正确（默认 %s）。" % DEFAULT_BASE_URL)
        return False

    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        say("[警告] 代理 /models 返回的不是 JSON，仍将继续尝试处理。")
        return True

    ids: list[str] = []
    if isinstance(data, dict):
        models = data.get("data")
        if not isinstance(models, list):
            models = data.get("models")
        if isinstance(models, list):
            for model in models:
                if isinstance(model, dict) and model.get("id"):
                    ids.append(str(model["id"]))
                elif isinstance(model, str):
                    ids.append(model)
    if ids:
        say("[就绪] 本地代理在线，共发现 %d 个模型，例如：%s ..." % (len(ids), "、".join(ids[:5])))
    else:
        say("[就绪] 本地代理在线。")
    return True


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")  # 避免个别文件名导致 UnicodeEncodeError
        except Exception:  # noqa: BLE001
            pass

    args = parse_args(argv)
    out_dir = Path(args.out_dir).expanduser().resolve()
    try:
        files, base = collect_files(args.input, args.exts_set, out_dir)
    except SystemExit:
        raise
    except OSError as exc:
        say("[错误] 扫描输入目录失败：%s" % exc)
        return 2

    items = [(file_path, safe_relative(file_path, base)) for file_path in files]
    if not items:
        say("[空] 在 %s 中没有找到符合扩展名的文件。" % args.input)
        say("     目录扫描只处理 --exts 里的扩展名（当前：%s）。" % (args.exts or "（未限制）"))
        say('     也可以把 --input 指向具体文件，或传 --exts "" 表示不限制扩展名。')
        return 1

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        say("[错误] 无法创建输出目录 %s：%s" % (out_dir, exc))
        return 2

    opener = make_opener()
    if not check_proxy(opener, args):
        return 2
    return run_batch(items, args, out_dir, opener)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[已取消]")
        raise SystemExit(130)
