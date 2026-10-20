# Notion AI 本地代理（ai-proxy）

把 **Notion 网页版里的 AI** 变成 **本机可用的 OpenAI 兼容接口**。

启动后，你会得到这样一个地址：

```text
http://127.0.0.1:8787/v1
```

任何支持 OpenAI 接口的客户端（Cherry Studio、Cursor、各种 SDK、自己写的脚本）都可以连它，
直接使用 Notion AI 背后的 Claude、GPT、Gemini、Kimi、Grok、DeepSeek、GLM 等模型。

> 本机自用工具：只监听 `127.0.0.1`，别人访问不到；登录凭据只存在本机 `secret/` 目录里。
> 使用前请先看文末的 [安全与风险提示](#10-安全与风险提示)，了解可能的风控与账号风险。

---

## 目录

1. [它能做什么](#1-它能做什么)
2. [准备工作](#2-准备工作)
3. [快速开始（3 步）](#3-快速开始3-步)
4. [验证是否启动成功](#4-验证是否启动成功)
5. [在客户端里怎么填](#5-在客户端里怎么填)
6. [模型怎么选](#6-模型怎么选)
7. [批量处理文件（batch_process.py）](#7-批量处理文件batch_processpy)
8. [登录态过期怎么办](#8-登录态过期怎么办)
9. [常见问题](#9-常见问题)
10. [安全与风险提示](#10-安全与风险提示)
11. [文件结构说明](#11-文件结构说明)
12. [附录：接口与配置](#12-附录接口与配置)

---

## 1. 它能做什么

- 提供 OpenAI 兼容接口，把请求转发给 **你自己的 Notion AI 网页登录态**：
  - `POST /v1/chat/completions`：聊天补全（支持 `stream: true` 流式输出）
  - `GET /v1/models`：模型列表（动态来自你的 Notion 账号）
  - `GET /health`：健康检查
- 模型列表动态发现，通常有 **30 个以上**，涵盖 Claude / GPT / Gemini / Kimi / Grok / DeepSeek / GLM 等。
- 支持多轮对话、流式输出、`web_search`（联网搜索）、`reasoning_effort`（思考强度，部分模型）等可选参数。
- 附带四个示例脚本：
  - `examples/openai_python.py`：官方 openai SDK 示例（非流式 + 流式）
  - `examples/curl_chat.cmd`：curl 示例（已处理中文编码）
  - `examples/batch_process.py`：**批量处理工具**，把一整个目录的代码/文本交给 AI 处理，结果存成 `.md`
  - `examples/auto_agent.py`：**自动改代码 agent**，让 AI 自己读文件、改文件（dry-run 预览 + 自动备份）

**不支持的（别在这些地方浪费时间）：**

| 不支持的能力 | 说明 |
|---|---|
| 原生 `tools` / function calling | Notion 网页接口不提供原生工具调用，Cursor 的 Agent 这类客户端用不了；但 `examples/auto_agent.py` 用「文本 JSON 协议」在本地执行读写文件，能实现**有限的自动改代码** |
| 图片 / 文件附件 | ✅ **已支持**（2026-10 实测）：OpenAI 的 `image_url` / `file` part、Anthropic 的 `image` / `document` block；支持本地文件（base64）、`data:` URL、公网 URL。图片/PDF 走 `attachment` step，文本类文件（txt/md/csv/json/代码等）走 `computer-file` step。限制见 [5.8](#58-发图片--文件附件) |
| `temperature`、`max_tokens` 等采样参数 | 会被忽略，不生效 |
| `/v1/embeddings`、`/v1/images`、`/v1/audio` | 未实现 |
| 给别的电脑/手机远程使用 | 默认只绑定 `127.0.0.1`，而且**强烈不建议**改成对其他设备开放 |

关于流式：代理是"伪流式"（`streamMode: "pseudo"`）——等 Notion 生成完整回答后分块推送，
所以可能先停顿一会儿、再一次性出现内容，不是逐字打字效果，但客户端兼容性最好。

---

## 2. 准备工作

| 需要 | 说明 |
|---|---|
| Windows 10 / 11 | 本项目在 Windows 上开发和验证 |
| Node.js 18+ | 运行代理；当前机器 Node v25 可用。官网：<https://nodejs.org/> |
| Python 3.10+ | 仅刷新登录态时用；当前机器 Python 3.12 可用 |
| Edge 浏览器 | 登录 <https://app.notion.com>，代理从它的登录态里取 cookie |
| Windows 自带 `curl.exe` | 代理访问 Notion 时用它（TLS 指纹问题），Win10/11 一般自带 |
| `requests`、`websocket-client` | 仅刷新登录态的脚本需要；缺了就执行 `pip install requests websocket-client` |

代理本身是 **零 npm 依赖** 的单文件程序：**不需要** `npm install`，也**不需要**编译。

---

## 3. 快速开始（3 步）

### 第 1 步：让 Edge 带调试端口启动，并登录 Notion

1. **完全关闭 Edge**（包括所有窗口；建议在任务管理器里确认没有 `msedge.exe` 残留）。
2. 按 `Win + R`，输入下面命令并回车：

   ```bat
   "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --remote-debugging-port=9222
   ```

   如果提示找不到文件，换成 `"C:\Program Files\Microsoft\Edge\Application\msedge.exe" --remote-debugging-port=9222` 再试。
3. 在这个 Edge 里打开 <https://app.notion.com>，确认已经登录（能看到你的工作区）。
4. 验证调试端口是否生效（可选）：在 cmd 里运行

   ```bat
   netstat -ano | findstr :9222
   ```

   能看到 `LISTENING` 就说明可以。

> 说明：Edge 已经在运行时再加 `--remote-debugging-port` 不生效，必须先把 Edge 完全关掉再执行上面的命令。
> 以后 Edge 正常双击打开也没关系，只要**需要刷新登录态时**用调试端口方式启动即可。

### 第 2 步：刷新登录态（首次或过期时）

双击项目根目录的：

```text
update-cookie.cmd
```

它会运行 `python tools\cdp_cookie_extract.py`，从本机 Edge 读取 Notion cookie，
写入 `secret\credential.json`。看到 `RESULT: OK` 就是成功。

> 只在本机读取，不会打印 token，不会上传任何东西。
> 如果之前已经成功过、代理也正常，可以跳过这一步。

### 第 3 步：启动代理

双击项目根目录的：

```text
start-proxy.cmd
```

会打开一个黑色窗口，首次启动会自动发现 spaceId 和模型列表，稍等几秒，看到类似下面的内容就成功了：

```text
就绪 | space=xxxxxxxx… | 模型 36 个 | 接口 http://127.0.0.1:8787/v1
本地 API 未设置密钥（config.json.apiKey），任何 key 均可调用
```

**这个窗口要一直开着**——关掉窗口 = 停止代理，客户端就连接不上了。
可以最小化。想停止代理：在窗口里按 `Ctrl + C`，或直接关窗口。

如果你更喜欢命令行，也可以：

```bat
cd /d C:\你的路径\ai-proxy
node notion-proxy.js                  :: 启动
node notion-proxy.js --check          :: 只校验登录态、列出模型，然后退出（不启动服务）
node notion-proxy.js --port 9000      :: 临时换端口启动
```

### 第 4 步（可选）：用 exe 运行（不需要装 Node）

如果不想安装 Node.js，也可以直接用打包好的 `notion-proxy.exe`：

1. 双击项目根目录的 `notion-proxy.exe`。
2. 看到 `就绪` 日志后，客户端照常连接 `http://127.0.0.1:8787/v1`。

注意事项：

- `notion-proxy.exe` 是单文件打包版（约 91MB），**不需要安装 Node.js**；
- exe 必须和 `config.json`、`secret\`、`tools\` 放在**同一个目录**；
- **不要同时开两份代理**（例如先开了 `start-proxy.cmd` 又双击 exe）：端口 8787 会冲突，后启动的会失败；
- 需要重新构建时，运行 `tools\build-exe.ps1`；
- 这是自己构建的**未签名程序**，Windows Defender 可能误报；postject 注入时提示签名异常也属正常现象。

登录态：代理内置 CDP 刷新（需要 Edge 以 `--remote-debugging-port=9222` 启动），打开 exe 前确保 Edge 已登录 Notion；也可以先运行 `notion-proxy.exe --refresh` 手动刷新。

---

## 4. 验证是否启动成功

按顺序做，都通过就说明整条链路通了：

### 4.1 健康检查

浏览器打开 <http://127.0.0.1:8787/health>，应该看到类似：

```json
{
  "ok": true,
  "upstream": "https://app.notion.com",
  "spaceId": "xxxxxxxx…",
  "models": 36,
  "cookieOk": true,
  "uptimeSeconds": 12
}
```

- `ok: true`：代理活着
- `cookieOk: true`：登录态有效
- `models`：可用模型数量

### 4.2 模型列表

浏览器打开 <http://127.0.0.1:8787/v1/models>，能看到 `claude-sonnet4.6`、`gpt-5.6-sol` 等一堆 id。

### 4.3 真正发一条对话

**推荐**：双击或运行 `examples\curl_chat.cmd`（已处理中文编码，最省事）：

```bat
examples\curl_chat.cmd
```

它会依次调用 `/health`、`/v1/models`、`/v1/chat/completions`，最后返回一段中文回答。

**或者**用 Python SDK 示例（需要先 `pip install openai`）：

```bat
python examples\openai_python.py
```

**或者**用一条最简单的英文 curl 命令（手动在 cmd 里敲时，中文容易乱码，先用英文验证）：

```bat
curl.exe http://127.0.0.1:8787/health
curl.exe http://127.0.0.1:8787/v1/models
```

---

## 5. 在客户端里怎么填

通用规则就三条：

| 配置项 | 填什么 |
|---|---|
| **Base URL / API 地址** | `http://127.0.0.1:8787/v1`（**末尾要带 `/v1`**，不要填到 `/chat/completions`） |
| **API Key / 密钥** | 随便填一个非空值，例如 `sk-local`（本地代理默认不校验；`config.json` 里设置了 `apiKey` 才校验） |
| **模型 ID** | 从 `/v1/models` 查，或手动填 `claude-sonnet4.6` 等；不要填中文显示名 |

### 5.1 Cherry Studio

1. 设置 → 模型服务 → 添加服务商（类型选 **OpenAI**，名称随意，如 `Notion`）。
2. API 地址 / Base URL 填 `http://127.0.0.1:8787/v1`。
3. API 密钥填 `sk-local`。
4. 在「模型」里点 **添加模型**，手动填模型 id（如 `claude-sonnet4.6`、`gpt-5.6-sol`）；
   如果客户端支持「从 API 获取模型列表」，也可以直接拉取 `/v1/models`。
5. 新建对话，选这个服务商和你添加的模型，发消息即可。

### 5.2 Cursor

1. 打开 Cursor 设置（`Ctrl + Shift + J`）→ **Models**。
2. 填 **OpenAI API Key**：`sk-local`。
3. 打开 **Override OpenAI Base URL**，填 `http://127.0.0.1:8787/v1`。
4. 点 **+ Add model**，填 `claude-sonnet4.6` 等模型 id，并勾选启用。
5. 注意：Cursor 的 **Agent / Composer / 自动改代码** 依赖工具调用（tools），本代理不支持，可能报错；
   建议只在普通 Chat 对话里使用自定义模型。补全（Tab 补全）功能保持 Cursor 默认模型即可。

### 5.3 任意 OpenAI SDK（以 Python 为例）

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8787/v1",
    api_key="sk-local",          # 本地代理不校验，随便填非空
    timeout=600.0,               # Notion 生成可能较慢，建议给足超时
)

resp = client.chat.completions.create(
    model="claude-sonnet4.6",
    messages=[{"role": "user", "content": "你好，用三句话介绍一下你自己"}],
)
print(resp.choices[0].message.content)

# 流式
stream = client.chat.completions.create(
    model="claude-sonnet4.6",
    messages=[{"role": "user", "content": "写一句中文欢迎语"}],
    stream=True,
)
for chunk in stream:
    delta = chunk.choices[0].delta.content or ""
    print(delta, end="", flush=True)
```

完整可运行版本见 [`examples/openai_python.py`](examples/openai_python.py)。

### 5.4 其他支持 OpenAI 的软件

只要软件允许自定义 OpenAI 的 **Base URL + API Key + 模型名**，就按上面三条填。
注意有些软件会把 Base URL 自动拼成 `/v1/chat/completions`，所以这里填到 `/v1` 为止。
个别软件（例如某些旧版）要求填完整接口地址，那就填 `http://127.0.0.1:8787/v1/chat/completions`。

### 5.5 Codex CLI

两种接法：

**方式 A：一键启动脚本**

```bat
examples\codex-notion.cmd exec "用一句话解释这个项目"
```

脚本会自动使用 `opus-5.5` 模型和 Responses 接口（`wire_api = "responses"`）。

**方式 B：cc-switch**

在 cc-switch 里选择供应商「Notion AI (本地反代)」即可（详见 5.7）。

> 注意：Codex CLI 只支持 Responses 接口；本代理不支持工具调用，所以 Codex 只能当聊天用，不能自动改文件/跑命令。

### 5.6 Claude Code

**方式 A：一键启动脚本**

```bat
examples\claude-code.cmd -p "用一句话解释这个项目"
```

脚本会自动配置 Anthropic 端点和本地 key；模型可以用 `--model opus` 或 `--model opus-5.5`。

**方式 B：cc-switch**

在 cc-switch 里选择 Claude 供应商「Notion AI (本地反代)」；模型同样用 `--model opus` 或 `opus-5.5`。

> 同样只能当聊天用：Claude Code 的 Agent/工具能力不可用。

### 5.7 cc-switch

cc-switch 里已经内置两个供应商（Claude + Codex），都指向本地 `http://127.0.0.1:8787`：

- **Claude**：走 cc-switch 的本地代理 `15721` → 本代理 `8787`；
- **Codex**：直接指向本地 8787 的 Responses 接口。

注意：Claude 的「自动故障转移」已经关闭（否则会切到旧的 401 供应商）；如果要在 cc-switch 里重新打开，请自行评估风险。

### 5.8 发图片 / 文件（附件）

代理已实现 Notion 网页 AI 的附件上传（2026-10 抓包逆向后完整打通），客户端只要按标准协议传附件即可：

**OpenAI 协议（`/v1/chat/completions`）**

```python
import base64
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="sk-local", timeout=600.0)
b64 = base64.b64encode(open("screenshot.png", "rb").read()).decode()

resp = client.chat.completions.create(
    model="opus-5.5",
    messages=[{
        "role": "user",
        "content": [
            {"type": "text", "text": "这张截图里有什么问题？"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ],
    }],
)
print(resp.choices[0].message.content)
```

文本/代码文件用 `file` part（UTF-8 文本按 base64 传，或直接传 `file_data`）：

```python
{"type": "file", "file": {"filename": "main.py", "file_data": base64.b64encode(src).decode(), "mime_type": "text/plain"}}
```

**Anthropic 协议（`/v1/messages`，Claude Code 用）**

```python
{"type": "image",   "source": {"type": "base64", "media_type": "image/png", "data": b64}}
{"type": "document","source": {"type": "base64", "media_type": "application/pdf", "data": b64}}
```

**行为说明**

- 图片、PDF、Office 文档 → `attachment` step，模型用视觉/文档解析直接读；
- 文本类文件（`.txt .md .csv .json .py .js .ts …`）→ `computer-file` step，模型用 computer 工具读取；
- 附件只取**最后一条 user 消息**里的；同一个请求里可以带多个附件（顺序保留）；
- 单文件上限 `attachmentMaxBytes`（默认 14MB）；Notion 签名策略本身允许到 20MB；
- **会话绑定**：附件 fileUrl 与 thread 绑定，跨 thread 引用会「图片加载不出来」，所以代理在需要时会先在 Notion 建一个空 thread（`saveTransactionsFanout`）再上传——`threadMode=new` 也能正常用附件，只在 Notion 里多一条正常对话，不会多建无用会话；
- `thread_id` 显式指定时附件会直接传到该会话，适合「同一个 Notion 对话里连续追问同一张图」。

> 附件会原样上传到 Notion 的存储（S3），**不要传密钥、客户隐私、/未公开合同**；`secret/` 目录下的凭据文件更不要作为附件发送。

---

## 6. 模型怎么选

模型列表是**动态**的（来自你自己的 Notion 账号），完整列表请以 **`/v1/models`** 为准：

```bat
curl.exe http://127.0.0.1:8787/v1/models
```

或者运行 `node notion-proxy.js --check`，它会打印每个模型的「对外 id / Notion 内部代号 / 显示名」。

常见对外 id（可直接填到客户端的模型框里）：

| 模型 id | 特点 / 建议 |
|---|---|
| `claude-sonnet4.6` | **默认，日常首选**，速度和质量的平衡点 |
| `claude-sonnet5` | 更强的 Sonnet，写代码更稳，稍慢 |
| `claude-opus4.6` / `claude-opus4.7` / `claude-opus4.8` / `claude-opus5` | 最强一档，适合复杂推理、长代码、难题；更慢 |
| `claude-haiku4.5` | 轻量快速，适合简单任务 |
| `gpt-5.6-sol` / `gpt-5.6-terra` / `gpt-5.6-luna` | GPT-5.6 系列不同风格 |
| `gpt-5.5` / `gpt-5.4` | 上一代 GPT，稳定 |
| `gpt-6-astra` | 更新的 GPT 系 |
| `gemini-3.1pro` | Google 强模型，长上下文 |
| `gemini-3.5flash` / `gemini-3flash` | 快、便宜，适合批量摘要 |
| `kimi-2.7` | 中文友好 |
| `grok-4.3` / `grok-build0.1` / `spacexai-4.5` | Grok 系列 |
| `deepseek-v4pro` | DeepSeek 强模型 |
| `glm-5.2` | GLM 系列 |
| `fable-5` | 需要更高套餐（Business/Enterprise）的账号才可用，不可用时换别的 |

补充说明：

- **也可以用 Notion 内部代号**，例如 `almond-croissant-low`（就是 `claude-sonnet4.6`）、`orange-mousse`（`gpt-5.6-sol`），
  由代理自动映射。
- **模型 id 不存在时**，代理会回退到默认模型（`config.json` 的 `defaultModel`），并在代理窗口打印一条日志；
  所以如果回答"不对劲"，先看窗口日志确认实际用了哪个模型。
- **个别 Gemini 模型**（如 `gemini-3.5flash`）在 Notion 内部走的是另一种对话类型，如果遇到异常/空回答，换 `gemini-3.1pro` 或 Claude 系再试。
- 模型是否可用、效果如何，取决于你的 Notion 套餐和 Notion 服务端状态；列表会随账号/时间变化。

---

## 7. 批量处理文件（batch_process.py）

这是给非技术用户准备的**批量工具**：把一整个目录的代码/文本文件逐个发给本地代理处理，
结果保存成 Markdown（`.md`）文件，并保留原来的目录结构。

它**只依赖 Python 标准库**，不需要 `pip install` 任何东西。前提只有一个：**代理已经启动**。

### 7.1 最简单的用法

在 `ai-proxy` 目录下打开 cmd，运行：

```bat
python examples\batch_process.py --input src --prompt "请用中文解释这个文件的作用，并给出改进建议：{content}" --out-dir out
```

- `--input src`：处理 `src` 目录（递归所有子目录）
- `--prompt "..."`：提示词模板，**必须包含 `{content}`**（文件内容），可选 `{path}`（相对路径）
- `--out-dir out`：结果写到 `out` 目录

处理中会看到进度条式日志：

```text
[################----]  80%  8/10  成功  src\app.py  (6.2s，1280 字符)
[################----]  90%  9/10  失败  src\bad.py  (0.3s)
        原因：HTTP 502：Notion 上游异常（HTTP 502）。

[汇总] 共 10 个文件：成功 9 个，失败 1 个，跳过 0 个
[汇总] 输出目录：C:\...\ai-proxy\out
```

结果文件示例：`src\app.py` → `out\src\app.py.md`，内容开头会标注源文件、模型和生成时间。

### 7.2 参数说明

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--input` | `.`（当前目录） | 文件、目录（递归）、或 glob，例如 `src` 或 `"src/**/*.py"` |
| `--prompt` | 必填 | 提示词模板，必须含 `{content}`，可选 `{path}` |
| `--model` | `claude-sonnet4.6` | 使用的模型 id（见 `/v1/models`） |
| `--out-dir` | `./out` | 输出目录；同名文件会被覆盖 |
| `--concurrency` | `1` | 同时处理几个文件，**最大 3**；建议 1~2（见下方注意） |
| `--base-url` | `http://127.0.0.1:8787/v1` | 代理地址；换端口时改这里 |
| `--api-key` | `sk-local` | 代理默认不校验，随便填非空 |
| `--max-chars` | `120000` | 单个文件最多读取的字符数；超长会截断**并在结果里标注** |
| `--exts` | `.py,.js,.ts,.java,.go,.rs,.c,.cpp,.h,.md,.txt,.json,.yaml,.yml` | 目录扫描时处理的扩展名；传 `""` 表示不限制 |
| `--timeout` | `600` | 单次请求超时秒数 |
| `--thread-mode` | `single` | Notion 会话模式：`single`=复用同一个会话；`new`=每个文件新建会话 |

> **关于 `--thread-mode`**：默认 `single` 会复用同一个 Notion 会话，避免每个文件都在 Notion 里新建对话；代价是会话上下文会累积，轮数到 **30 轮**或单次输入超过 **30 万 token** 时会自动换一个新会话。`new` 则是每个文件新建会话，隔离更彻底，但会在 Notion 里堆积大量对话。

### 7.3 常用示例

```bat
:: 1) 批量解释一个目录里的代码
python examples\batch_process.py --input src --prompt "逐段解释这段代码做了什么，用中文输出：{content}"

:: 2) 只处理 Python / TypeScript，并发 2
python examples\batch_process.py --input src --exts .py,.ts --concurrency 2 --prompt "审查代码，指出问题和改进点（文件 {path}）：{content}" --out-dir out

:: 3) 用 glob 指定文件（注意 cmd 里要加引号）
python examples\batch_process.py --input "docs/**/*.md" --prompt "把下面的文档压缩成 5 条要点：{content}" --out-dir out

:: 4) 只处理单个文件
python examples\batch_process.py --input README.md --prompt "请总结成 5 条要点：{content}"

:: 5) 换模型、换端口
python examples\batch_process.py --input src --model claude-opus5 --base-url http://127.0.0.1:9000/v1 --prompt "深度审查：{content}"
```

### 7.4 使用注意

- **格式要求**：`--prompt` 必须包含 `{content}`；`{path}` 会被替换成相对路径（如 `src/app.py`）。
- **目录扫描会自动跳过**：`.git`、`node_modules`、`__pycache__`、`.venv`/`venv`、`.tmp`、`secret/`、`ref/`、`out/` 以及 `--out-dir` 目录本身。
- **显式文件和 glob 不再按 `--exts` 过滤**（你指定什么就处理什么）；目录扫描才按 `--exts` 过滤。
- **编码**：优先按 UTF-8 读取，失败自动回退 GBK；二进制文件和空文件会跳过并提示。
- **单个失败不影响其他文件**：429/5xx/连接类错误会自动重试 3 次；失败清单会在最后汇总。
- **Ctrl + C 随时停止**：已经写出的结果会保留；退出码 `130` 表示用户中断。
- **超长文件**：超过 `--max-chars`（默认 12 万字符）会截断，并在结果 md 开头标注「已截断」。想更完整处理请调大该值，但注意代理 `config.json` 的 `maxBodyBytes` 是 32MB。
- **并发别太猛**：代理 `config.json` 里 `maxConcurrent` 默认为 2（同时处理的请求数），批量工具设 3 时多出来的会排队；为了降低 Notion 限流风险，建议 `--concurrency 1`，最多 2。
- **会话隔离 vs 会话数量**：想完全隔离文件用 `--thread-mode new`；想减少 Notion 会话数用默认 `single`。
- **提示词里有中文时**：cmd 里用双引号把整个 `--prompt` 包起来即可；**不要在提示词里再嵌双引号**（会截断参数）。PowerShell 里建议用单引号。
- **结果会发给谁**：文件内容会通过本机代理发给 Notion AI。**不要把含密码、密钥、客户隐私的目录作为输入**（工具会自动跳过 `secret/`，但这只是兜底，不保证覆盖所有敏感目录）。

### 7.5 自动改代码 agent（auto_agent.py）

`batch_process.py` 是「文件进、结果出」；如果你想让 AI **直接改文件**，用 `auto_agent.py`：

```bat
:: 先预览（默认 dry-run，只打印 diff，不写盘）
python examples\auto_agent.py --task "把 greet.py 的返回值改成 hi + name" --dir src

:: 确认没问题再真正写盘（修改前自动备份到 src\.autoedit-backup\时间戳\）
python examples\auto_agent.py --task "把 greet.py 的返回值改成 hi + name" --dir src --apply
```

工作原理：模型没有原生工具调用，脚本用「文本 JSON 协议」模拟——模型每轮只输出一个
`{"tool": "read_file", ...}` 这样的 JSON 代码块，脚本在本地执行（列目录 / 读文件 / 覆盖写 / 精确替换），
把结果回传，直到模型调用 `finish`。

| 参数 | 默认 | 说明 |
|---|---|---|
| `--task` | 必填 | 要做的修改，写得越具体越好 |
| `--dir` | `.` | 只允许修改这个目录内的文件 |
| `--apply` | 关 | 不加 = 只预览 diff；加上 = 真的写盘 |
| `--model` | `opus-5.5` | 模型 id |
| `--max-steps` | `8` | 最多几轮工具调用 |
| `--thread-mode` | `single` | 复用同一个 Notion 会话（省对话数） |
| `--allow-exec` | 关 | 允许模型在 `--dir` 下跑命令（测试/编译检查），**默认关闭** |
| `--exec-timeout` | `120` | 单条命令超时秒数 |
| `--test-cmd` | 空 | **自动测试命令**（固定字符串，如 `python -m pytest -q`）：启动时先跑一次基线，之后每次改完文件自动再跑并把结果回传模型；模型改不了这条命令 |
| `--test-timeout` | `300` | 自动测试命令的超时秒数 |

安全与限制：

- **默认 dry-run**：不加 `--apply` 绝不写盘，先看 diff 再决定。
- 写盘前自动备份；只允许改 `--dir` 内文件；拒绝 `secret/`、`.git/`、`node_modules/` 等；不会删文件。
- **命令执行默认关闭**：只有加 `--allow-exec` 才允许模型跑命令，且带破坏性命令黑名单（删除、格式化、`git push` 等）、超时和输出截断；命令在 `--dir` 目录下执行。
- **上下文会涨**：复用会话时输入 token 每轮大约 +3~4 万，代理在 30 万 token 会自动换新会话；
  脚本每轮都重复任务描述，换了会话也能接着做，但长任务建议拆小、`--max-steps` 别开太大。
- 实测：让模型把 `greet()` 改成 f-string 并加注释，4 轮完成（列目录 → 读文件 → 替换 → finish），diff、备份都正常；
  加 `--allow-exec` 后让模型跑 `python -m py_compile`，退出码 0 后正确 `finish`。
- 会跑测试的用法：

```bat
python examples\auto_agent.py --task "修复 test_math.py 里失败的用例，改完跑 pytest 验证" --dir src --max-steps 10 --apply --allow-exec
```

**推荐用 `--test-cmd` 做「自动测试闭环」**：不用教模型跑命令，脚本会在每次改完后自己跑测试、把结果喂回模型：

```bat
:: 启动时先跑一次 pytest 记录基线，之后每改一个文件就自动重跑，直到全绿再 finish
python examples\auto_agent.py --dir . --apply --test-cmd "python -m pytest -q" ^
  --task "把 greet() 改成返回 hello {name}，让测试全绿" --max-steps 8
```

实测：初始 `2 failed` → AI 读文件 → 改 `greet.py` → 脚本自动 `pytest` 得到 `2 passed` → 模型读到结果后 `finish`，
全程 4 轮、改 1 个文件、无人工干预。`--test-cmd` 是启动时写死的命令，模型无法修改，比让模型自由 `exec` 安全；
它**不需要** `--allow-exec`（模型没有命令执行权，只是脚本按固定命令跑测试）。

更多示例见 [`examples/README.md`](examples/README.md)，或运行：

```bat
python examples\batch_process.py --help
```

---

## 8. 登录态过期怎么办

典型表现：客户端报 **401**，或代理窗口出现：

```text
收到 401，尝试自动刷新 Notion 登录态（CDP）...
```

代理会自动尝试用内置 CDP 刷新一次（需要 Edge 以 `--remote-debugging-port=9222` 启动）；内置方式失败时，会再回退到 `python tools\cdp_cookie_extract.py`。如果还是 401，手动处理：

1. **完全关闭 Edge**（所有窗口，任务管理器确认没有 `msedge.exe` 残留）。
2. 用调试端口重新启动 Edge（`Win + R` 粘贴）：
   ```bat
   "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --remote-debugging-port=9222
   ```
   （路径不对就换成 `C:\Program Files\Microsoft\Edge\Application\msedge.exe`）
3. 在这个 Edge 里打开 <https://app.notion.com>，确认已登录（必要的话重新登录一次）。
4. 双击 `update-cookie.cmd`，看到 `RESULT: OK` 即刷新成功。
5. 回到客户端重试（代理在收到 401 时会重新读取凭据，通常不用重启代理）。

补充：

- 刷新脚本需要 `requests` 和 `websocket-client`，如果报 `ModuleNotFoundError`，执行：
  ```bat
  pip install requests websocket-client
  ```
- 刷新失败时先检查 `netstat -ano | findstr :9222` 有没有输出；没有输出说明 Edge 不是用调试端口启动的。
- **重启电脑后**：通常只需要重新双击 `start-proxy.cmd`；cookie 已保存在 `secret\credential.json` 里，只有出现 401 才需要重新刷新。
- **更换 Notion 账号 / 换工作区**后，需要关闭 Edge → 用调试端口启动 → 登录新账号 → 运行 `update-cookie.cmd`。

---

## 9. 常见问题

### 9.1 报 401

| 现象 | 原因 | 处理 |
|---|---|---|
| 401，错误信息里有「Notion 登录态已失效」 | Notion cookie 过期 | 按上一节刷新登录态（`update-cookie.cmd`） |
| 401，错误信息是「本地 API key 不正确」 | `config.json` 里设置了 `apiKey`，客户端 key 不匹配 | 把客户端的 API Key 改成 `config.json.apiKey` 的值，或把 `apiKey` 改回 `""` 后重启代理 |

### 9.2 报 403

- 错误信息含「Cloudflare 校验」：Notion 边缘节点认为你像机器人。**等几分钟再试**，或换网络出口（切换 Wi-Fi/手机热点），或降低请求频率。
- 错误信息是「Notion 返回 403」：模型不可用（比如账号套餐不支持 `fable-5`）或权限问题。**换个模型**（如 `claude-sonnet4.6`）再试。
- 频繁出现 403 说明风控在收紧：**停一会儿，降并发，别多客户端同时狂刷**。

### 9.3 报 429（限流）

Notion 对 AI 请求有限流。代理内部已经会自动重试 + 退避，仍失败就：

1. 等 **10~30 秒**（或几分钟）再试；
2. 批量工具把 `--concurrency` 降到 `1`；
3. 关掉其他正在用的客户端/脚本，避免同时请求；
4. 降低文件数量，分批处理。

### 9.4 报 502 / 503

上游 Notion 临时异常或返回空内容，代理会重试。处理：稍等再试；换模型；如果某个文件特别大，先用 `--max-chars` 截断。

### 9.5 连接被拒绝 / 客户端提示 `ECONNREFUSED` / 无法连接

1. **代理没启动**：双击 `start-proxy.cmd`，看到「就绪」。
2. **代理窗口被关了**：重新启动，保持窗口开着（可最小化）。
3. **端口不对**：默认 `8787`；如果你用了 `--port 9000` 或在 `config.json` 改过端口，客户端 Base URL 要同步改。
4. **启动失败**：看代理窗口里的报错。代码为 1 且提示「启动失败」，通常是登录态或网络问题。

### 9.6 端口 8787 被占用（`EADDRINUSE`）

查看谁占用了端口：

```bat
netstat -ano | findstr :8787
```

最后一列是 PID，确认不是代理本身后结束它（谨慎操作）：

```bat
taskkill /PID 12345 /F
```

或者换个端口启动：

```bat
node notion-proxy.js --port 9000
```

然后把客户端的 Base URL 改成 `http://127.0.0.1:9000/v1`。

### 9.7 客户端"检查连接"失败 / 拉不到模型列表

- Base URL 必须是 `http://127.0.0.1:8787/v1`（结尾 `/v1`）；填到 `/chat/completions` 的会失败。
- API Key 不能留空，随便填一个非空值（如 `sk-local`）。
- 有的客户端不支持自动拉取模型：**手动添加模型 id**（如 `claude-sonnet4.6`）即可。
- 可以用浏览器打开 <http://127.0.0.1:8787/v1/models> 先确认代理本身正常。

### 9.8 回答为空 / 很慢 / 突然卡住

- Notion 生成本身可能较慢（复杂问题几十秒很正常），客户端超时时间建议调大（如 600 秒）。
- 伪流式会"先卡住、后一次性出字"，属正常现象。
- 代理 `config.json` 里 `requestTimeoutMs` 默认 300000（5 分钟）；更长任务可调大后重启代理。
- 空回答时换模型重试；Gemini 系个别模型可换 `gemini-3.1pro`。

### 9.9 模型名报错 / 回答风格不对

- 确认模型 id 是从 `/v1/models` 复制的，别填中文显示名。
- 填错模型 id 时代理会**回退到默认模型**并在窗口打日志（`模型 "xxx" 未找到，回退到 ...`），所以不会报错但结果可能不是你要的模型。
- 想固定默认模型：修改 `config.json` 的 `defaultModel`，重启代理。

### 9.10 中文乱码

- 用我们提供的 `.cmd` 脚本（`start-proxy.cmd`、`update-cookie.cmd`、`examples\curl_chat.cmd`），它们已自动执行 `chcp 65001`。
- 手动在 cmd 里粘贴含中文的 JSON 容易乱码：先用英文验证，或直接运行 `examples\curl_chat.cmd`。
- 批量工具用 UTF-8 保存结果，用支持 UTF-8 的编辑器打开（VS Code、Notepad++ 等）。

### 9.11 启动时报「找不到 node / python / curl」

- `node`：安装 Node.js 18+ 后重开 cmd 窗口（或重启电脑）。
- `python`：安装 Python 3 时勾选 **Add python.exe to PATH**。
- `curl`：Windows 10/11 自带 `C:\Windows\System32\curl.exe`；极老系统可手动安装后再试。

### 9.12 代理窗口里那些日志是什么意思

```text
[12:00:01] 校验 Notion 登录态...          → 启动时检查 cookie
[12:00:03] 登录态有效                     → 正常
[12:00:03] 就绪 | space=... | 模型 36 个  → 启动成功
[12:01:10] chat model=claude-sonnet4.6(almond-croissant-low) stream=false content=520字 耗时=8.2s
                                          → 每次对话的处理结果
[12:05:00] 收到 401，尝试自动刷新...       → cookie 过期，正在自动刷新
[12:06:00] 重试第 1 次（1400ms 后）...     → Notion 限流/异常，代理在重试
```

如果客户端报错，先看这个窗口的最后几行，基本能定位原因。

---

### 9.13 附件上传失败 / 模型说「图片加载不出来」

- **`413` / 提示附件过大**：调大 `config.json` 的 `attachmentMaxBytes`，但不要超过 20MB（S3 签名策略上限）；
- **模型说看不到附件内容**：确认附件是放在**最后一条 user 消息**里（历史消息里的附件不会重新上传）；
- **PDF/图片能读、txt 读不到**：文本类文件走 `computer-file` + computer 工具读取，需要模型支持工具调用（Opus/GPT 系列都可以）；
- **`502` 附件上传失败**：多半是登录态过期或附件接口临时异常，先跑 `update-cookie.cmd`，再重试；
- **附件会留在 Notion 存储里**：想彻底删除请到 Notion 对话里删掉那条消息，或在 Notion 设置里清理上传文件。

## 10. 安全与风险提示

**请务必阅读。** 这个工具把浏览器里的登录态变成了 API，属于本机自用工具，有明确风险边界。

### 10.1 凭据安全

- `secret\credential.json` 里是本机 Notion 的 `token_v2` 等 cookie，**等同于你的账号登录凭据**：
  拿到它的人可以直接操作你的 Notion。
- **不要**把 `secret\` 目录发给任何人，不要截图，不要上传网盘/GitHub/聊天软件。
  项目已在 `.gitignore` 中忽略 `secret/`。
- 不要在有录屏/共享屏幕的场合打开 `secret\` 里的文件。
- 如果怀疑泄露：立刻在 Notion 里退出所有设备（退出登录）→ 重新登录 → 重新运行 `update-cookie.cmd`。

### 10.2 网络与暴露面

- 代理默认只监听 `127.0.0.1`，**只有本机能访问**。
- **不要**把 `config.json` 的 `bind` 改成 `0.0.0.0`，也不要用内网穿透/反代把它暴露到公网：
  这等于把你的 Notion 账号借给所有人用，会立刻被滥用、限流甚至封号。
- 默认不校验 API Key（`apiKey: ""`）。如果本机有其他不可信程序/用户，建议在 `config.json` 里
  设置一个 `apiKey`（例如 `"apiKey": "my-secret-key"`），客户端同步使用该 key。

### 10.3 Notion 使用条款与风控

- 本工具通过**模拟网页请求**使用 Notion AI，这与直接使用网页版的行为不完全一致，
  **可能违反 Notion 的使用条款**。
- Notion 可能检测异常请求并触发：限流（429）、Cloudflare 校验（403）、功能临时禁用，极端情况下**账号被限制**。
- 本项目**不承诺稳定**：Notion 更新接口后，代理可能随时失效，需要重新分析适配。
- 建议：**个人学习/自用，低频率使用**；不要用于自动化刷量、对外提供服务、商业用途；
  不要用公司/共享账号试；重要账号请自行评估风险，或使用小号。
- 你的对话内容会**经本机代理发送到 Notion 服务器**，请遵守你与 Notion 之间的隐私约定。

### 10.4 批量处理的数据安全

- `examples/batch_process.py` 会把输入文件内容发送给 Notion AI。
- `examples/auto_agent.py` 同样会把读到的文件内容发给 Notion AI，并且在你加 `--apply` 后会**直接把模型给的修改写进文件**：
  默认先 dry-run 看 diff，确认再 `--apply`；每次写盘前都有备份（`.autoedit-backup/`）。
- 不要把 `.env`、密钥文件、客户数据、公司机密目录作为输入。
- 工具会跳过 `secret/`、`.git`、`node_modules/`、`ref/` 等常见目录，但**这只是兜底**，
  请自己确认 `--input` 指向的目录里没有敏感内容。

### 10.5 合规提醒

请仅在你有权使用的 Notion 账号和网络环境下使用本工具，遵守 Notion 服务条款及所在地区的法律法规。
因使用本工具产生的账号、数据、合规风险由使用者自行承担。

---

## 11. 文件结构说明

```text
ai-proxy/
├─ notion-proxy.js            # 核心代理（Node 单文件，零 npm 依赖）
├─ notion-proxy.exe            # 打包好的单文件程序（可重新构建，约 91MB）
├─ config.json                # 配置（端口、默认模型、并发、日志等）
├─ start-proxy.cmd            # 一键启动（双击）
├─ update-cookie.cmd          # 刷新 Notion 登录态（双击）
├─ README.md                  # 本文件
├─ PROJECT-INTRO.md            # 项目介绍（面向想了解/维护项目的人）
├─ .gitignore                 # 忽略 secret/、node_modules/、日志等
├─ examples/
│  ├─ openai_python.py        # openai SDK 示例（非流式 + 流式）
│  ├─ attach_image.py         # 图片/文件附件示例（OpenAI + Anthropic 两种协议）
│  ├─ curl_chat.cmd           # curl 示例（含中文请求）
│  ├─ batch_process.py        # 批量处理工具（仅标准库）
│  ├─ auto_agent.py           # 自动改代码 agent（文本 JSON 工具协议，dry-run + 备份）
│  ├─ claude-code.cmd         # Claude Code 启动脚本（Anthropic 接口）
│  ├─ claude-code-settings.json # Claude Code 的 Base URL/模型配置
│  ├─ codex-notion.cmd        # Codex CLI 启动脚本（Responses 接口）
│  ├─ codex-notion.config.toml # Codex profile 配置
│  └─ README.md               # 示例说明
├─ tools/
│  ├─ build-exe.ps1            # Node SEA 打包脚本（重新生成 exe）
│  ├─ cdp_cookie_extract.py   # 从 Edge 调试端口提取 cookie（刷新登录态用）
│  ├─ extract_cookie.py       # 备用：从本机浏览器配置离线读 cookie
│  └─ notion_probe.py         # 调试用：探测 Notion 内部接口
├─ secret/                    # 本机登录凭据（自动生成，绝对不要外传）
│  ├─ credential.json         #   cookie / token_v2
│  ├─ workspace.json          #   spaceId、模型列表缓存
│  └─ ...                     #   探测日志等
├─ docs/                      # 内部技术资料（upstream 协议说明）
├─ ref/                       # 参考源码（体积大，可忽略/删除）
├─ .tmp/                      # 临时文件
└─ out/                       # 批量处理输出（自动创建，可删除）
```

**哪些可以删：** `out/`（批量输出）、`.tmp/`、`ref/`（参考源码）；删了不影响使用。
**哪些绝对不能外传：** `secret/` 整个目录。

> **本仓库不包含** `secret/`（本机登录凭据）、`ref/`（第三方参考源码）、`.tmp/`（本机临时文件）和
> `notion-proxy.exe`（91MB 打包产物），它们在 `.gitignore` 里；exe 请自己用 `tools/build-exe.ps1` 构建。

---

## 12. 附录：接口与配置

### 12.1 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 健康检查（无需 key） |
| `GET` | `/v1/models`（也支持 `/models`） | 模型列表（OpenAI 格式） |
| `POST` | `/v1/chat/completions`（也支持 `/chat/completions`） | 聊天补全，支持 `stream` |

请求体兼容 OpenAI 的 `model` / `messages` / `stream`。另外支持几个可选项：

| 可选字段 | 说明 |
|---|---|
| `web_search: true` | 让 Notion 联网搜索（默认关闭，可在 `config.json` 里打开全局默认） |
| `reasoning_effort: "low" \| "medium" \| "high"` | 思考强度，仅部分模型支持；不支持时忽略 |
| `thread_id: "..."` | 复用某个 Notion 会话线程（高级用法） |

`temperature`、`max_tokens`、`tools` 等字段会被忽略（Notion 网页接口不暴露）。

### 12.2 config.json 常用配置

改完需要**重启代理**才生效。一般只需关注前几项：

| 字段 | 默认值 | 说明 |
|---|---|---|
| `port` | `8787` | 监听端口 |
| `bind` | `127.0.0.1` | 监听地址；**不要改成 0.0.0.0** |
| `apiKey` | `""` | 留空 = 不校验任何 key；设置后客户端必须使用该 key |
| `defaultModel` | `"claude-sonnet4.6"` | 默认模型；模型 id 未知时也回退到它 |
| `maxConcurrent` | `2` | 同时处理的上游请求上限（批量工具并发别超过太多） |
| `threadMode` | `"new"` | 会话模式：`"new"` 每个请求新建会话；`"single"` 复用同一个会话 |
| `threadReuseMaxTurns` | `30` | `single` 模式下同一会话最多复用轮数，超过换新会话 |
| `threadReuseMaxInputTokens` | `300000` | `single` 模式下输入超过该 token 数就换新会话 |
| `enableAttachments` | `true` | 是否允许图片/文件附件（走 Notion 专用上传接口，见 5.8） |
| `attachmentMaxBytes` | `14680064` | 单个附件大小上限（14MB）；Notion 签名策略本身允许到 20MB |
| `requestTimeoutMs` | `300000` | 单次上游请求超时（毫秒） |
| `useWebSearch` | `false` | 是否默认开启联网搜索 |
| `includeReasoning` | `true` | 是否在响应里附带模型的思考内容（`reasoning_content`） |
| `streamMode` | `"pseudo"` | 流式模式（伪流式，兼容性最好） |
| `autoRefreshCookie` | `true` | 401 时是否自动尝试刷新登录态 |
| `validateOnStart` | `true` | 启动时是否校验登录态 |
| `debugSaveRaw` | `false` | 排查问题时设为 `true`，会把原始流保存到 `secret/debug/`（可能含对话内容，注意隐私） |
| `curlPath` | `"curl.exe"` | 访问 Notion 用的 curl 路径 |
| `python` | `"python"` | 刷新登录态用的 python 命令 |

### 12.3 命令行参数

```bat
node notion-proxy.js                  :: 启动服务
node notion-proxy.js --check          :: 只校验登录态并打印可用模型，然后退出
node notion-proxy.js --port 9000      :: 临时指定端口
node notion-proxy.js --refresh        :: 从浏览器调试端口刷新登录态（需要 Edge 以 --remote-debugging-port=9222 启动）
node notion-proxy.js --help           :: 帮助
```

### 12.4 常用命令速查

```bat
:: 启动 / 停止
start-proxy.cmd
:: 停止：在代理窗口按 Ctrl+C，或关窗口

:: 刷新登录态
update-cookie.cmd

:: 检查代理
curl.exe http://127.0.0.1:8787/health
curl.exe http://127.0.0.1:8787/v1/models

:: 发一条中文对话（推荐脚本，编码已处理）
examples\curl_chat.cmd

:: 只校验登录态 + 列出模型
node notion-proxy.js --check

:: 批量处理一个目录
python examples\batch_process.py --input src --prompt "请解释这个文件：{content}" --out-dir out
```

---

**祝使用顺利。** 遇到问题先看代理窗口的日志，再对照本文的[常见问题](#9-常见问题)。
