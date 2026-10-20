# ai-proxy：把 Notion AI 变成 OpenAI / Anthropic 兼容的本地接口

> 一句话：用你自己的 Notion 网页登录态，在本机 `127.0.0.1:8787` 上提供同时兼容 **OpenAI Chat Completions、OpenAI Responses、Anthropic Messages** 的接口，供 Cherry Studio、Cursor、OpenAI SDK、Codex CLI、Claude Code 等客户端调用。它不是破解，也不是共享 API：用的就是你账号在浏览器里已登录的 Notion AI，请求只从本机发出，凭据只落在本机。

| 项目 | 内容 |
|---|---|
| 项目名 | ai-proxy（Notion AI 本地反代） |
| 核心 | `notion-proxy.js`：Node.js 单文件、零 npm 依赖、约 2000 行 |
| 上游 | `POST https://app.notion.com/api/v3/runInferenceTranscript`，Cookie 鉴权，NDJSON 响应 |
| 平台 | Windows 10/11；源码版需 Node 18+；另有免安装 Node 的单文件 exe；系统自带 `curl.exe` |
| 默认地址 | `http://127.0.0.1:8787/v1` |

## 1. 这是什么

Notion 网页版里有一套可用的 AI 模型（Claude、GPT、Gemini、Kimi、DeepSeek、GLM、Grok 等），但只存在于 Notion 的聊天界面，没有公开 API。ai-proxy 把这份"网页里的 AI"翻译成本地标准接口：

1. 从本机 Edge 的登录态获取 Notion Cookie；
2. 把 OpenAI / Anthropic 请求翻译成 Notion 的 `runInferenceTranscript` 请求；
3. 用 Windows 自带 `curl.exe` 发出（TLS 原因见 4.4 节）；
4. 解析 NDJSON 流，再把最终答案包装成客户端认识的响应。

它面向两种读者：想低门槛使用 Notion 模型的人，以及需要接手维护它的人。**定位是个人、本机、低频自用**，不是生产网关，不建议共享或对外服务（见第 9 节）。

## 2. 解决什么问题 / 使用场景

Notion AI 只能在网页里用，而 Cherry Studio、Cursor、各类 SDK、Codex CLI、Claude Code 只认标准接口。ai-proxy 在中间做协议翻译，把同一个账号的 AI 能力开放给这些工具。

| 场景 | 做法 |
|---|---|
| 桌面聊天客户端 | Cherry Studio / Cursor 填 `http://127.0.0.1:8787/v1`，模型从 `/v1/models` 拉取 |
| 脚本与 SDK 实验 | OpenAI Python/Node SDK 直连本地代理，支持流式 |
| CLI 编程工具 | Codex CLI 走 Responses 接口；Claude Code 走 Anthropic Messages |
| 批量处理 | `examples/batch_process.py` 按目录/glob 批量请求，结果写 `.md` |
| 自动改代码（有限） | `examples/auto_agent.py`：用文本 JSON 工具协议让模型列目录、读文件、替换文件，默认 dry-run + 自动备份 |
| 多模型对比 | 同一客户端里切换 Opus 5.5、GPT-6.1、Gemini 3.x、Kimi K3 等 |
| 学习逆向/代理 | `docs/upstream-spec.md` 记录了完整的上游协议分析 |

不适合：依赖**原生**工具调用的 Agent（Cursor Agent、Claude Code 的自动改文件等）、图片输入、多人共享、生产流量、对外售卖 API。这些是上游能力边界，不是配置问题（`examples/auto_agent.py` 的文本协议只覆盖「读文件 / 改文件」这类有限操作，不能执行命令）。

## 3. 整体架构

```text
客户端（Cherry Studio / Cursor / OpenAI SDK / Codex CLI / Claude Code）
        │  HTTP / SSE，仅本机
        ▼
[可选] cc-switch 本地代理 127.0.0.1:15721
        │  转发
        ▼
notion-proxy.js  127.0.0.1:8787
        │  选模型、读登录态、构造 transcript
        │  spawn curl.exe，JSON 从 stdin 写入
        ▼
curl.exe（Windows Schannel TLS）
        │  HTTPS POST
        ▼
app.notion.com /api/v3/runInferenceTranscript
        │  NDJSON：patch-start / patch / record-map
        ▼
notion-proxy.js 解析出正文、reasoning、usage
        │  包装成 OpenAI / Anthropic / Responses 响应
        ▼
客户端

登录态旁路：
Edge（--remote-debugging-port=9222）
        │  CDP Storage.getCookies
        ▼
notion-proxy.js 内置 CDP（或 tools/cdp_cookie_extract.py）→ secret/credential.json
```

