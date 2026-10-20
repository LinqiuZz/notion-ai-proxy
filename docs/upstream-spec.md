# Notion AI Web 上游协议规范（Notion → OpenAI 兼容代理用）

> 逆向侦察产出。目标：把 `POST /api/v3/runInferenceTranscript` 的**完整协议细节**写成可直接照写的规格。
> 不含代理实现代码（由 Lead 写）。所有结论都标注了来源：**实测**（本机 2026-10-20 只读探测）、
> **代码**（参考仓库）、**未确认**（无法验证/互相矛盾）。
>
> 本地参考仓库（已 clone，只读）：
> - `work/ai-proxy/ref/pdxjie/` —— notion2api，5 个模型版本，cloudscraper
> - `work/ai-proxy/ref/maverickxone/` —— notion2api fork/进化版，**23 个模型**，`NOTION_URL` 可覆盖
> - `work/ai-proxy/ref/omniroute/` —— OmniRoute（`diegosouzapw/OmniRoute`）的 `notion-web` provider，
>   **2026-07 起用 app.notion.com + Chrome TLS 指纹**，信息最新
> - `work/ai-proxy/ref/omniroute_tests/`、`work/ai-proxy/ref/omniroute_pr7600_patches/` —— 真实 NDJSON fixture
> - `work/ai-proxy/ref/app_notion_chat.html`、`app_notion_js*/` —— app.notion.com 前端 bundle 抓取
>
> ⚠️ 任务描述里“pdxjie 支持 23 个模型”不准确：pdxjie 仓库 `model_registry.py` 只有 5 个模型；
> **23 个模型在 maverickxone 仓库**（`ref/maverickxone/app/model_registry.py:1-25`，其
> `docs/PROJECT_PROGRESS.md:288-312` 有完整映射表）。

---

## 0. 结论速览（TL;DR）

| 项 | 结论 |
|---|---|
| 当前实际可用 host | **`https://app.notion.com`（首选）**，`https://www.notion.so`（兼容可用，也可用 `NOTION_URL` 覆盖） |
| 无效 host | `https://www.notion.com`：营销站，`/api/v3/*` 返回 HTML 404 |
| Endpoint | `POST {BASE}/api/v3/runInferenceTranscript` |
| 鉴权 | Cookie：`token_v2`（必需）；`notion_user_id`/`space_id` 强烈建议；`notion_browser_id` 可选 |
| 请求头 | `Content-Type: application/json`、`Accept: application/x-ndjson`、浏览器 UA、`Origin: {BASE}`、`Referer: {BASE}/ai`、`x-notion-space-id`、`x-notion-active-user-header`、`notion-client-version`、`notion-audit-log-platform: web` + `sec-fetch-*` 指纹头 |
| 请求体 | `{traceId, spaceId, threadId, threadType:"workflow", createThread, isPartialTranscript, transcript:[config, context, user..., agent-inference...], asPatchResponse:true, generateTitle, ...}` |
| 模型选择 | `transcript[0].value.model = "<Notion 内部食物代号>"`，同时 `value.type="workflow"`、`modelFromUser:true` |
| 联网搜索 | `transcript[0].value.useWebSearch = true` + `searchScopes:[{"type":"everything"}]` |
| 思考程度 | `transcript[0].value.reasoningEffort`（web 客户端字段，取值来自 `getAvailableModels` 的 `modelConfiguration.supportedReasoningEfforts`；参考实现都不传，**未确认**取值） |
| 响应 | **NDJSON**（不是 SSE），每行一个 JSON；无 `[DONE]`；HTTP EOF 即结束 |
| 响应语义 | patch 流（增量/快照混合）+ 末尾 `record-map` 是**权威最终答案**；参考实现做法：优先 record-map，其次 patch 文本 |
| Cloudflare | 两个 host 都在 CF 后面（有 `CF-Ray`、`__cf_bm`）。curl/Schannel 与 Chrome 能过；**Node undici TLS 指纹会被拒**（HTTP 200 + in-band `temporarily-unavailable`）。Windows 本地建议 `curl_cffi(impersonate="chrome")` 或 `cloudscraper` + 浏览器头 |
| 错误 | 401（token 失效）、403（CF/账号受限）、429（限流）、200+in-band error、400 ValidationError（createThread:false 但没有 threadId） |

---

## 1. Host / Endpoint（含 2026-10-20 实测证据）

### 1.1 实测（本机 curl，无凭据，只读）

| 探测 | 结果 |
|---|---|
| `GET https://www.notion.so/ai` | **307** → `Location: https://app.notion.com/ai?__dm_a=1` |
| `GET https://www.notion.so/chat` | **307** → `https://app.notion.com/chat?__dm_a=1` |
| `POST https://www.notion.so/api/v3/runInferenceTranscript` | **401** `{"isNotionError":true,"errorId":"...","name":"UnauthorizedError","debugMessage":"Must be authenticated.","message":"Something went wrong. (401)"}` |
| `POST https://app.notion.com/api/v3/runInferenceTranscript` | **401**（同上，说明该 host 上接口存在，非 404） |
| `POST https://www.notion.com/api/v3/runInferenceTranscript` | **404 HTML**（`Page not found`，营销站，无 API） |
| `POST https://app.notion.com/api/v3/getAvailableModels` `/getSpaces` `/loadUserContent` `/authValidate` | 全部 **401**（接口都在） |
| `OPTIONS https://app.notion.com/api/v3/runInferenceTranscript` | **200**（CF 层放行；预检无 CORS 头，跨域前端不可用，但本地反代无所谓） |

响应头可见 `CF-Ray`、`Server: cloudflare`、`Set-Cookie: __cf_bm=...; Domain=notion.com`、
`Set-Cookie: device_id=...; Domain=app.notion.com`。

### 1.2 参考实现用法

| 实现 | base URL | 说明 |
|---|---|---|
| pdxjie | 硬编码 `https://www.notion.so`（`ref/pdxjie/app/notion_client.py:47`） | 旧版 |
| maverickxone | `NOTION_URL=os.getenv("NOTION_URL","https://www.notion.so")`（`ref/maverickxone/app/notion_client.py:22`）→ `f"{NOTION_URL}/api/v3/runInferenceTranscript"` | `Origin=NOTION_URL`，`Referer=f"{NOTION_URL}/chat"` |
| OmniRoute | `const BASE_URL = "https://app.notion.com"`（`ref/omniroute/open-sse/executors/notion-web.ts:86-93`），注释明确写 **“Both app.notion.com and www.notion.so work; prefer the AI surface host”**；`notionWebModels.ts:18-24` 同样偏好 app.notion.com | 2026-07-19/20 活体抓包在 Business workspace 验证 |

### 1.3 建议

- 代理**默认 `BASE_URL=https://app.notion.com`**（当前网页 AI 在这里），并做成可配置；
  若 401/403 再 fallback `https://www.notion.so`。
- `Origin` / `Referer` 必须与 `BASE_URL` 同源（`Referer: {BASE}/ai`；OmniRoute 对自定义 agent 用 `/agent/<id>?wfv=chat`）。
- **host 与 cookie 域的关系（未确认，需用户配合验证）**：老登录态 `token_v2` 域可能是 `www.notion.so`；
  新网页（app.notion.com）的 cookie 可能挂在 `app.notion.com` 或 `.notion.com`。
  如果迁移后 401，先换 `BASE_URL` 试另一个 host；同一 token 是否跨域有效未验证。
- 建议先让用户从**当前登录的站点**（现在是 app.notion.com）复制 **完整 Cookie 请求头**，最稳。

---

## 2. 鉴权：Cookie 清单与获取

### 2.1 Cookie 字段

