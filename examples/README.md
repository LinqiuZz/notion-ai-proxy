# examples — 使用示例

这里的脚本都假设**代理已经在运行**（双击项目根目录的 `start-proxy.cmd`，等窗口出现 `就绪`）。
默认地址是 `http://127.0.0.1:8787/v1`，默认模型是 `claude-sonnet4.6`。

> 更完整的说明（客户端配置、模型列表、常见问题、安全提示）请看上一级目录的 [README.md](../README.md)。

| 文件 | 用途 | 运行方式 |
|---|---|---|
| `openai_python.py` | 用官方 `openai` SDK 调代理：列模型、非流式、流式 | `python examples\openai_python.py`（需 `pip install openai`） |
| `attach_image.py` | 发图片 / 文件附件（OpenAI `image_url`/`file`、Anthropic `image`） | `python examples\attach_image.py`（只用标准库） |
| `curl_chat.cmd` | 用 Windows 自带 `curl.exe` 调代理，含中文请求 | 双击，或 `examples\curl_chat.cmd` |
| `batch_process.py` | 批量处理一个目录/一批文件，结果存成 `.md` | 见下方命令 |
| `auto_agent.py` | 让 AI 自动读文件、改文件（dry-run + 备份） | 见下方命令 |

---

## 1. openai_python.py

需要安装一次官方 SDK：

```bat
pip install openai
```

然后运行：

```bat
python examples\openai_python.py
```

可以用环境变量改地址/模型：

```bat
set OPENAI_BASE_URL=http://127.0.0.1:8787/v1
set OPENAI_API_KEY=sk-local
set NOTION_MODEL=claude-opus5
python examples\openai_python.py
```

## 2. attach_image.py（图片 / 文件附件）

只用 Python 标准库。演示三种写法，默认三项依次跑一遍：

```bat
:: 默认：用内置 1×1 红色小图 + 一段示例文本文件
python examples\attach_image.py

:: 用你自己的图片
python examples\attach_image.py --image C:\path\to\screenshot.png

:: 复用某个 Notion 会话（同一个对话里追问同一张图）
python examples\attach_image.py --thread <thread-id>

:: 只跑其中一项
python examples\attach_image.py --only file
```

行为说明：

- 图片 / PDF / Office 文档 → 走 Notion 的 `attachment` step，模型直接“看”图或解析文档；
- 文本类文件（`.txt .md .csv .json .py ...`）→ 走 `computer-file` step，模型用 computer 工具读取；
- 单文件上限由 `config.json` 的 `attachmentMaxBytes` 控制（默认 14MB）；
- 附件只取**最后一条 user 消息**里的，历史消息里的附件不会重发。

## 3. curl_chat.cmd

双击即可。它会依次请求 `/health`、`/v1/models`、`/v1/chat/completions`（中文请求体）。

想换端口或换模型：

```bat
set NOTION_MODEL=claude-opus5
examples\curl_chat.cmd
examples\curl_chat.cmd http://127.0.0.1:9000
```

> 脚本里已经执行了 `chcp 65001`，所以中文请求不会乱码。手动在 cmd 里粘贴中文 JSON 容易乱码，建议直接用这个脚本。

## 4. batch_process.py（批量处理）

只依赖 Python 标准库，**不需要** `pip install`。它会把每个文件的内容套进 `--prompt` 模板，
发给本地代理处理，结果写到 `--out-dir`，文件名是「原文件名 + `.md`」，并保留目录结构。

`--prompt` 里必须写 `{content}`（文件内容），可选写 `{path}`（相对路径）。

常用命令（在 `ai-proxy` 目录下运行）：

```bat
:: 处理一个目录（递归），默认扩展名 .py/.js/.../.md/.txt 等
python examples\batch_process.py --input src --prompt "请用中文解释这个文件的作用，并给出改进建议：{content}" --out-dir out

:: 只处理某类文件，并发 2
python examples\batch_process.py --input src --exts .py,.ts --prompt "审查代码（{path}）：{content}" --out-dir out --concurrency 2

:: 用 glob 指定文件；显式 glob 不会再被 --exts 过滤
python examples\batch_process.py --input "src/**/*.py" --prompt "总结这个文件：{content}" --model claude-opus5

:: 只处理单个文件
python examples\batch_process.py --input README.md --prompt "请总结成 5 条要点：{content}"
```

输出示例：

```
out\src\app.py.md
out\src\utils\db.ts.md
```

处理过程中打印进度条；按 `Ctrl+C` 可随时停止（已写出的结果会保留）；单个文件失败不影响其他文件。
完整参数说明见主 README 的「批量处理文件」一节，或运行：

```bat
python examples\batch_process.py --help
```

## 5. auto_agent.py（自动改代码）

`batch_process.py` 只产出文本；`auto_agent.py` 会**直接修改文件**。模型本身没有工具调用能力，
脚本用「文本 JSON 协议」模拟：模型每轮只输出一个 JSON 代码块（如 `{"tool": "read_file", "path": "a.py"}`），
脚本在本地执行（列目录 / 读文件 / 覆盖写 / 精确替换），把结果回传，直到模型调用 `finish`。

```bat
:: 1) 先预览：只打印 diff，不写盘（默认行为）
python examples\auto_agent.py --task "把 greet.py 的返回值改成 hi + name" --dir src

:: 2) 确认没问题后真正写盘（自动备份到 src\.autoedit-backup\时间戳\）
python examples\auto_agent.py --task "把 greet.py 的返回值改成 hi + name" --dir src --apply

:: 3) 稍微大一点的任务（最多 10 轮工具调用）
python examples\auto_agent.py --task "给 utils.py 里所有函数加中文 docstring" --dir src --max-steps 10 --apply

:: 4) 改完自己跑测试验证（命令执行默认关闭，需 --allow-exec）
python examples\auto_agent.py --task "修复 test_math.py 失败的用例，改完跑 pytest 验证" --dir src --max-steps 10 --apply --allow-exec
```

参数：`--task`（必填）、`--dir`（工作目录，只允许改这个目录）、`--apply`（真正写盘）、
`--model`（默认 `opus-5.5`）、`--max-steps`（默认 8）、`--thread-mode`（默认 `single`，复用 Notion 会话）、
`--allow-exec`（允许跑命令，默认关闭）、`--exec-timeout`（命令超时，默认 120 秒）。

安全：默认 dry-run；写盘前自动备份；只改 `--dir` 内文件；拒绝 `secret/`、`.git/`、`node_modules/` 等目录；
不会删文件。命令执行默认关闭，加 `--allow-exec` 后才有 `exec` 工具，并且带破坏性命令黑名单（删除 / 格式化 / `git push` 等）、
超时和输出截断。注意：复用会话时输入 token 每轮约 +3~4 万，长任务建议拆小。