三条路径互相独立：**模型请求**走 8787 → curl.exe → Notion；**登录态**走 Edge → CDP → `secret/`；**模型发现**在启动时调 `getSpaces` + `getAvailableModels`，缓存到 `secret/workspace.json`。另外提供 `tools/build-exe.ps1` 打包的单文件 `notion-proxy.exe`，逻辑相同，可免安装 Node 运行。

## 4. 工作原理

### 4.1 鉴权：Cookie，而不是 API Key

上游只认 Notion 网页登录态。代理从 `secret/credential.json` 读 `token_v2`、`notion_user_id`、`notion_browser_id`，拼成 `Cookie` 头；同时带 `x-notion-space-id`、`x-notion-active-user-header`、浏览器 UA、`Origin`/`Referer`、`notion-client-version` 和 `sec-fetch-*` 等指纹头，尽量贴近网页请求。

登录态获取以 CDP 为主：代理内置 Node WebSocket 客户端，读取 Edge/Chrome/Brave 的 `DevToolsActivePort`，连 DevTools WebSocket 调 `Storage.getCookies` 拿 HttpOnly Cookie，过滤 Notion 相关项后写 `secret/credential.json`；启动时若没有凭据会自动尝试，`--refresh` 可手动刷新；`tools/cdp_cookie_extract.py` 保留为 Python 兜底。这样做是为了避开 Chrome/Edge 127+ 的 App-Bound Encryption（v20）——离线解密基本走不通，让浏览器自己交出解密后的值更稳。前提是浏览器以 `--remote-debugging-port=9222` 启动并已登录 Notion。

401 时，代理在 `autoRefreshCookie: true` 下会自动尝试刷新登录态一次（从浏览器调试端口重新读取 Cookie），成功后重载凭据并重试；仍失败则返回 401 并提示刷新方法。刷新后通常不需要重启代理，下一次请求遇到 401 时会再次自动重载。

### 4.2 请求构造

上游接口是 `POST https://app.notion.com/api/v3/runInferenceTranscript`。代理把客户端消息整理成 Notion 的 `transcript`：

- **config**：Notion 内部模型代号（如 `almond-croissant-low`）、`threadType`（默认 `workflow`）、联网搜索开关、可选 `reasoningEffort`；
- **context**：时区 `Asia/Shanghai`、当前时间、`userId`、`spaceId`、`surface`（默认 `workflows`，markdown-chat 模型为 `ai_module`）；
- **user**：`{ type: "user", value: [[文本]], userId, createdAt }`；
- **agent-inference**：历史 assistant 消息，`value` 是 `{type:"text", content}` 块。

顶层含 `traceId`、`spaceId`、`threadId`、`createThread`、`asPatchResponse: true`、`generateTitle: false` 等；`threadId` 默认每请求新生成，因此默认是**每个请求在 Notion 新建一个线程**。

线程策略由 `threadMode` 控制：默认 `"new"`（每请求新建）；设为 `"single"`（或单次请求用 body `thread_mode` / 请求头 `X-Notion-Thread-Mode` 覆盖）时复用同一个 Notion 会话——复用请求发 `createThread:false` + `isPartialTranscript:true`、只带最新一条 user 消息（历史在服务端），内部加锁串行，状态存 `secret/thread-state.json`，到达 `threadReuseMaxTurns` / `threadReuseMaxInputTokens` 或复用失败（400/404/502/503）时自动换新会话重试。批量脚本默认 `single` 以减少 Notion 会话堆积，代价是上下文 token 随轮数增长。

system prompt 由 `config.systemPromptMode` 控制：默认 `prefix` 把 system 内容拼到第一条 user 消息前，绕开 Notion 对独立 system 块的注入检测；`context` 模式则作为额外 context 块发送（更接近原生结构，但未作为默认验证）。`temperature`、`max_tokens`、`tools` 等字段不会生效。

### 4.3 NDJSON 流解析

Notion 返回 **NDJSON**（每行一个 JSON，不是 SSE，也没有 `[DONE]`），HTTP EOF 即结束。主要记录：