| 字段 | 必需性 | 用途 / 来源 |
|---|---|---|
| `token_v2` | **必需** | 登录态主 token；`ref/pdxjie/README.md:45-48`、`maverickxone/README.md:52` |
| `notion_user_id` | 强烈建议 | 用于 `x-notion-active-user-header`；也可从 `getSpaces`/`loadUserContent` 推导（`login.py:384`） |
| `space_id` | 强烈建议 | 请求体 `spaceId` + `x-notion-space-id`；也可从 `getSpaces` 推导 |
| `notion_browser_id` | 可选 | OmniRoute 支持拼进 Cookie（`notion-web.ts:202-213`、PR 文档 `authHint`）；notion2api 不用 |
| `device_id` | 可选 | app.notion.com 会下发（`Domain=app.notion.com`），实测不需要手工带 |
| `cf_clearance` / `__cf_bm` | 可选/自动 | cloudscraper 自动处理；手写请求可不带，实测 curl 无 cookie 也拿到 JSON 401 而非 CF 挑战 |
| `space_view_id` | 只用 Notion 元数据 | 放 `transcript[].context.value.spaceViewId`（notion2api），不是鉴权必需 |
| `user_name`/`user_email` | 可选 | `context.value.userName/userEmail` |

- 最小可用集合（3 个参考实现交集）：`token_v2` + `space_id` + `user_id`。notion2api 的
  `REQUIRED_ACCOUNT_FIELDS = {"token_v2","space_id","user_id"}`（`ref/maverickxone/app/config.py:8`）。
- Cookie 传递方式两种：
  1. `requests/cloudscraper` 的 `cookies=` dict（pdxjie）；2. 直接拼 `Cookie` header 字符串
  （maverickxone 改为 header，原因：CF challenge cookie 可能是非 ASCII，`cookie jar` 编码崩溃，见
  `ref/maverickxone/app/notion_client.py:267-269`、`PROJECT_PROGRESS.md:453`）。**推荐 header 字符串**。

### 2.2 浏览器端提取脚本

- `ref/pdxjie/scripts/extract_notion_info.js`（99 行，简单版）：
  `POST /api/v3/loadUserContent`（`credentials:'include'`）→ 从 `recordMap` 取
  `notion_user` 第一个 id / `space` 第一个 id / `space_view` 第一个 id + `given_name|name`、`email`；
  `token_v2` 需手工填（脚本输出 `YOUR_TOKEN_V2_HERE` 占位）。
- `ref/maverickxone/scripts/extract_notion_info.js`（237 行，增强版）：
  先 `POST /api/v3/getSpaces`（返回 `{user_id: {notion_user, space, space_view}}`，支持多账号/多工作区），
  失败则 fallback `loadUserContent`；读 `document.cookie` 里的 `notion_user_id` 标记“当前活跃账号”；
  交互式选择 user/workspace 后输出：
  ```json
  {"token_v2":"YOUR_TOKEN_V2","space_id":"...","user_id":"...","space_view_id":"...","user_name":"...","user_email":"..."}
  ```
  工作区字段：`space.name`、`space.plan_type|subscription_tier`；`space_view.space_id → space_view_id`。
- `ref/maverickxone/login.py`：用 CDP（`localhost:9222`）从真实 Chrome/Edge 抓 cookie
  （`_extract_cookies_from_cdp` → 等 `token_v2` 出现），再调 `POST {BASE}/api/v3/getSpaces`、
  `POST {BASE}/api/v3/loadUserContent` 枚举账号/工作区；`NOTION_DOMAIN`（默认 `www.notion.so`）
  决定 cookie 写入域（`login.py:25-28, 344-361`）。**本任务不运行它，也不接触真实凭据。**
- 当前站点若是 app.notion.com：让用户在 **app.notion.com** 的 DevTools → Application → Cookies
  找 `token_v2`，并在 Network 里对任意 `/api/v3/*` 请求右键 **Copy as cURL**，
  直接拿到完整 Cookie + 真实请求头（最可靠）。

---

## 3. 请求头完整清单

### 3.1 notion2api（maverickxone，`ref/maverickxone/app/notion_client.py:273-290`）

```http
Content-Type: application/json
Accept: application/x-ndjson
User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36
x-notion-space-id: <space_id>
x-notion-active-user-header: <user_id>
notion-audit-log-platform: web
notion-client-version: 23.13.20260228.0625
origin: <NOTION_URL>
referer: <NOTION_URL>/chat
sec-ch-ua: "Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"
sec-ch-ua-mobile: ?0
sec-ch-ua-platform: "Linux"
sec-fetch-dest: empty
sec-fetch-mode: cors
sec-fetch-site: same-origin
cookie: token_v2=...; notion_user_id=...
```

> 该文件用**俄文注释**记录 A/B 实测（2026-09-06）：不带浏览器指纹（`sec-fetch-*` + 新 UA）时
> 上游返回**空流**；带则正常 NDJSON。见 `notion_client.py:271-272`。

pdxjie 版少了 `sec-*`，UA 是 `Chrome/145.0.0.0`，`referer: {base}/ai`，`notion-client-version` 同。

### 3.2 OmniRoute（`notion-web.ts:394-420` + `notionWebModels.ts:40-50`）

```http
Content-Type: application/json
Accept: application/x-ndjson
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) ... Chrome/146.0.0.0 Safari/537.36
Cookie: token_v2=...; space_id=...; notion_user_id=...; notion_browser_id=...
Origin: https://app.notion.com
Referer: https://app.notion.com/ai            # 自定义 agent: /agent/<workflowId 去横线>?wfv=chat
notion-client-version: 23.13.20260720.1949    # 2026-07-20 抓包版本
notion-audit-log-platform: web
x-notion-space-id: <space_id>
x-notion-active-user-header: <user_id>        # 有 user_id 就带
Accept-Language: en-US,en;q=0.9
sec-ch-ua: "Chromium";v="149", "Not)A;Brand";v="24"
sec-ch-ua-mobile: ?0
sec-ch-ua-platform: "Windows"
sec-fetch-dest: empty
sec-fetch-mode: cors
sec-fetch-site: same-origin
priority: u=1, i
cache-control: no-cache
pragma: no-cache
```

### 3.3 哪些头是必需的（按证据强度）

| 头 | 必需性 | 证据 |
|---|---|---|
| `Cookie`（含 token_v2） | **必需** | 401 实测；三个实现一致 |
| `Content-Type: application/json` | **必需** | 一致 |
| `Accept: application/x-ndjson` | 建议必需 | 一致；不带可能走 JSON 错误而非流 |
| `Origin` / `Referer`（与 host 同源） | 强烈建议 | 一致；CF/风控会看 |
| `x-notion-space-id` | 强烈建议（OmniRoute 称必需） | 两个 web 实现都带；值=spaceId |
| `x-notion-active-user-header` | 强烈建议 | 同上 |
| `notion-client-version` | 建议（版本号可随抓包更新） | 一致；OmniRoute 支持 env 覆盖 |
| `notion-audit-log-platform: web` | 建议 | 一致 |
| `sec-ch-ua*` / `sec-fetch-*` / `priority` / `cache-control` | 建议 | maverickxone A/B 实测：缺指纹→空流；OmniRoute 见 §9 TLS |
| `User-Agent` 现代 Chrome | **重要** | 同上 |

> `notion-client-version` 当前参考值已偏旧（2026-02 / 2026-07）。它看起来是宽松校验；若遇
> 400/403 或持续空流，让用户从浏览器 Network 复制真实值覆盖。**是否严格校验：未确认。**

---

## 4. 请求体：完整 schema + 真实示例

### 4.1 完整示例（notion2api workflow 首轮，`ref/maverickxone/app/notion_client.py:292-318`）

```json
{
  "traceId": "b0b9a2f2-5a70-4c3f-9a41-8d1f2c0a7e11",
  "spaceId": "1f2e3d4c-5b6a-4789-9abc-def012345678",
  "threadId": "7c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f",
  "threadType": "workflow",
  "createThread": true,
  "generateTitle": true,
  "saveAllThreadOperations": true,
  "setUnreadState": true,
  "isPartialTranscript": false,
  "asPatchResponse": true,
  "isUserInAnySalesAssistedSpace": false,
  "isSpaceSalesAssisted": false,
  "threadParentPointer": {
    "table": "space",
    "id": "1f2e3d4c-5b6a-4789-9abc-def012345678",
    "spaceId": "1f2e3d4c-5b6a-4789-9abc-def012345678"
  },
  "transcript": [
    {
      "id": "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
      "type": "config",
      "value": {
        "type": "workflow",
        "model": "almond-croissant-low",
        "modelFromUser": true,
        "useWebSearch": true,
        "useReadOnlyMode": false,
        "writerMode": false,
        "isCustomAgent": false,
        "isCustomAgentBuilder": false,
        "useCustomAgentDraft": false,
        "use_draft_actor_pointer": false,
        "enableAgentAutomations": true,
        "enableAgentIntegrations": true,
        "enableCustomAgents": true,
        "enableAgentDiffs": true,
        "enableAgentCreateDbTemplate": true,
        "enableCsvAttachmentSupport": true,
        "enableDatabaseAgents": false,
        "enableAgentThreadTools": false,
        "enableRunAgentTool": false,
        "enableAgentDashboards": false,
        "enableAgentCardCustomization": true,
        "enableSystemPromptAsPage": false,
        "enableUserSessionContext": false,
        "enableCreateAndRunThread": true,
        "enableAgentGenerateImage": false,
        "enableSpeculativeSearch": false,
        "enableUpdatePageV2Tool": true,
        "enableUpdatePageAutofixer": true,
        "enableUpdatePageMarkdownTree": false,
        "enableUpdatePageOrderUpdates": true,
        "enableAgentSupportPropertyReorder": true,
        "enableAgentVerification": false,
        "useServerUndo": true,
        "databaseAgentConfigMode": false,
        "isOnboardingAgent": false,
        "availableConnectors": [],
        "customConnectorNames": [],
        "searchScopes": [{ "type": "everything" }],
        "useSearchToolV2": false,
        "useRulePrioritization": false,
        "enableExperimentalIntegrations": false,
        "enableAgentViewNotificationsTool": false,
        "enableScriptAgent": false,
        "enableScriptAgentAdvanced": false,
        "enableScriptAgentSlack": false,
        "enableScriptAgentMcpServers": false,
        "enableScriptAgentMail": false,
        "enableScriptAgentCalendar": false,
        "enableScriptAgentCustomAgentTools": false,
        "enableScriptAgentSearchConnectorsInCustomAgent": false,
        "enableScriptAgentGoogleDriveInCustomAgent": false,
        "enableQueryCalendar": false,
        "enableQueryMail": false,
        "enableMailExplicitToolCalls": true
      }
    },
    {
      "id": "1b2c3d4e-5f6a-4b7c-8d9e-0f1a2b3c4d5e",
      "type": "context",
      "value": {
        "timezone": "Asia/Shanghai",
        "userName": "user",
        "userId": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "userEmail": "user@example.com",
        "spaceName": "Notion",
        "spaceId": "1f2e3d4c-5b6a-4789-9abc-def012345678",
        "spaceViewId": "bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
        "currentDatetime": "2026-10-20T12:46:21.123+08:00",
        "surface": "workflows",
        "agentName": "user"
      }
    },
    {
      "id": "2c3d4e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f",
      "type": "user",
      "value": [["你好，介绍一下你自己"]],
      "userId": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
      "createdAt": "2026-10-20T12:46:21.123+08:00"
    }
  ],
  "debugOverrides": {
    "emitAgentSearchExtractedResults": true,
    "cachedInferences": {},
    "annotationInferences": {},
    "emitInferences": false
  }
}
```

### 4.2 顶层字段表

| 字段 | 类型 | 必需 | 说明 |
|---|---|---|---|
| `traceId` | uuid string | 是 | 每次请求新 UUID（`str(uuid.uuid4())`） |
| `spaceId` | string | 是 | = 账号 space_id |
| `threadId` | uuid string | 是 | 首轮新生成；后续复用同一 id 维持会话 |
| `threadType` | `"workflow"` \| `"markdown-chat"` | 是 | 与 config.value.type 相同；默认 workflow |
| `createThread` | bool | 是 | 首轮 `true`；后续 `false`（复用 thread）。**`false` 但服务端不认识的 threadId → 400 ValidationError（OmniRoute 活体抓包，`notion-web.ts:10-19`）** |
| `isPartialTranscript` | bool | 是 | 首轮 `false`；复用 thread 时 `true`（notion2api 注释：否则 AI 失忆/校验失败，`notion_client.py:242-244`）；OmniRoute follow-up 也置 true |
| `generateTitle` | bool | 否 | 首轮 true（生成标题），后续 false |
| `saveAllThreadOperations` | bool | 否 | true |
| `setUnreadState` | bool | 否 | true（首轮） |
| `asPatchResponse` | bool | 强烈建议 | true（NDJSON patch 流；OmniRoute 另加 `patchResponseVersion: 2`） |
| `isUserInAnySalesAssistedSpace` / `isSpaceSalesAssisted` | bool | 否 | false |
| `threadParentPointer` | object | 否 | `{table:"space", id:spaceId, spaceId}`；自定义 agent 用 `{table:"workflow", id:workflowId, spaceId}` |
| `transcript` | array | 是 | 见 §4.3 |
| `debugOverrides` | object | 否 | 见上；`emitAgentSearchExtractedResults:true` 让搜索提取结果出现在流里 |
| `createdSource` | string | 否 | OmniRoute 用 `"ai_module"`（自定义 agent `"custom_agent"`） |
| `supportsCustomAgentNudgeTranscriptStep` | bool | 否 | OmniRoute 用 true |

### 4.3 transcript 数组：block 类型表

| block `type` | `value` 结构 | 角色 | 来源 |
|---|---|---|---|
| `config` | object（见 §4.4） | **必须在第 1 个** | 三个实现一致 |
| `context` | object（timezone/userId/spaceId/surface/currentDatetime/instructions…） | 第 2 个 | 一致 |
| `user`（等价 `human`） | **rich-text tuple**：`[[text]]` 或 `[[text, marks]]`；可带 `userId`、`createdAt` | 用户消息 | maverickxone/OmniRoute 现行；OmniRoute 旧 PR 版用 `human` |
| `agent-inference` | `[{"type":"text","content":"..."}]` | **assistant 消息 / thinking 段** | 一致 |
| `ai` | `[[text]]` | assistant（旧式，OmniRoute PR 版） | PR #7600 首版；现行代码已改 `agent-inference` |
| `assistant` | `[[text]]` | assistant（notion2api heavy 模式历史消息写法） | `ref/maverickxone/app/conversation.py:303-310`；现行 web 客户端用 `agent-inference` 更稳，**建议不要用 `assistant`** |
| `context`（用于 system） | — | OmniRoute 把 system prompt 折进 `context.value.instructions`；notion2api 把 system 拼到第一条 user 前面 | 两种做法均可 |
| `debug-inference` / `memory-agent` | 内部调试/记忆工具用 | 与正常聊天无关（live bundle 见到） | — |