| 类型 | 作用 |
|---|---|
| `patch-start` | 流开始；`data.s` 可能携带错误 |
| `patch` | 正文；`o` 为 `a`/`x`/`p`，`p` 是类似 `/s/2/value/0/content` 的路径 |
| `record-map` | 末尾权威快照；最终答案在 `thread_message[*].step` |
| `markdown-chat` / `agent-inference` | 直出事件；前者给字符串，后者给块数组 |
| `error` | in-band 错误，常见 `subType: temporarily-unavailable` |

解析器两条路一起走：流中按 step/value 累积文本，`mergeText()` 同时兼容"累计快照"和"增量片段"（新值以旧值开头 → 快照更新；旧值以新值开头 → 忽略；否则拼接）；流结束后优先用 `record-map` 最终答案，按 `text`/`markdown-chat` > `agent-inference` 挑选，没有 record-map 才退回累积文本。

补充：`thinking`/`reasoning` 块单独累积为 OpenAI 的 `reasoning_content`，与正文重复时去重；`<lang ...>` 等 Notion 标记会被清理；token 数优先取 `usage_summary`（含缓存读写），缺失时按字符/4 估算；可重试的 in-band 错误和 429/5xx 会按配置重试，整条流无文本视为空响应错误。

### 4.4 为什么绕一道 curl.exe（TLS 指纹）

这是项目能工作的前提，也是最容易被误判的地方。Notion 边缘节点按 **TLS 指纹**识别客户端：Node.js/OpenSSL 的默认握手会被拒绝，表现不是连接失败，而是 **HTTP 200 + 空流**，或流里只返回 `temporarily-unavailable`；同样的 Cookie 和请求体，用 Windows 自带 `curl.exe`（Schannel）或 Chrome 发就正常。

因此代理不直接用 Node 发 HTTPS，而是 `spawn` 一个 `curl.exe`：

```text
curl.exe -sS -N --max-time 300 -X POST https://app.notion.com/api/v3/runInferenceTranscript \
  --data-binary @- -w "\n__HTTP_STATUS__%{http_code}" -H "..." ...
```

请求体从 stdin 写入，stdout 按行喂给解析器，`-w` 在末尾输出 HTTP 状态码，用来区分"连接失败"与"上游 4xx"。代价是每请求多一个进程且绑定 Windows；收益是整条链路可用。若出现"HTTP 200 但空正文"，先怀疑 TLS/边缘策略，而不是解析器。

### 4.5 模型发现与回退

首次启动（或 `secret/workspace.json` 不存在）时，代理依次调用 `POST /api/v3/getSpaces`（拿 `spaceId`、`userId`）和 `POST /api/v3/getAvailableModels`（拿模型列表），结果缓存到 `secret/workspace.json`，后续启动直接读缓存。当前账号实测发现 **36 个模型**，覆盖 GPT-6.1 / GPT-5.6、Opus 5.5 / Sonnet 5.5、Gemini 3.x、Kimi K3、DeepSeek V4 Pro、GLM 5.3、Grok 4.6 等；具体以 `/v1/models` 为准，会随套餐和服务端变化。

注册表同时接受对外 id（`claude-sonnet4.6`、`opus-5.5`）、Notion 内部代号（`almond-croissant-low`）和显示名 slug。未知模型不会报错，而是回退到 `config.defaultModel`（默认 `claude-sonnet4.6`）并在窗口打日志——所以"回答风格不对"时先确认实际模型。个别 Gemini 模型需要 `threadType: markdown-chat`，由 `markdownChatModels` 指定；`node notion-proxy.js --check` 可只校验登录态并打印模型映射后退出。

## 5. 功能清单与已验证能力