> `user` vs `human`：现行 app.notion.com 抓包用 `user`；OmniRoute 注释明确“legacy `human` also
> works with createThread, but `user` matches the current web client”（`notionTranscriptBuilder.ts:11-13`）。
> **建议用 `user`。**
>
> assistant 历史消息用 `agent-inference`，`value` 是 **对象数组**（`[{type:"text",content}]`），
> **不是** `[[text]]`。这是两种不同的 rich-text 表示，写错会被忽略或 400。

### 4.3.1 附件上传（图片 / 文件，部分逆向）

Notion 网页 AI 支持在对话里附带文件/图片，走专用上传接口（证据：`ref/app_notion_js/UISpacePermissionGroupToken-*.js`）：

1. **申请签名 URL**：`POST /api/v3/getUploadFileUrlForAssistantChatTranscriptUpload`

   ```json
   {
     "name": "a.png",
     "contentType": "image/png",
     "contentLength": 12345,
     "assistantChatTranscriptSessionPointer": {"...": "会话指针，含 spaceId/threadId 等"},
     "createThread": true,
     "threadType": "workflow",
     "workflowId": "可选",
     "agentMemorySettings": "可选",
     "allowUnsupportedTypes": false
   }
   ```

2. **上传文件**：响应的 `data` 里含 `signedUploadPostUrl`（S3 POST 地址）、`postHeaders`
   （`[{name,value}]`）、`fields`，以及上传完成后的 `url` 和 `signedToken`。
   浏览器用 `FormData` 把 `fields` + `file` 提交到 `signedUploadPostUrl`，成功后拿 `data.url` 作为文件地址。

3. **未确认**：transcript 里如何引用该附件——是直接把 `url` 放进 `user` block，
   还是先 `submitTransaction` 建 `image` / `file_upload` block 再让 AI 读取，尚未抓到。
   需要一次「带附件的 `runInferenceTranscript`」抓包（方法见 `tools/capture_attachment.py`）。

### 4.4 `config.value` 字段表（模型 / 思考 / 搜索）

| 字段 | 值 | 说明 |
|---|---|---|
| `type` | `"workflow"` / `"markdown-chat"` | 线程类型。markdown-chat 仅个别 Gemini 代号使用（maverickxone：`vertex-gemini-3.5-flash`；pdxjie 旧逻辑：Gemini 系全走 markdown-chat） |
| **`model`** | **Notion 内部食物代号**，如 `almond-croissant-low` | **模型选择的唯一开关**，见 §4.5 映射表 |
| `modelFromUser` | bool | 用户显式选模型 true；默认模型/自定义 agent false |
| **`useWebSearch`** | bool | 联网搜索开关（参考实现恒 true） |
| `searchScopes` | `[{"type":"everything"}]` | 搜索范围 |
| `useSearchToolV2` | bool | false（参考） |
| `isCustomAgent` / `isCustomAgentBuilder` / `useCustomAgentDraft` | false | 非自定义 agent |
| `reasoningEffort` | 字符串（未确认取值） | **思考程度**：live app bundle（`agentWriter` chunk）显示 workflow config 支持该字段，取值来自 `getAvailableModels` 行的 `modelConfiguration.supportedReasoningEfforts`（如 low/medium/high）。参考实现都不传 → 用服务端默认 |
| `workflowId` | uuid | 仅自定义 agent（`isCustomAgent:true`） |
| 其余 `enableXxx` | 各种 bool | 功能开关，照抄 §4.1 即可（OmniRoute 2026-07 抓包值见 `notionTranscriptBuilder.ts:31-66`；notion2api 值见 `conversation.py:340-399`） |

> **联网搜索可否关闭**：`useWebSearch:false` 理论上可行，但参考实现从未验证 → **未确认**。
> 如果 Lead 想暴露 `web_search` 开关，建议先恒 true 跑通，再单测 false。

### 4.5 模型 ID 映射表（23 个，`ref/maverickxone/app/model_registry.py:1-25`）

`config.value.model` 填右列“Notion 内部代号”；对外 OpenAI 模型名用左列。

| 对外 model id | Notion 内部代号 | threadType | 备注 |
|---|---|---|---|
| claude-sonnet4.6 | `almond-croissant-low` | workflow | **默认**，速度/质量平衡 |
| claude-sonnet5 | `angel-cake-high` | workflow | |
| claude-opus4.6 | `avocado-froyo-medium` | workflow | |
| claude-opus4.7 | `apricot-sorbet-high` | workflow | |
| claude-opus4.8 | `ambrosia-tart-high` | workflow | |
| claude-opus5 | `agave-flan` | workflow | |
| claude-haiku4.5 | `anthropic-haiku-4.5` | workflow | |
| gpt-6-astra | `orlando-quinn` | workflow | |
| gpt-5.6-sol | `orange-mousse` | workflow | |
| gpt-5.6-terra | `orchid-muffin` | workflow | |
| gpt-5.6-luna | `olive-jellyroll` | workflow | |
| gpt-5.5 | `opal-quince-medium` | workflow | |
| gpt-5.4 | `oval-kumquat-medium` | workflow | |
| gemini-3.5flash | `vertex-gemini-3.5-flash` | **markdown-chat** | 唯一 markdown-chat |
| gemini-3.1pro | `galette-medium-thinking` | workflow | |
| gemini-3flash | `gingerbread` | workflow | |
| kimi-2.7 | `fireworks-kimi-k2.7` | workflow | |
| grok-4.3 | `xigua-mochi-medium` | workflow | |
| spacexai-4.5 | `strawberry-whoopiepie` | workflow | |
| grok-build0.1 | `xinomavro-cake` | workflow | |
| deepseek-v4pro | `baseten-deepseek-v4-pro` | workflow | |
| glm-5.2 | `baseten-glm-5.2` | workflow | |
| fable-5 | `acai-budino-high` | workflow | OmniRoute：需 Business/Enterprise 计划，否则 `isDisabled` |

**更稳的做法：动态发现模型（推荐）** —— OmniRoute 提供 `POST /api/v3/getAvailableModels`
（`ref/omniroute/open-sse/services/notionWebModels.ts:23, 254-296`）：

- 请求：`POST https://app.notion.com/api/v3/getAvailableModels`，body `{"spaceId":"..."}`，
  headers 同 §3.2（`x-notion-space-id`、`x-notion-active-user-header`）。
- 响应：`{"models":[{ "model":"orange-mousse", "modelMessage":"GPT-5.6 Sol",
  "modelFamily":"openai", "isDisabled":false, "disabledReason":"...",
  "modelConfiguration":{"supportedReasoningEfforts":["low","medium","high"],...}}, ...]}`
- 映射：对外 id = `modelMessage` 的 slug（`"GPT-5.6 Sol" → "gpt-5.6-sol"`，
  `slugifyNotionDisplayName`，`notionWebModels.ts:109-118`）；`config.value.model` = `model` 字段；
  跳过 `isDisabled:true`。
- OmniRoute 的离线 fallback 表（43 条，含 2026 下半年新模型：`albuquerque-quinn`=Opus 5.5、
  `achira-donut`=Sonnet 5.5、`amla-jam`=Haiku 5.5、`grapefruit-zeppole`=Gemini 3.7 Flash、
  `oregon-grape-medium`=GPT-5.4 Mini、`otaheite-apple-medium`=GPT-5.4 Nano、
  `soursop-shortcake`=Grok 4.6、`fireworks-kimi-k3`、`baseten-glm-5.3` 等）见
  `ref/omniroute/open-sse/services/notionWebFallbackModels.ts:31-282`。
- 兼容层建议：`resolveNotionCodename(model)` 同时接受 外部 slug / provider 前缀
  （`notion-web/`、`nw/`）/ 显示名 / 内部代号，统一转成内部代号（`notionWebModels.ts:731-746`）。