| 能力 | 状态 | 说明 |
|---|---|---|
| OpenAI 非流式 | ✅ | `/v1/chat/completions` 返回 `chat.completion` |
| OpenAI SSE 流式 | ✅（伪流式） | 完整生成后一次性推送，客户端兼容 |
| OpenAI Responses | ✅ | Codex CLI，`wire_api = "responses"` |
| Anthropic Messages | ✅ | Claude Code，`/v1/messages` + SSE 事件 |
| 多轮对话 | ✅ | 历史消息进入 transcript；默认每请求新线程，可开 `single` 复用 |
| system prompt | ✅ | `prefix` 模式 |
| 中文 | ✅ | 示例脚本统一 UTF-8 / `chcp 65001` |
| 长代码审查 | ✅ | 受请求体 32MB 上限约束 |
| 真实 token usage | ✅ | 优先取上游 `usage_summary` |
| 模型动态发现 | ✅ | 当前账号 36 个 |
| 会话复用 | ✅ | `threadMode=single`：连续 2 次请求只新建 1 个 Notion 对话（turns=2）；第 2 次 input≈72k tokens |
| 内置 CDP 刷新 | ✅ 已实现 | Node WebSocket 直连 DevTools；`--refresh` 手动触发，Python 脚本兜底；成功路径需浏览器以调试端口启动 |
| 单文件 exe | ✅（未签名） | Node SEA 打包；8788 端口启动，`/health`、`/v1/models` 正常（36 模型） |
| SDK / Codex CLI / Claude Code / cc-switch | ✅ | 分别验证通过；cc-switch 为 15721 → 8787 转发 |
| 批量目录/glob 处理 | ✅ | `batch_process.py`，仅标准库 |
| 自动改文件（agent 脚本） | ✅（脚本侧协议） | `auto_agent.py` 用文本 JSON 协议模拟工具调用；4 轮完成一次实测改写，dry-run + 备份；加 `--allow-exec` 后可跑测试/编译（默认关闭） |
| tools / function calling | ❌ | 上游不提供；相关客户端只能当聊天用 |
| 图片、文件上传 | ❌ | 只支持纯文本 |
| 真流式逐字输出 | ❌ | 当前是伪流式 |

## 6. 接口说明

### 6.1 接口一览

| 方法 | 路径 | 协议 | 说明 |
|---|---|---|---|
| GET | `/health` | JSON | 健康检查；无需 key；含 `cookieOk`、模型数、uptime |
| GET | `/v1/models`（也支持 `/models`） | OpenAI | 模型列表；Codex 的 UA 会收到 `{ "models": [] }`，避免它解析元数据出错 |
| POST | `/v1/chat/completions`（也支持 `/chat/completions`） | OpenAI | 聊天补全，支持 `stream` |
| POST | `/v1/messages` | Anthropic | Claude Code 使用，返回 Anthropic 消息/SSE 事件 |
| POST | `/v1/responses` | OpenAI Responses | Codex CLI 使用 |
| POST | `/v1/messages/count_tokens` | Anthropic | 按请求 JSON 长度/4 估算，不是真实分词 |

### 6.2 请求字段与错误

通用字段：`model`、`messages`、`stream`。额外支持 `web_search`（三个入口都透传）、`reasoning_effort`（仅 Chat，且仅部分模型）、`thread_id`（Chat/Anthropic，复用 Notion 线程的高级用法）、`thread_type`（Chat）、`thread_mode`（三个入口均可，`new`/`single`；也可用请求头 `X-Notion-Thread-Mode`）。`temperature`、`max_tokens`、`top_p`、`tools`、`tool_choice` 等会被忽略；Anthropic 的 `system` 会被提取，`tool_use`/`tool_result`/`image` 会被压平成文本占位。

本地鉴权：`config.apiKey` 默认为空（任何 key 放行）；设置后接受 `Authorization: Bearer`、`x-api-key`、`api-key`。错误格式：OpenAI 系为 `{"error":{"message","type","code"}}`，`/v1/messages` 为 Anthropic 风格 `{"type":"error","error":{...}}`。常见状态：401（登录态/local key）、403（Cloudflare 或模型不可用）、429（限流，会重试）、502/503（上游异常或空响应）。

## 7. 客户端接入

通用规则：Base URL 填 `http://127.0.0.1:8787/v1`（Anthropic 客户端填 `http://127.0.0.1:8787`）；API Key 填非空值如 `sk-local`；模型 id 从 `/v1/models` 复制，不要填中文显示名。