### 4.6 对话（thread）生命周期语义（重要）

1. **首轮**：`createThread:true` + 新 `threadId` + `isPartialTranscript:false` + `generateTitle:true`，
   transcript = `config + context + 历史(空) + user`。
2. **后续轮**：`createThread:false` + **同一 `threadId`** + `isPartialTranscript:true`；
   transcript 二选一：
   - notion2api：发**完整** transcript（历史 user/agent-inference 全部重发，`isPartialTranscript:true`）；
   - OmniRoute：只发**最后一条 assistant 之后**的消息（`messagesForNotionTranscript`，
     `notionTranscriptBuilder.ts:134-158`），理由：重发整个 agent 工具循环会触发
     `temporarily-unavailable`。
   两种都声称可用；建议默认发完整（更简单、与 notion2api 一致），出问题再改增量。
3. `createThread:false` 但 threadId 服务端未知 → **HTTP 400 ValidationError**（OmniRoute 活体验证）。
4. **不要删除 thread**：删除会导致后续上下文丢失（notion2api 注释“AI 失忆”，
   `notion_client.py:339-342`）。代价是 Notion 首页会累积会话。
5. markdown-chat（Gemini 3.5 Flash）需要先用 `POST /api/v3/saveTransactions` **预创建 thread**
   （`_create_thread`，`notion_client.py:99-162`：transaction `set` 一条 `thread` 记录，
   `{id, version:1, parent_id:spaceId, parent_table:"space", space_id, created_time, created_by_id,
   created_by_table:"notion_user", messages:[], data:{}, alive:true, type:threadType}`）；
   预创建失败则退回 `createThread:true`。
6. 想做“OpenAI 无状态”模式：每次新 thread（createThread true），但会有 3s 左右首字延迟 + 首页会话堆积。

---

## 5. 响应流格式（NDJSON）

### 5.1 总览

- `Content-Type: application/x-ndjson`（实测线上可能省略）；**不是 SSE**，不要按 `data:` 分行解析。
- 每行一个独立 JSON；**没有 `[DONE]` 结束标记**；HTTP EOF 即结束。
- 记录分类（来自 `ref/maverickxone/app/stream_parser.py:766-1010`、`ref/omniroute/open-sse/services/notionStreamParser.ts`）：
  1. `{"type":"patch-start","data":{"s":[...]},"version":1}` —— 流开始/错误容器；
  2. `{"type":"patch","v":[ patch, ... ]}` —— 主体（文本增量/快照）；
  3. `{"type":"record-map","recordMap":{...}}` —— **终端权威快照**；
  4. `{"type":"markdown-chat","value":"..."}` / `{"type":"agent-inference","value":[...]}` —— 直出事件；
  5. `{"value":[["..."]]}` —— 旧版裸 rich-text 累计快照（无 type）；
  6. `{"type":"error","subType":"...","message":"...","isRetryable":false}` 或嵌在 `patch-start.data.s[]` 里。

### 5.2 patch 对象结构

```json
{"o":"a","p":"/s/-","v":{"id":"<uuid>","type":"agent-inference","value":[]}}
{"o":"a","p":"/s/2/value/-","v":{"id":"<uuid>","type":"text","content":"Hello"}}
{"o":"x","p":"/s/2/value/0/content","v":" world"}
{"o":"p","p":"/s/2/value/0/content","v":"replacement text"}
```

| 字段 | 说明 |
|---|---|
| `o` | 操作：`"a"`=append（新建 step 或 value block）；`"x"`=扩展/追加字符串；`"p"`=replace/path 替换 |
| `p` | 路径字符串：`/s/<index>[/value/<vi>[/content]]`；`/s/-` 表示新 step，`/s/N/value/-` 表示该 step 新 value block。（参考实现也兼容 list 形式，`_normalize_path` 用 "/" join） |
| `v` | 新建时是对象；`x`/`p` 追加时是**字符串片段** |
| `type` | 有时 patch 顶层也带 `type`（`_extract_text_from_patch` 会看 `patch["type"]` 与 `v["type"]`） |

### 5.3 rich-text 结构

- user/旧式：`value = [[text], [text, marks]]`，每段第一位是字符串文本。
- agent-inference：`value = [{"type":"text","content":"..."}]`（对象数组，只取 `type=="text"` 的 `content`）。
- markdown-chat：`value` 直接是字符串（或可递归取 `value/content/text`）。
- 文本里会夹 **Notion 内部标记**：`<lang primary="zh-CN">…</lang>` / `<lang .../>` / 孤立属性残片
  `primary="zh-CN"`。必须清理（参考实现有 3 层正则：`_strip_lang_tags`、
  `_strip_primary_attr_fragments`、`_clean_notion_markup`，`stream_parser.py:65-186`）。
- 搜索引用脚注 `[^1]`、文末脚注定义需要剥离（`PROJECT_PROGRESS.md:455`）。

### 5.4 累计快照 vs 增量（关键，两种语义并存）

| 记录 | 语义 | 证据 |
|---|---|---|
| 裸 `{"value":[[...]]}` | **累计快照**：每行是当前完整文本，只保留最后一条 | OmniRoute 注释 + 测试（`executor-notion-web.test.ts:578-585`）；`#7163` 曾因逐帧拼接导致重复 |
| `o:"a"` 新建 step（`/s/-`） | 新段落开始，`v.type` 定角色 | `stream_parser.py:831-882` |
| `o:"a"` 新建 value block（`/s/N/value/-`） | 新文本块；参考实现当“增量片段”拼接（notion2api）或“快照”覆盖（OmniRoute） | **两派做法冲突 → 语义未确认** |
| `o:"x"` / `o:"p"` path 字符串 | **增量**：追加/替换片段 | `stream_parser.py:437-445` |
| `record-map.thread_message[*].value.value.step` | **最终权威全文**（`agent-inference` 或 `markdown-chat`） | 三个实现都优先用它 |

**给 Lead 的健壮策略（推荐照抄）**：

1. 流式过程中维护每个段落（step index / value index）的 `acc` 字符串。
2. 每拿到一个文本片段 `frag`：
   - `frag.startswith(acc)` → 快照更新：`delta = frag[len(acc):]`，`acc = frag`；
   - `acc.startswith(frag)` → 忽略（重复快照）；
   - 否则 `acc += frag; delta = frag`（增量拼接）。
   这样同时兼容快照与增量，不会重复也不会漏。
3. 流结束时如果收到 `record-map`，用其中的权威全文**替换**已累积的正文（非流式直接用它）。
4. 对“思考/工具”段落单独累积，映射到 OpenAI `reasoning_content`（或丢弃），不要混进 `content`。
5. 正文与思考可能**重复**（thinking 泄露），参考实现用 `SequenceMatcher`/后缀重叠做后处理
   （`_trim_redundant_thinking` / `_build_thinking_replacement`，`chat.py:305-412`）；初版可先不做。

### 5.5 段落/记录角色识别

| Notion 侧标记 | 归类 | 处理 |
|---|---|---|
| patch `v.type == "text"` | 正文 | → `delta.content` |
| patch `v.type == "markdown-chat"` | 正文（最终全文） | → `delta.content`（覆盖式） |
| patch `v.type == "agent-inference"` | **思考/推理**（多数模型） | → `reasoning_content`（或丢弃） |
| patch `v.type == "agent-tool-result"` / `tool` / `search` / `web` / `citation` | 工具/搜索 | → 搜索面板元数据，不入正文 |
| `v.type == "title"` | 标题 | 丢弃 |
| `record-map` step type `text` (优先级 350) / `markdown-chat` (400) | **正文最终答案** | 优先于 `agent-inference`(300)（`FINAL_STEP_PRIORITIES`，`stream_parser.py:57-62`） |
| `record-map` step type `agent-inference` | 若没有 text/markdown-chat → 它就是正文；否则是 thinking | 见 `_extract_final_content_from_record_map`，`stream_parser.py:559-661`；OmniRoute 取最长候选 |
| `{type:"error", subType:"temporarily-unavailable"}` | 上游错误 | 重试（见 §7） |

### 5.6 搜索记录的识别

两条路径（`stream_parser.py:250-477, 961-1001`）：

1. **结构化 patch**：patch 路径或 type 含 `search/web/tool/citation`，或 `v` 里出现
   `queries/questions/sources/citations/results/url/urls/...` 键 → 收集
   `{queries:[...], sources:[{title,url,snippet}], categories:[...]}` 去重后作为 `search` 事件输出。
2. **文本 JSON 片段**：正文流里会出现 `{"default":..., "questions":[...], "queries":...}` 形状的
   JSON 字符串（被拆成多个 patch）；按括号深度累积后 `json.loads` 提取，然后**该片段不进入正文**。
3. `debugOverrides.emitAgentSearchExtractedResults: true` 让上游把搜索结果以结构化 patch 发出。
   关闭则正文里只剩引用脚注（需自行剥离）。

### 5.7 真实 NDJSON 样例（来自 OmniRoute fixture，`ref/omniroute_tests/executor-notion-web.test.ts:179-199, 592-622`）

```text
{"type":"patch-start","data":{"s":[]},"version":1}
{"type":"patch","v":[{"o":"a","p":"/s/-","v":{"id":"...","type":"agent-inference","value":[]}}]}
{"type":"patch","v":[{"o":"a","p":"/s/2/value/-","v":{"id":"...","type":"text","content":"<lang primary=\"en-US\"/>"}}]}
{"type":"patch","v":[{"o":"x","p":"/s/2/value/0/content","v":"Hello"}]}
{"type":"patch","v":[{"o":"x","p":"/s/2/value/0/content","v":" there"}]}
{"type":"record-map","recordMap":{"thread_message":{"m1":{"value":{"value":{"step":{"type":"agent-inference","value":[{"type":"text","content":"<lang primary=\"en-US\"/>Hello there!"}]}}}}}}}
```

兼容性说明：如果上游/反代把 NDJSON 重新包成 SSE，解析器应兼容行首 `data: ` 前缀
（OmniRoute `notionStreamParser.ts:168-183` 就做了这个剥离）。

### 5.8 解析伪代码（Python 可直接照写）

```python
import json, uuid

def iter_records(resp):
    for raw in resp.iter_lines(decode_unicode=True):
        if not raw: continue
        line = raw.strip()
        if line.startswith("data:"): line = line[5:].strip()   # 兼容被包成 SSE
        if not line or line == "[DONE]": continue
        try: rec = json.loads(line)
        except json.JSONDecodeError: continue
        yield rec

def parse_inference(resp):
    """返回 (content, thinking, search, final_from_record_map)"""
    content, thinking = "", ""
    search = {"queries": [], "sources": []}
    final_text = ""
    seg_acc = {}          # (step_idx, value_idx) -> acc text
    for rec in iter_records(resp):
        t = rec.get("type")
        if t == "error":
            raise UpstreamError(rec.get("subType"), rec.get("message"), retry=True)
        if t == "patch-start":
            for item in (rec.get("data") or {}).get("s") or []:
                if item.get("type") == "error":
                    raise UpstreamError(item.get("subType"), item.get("message"), retry=True)
            continue
        if t == "record-map":
            final_text = extract_record_map_final(rec["recordMap"]) or final_text
            continue
        if t == "markdown-chat" and isinstance(rec.get("value"), str):
            final_text = clean(rec["value"]) or final_text; continue
        if t == "agent-inference":
            thinking += extract_value_items(rec.get("value")); continue
        if t == "patch":
            for op in rec.get("v") or []:
                o, p, v = op.get("o"), op.get("p") or "", op.get("v")
                if isinstance(v, str):                       # 增量
                    key = p
                    frag = clean(v)
                    if key not in seg_acc: seg_acc[key] = ""
                    seg_acc[key] = merge(seg_acc[key], frag)
                    emit(content_delta_for(key, frag))
                elif isinstance(v, dict):
                    typ = (op.get("type") or v.get("type") or "").lower()
                    txt = extract_block_text(v)               # rich-text / content items
                    if not txt: continue
                    if typ == "title": continue
                    if typ in ("text", "markdown-chat"):      # 正文
                        key = p
                        if p.endswith("/s/-"): key = f"new:{uuid.uuid4()}"
                        seg_acc[key] = merge(seg_acc.get(key, ""), txt)
                        emit(content_delta(txt))
                    elif typ == "agent-inference":            # thinking
                        emit_thinking(clean(txt))
                    else:                                     # tool/search
                        collect_search(v, search)
    # record-map 权威化
    if final_text: content = final_text
    return content, thinking, search

def merge(acc, frag):
    if frag.startswith(acc): return frag          # 快照
    if acc.startswith(frag): return acc           # 重复快照
    return acc + frag                             # 增量

def clean(s):
    import re
    s = re.sub(r"<lang\b[^>]*>(.*?)</lang>", r"\1", s, flags=re.S)
    s = re.sub(r"</?lang\b[^>]*>", "", s)
    s = re.sub(r'\bprimary="[a-zA-Z\-]{1,15}"\s*', "", s)
    return s.strip()
```

> record-map 提取：`recordMap.thread_message[*].value.value.step`，`step.type in
> ("text","markdown-chat","agent-inference")`，按 §5.5 优先级取；`FINAL_STEP_PRIORITIES` +
> 最长文本兜底。完整参考：`ref/maverickxone/app/stream_parser.py:559-661`、
> `ref/omniroute/open-sse/services/notionStreamParser.ts:44-80, 192-215`。

---

## 6. 非流式 / 流式的 OpenAI 映射（参考实现做法）

- 非流式：直接返回 `chat.completion`，`choices[0].message.content` = 最终正文；
  可加扩展字段：`message.reasoning_content`（思考）、`message.thinking`、搜索元数据；
  usage 是假的（Notion 不返回 token 数），OmniRoute 按 `len/4` 估算并标 `estimated:true`
  （`notion-web.ts:234-248`）。
- 流式（OpenAI SSE）：
  - `data: {"choices":[{"delta":{"content":"..."}}]}`；
  - 思考：`delta.reasoning_content`（`chat.py:148-172`）或自定义事件 `{"type":"thinking_chunk","text":...}`；
  - 搜索：`{"type":"search_metadata","searches":{...}}`；
  - 文本最终修正：`{"type":"content_replace","content":"..."}`（前端协议，可选）；
  - 结束：`{"choices":[{"delta":{},"finish_reason":"stop"}]}` 然后 `data: [DONE]\n\n`。
- OmniRoute 因为不确定增量语义，直接**伪流式**：读完整个上游 body，解析出全文，再一次性发一个
  SSE chunk + `[DONE]`（`notion-web.ts:278-305`）。**首版建议照此实现**，稳定后再做真流式。

---

## 7. 错误码、限流、Cloudflare / TLS

### 7.1 HTTP 状态