| 客户端 | 配置要点 |
|---|---|
| Cherry Studio | 添加 OpenAI 类型服务商，Base URL 填 `.../v1`，手动或从 API 添加模型 |
| Cursor | 填 API Key 和 Override OpenAI Base URL；Agent/Composer 不可用 |
| cc-switch | 已验证本地代理 15721 → 8787 转发链路，由 cc-switch 统一管理客户端配置 |

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="sk-local", timeout=600.0)
resp = client.chat.completions.create(
    model="claude-sonnet4.6",
    messages=[{"role": "user", "content": "你好，用三句话介绍你自己"}],
)
print(resp.choices[0].message.content)
```

Codex CLI 和 Claude Code 的启动脚本在 `examples/`：`codex-notion.cmd` 使用 `wire_api = "responses"`，`claude-code.cmd` 走 Anthropic 端点。

对应关键配置：Codex 的 `base_url = "http://127.0.0.1:8787/v1"`、`wire_api = "responses"`；Claude Code 的 `ANTHROPIC_BASE_URL = "http://127.0.0.1:8787"`、`ANTHROPIC_AUTH_TOKEN = "sk-local"`。注意 **两者都只能当聊天用**：代理不提供工具调用，Codex 不能自动改文件/跑命令，Claude Code 的 Agent 能力同样不可用。

## 8. 快速开始

### 8.1 环境

| 依赖 | 要求 | 用途 |
|---|---|---|
| Windows / Node | Win 10/11；源码运行需 Node 18+（开发机 v25），exe 版免安装 | 运行平台 |
| Python | 3.10+（开发机 3.12） | 可选：Python 兜底刷新时需要 `requests`、`websocket-client` |
| Edge | 可登录 app.notion.com，最好能用调试端口启动 | 提供登录态 |
| curl.exe | Windows 自带 | 访问 Notion |

代理运行时零 npm 依赖：源码版不需要 `npm install`，也不需要编译；exe 版直接运行。

### 8.2 三步启动

```bat
:: 1) 完全关闭 Edge 后，带调试端口启动并登录 Notion
"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --remote-debugging-port=9222

:: 2) 刷新登录态（可跳过：启动时没有凭据会自动从调试端口读取；也可用 notion-proxy.exe --refresh）
update-cookie.cmd

:: 3) 启动代理，窗口需保持开启
start-proxy.cmd
```

启动成功会打印类似 `就绪 | space=xxxxxxxx… | 模型 36 个 | 接口 http://127.0.0.1:8787/v1`；窗口要一直开着，关掉即停止。