| 状态 | 表现 | 处理 |
|---|---|---|
| **401** | JSON：`{"isNotionError":true,"errorId":"<uuid>","name":"UnauthorizedError","debugMessage":"Must be authenticated.","message":"Something went wrong. (401)"}`（**实测原文**） | `token_v2` 过期/失效/域不对 → 报 401，提示重新取 cookie；换 host 再试一次 |
| **403** | 可能是 CF 挑战 HTML（`cf-chl`/Just a moment），也可能是账号/工作区 AI 受限 | 重建 cloudscraper/换 TLS 指纹 + 浏览器头重试一次；仍 403 提示换账号 |
| **429** | Notion 工作区限流 | 等 10–30s / 换账号；pdxjie **不重试**（避免冷却），maverickxone **允许重试**（`>=500 or ==429`） |
| **400** | 常见 `createThread:false` + 未知 threadId → ValidationError；也可能 config/transcript 字段不合法 | 检查 thread 生命周期；首轮必须 createThread:true |
| **5xx** | Notion 抖动 | 重试（同 threadId，`createThread:false`） |

### 7.2 HTTP 200 但 in-band 错误（很常见，最容易误判为“空回复”）

```json
{"id":"e1","type":"error","message":"Something went wrong. Please try again later.","subType":"temporarily-unavailable","isRetryable":false}
```
也可能嵌套在 patch-start 里：
```json
{"type":"patch-start","data":{"s":[{"type":"error","message":"Something went wrong. Please try again later.","subType":"temporarily-unavailable","isRetryable":false}]},"version":1}
```
处理（`notionStreamParser.ts:221-305`）：
- 判定 `type=="error"` 或存在 `subType`；
- `subType` 含 `temporarily`/`unavailable`/`rate`/`timeout`/`overloaded` → **视为可重试**
  （上游常把 `isRetryable` 置 false，但短等后重试会成功）；
- 重试策略：同 threadId、`createThread:false`、延迟 ~0.7–1.1s；仍失败 → 502/503。
- 若整条流无任何可解析文本 → `502 NOTION_EMPTY`（参考实现显式判空）。

### 7.3 代理侧结构化错误（可借鉴的 11 个错误码，`ref/maverickxone/app/api/chat.py:31-128`）

`NOTION_401` / `NOTION_403` / `NOTION_429` / `NOTION_5XX` / `NETWORK_TIMEOUT` / `NETWORK_ERROR` /
`NOTION_EMPTY` / `UPSTREAM_UNKNOWN` / `POOL_COOLING` / …，统一返回
`{"error":{"message","type","code","suggestion","detail"}}` + 503。Lead 可以照抄这个错误结构。

### 7.4 Cloudflare / TLS 结论（重点）

- 实测：`app.notion.com` 与 `www.notion.so` **都在 Cloudflare 后面**（`Server: cloudflare`、
  `CF-Ray`、`__cf_bm` cookie）；但本机 Windows `curl 8.21 (Schannel)` 不带任何 cookie 直接 POST
  就拿到 **JSON 401**，说明 **CF 没有拦截普通 TLS 客户端**（至少对该路径/该出口 IP）。
- OmniRoute 关键发现（`ref/omniroute/open-sse/executors/notion-web.ts:20-29, 495-560`）：
  - **Node/undici 默认 TLS 指纹会被 Notion 边缘拒绝**，表现为 **HTTP 200 + in-band
    `temporarily-unavailable` + 空正文**；
  - 同样 cookie/body 下，**curl/Schannel 和 Chrome 正常**；
  - 因此 OmniRoute 固定走 **wreq-js 的 Chrome JA3/JA4 指纹**（`notionTlsClient.ts:22-32`，
    `tlsProfile:"chrome_146"`, `emulationOs:"windows"`）。
  → **Python 代理建议用 `curl_cffi.requests` 并 `impersonate="chrome"`（或 "chrome136"/最新）**；
    纯 `requests`/httpx 有被 CF 软拦风险。
- notion2api 的做法：
  - `cloudscraper.create_scraper()` 自动过 CF JS 挑战；
  - **每账号复用一个 scraper 实例**，保留 challenge cookie（`notion_client.py:63-65`）；
  - 403 时**重建 scraper** 重试一次（`notion_client.py:347-363`）；
  - Cookie 用 header 字符串传，绕开 cookie jar 非 ASCII 编码问题；
  - 带完整浏览器头；UA 用最新 Chrome（`Chrome/145`/`152`/`146` 各版本）。
- 若仍然空流/403：加**出口代理**（住宅 IP 更稳）、降低频率、多账号轮换。
- **风控提醒**：Notion 可能对“服务器 IP 大量建 thread / 高频”暂停工作区 AI 功能，
  Business Trial 工作区尤其敏感（`ref/maverickxone/docs/issues.md:131-146`）。
  本地 Windows + 单账号 + 低频使用风险较低，但仍建议复用 thread、不要每请求新建。

### 7.5 限流与重试参数（参考）

| 实现 | 上游超时 | 重试 | 冷却 |
|---|---|---|---|
| pdxjie | `(15, 120)`（connect, read） | ≥500 重试；**429 不重试**（`notion_client.py:319`） | — |
| maverickxone | `(15, 120)` | `max(3, 账号数)` 次，429/5xx 可重试（`account_pool.py`） | 每账号 3s；全冷却时最多等 15s |
| OmniRoute | 180s（env `OMNIROUTE_NOTION_TLS_TIMEOUT_MS`） | 1 次自动重试（约 0.7–1.1s），同 threadId | 连接冷却由上层管 |

代理侧限流参考：Lite 30/min、Standard 25/min、Heavy 20/min（按 IP）。

---

## 8. 参考实现代码位置索引（供 Lead 借鉴）

### 8.1 pdxjie（`work/ai-proxy/ref/pdxjie/`）

| 文件:行 | 内容 |
|---|---|
| `app/notion_client.py:47` | endpoint `https://www.notion.so/api/v3/runInferenceTranscript` |
| `app/notion_client.py:91-97` | saveTransactions 用的轻量 headers |
| `app/notion_client.py:99-208` | thread 预创建 / 删除（saveTransactions transaction 结构） |
| `app/notion_client.py:246-261` | 正式请求的 cookies + headers |
| `app/notion_client.py:263-289` | 请求体（含 `debugOverrides`） |
| `app/notion_client.py:308-352` | cloudscraper 发送 + 错误包装 |
| `app/stream_parser.py:679-1010` | `parse_stream`：段落注册表 + patch 解析 + record-map + 搜索提取 |
| `app/conversation.py:284-399` | `_build_dialog_block` / `_build_config_block` |
| `app/conversation.py:401-418` | `_build_context_block` |
| `app/api/chat.py` | OpenAI 映射、thinking/search 事件、错误分类 |
| `app/model_registry.py:1-7` | 5 个模型映射（旧） |
| `app/account_pool.py` | 轮询 + 冷却 |

### 8.2 maverickxone（`work/ai-proxy/ref/maverickxone/`，信息最全的 Python 实现）

| 文件:行 | 内容 |
|---|---|
| `app/notion_client.py:19-22` | `NOTION_CLIENT_VERSION` / `NOTION_URL` env |
| `app/notion_client.py:59-60` | endpoint 拼接 |
| `app/notion_client.py:67-70` | Cookie header 拼装（token_v2 + notion_user_id） |
| `app/notion_client.py:273-290` | **完整 headers（含 sec-fetch 指纹）** |
| `app/notion_client.py:292-318` | **请求体** |
| `app/notion_client.py:336-410` | cloudscraper + 403 重建 + 错误处理 |
| `app/conversation.py:288-310` | `_build_dialog_block`（user/assistant/agent-inference） |
| `app/conversation.py:312-399` | `_build_config_block`（**长版 config 字段全表**） |
| `app/conversation.py:401-418` | `_build_context_block` |
| `app/conversation.py:1989-2115` | `build_lite_transcript` / `build_standard_transcript` |
| `app/stream_parser.py:734-1010` | **主解析器（段落注册表版）** |
| `app/stream_parser.py:559-661` | record-map 最终内容提取 + 优先级 |
| `app/stream_parser.py:417-446` | patch 文本提取（`a`/`x`/`p`） |
| `app/api/chat.py:31-128` | 结构化错误码 |
| `app/api/chat.py:148-172` | OpenAI SSE chunk 构造 |
| `app/api/chat.py:634+` | Standard 模式流式生成器（thinking/search 事件） |
| `app/model_registry.py:1-25` | **23 模型映射** |
| `app/account_pool.py:90-114` | 冷却逻辑 |
| `docs/PROJECT_PROGRESS.md:231-312` | 协议/模型/反爬总结 |
| `docs/issues.md` | 401/429/风控排查 |

### 8.3 OmniRoute（`work/ai-proxy/ref/omniroute/`，2026 最新 + 活体验证）

| 文件:行 | 内容 |
|---|---|
| `open-sse/executors/notion-web.ts:86-93` | `BASE_URL=https://app.notion.com`、UA、client-version |
| `open-sse/executors/notion-web.ts:349-392` | **请求体**（含 `patchResponseVersion:2`、`createdSource`） |
| `open-sse/executors/notion-web.ts:394-420` | **headers**（含 `BROWSER_HEADERS`） |
| `open-sse/executors/notion-web.ts:495-560` | TLS 请求 + 401/403/429/5xx 分类 |
| `open-sse/services/notionTranscriptBuilder.ts:31-194` | config/context/message → transcript；follow-up 只发增量 |
| `open-sse/services/notionStreamParser.ts:192-215` | NDJSON 解析 + 最长候选选择 |
| `open-sse/services/notionStreamParser.ts:221-305` | **in-band error 识别（含嵌套 patch-start.data.s）** |
| `open-sse/services/notionWebModels.ts:23-50` | `getAvailableModels` / `getSpaces` + BROWSER_HEADERS |
| `open-sse/services/notionWebModels.ts:254-296` | 模型发现响应解析 + 请求头/体构造 |
| `open-sse/services/notionWebModels.ts:731-746` | `resolveNotionCodename` |
| `open-sse/services/notionTlsClient.ts:22-32` | **Chrome TLS 指纹配置（wreq-js, chrome_146）** |
| `open-sse/services/notionThreadSessions.ts` | thread 绑定策略（前缀 hash / sticky / 重试不新建） |
| `open-sse/services/notionWebFallbackModels.ts:31-282` | 43 条离线模型表 |
| `tests/.../executor-notion-web.test.ts:179-199, 592-622` | **真实 NDJSON fixture** |
| `PR #7600 patch`：`omniroute_pr7600_patches/open-sse__executors__notion-web.ts.patch` | 首版（`human`/`ai` transcript、www.notion.so） |

### 8.4 前端 bundle 佐证（app.notion.com 当前站点）

- 抓到的 `/ai`（被 307 到 app）与 `/chat` HTML 都加载同一套 `/_assets/*.js`。
- chunk 名→hash 映射在 `app-31aac002d32f7f8a.js`（webpack `d.u=` map）。
- **`agentWriter-6d0fa11af18f6b78.js` 内含 `callStreamApi({eventName:"runInferenceTranscript", ...})`**，
  请求体形如：
  ```js
  {transcript:[{type:"config", value:{type:"debug-inference"|"memory-agent", model, ...}}],
   traceId, spaceId, generateTitle:false,
   isUserInAnySalesAssistedSpace:..., isSpaceSalesAssisted:...}
  ```
  且调用时带 `headers: WS({spaceId})`（前端自己的 header 构造器），并 `for await` 逐条消费
  `e.type`（`debug-inference`/`memory-agent`/`error`）。
- 结论：**app.notion.com 的前端确实在用同一个 `runInferenceTranscript` API 路径**
  （`/api/v3/` 前缀 + 同源调用；eventName→URL 的映射模块未定位到，**未确认**具体拼装代码，
  但实测 401 证明 URL 存在）。
- 该 chunk 还含 `getInferenceTranscriptInferences`、`downloadInferenceTranscriptArtifacts`、
  `saveInferenceTranscriptFeedback` 等同类接口。

---

## 9. 未确认 / 风险清单（如实标注）

1. **`token_v2` 跨域有效性**：app.notion.com 登录态 cookie 能否直接用于 www.notion.so（及反向），
   未实测（无凭据）。建议 cookie 与 host 同源使用。
2. **`o:"a"` 新建 value block 是增量还是快照**：notion2api 当增量拼接、OmniRoute 当快照覆盖；
   本机无凭据无法抓真实流验证。§5.4 的 merge 策略可同时兼容。
3. **`reasoningEffort` 合法取值/是否生效**：仅从前端 bundle 看到字段，参考实现均未使用。
4. **`useWebSearch:false` 是否真的关闭搜索**：未验证。
5. **`notion-client-version` 是否严格校验**：未验证；版本号已偏旧。
6. **`human` vs `user`**：现行用 `user`；`human` 据 OmniRoute 注释在 createThread:true 下也可用。
7. **in-band error 的其它 `subType`**（限流/校验类）：只确认了 `temporarily-unavailable`。
8. **CF 对 Python `requests` 的容忍度**：本机 curl 通过；Python 具体指纹未测。用 curl_cffi 最稳。
9. **Notion 免费/Plus 账号的模型可用性**：模型列表与计划相关（`getAvailableModels.isDisabled`），
   需用户账号实际探测。
10. **限流阈值**：未确认具体 QPS/RPM，仅“每工作区有限制、429 恢复较快”的定性结论。

---

## 10. 给 Lead 的最小可行实现路径（建议）

1. 让用户提供（或从浏览器复制的）**完整 Cookie 请求头**（app.notion.com 或 www.notion.so，
   与所选 BASE 同源）+ `space_id`（没有就让用户跑 `ref/maverickxone/scripts/extract_notion_info.js`，
   或代理自己调 `POST /api/v3/getSpaces` 推导）。
2. 代理：`POST {BASE}/api/v3/runInferenceTranscript`，headers 照 §3.2（先用 §3.1 的保守集合 +
   sec-fetch 指纹 + 最新 Chrome UA），`Accept: application/x-ndjson`，`stream=True`，超时 `(15,180)`。
3. 请求体照 §4.1：首轮 `createThread:true` + 新 threadId；一个 OpenAI conversation 复用一个 threadId
   （内存 dict 即可），后续 `createThread:false` + `isPartialTranscript:true`。
4. 模型：先用 §4.5 的 23 条表（默认 `claude-sonnet4.6` / `almond-croissant-low`）；
   跑通后换成 `getAvailableModels` 动态发现。
5. 解析：用 §5.8 伪代码（patch + record-map + merge + lang 清理 + in-band error）。
   首版直接**伪流式**（读完再发 SSE chunk + `[DONE]`），减少不确定性。
6. 传 `useWebSearch:true`、`debugOverrides.emitAgentSearchExtractedResults:true`；
   搜索/思考先不暴露，稳定后再做 thinking/search 面板字段。
7. HTTP 客户端：Python 用 `curl_cffi.requests.Session(impersonate="chrome")`；
   若用 `requests`，加 `cloudscraper` 兜底并复用实例。403→重建会话重试一次；429→退避。
8. 错误映射：401→提示重取 cookie；403→403/502；429→429（带 Retry-After 可选）；
   空流/in-band error→重试 1 次后 502/503。
9. 冒烟测试顺序：`POST /api/v3/getSpaces`（验证 cookie）→ `getAvailableModels`（验证 space）
   → `runInferenceTranscript` 单轮非流式 → 流式 → 多轮 thread 复用。