不想安装 Node 也可以：运行 `tools\build-exe.ps1`（需 Node/npx）生成 `notion-proxy.exe`（Node SEA，约 91MB），双击即用。exe 必须与 `config.json`、`secret\`、`tools\` 放同一目录；它是自构建未签名程序，postject 会提示签名异常，Windows Defender 可能误报，重新构建即可。

### 8.3 验证与常用命令

```bat
curl.exe http://127.0.0.1:8787/health
curl.exe http://127.0.0.1:8787/v1/models
examples\curl_chat.cmd
node notion-proxy.js --check          :: 只校验登录态、打印模型
node notion-proxy.js --port 9000      :: 临时换端口
node notion-proxy.js --parse x.ndjson :: 离线解析保存的原始流
notion-proxy.exe --refresh            :: 从浏览器调试端口刷新登录态（源码版用 node notion-proxy.js --refresh）
```

### 8.4 关键配置（config.json）

| 字段 | 默认值 | 说明 |
|---|---|---|
| `port` / `bind` | `8787` / `127.0.0.1` | 监听地址；不要改成 `0.0.0.0` |
| `defaultModel` | `claude-sonnet4.6` | 默认模型，也是未知模型回退目标 |
| `maxConcurrent` / `retries` | `2` / `2` | 并发上限与重试次数 |
| `requestTimeoutMs` | `300000` | 单次上游超时（5 分钟） |
| `apiKey` | `""` | 留空不校验 |
| `autoRefreshCookie` / `validateOnStart` | `true` / `true` | 401 自动刷新、启动校验 |
| `threadMode` / `threadReuseMaxTurns` / `threadReuseMaxInputTokens` | `"new"` / `30` / `300000` | 会话复用开关与轮数/输入 token 轮换阈值 |
| `systemPromptMode` / `streamMode` | `"prefix"` / `"pseudo"` | system prompt 模式；伪流式（当前仅此一种，字段保留） |
| `debugSaveRaw` / `maxBodyBytes` | `false` / `33554432` | 保存原始 NDJSON（含对话内容）；请求体上限 32MB |

## 9. 限制与风险

| 限制 | 影响 |
|---|---|
| 不支持 tools / function calling | Codex、Claude Code、Cursor Agent 不能真正执行工具 |
| 不支持图片/文件 | 只能纯文本对话 |
| 采样参数被忽略 | `temperature`、`max_tokens` 等无效 |
| 伪流式 | 首字延迟取决于完整生成时间，不是逐字输出 |
| 默认无状态（可 single 复用） | 默认每请求新建线程；single 缓解会话堆积，但上下文/token 累积 |
| `count_tokens` 是估算 | Claude Code 的 token 预算不精确 |
| 依赖账号与套餐 | 模型列表和可用性由 Notion 服务端决定 |

账号与合规：走的是 Notion **网页接口而非官方 API**，可能违反使用条款；可能遇到限流（429）、Cloudflare 校验（403）、功能临时禁用，极端情况下账号受限。接口随时可能变，代理不承诺稳定，失效时需要重新抓包分析（`docs/upstream-spec.md` 是入口）。建议个人低频自用，不要刷量、对外服务或使用公司共享账号；对话内容会经本机代理发到 Notion。

凭据与暴露面：`secret/credential.json` 里的 `token_v2` 等同账号登录态，**不要外传、截图或上传**（`.gitignore` 已忽略 `secret/`）。代理默认只监听 `127.0.0.1`，不要把 `bind` 改成 `0.0.0.0`，也不要用内网穿透暴露到公网。默认不校验本地 key；本机有其他不可信程序时建议设置 `apiKey`。怀疑泄露时：在 Notion 退出所有设备 → 重新登录 → 重跑 `update-cookie.cmd`。批量脚本会把输入文件发给 Notion，不要指向密钥或客户数据目录。exe 为自构建未签名程序，postject 会提示签名异常，Windows Defender 可能误报。

## 10. 文件结构

```text
ai-proxy/
├─ notion-proxy.js / notion-proxy.exe  # 核心源码 / Node SEA 打包产物（约 91MB，免安装 Node）
├─ config.json / start-proxy.cmd / update-cookie.cmd
├─ PROJECT-INTRO.md / README.md
├─ examples/                # openai_python.py、curl_chat.cmd、batch_process.py（--thread-mode single|new）、auto_agent.py
│                           # claude-code.cmd、claude-code-settings.json
│                           # codex-notion.cmd、codex-notion.config.toml
├─ tools/                   # build-exe.ps1、cdp_cookie_extract.py
│                           # extract_cookie.py、notion_probe.py
├─ docs/upstream-spec.md    # 上游协议逆向规范
├─ secret/                  # credential.json / workspace.json / thread-state.json / debug/
└─ ref/                     # 参考源码（体积大，可删）
```

可删：`ref/`、`.tmp/`、`out/`、`secret/debug/`；绝不能外传：整个 `secret/`。

## 11. 技术亮点与设计取舍

| 设计 | 收益 | 代价 |
|---|---|---|
| 单文件、零 npm 依赖 | 部署只需 Node + curl，审计成本低 | 文件大、难拆测试、复用性差 |
| 上游走 curl.exe | 绕开 TLS 指纹拒绝，是项目成立的前提 | 绑定 Windows、每请求一个进程 |
| 双路径 NDJSON 解析 | 同时兼容增量与快照语义，record-map 兜底 | 逻辑复杂，流中间态可能短暂重复 |
| 伪流式 | 不依赖不确定的增量语义，客户端兼容好 | 首字慢，不是真流式 |
| system prompt prefix | 绕开注入检测，简单有效 | 不是真正独立的 system 角色 |
| 动态发现 + 静态别名 | 模型换名/换套餐不用改代码 | 缓存可能过期，slug 不保证稳定 |
| 会话复用（single） | 减少 Notion 会话堆积，批量处理更省会话 | 上下文 token 随轮数增长，靠阈值自动轮换 |
| CDP 取 Cookie + exe 打包 | 免 Python、免安装 Node，分发简单 | 需浏览器调试端口；exe 约 91MB 且未签名 |
| 多协议适配 | 同一后端接三个客户端生态 | 三种协议字段/事件都要维护 |

## 12. 未来可做

1. **真流式**：在增量语义确认后按块推送 SSE，并用 record-map 做最终校正，实现逐字输出。
2. **工具调用适配**：受上游限制，最多做协议层模拟，无法真正执行工具；若 Notion 未来开放工具接口再考虑。
3. **多账号/自动轮换**：账号池提高可用性，但会显著增加风控与合规复杂度，慎做。
4. **更准的 token 统计**：复用 `usage_summary`，或引入真实 tokenizer，让 `count_tokens` 与上游口径一致。
5. **结构化搜索/引用与可观测性**：把搜索结果整理成 OpenAI 注释或 Anthropic citations；补请求日志、错误率/耗时面板，并用 `ref/` 里的 NDJSON fixture 做解析器回归测试。
