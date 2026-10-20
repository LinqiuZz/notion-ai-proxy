#!/usr/bin/env node
'use strict';

/**
 * notion-proxy.js — 本地 OpenAI 兼容网关，把 Notion AI（网页登录态）转成标准 API。
 *
 * 设计要点
 * - 零 npm 依赖：只用 Node 内置模块。
 * - 上游请求走 Windows 自带 curl.exe（Schannel TLS）。Notion 的边缘节点会按 TLS 指纹
 *   拒绝 Node/OpenSSL 的握手（表现为 HTTP 200 + 空流 / in-band temporarily-unavailable）。
 *   本机实测 curl/Schannel 与 Chrome 指纹都能通过。
 * - 鉴权：读取 secret/credential.json（由 tools/cdp_cookie_extract.py 从本机 Edge 提取）。
 * - 模型：启动时读取 secret/workspace.json（getAvailableModels 的结果），也可自动重新发现。
 * - 响应：解析 Notion 的 NDJSON（patch-start / patch / record-map），取 record-map 的
 *   最终 step 作为权威全文；返回 OpenAI 的 chat.completion / SSE chunk。
 *
 * 用法：
 *   node notion-proxy.js                启动服务（默认 http://127.0.0.1:8787/v1）
 *   node notion-proxy.js --check        只验证登录态与模型列表，然后退出
 *   node notion-proxy.js --port 9000    临时覆盖端口
 */

const http = require('http');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');
const crypto = require('crypto');

// Windows 控制台默认编码是 GBK，切到 UTF-8 让中文日志正常显示（失败无影响）
if (process.platform === 'win32') {
  try {
    require('child_process').spawnSync('chcp', ['65001'], { stdio: 'ignore', windowsHide: true });
  } catch (_) {
    /* ignore */
  }
}

// 打包成单文件 exe 时（Node SEA / pkg），配置与 secret 放在 exe 同目录。
function isPackaged() {
  if (typeof process.pkg !== 'undefined') return true;
  try {
    return require('node:sea').isSea();
  } catch (_) {
    return false;
  }
}

const PACKAGED = isPackaged();
const ROOT = PACKAGED ? path.dirname(process.execPath) : __dirname;
const CONFIG_PATH = path.join(ROOT, 'config.json');

const DEFAULT_CONFIG = {
  port: 8787,
  bind: '127.0.0.1',
  upstreamHost: 'https://app.notion.com',
  curlPath: 'curl.exe',
  python: 'python',
  credentialFile: 'secret/credential.json',
  workspaceFile: 'secret/workspace.json',
  refreshScript: 'tools/cdp_cookie_extract.py',
  apiKey: '',
  defaultModel: 'claude-sonnet4.6',
  defaultThreadType: 'workflow',
  markdownChatModels: ['vertex-gemini-3.5-flash'],
  useWebSearch: false,
  includeReasoning: true,
  streamMode: 'pseudo',
  requestTimeoutMs: 300000,
  maxBodyBytes: 33554432,
  maxConcurrent: 2,
  retries: 2,
  autoRefreshCookie: true,
  validateOnStart: true,
  debugSaveRaw: false,
  systemPromptMode: 'prefix',
  threadMode: 'new', // new = 每个请求新建 Notion 会话；single = 复用同一个会话（省会话数）
  threadReuseMaxTurns: 30, // single 模式下，同一会话最多复用多少轮，超过就换新会话
  threadReuseMaxInputTokens: 300000, // single 模式下，单次输入超过这个 token 数就换新会话
  threadStateFile: 'secret/thread-state.json',
  enableAttachments: true, // 支持图片/文件附件（走 Notion 聊天专用上传接口）
  attachmentMaxBytes: 14680064, // 单个附件上限 14MB（Notion 签名策略上限 20MB）
  clientVersion: '23.13.20260720.1949',
  userAgent:
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
    '(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36',
};

// 已知的对外模型别名 -> Notion 内部食物代号（老版本兼容；新模型从 /v1/models 动态获取）
const KNOWN_ALIASES = {
  'gpt-5.6-sol': 'orange-mousse',
  'gpt-5.6-terra': 'orchid-muffin',
  'gpt-5.6-luna': 'olive-jellyroll',
  'gpt-5.4': 'oval-kumquat-medium',
  'gpt-5.5': 'opal-quince-medium',
  'gemini-3.5flash': 'vertex-gemini-3.5-flash',
  'claude-sonnet4.6': 'almond-croissant-low',
  'claude-sonnet5': 'angel-cake-high',
  'claude-opus4.6': 'avocado-froyo-medium',
  'claude-opus4.7': 'apricot-sorbet-high',
  'claude-opus4.8': 'ambrosia-tart-high',
  'claude-opus5': 'agave-flan',
  'kimi-2.7': 'fireworks-kimi-k2.7',
  'deepseek-v4pro': 'baseten-deepseek-v4-pro',
  'glm-5.2': 'baseten-glm-5.2',
  'grok-4.3': 'xigua-mochi-medium',
  'spacexai-4.5': 'strawberry-whoopiepie',
  'grok-build0.1': 'xinomavro-cake',
  'gemini-3.1pro': 'galette-medium-thinking',
  'claude-haiku4.5': 'anthropic-haiku-4.5',
  'fable-5': 'acai-budino-high',
  'gemini-3flash': 'gingerbread',
  'gpt-6-astra': 'orlando-quinn',
};

const REVERSE_ALIASES = Object.fromEntries(
  Object.entries(KNOWN_ALIASES).map(([alias, code]) => [code, alias])
);

function loadConfig() {
  let user = {};
  if (fs.existsSync(CONFIG_PATH)) {
    try {
      user = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8'));
    } catch (e) {
      console.warn(`[warn] config.json 解析失败，使用默认配置: ${e.message}`);
    }
  }
  return { ...DEFAULT_CONFIG, ...user };
}

const cfg = loadConfig();
const PATHS = {
  credential: path.resolve(ROOT, cfg.credentialFile),
  workspace: path.resolve(ROOT, cfg.workspaceFile),
  refreshScript: path.resolve(ROOT, cfg.refreshScript),
  threadState: path.resolve(ROOT, cfg.threadStateFile || 'secret/thread-state.json'),
};

const state = {
  cred: null,
  workspace: null,
  registry: null,
  startedAt: Date.now(),
  lastAuthError: null,
  requireAuth: false,
};

function log(...args) {
  const t = new Date().toISOString().slice(11, 19);
  console.log(`[${t}]`, ...args);
}

function uuid() {
  return crypto.randomUUID();
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function mask(id) {
  if (!id) return '';
  return `${String(id).slice(0, 8)}…`;
}

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

function writeJson(file, obj) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, JSON.stringify(obj, null, 2), 'utf8');
}

class HttpError extends Error {
  constructor(status, message, extra) {
    super(message);
    this.status = status;
    this.extra = extra || {};
  }
}

// ───────────────────────────── 凭据 / 工作区 ─────────────────────────────

function loadCredential() {
  if (!fs.existsSync(PATHS.credential)) {
    throw new Error(
      `缺少登录态文件 ${PATHS.credential}。请先运行：python tools\\cdp_cookie_extract.py`
    );
  }
  const cred = readJson(PATHS.credential);
  if (!cred.token_v2 && !cred.cookie_header) {
    throw new Error(`凭据文件缺少 token_v2：${PATHS.credential}`);
  }
  if (!cred.cookie_header) {
    const parts = [`token_v2=${cred.token_v2}`];
    if (cred.notion_user_id) parts.push(`notion_user_id=${cred.notion_user_id}`);
    if (cred.notion_browser_id) parts.push(`notion_browser_id=${cred.notion_browser_id}`);
    cred.cookie_header = parts.join('; ');
  }
  return cred;
}

function normalizeModelKey(text) {
  return String(text || '')
    .toLowerCase()
    .replace(/[^a-z0-9]/g, '');
}

function buildRegistry(ws) {
  const entries = [];
  const byId = new Map();
  const byNorm = new Map();
  const register = (key, entry) => {
    if (!key) return;
    byId.set(String(key).toLowerCase(), entry);
    byNorm.set(normalizeModelKey(key), entry);
  };
  const models = Array.isArray(ws && ws.models) ? ws.models : [];
  for (const m of models) {
    const code = m && m.model;
    if (!code) continue;
    const display = m.display || code;
    const displaySlug = slug(display) || slug(code);
    // 有历史别名时优先用别名做对外 id（如 claude-sonnet4.6），否则用显示名 slug
    const id = REVERSE_ALIASES[code] || displaySlug;
    const entry = {
      id,
      code,
      display,
      family: m.family || '',
      disabled: !!m.disabled,
      efforts: Array.isArray(m.efforts) ? m.efforts : [],
    };
    entries.push(entry);
    register(id, entry);
    register(code, entry);
    register(display, entry);
    register(displaySlug, entry);
  }
  // 老别名映射到动态模型
  for (const [alias, code] of Object.entries(KNOWN_ALIASES)) {
    const hit = byId.get(code.toLowerCase()) || byNorm.get(normalizeModelKey(code));
    if (hit) register(alias, hit);
  }
  if (!entries.length) {
    // 极端降级：没有模型列表时用已知别名生成
    for (const [alias, code] of Object.entries(KNOWN_ALIASES)) {
      const entry = { id: alias, code, display: alias, family: '', disabled: false, efforts: [] };
      entries.push(entry);
      register(alias, entry);
      register(code, entry);
    }
  }
  return { entries, byId, byNorm };
}

function slug(text) {
  return String(text || '')
    .toLowerCase()
    .replace(/[^a-z0-9.]+/g, '-')
    .replace(/^-+|-+$/g, '');
}

function resolveModel(requested) {
  const reg = state.registry;
  let id = String(requested || cfg.defaultModel || '').trim();
  if (id) {
    id = id
      .replace(/^(anthropic|openai|notion-web|nw)\//i, '')
      .replace(/^notion-web\//i, '')
      .replace(/^nw\//i, '')
      .trim();
    const direct = reg.byId.get(id.toLowerCase()) || reg.byNorm.get(normalizeModelKey(id));
    if (direct) return { entry: direct, requested: requested || direct.id };
    const aliasCode = KNOWN_ALIASES[id] || KNOWN_ALIASES[id.toLowerCase()];
    if (aliasCode) {
      const hit = reg.byId.get(aliasCode.toLowerCase()) || reg.byNorm.get(normalizeModelKey(aliasCode));
      if (hit) return { entry: hit, requested: requested };
    }
  }
  const fallback =
    reg.byId.get(String(cfg.defaultModel || '').toLowerCase()) ||
    reg.byNorm.get(normalizeModelKey(cfg.defaultModel)) ||
    reg.entries.find((e) => !e.disabled) ||
    reg.entries[0];
  return { entry: fallback, requested: requested || fallback.id, fallback: true };
}

function notionHeaders() {
  const cred = state.cred;
  const ws = state.workspace || {};
  const headers = {
    'Content-Type': 'application/json',
    Accept: 'application/x-ndjson',
    'User-Agent': cfg.userAgent || ws.userAgent || DEFAULT_CONFIG.userAgent,
    Origin: cfg.upstreamHost,
    Referer: `${cfg.upstreamHost}/chat`,
    'notion-client-version': cfg.clientVersion || ws.clientVersion || DEFAULT_CONFIG.clientVersion,
    'notion-audit-log-platform': 'web',
    'sec-ch-ua': '"Chromium";v="146", "Not?A_Brand";v="24", "Google Chrome";v="146"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Windows"',
    'sec-fetch-dest': 'empty',
    'sec-fetch-mode': 'cors',
    'sec-fetch-site': 'same-origin',
    Cookie: cred.cookie_header,
  };
  if (cred.notion_user_id) headers['x-notion-active-user-header'] = cred.notion_user_id;
  if (ws.spaceId) headers['x-notion-space-id'] = ws.spaceId;
  return headers;
}

// ───────────────────────────── curl 上游调用 ─────────────────────────────

function callNotion({ pathname, payload, onRecord, timeoutMs }) {
  return new Promise((resolve, reject) => {
    const bodyText = typeof payload === 'string' ? payload : JSON.stringify(payload);
    const headers = notionHeaders();
    const maxSec = Math.max(5, Math.ceil((timeoutMs || cfg.requestTimeoutMs) / 1000));
    const args = [
      '-sS',
      '-N',
      '--max-time',
      String(maxSec),
      '-X',
      'POST',
      cfg.upstreamHost + pathname,
      '--data-binary',
      '@-',
      '-w',
      '\n__HTTP_STATUS__%{http_code}',
    ];
    for (const [k, v] of Object.entries(headers)) args.push('-H', `${k}: ${v}`);

    let child;
    try {
      child = spawn(cfg.curlPath, args, { windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
    } catch (e) {
      reject(new Error(`无法启动 ${cfg.curlPath}: ${e.message}`));
      return;
    }

    let stdoutBuf = '';
    let rawOut = '';
    let stderr = '';
    let status = 0;
    let settled = false;
    const records = [];
    const timer = setTimeout(() => {
      try {
        child.kill();
      } catch (_) {
        /* ignore */
      }
    }, (timeoutMs || cfg.requestTimeoutMs) + 15000);

    function finish(err, result) {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (err) reject(err);
      else resolve(result);
    }

    function consumeLine(rawLine) {
      const line = rawLine.trim();
      if (!line) return;
      const statusMatch = line.match(/^__HTTP_STATUS__(\d{3})$/);
      if (statusMatch) {
        status = Number(statusMatch[1]);
        return;
      }
      let text = line;
      if (text.startsWith('data:')) text = text.slice(5).trim();
      let rec;
      try {
        rec = JSON.parse(text);
      } catch (_) {
        return;
      }
      records.push(rec);
      if (onRecord) {
        try {
          onRecord(rec);
        } catch (e) {
          log('解析记录异常:', e.message);
        }
      }
    }

    child.on('error', (e) => finish(new Error(`curl 启动失败: ${e.message}`)));
    child.stderr.setEncoding('utf8');
    child.stderr.on('data', (d) => {
      stderr += d;
    });
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (chunk) => {
      stdoutBuf += chunk;
      rawOut += chunk;
      let idx;
      while ((idx = stdoutBuf.indexOf('\n')) !== -1) {
        consumeLine(stdoutBuf.slice(0, idx));
        stdoutBuf = stdoutBuf.slice(idx + 1);
      }
    });
    child.on('close', (code) => {
      if (stdoutBuf.trim()) consumeLine(stdoutBuf);
      if (!status) status = code === 0 ? 0 : -1;
      const rawBody = rawOut.replace(/\n?__HTTP_STATUS__\d{3}\s*$/, '');
      finish(null, { status, records, raw: rawBody, stderr: stderr.trim(), exitCode: code });
    });
    child.stdin.on('error', () => {
      /* curl 提前退出时忽略 EPIPE */
    });
    child.stdin.write(bodyText);
    child.stdin.end();
  });
}

function isCloudflareBlock(text) {
  return /just a moment|cf-chl|challenges\.cloudflare\.com|attention required/i.test(text || '');
}

// ───────────────────────────── 附件（图片 / 文件） ─────────────────────────────
// Notion 网页 AI 支持在对话里附带文件，走专用上传接口（2026-10 抓包确认）：
//   1. POST /api/v3/getUploadFileUrlForAssistantChatTranscriptUpload -> 返回 S3 签名 POST 字段
//   2. multipart POST 到 signedUploadPostUrl                          -> 上传文件实体
//   3. POST /api/v3/processAgentAttachment                            -> 触发解析/审核（返回可忽略）
//   4. runInferenceTranscript 的 transcript 里插入
//      { id, type: "attachment", fileUrl, fileName, contentType, metadata }
// 关键：fileUrl 与 thread 绑定，跨 thread 引用会“图片加载不出来”，所以上传前 thread 必须已存在。
// 新会话模式下先用 saveTransactionsFanout 建空 thread record，再上传，最后用 createThread:false 发请求。

const EXT_BY_MIME = {
  'image/png': '.png',
  'image/jpeg': '.jpg',
  'image/gif': '.gif',
  'image/webp': '.webp',
  'application/pdf': '.pdf',
  'text/plain': '.txt',
  'text/markdown': '.md',
  'text/csv': '.csv',
  'application/json': '.json',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
  'application/vnd.openxmlformats-officedocument.presentationml.presentation': '.pptx',
  'application/zip': '.zip',
};

const EXT_BY_NAME = {
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.webp': 'image/webp',
  '.pdf': 'application/pdf',
  '.txt': 'text/plain',
  '.md': 'text/markdown',
  '.csv': 'text/csv',
  '.json': 'application/json',
  '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  '.zip': 'application/zip',
};

function sniffMime(buf) {
  if (!buf || buf.length < 4) return '';
  const b = buf;
  if (b[0] === 0x89 && b[1] === 0x50 && b[2] === 0x4e && b[3] === 0x47) return 'image/png';
  if (b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) return 'image/jpeg';
  if (b.slice(0, 6).toString('ascii') === 'GIF87a' || b.slice(0, 6).toString('ascii') === 'GIF89a')
    return 'image/gif';
  if (b.slice(0, 4).toString('ascii') === 'RIFF' && b.slice(8, 12).toString('ascii') === 'WEBP')
    return 'image/webp';
  if (b.slice(0, 4).toString('ascii') === '%PDF') return 'application/pdf';
  if (b[0] === 0x50 && b[1] === 0x4b && b[2] === 0x03 && b[3] === 0x04)
    return 'application/zip';
  return '';
}

function imageDimensions(buf, mimeType) {
  try {
    if (mimeType === 'image/png' && buf.length >= 24) {
      return { width: buf.readUInt32BE(16), height: buf.readUInt32BE(20) };
    }
    if (mimeType === 'image/gif' && buf.length >= 10) {
      return { width: buf.readUInt16LE(6), height: buf.readUInt16LE(8) };
    }
    if (mimeType === 'image/jpeg') {
      let i = 2;
      while (i + 9 < buf.length) {
        if (buf[i] !== 0xff) break;
        const marker = buf[i + 1];
        const len = buf.readUInt16BE(i + 2);
        if (marker >= 0xc0 && marker <= 0xcf && marker !== 0xc4 && marker !== 0xc8 && marker !== 0xcc) {
          return { height: buf.readUInt16BE(i + 5), width: buf.readUInt16BE(i + 7) };
        }
        i += 2 + len;
      }
    }
  } catch (_) {
    /* 解析失败就不带尺寸 */
  }
  return null;
}

function extByName(name) {
  const m = String(name || '').toLowerCase().match(/\.[a-z0-9]{1,8}$/);
  return m ? m[0] : '';
}

// 文本类文件在 Notion 里用 computer-file step（AI 通过 computer 工具 uploadFile + cat 读取），
// 图片/PDF 等二进制文件用 attachment step（走视觉/文档解析）。抓包确认（2026-10）。
const TEXTY_EXT = new Set([
  '.txt', '.md', '.markdown', '.csv', '.json', '.yaml', '.yml', '.xml', '.log', '.ini', '.toml',
  '.py', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', '.java', '.c', '.cpp', '.h', '.hpp', '.go',
  '.rs', '.rb', '.php', '.sh', '.bash', '.zsh', '.ps1', '.bat', '.cmd', '.sql', '.html', '.htm',
  '.css', '.scss', '.less', '.vue', '.lua', '.r', '.pl', '.kt', '.swift', '.dart',
]);

function isTextLikeAttachment(mimeType, name) {
  if (TEXTY_EXT.has(extByName(name))) return true;
  const mime = String(mimeType || '').toLowerCase();
  return mime.startsWith('text/') || /^application\/(json|xml|x-ndjson|yaml|x-yaml|csv)\b/.test(mime);
}

// 用 curl 跑一次请求（二进制安全），用于下载图片 / 上传到 S3
function execCurl(args, inputBuffer) {
  return new Promise((resolve, reject) => {
    let child;
    try {
      child = spawn(cfg.curlPath, args, { windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
    } catch (e) {
      reject(new Error(`无法启动 ${cfg.curlPath}: ${e.message}`));
      return;
    }
    const out = [];
    const err = [];
    child.stdout.on('data', (d) => out.push(d));
    child.stderr.on('data', (d) => err.push(d));
    child.on('error', (e) => reject(new Error(`curl 启动失败: ${e.message}`)));
    child.on('close', (code) =>
      resolve({
        stdout: Buffer.concat(out),
        stderr: Buffer.concat(err).toString('utf8').trim(),
        exitCode: code,
      })
    );
    child.stdin.on('error', () => {
      /* 忽略 EPIPE */
    });
    child.stdin.end(inputBuffer || Buffer.alloc(0));
  });
}

async function notionJson(pathname, payload) {
  const res = await callNotion({ pathname, payload });
  const text = String(res.raw || '').trim();
  let obj = res.records && res.records[0];
  if (!obj && text) {
    try {
      obj = JSON.parse(text);
    } catch (_) {
      obj = null;
    }
  }
  if (res.status !== 200 || !obj) {
    const detail = obj && (obj.message || obj.name) ? `${obj.name || 'Error'}: ${obj.message || ''}` : text.slice(0, 300);
    throw new HttpError(502, `Notion ${pathname} 失败（HTTP ${res.status}）${detail ? ': ' + detail : ''}`);
  }
  return obj;
}

// 新会话 + 带附件时：先建一个空的 thread record，让上传接口有可绑定的 thread
async function createThreadRecord(threadId) {
  const spaceId = state.workspace.spaceId;
  const userId = state.cred.notion_user_id || state.workspace.userId || '';
  const nowMs = Date.now();
  await notionJson('/api/v3/saveTransactionsFanout', {
    requestId: uuid(),
    transactions: [
      {
        id: uuid(),
        spaceId,
        debug: { userAction: 'WorkflowActions.createThread', clientCommitTimeMs: nowMs },
        operations: [
          {
            pointer: { table: 'thread', id: threadId, spaceId },
            path: [],
            command: 'set',
            args: {
              id: threadId,
              version: 0,
              space_id: spaceId,
              parent_id: spaceId,
              parent_table: 'space',
              created_by_id: userId,
              created_by_table: 'notion_user',
              created_time: nowMs,
              messages: [],
              data: {},
            },
          },
        ],
      },
    ],
  });
  return threadId;
}

function loadAttachmentData(item) {
  const maxBytes = Number(cfg.attachmentMaxBytes) || 14680064;
  return (async () => {
    let buffer = null;
    let mimeType = item.mimeType || '';
    let name = item.name || '';
    if (item.url) {
      const url = String(item.url);
      const dataMatch = url.match(/^data:([^;,]+)?(;base64)?,([\s\S]*)$/);
      if (dataMatch) {
        mimeType = mimeType || dataMatch[1] || '';
        buffer = dataMatch[2] ? Buffer.from(dataMatch[3], 'base64') : Buffer.from(decodeURIComponent(dataMatch[3]));
      } else {
        const res = await execCurl(['-sS', '-L', '--max-time', '180', url]);
        if (res.exitCode !== 0) throw new HttpError(400, `下载附件失败: ${res.stderr.slice(0, 200)}`);
        buffer = res.stdout;
        const ct = res.stderr ? '' : '';
        void ct;
      }
    } else if (item.dataBase64) {
      buffer = Buffer.from(String(item.dataBase64), 'base64');
    } else if (item.text) {
      buffer = Buffer.from(String(item.text), 'utf8');
      mimeType = mimeType || 'text/plain';
    }
    if (!buffer || !buffer.length) throw new HttpError(400, '附件内容为空');
    if (buffer.length > maxBytes) {
      throw new HttpError(413, `附件过大（${buffer.length} 字节，上限 ${maxBytes}）`);
    }
    mimeType = mimeType || sniffMime(buffer) || 'application/octet-stream';
    if (!name) {
      const ext = EXT_BY_MIME[mimeType] || (mimeType.startsWith('image/') ? '.' + mimeType.split('/')[1] : '.bin');
      name = `attachment${ext}`;
    }
    return { buffer, mimeType, name };
  })();
}

// 上传单个附件，返回可直接放进 transcript 的 attachment step
async function uploadAttachment({ spaceId, threadId, buffer, mimeType, name }) {
  const ext = extByName(name) || EXT_BY_MIME[mimeType] || '.bin';
  const storedName = `${uuid()}${ext}`;
  const urlResp = await notionJson('/api/v3/getUploadFileUrlForAssistantChatTranscriptUpload', {
    name: storedName,
    contentType: mimeType,
    contentLength: buffer.length,
    assistantChatTranscriptSessionPointer: { spaceId, table: 'thread', id: threadId },
    createThread: true,
    agentMemorySettings: { useMemories: true, excludeChatFromMemories: false },
  });
  if (!urlResp.url || !urlResp.signedUploadPostUrl) {
    throw new HttpError(502, '上传附件失败：Notion 没有返回签名地址');
  }

  const boundary = `----aiProxy${uuid().replace(/-/g, '')}`;
  const parts = [];
  for (const [key, value] of Object.entries(urlResp.fields || {})) {
    parts.push(Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="${key}"\r\n\r\n${value}\r\n`));
  }
  parts.push(
    Buffer.from(
      `--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="${storedName}"\r\n` +
        `Content-Type: ${mimeType}\r\n\r\n`
    )
  );
  parts.push(buffer);
  parts.push(Buffer.from(`\r\n--${boundary}--\r\n`));

  const args = [
    '-sS',
    '--max-time',
    '180',
    '-X',
    'POST',
    urlResp.signedUploadPostUrl,
    '--data-binary',
    '@-',
    '-H',
    `Content-Type: multipart/form-data; boundary=${boundary}`,
    '-w',
    '\n__STATUS__%{http_code}',
  ];
  for (const h of urlResp.postHeaders || []) {
    if (h && h.name) args.push('-H', `${h.name}: ${h.value}`);
  }
  const up = await execCurl(args, Buffer.concat(parts));
  const upText = up.stdout.toString('utf8');
  const statusMatch = upText.match(/__STATUS__(\d{3})/);
  const upStatus = statusMatch ? Number(statusMatch[1]) : 0;
  if (upStatus !== 200 && upStatus !== 201 && upStatus !== 204) {
    throw new HttpError(502, `上传附件到 S3 失败（HTTP ${upStatus || '?'}）`);
  }

  // 触发服务端解析/审核（图片、PDF 的元数据由它生成；失败不阻塞）
  try {
    await notionJson('/api/v3/processAgentAttachment', {
      url: urlResp.url,
      spaceId,
      aiSessionPointer: { spaceId, table: 'thread', id: threadId },
      permissionRecord: { spaceId, table: 'thread', id: threadId },
      source: 'user_upload',
      clientVersion: cfg.clientVersion,
      model: (state.registry && state.registry.entries && state.registry.entries[0] && state.registry.entries[0].code) || '',
    });
  } catch (e) {
    log('processAgentAttachment 失败（忽略）:', e.message);
  }

  const metadata = {
    fileSizeBytes: buffer.length,
    attachmentSource: 'user_upload',
    aiTraceId: uuid(),
    guardrail: { inferenceId: uuid(), attachmentRisk: 'skipped' },
    moderation: { status: 'passed' },
    estimatedTokens: { openai: Math.max(1, Math.round(buffer.length / 4)), anthropic: 0 },
  };
  const dims = imageDimensions(buffer, mimeType);
  if (dims && dims.width && dims.height) {
    metadata.width = dims.width;
    metadata.height = dims.height;
  }

  // 文本类文件：computer-file（AI 用 computer 工具读取）；其余：attachment（视觉/文档解析）
  if (isTextLikeAttachment(mimeType, name)) {
    return {
      id: uuid(),
      type: 'computer-file',
      fileUrl: urlResp.url,
      fileName: name,
      contentType: mimeType,
      metadata: { fileSize: buffer.length, attachmentSource: 'user_upload' },
    };
  }
  return {
    id: uuid(),
    type: 'attachment',
    fileUrl: urlResp.url,
    fileName: name,
    contentType: mimeType,
    metadata,
    base64EncodedFileUrl: '',
  };
}

// 把上一轮解析出来的附件描述（url / base64 / text）变成 transcript steps；需要 thread 已存在
async function prepareAttachmentSteps(items, spaceId, threadId) {
  const steps = [];
  for (const item of items) {
    const loaded = await loadAttachmentData(item);
    log(`上传附件 ${loaded.name}（${loaded.mimeType}, ${loaded.buffer.length} 字节）...`);
    steps.push(await uploadAttachment({ spaceId, threadId, ...loaded }));
  }
  return steps;
}

// OpenAI 风格：最后一条 user 消息的 content 数组里的 image_url / file
function extractOpenAIAttachments(rawMessages) {
  const list = Array.isArray(rawMessages) ? rawMessages : [];
  let lastUser = -1;
  for (let i = list.length - 1; i >= 0; i--) {
    if (list[i] && typeof list[i] === 'object' && (list[i].role === 'user' || !list[i].role)) {
      lastUser = i;
      break;
    }
  }
  if (lastUser < 0) return [];
  const content = list[lastUser].content;
  if (!Array.isArray(content)) return [];
  const out = [];
  for (const part of content) {
    if (!part || typeof part !== 'object') continue;
    if (part.type === 'image_url') {
      const v = part.image_url;
      const url = typeof v === 'string' ? v : v && v.url;
      if (url) out.push({ url, name: (v && v.name) || '', mimeType: (v && v.mime_type) || '' });
    } else if (part.type === 'input_image') {
      const url = typeof part.image_url === 'string' ? part.image_url : part.image_url && part.image_url.url;
      if (url) out.push({ url, name: part.filename || '', mimeType: part.mime_type || '' });
    } else if (part.type === 'file' && part.file) {
      out.push({
        dataBase64: part.file.file_data || part.file.data || '',
        name: part.file.filename || part.file.name || '',
        mimeType: part.file.mime_type || part.file.type || '',
      });
    }
  }
  return out;
}

// Anthropic 风格：image / document block
function extractAnthropicAttachments(rawMessages) {
  const list = Array.isArray(rawMessages) ? rawMessages : [];
  const out = [];
  for (const m of list) {
    if (!m || typeof m !== 'object' || m.role === 'assistant') continue;
    const content = m.content;
    if (!Array.isArray(content)) continue;
    for (const block of content) {
      if (!block || typeof block !== 'object') continue;
      const src = block.source;
      if (!src) continue;
      if (block.type === 'image' || block.type === 'document') {
        if (src.type === 'base64' && src.data) {
          out.push({ dataBase64: src.data, mimeType: src.media_type || (block.type === 'image' ? 'image/png' : 'application/pdf'), name: block.title || '' });
        } else if (src.type === 'url' && src.url) {
          out.push({ url: src.url, mimeType: src.media_type || '', name: block.title || '' });
        } else if (src.type === 'text' && src.data) {
          out.push({ text: src.data, mimeType: src.media_type || 'text/plain', name: block.title || '' });
        }
      }
    }
  }
  return out;
}

// ───────────────── 登录态刷新：直接从浏览器调试端口(CDP)读 cookie ─────────────────
// 打包成 exe 后没有 Python 也能刷新：Node 自带 WebSocket，连 Edge 的 DevTools
// 端点调用 Storage.getCookies 即可拿到解密后的 cookie。

function findDevToolsEndpoints() {
  const userDataDirs = [
    path.join(os.homedir(), 'AppData', 'Local', 'Microsoft', 'Edge', 'User Data'),
    path.join(os.homedir(), 'AppData', 'Local', 'Google', 'Chrome', 'User Data'),
    path.join(os.homedir(), 'AppData', 'Local', 'BraveSoftware', 'Brave-Browser', 'User Data'),
  ];
  const out = [];
  for (const ud of userDataDirs) {
    const file = path.join(ud, 'DevToolsActivePort');
    if (!fs.existsSync(file)) continue;
    try {
      const lines = fs.readFileSync(file, 'utf8').trim().split(/\r?\n/);
      const port = Number(lines[0]);
      const wsPath = (lines[1] || '').trim();
      if (!port || !wsPath.startsWith('/devtools/')) continue;
      out.push({ browser: path.basename(ud), port, wsUrl: `ws://127.0.0.1:${port}${wsPath}` });
    } catch (_) {
      /* ignore */
    }
  }
  return out;
}

function isNotionCookie(cookie) {
  if (!cookie || !cookie.name) return false;
  const name = String(cookie.name).toLowerCase();
  const domain = String(cookie.domain || '').toLowerCase();
  return (
    domain.includes('notion') ||
    ['token_v2', 'notion_user_id', 'notion_browser_id', 'notion_users'].includes(name)
  );
}

function cdpGetAllCookies(wsUrl) {
  if (typeof WebSocket === 'undefined') {
    return Promise.reject(new Error('当前运行环境不支持 WebSocket'));
  }
  return new Promise((resolve, reject) => {
    let ws;
    try {
      ws = new WebSocket(wsUrl);
    } catch (e) {
      reject(e);
      return;
    }
    const timer = setTimeout(() => {
      try {
        ws.close();
      } catch (_) {
        /* ignore */
      }
      reject(new Error('CDP 超时'));
    }, 15000);
    ws.onopen = () => ws.send(JSON.stringify({ id: 1, method: 'Storage.getCookies', params: {} }));
    ws.onmessage = (ev) => {
      let msg;
      try {
        msg = JSON.parse(String(ev.data));
      } catch (_) {
        return;
      }
      if (!msg || msg.id !== 1) return;
      clearTimeout(timer);
      try {
        ws.close();
      } catch (_) {
        /* ignore */
      }
      if (msg.result && Array.isArray(msg.result.cookies)) resolve(msg.result.cookies);
      else reject(new Error('CDP 返回错误: ' + JSON.stringify(msg.error || {}).slice(0, 200)));
    };
    ws.onerror = () => {
      clearTimeout(timer);
      reject(new Error('CDP WebSocket 连接失败'));
    };
  });
}

async function extractCredentialViaCdp() {
  const endpoints = findDevToolsEndpoints();
  if (!endpoints.length) {
    throw new Error('没找到浏览器调试端口（需要 Edge 以 --remote-debugging-port=9222 启动并已登录 Notion）');
  }
  const errors = [];
  for (const ep of endpoints) {
    try {
      log(`尝试从 ${ep.browser} 调试端口读取 Notion 登录态...`);
      const cookies = await cdpGetAllCookies(ep.wsUrl);
      const notion = cookies.filter(isNotionCookie);
      const tokens = notion.filter((c) => c.name === 'token_v2' && c.value);
      tokens.sort(
        (a, b) =>
          (String(b.domain) === '.app.notion.com') - (String(a.domain) === '.app.notion.com')
      );
      if (!tokens.length) {
        errors.push(`${ep.browser}: 没有 token_v2`);
        continue;
      }
      const tok = tokens[0];
      const pick = (name) => {
        const hit = notion.find((c) => c.name === name);
        return (hit && hit.value) || '';
      };
      const cred = {
        token_v2: tok.value,
        notion_user_id: pick('notion_user_id'),
        notion_browser_id: pick('notion_browser_id'),
        cookie_header: [
          `token_v2=${tok.value}`,
          pick('notion_user_id') ? `notion_user_id=${pick('notion_user_id')}` : '',
          pick('notion_browser_id') ? `notion_browser_id=${pick('notion_browser_id')}` : '',
        ]
          .filter(Boolean)
          .join('; '),
        token_domain: tok.domain || '',
        source: `${ep.browser}:${ep.port}`,
        extracted_at: new Date().toISOString(),
      };
      writeJson(PATHS.credential, cred);
      log(`登录态已写入 ${PATHS.credential}`);
      return cred;
    } catch (e) {
      errors.push(`${ep.browser}: ${e.message}`);
    }
  }
  throw new Error('CDP 读取失败：' + errors.join('; '));
}

async function refreshCookie() {
  if (!cfg.autoRefreshCookie) return false;
  log('收到 401，尝试自动刷新 Notion 登录态（浏览器调试端口 CDP）...');
  try {
    state.cred = await extractCredentialViaCdp();
    state.lastAuthError = null;
    return true;
  } catch (e) {
    log('内置 CDP 刷新失败:', e.message);
  }
  // 兼容旧方案：调用 Python 脚本（需要 Python + requests + websocket-client）
  if (!fs.existsSync(PATHS.refreshScript)) return false;
  log('尝试用 Python 脚本刷新...');
  const ok = await new Promise((resolve) => {
    let child;
    try {
      child = spawn(cfg.python, [PATHS.refreshScript], { windowsHide: true, cwd: ROOT });
    } catch (e) {
      resolve(false);
      return;
    }
    let out = '';
    let err = '';
    const timer = setTimeout(() => {
      try {
        child.kill();
      } catch (_) {
        /* ignore */
      }
    }, 45000);
    child.stdout.on('data', (d) => (out += d));
    child.stderr.on('data', (d) => (err += d));
    child.on('error', () => {
      clearTimeout(timer);
      resolve(false);
    });
    child.on('close', (code) => {
      clearTimeout(timer);
      if (code === 0) {
        try {
          state.cred = loadCredential();
          log('登录态已刷新');
          resolve(true);
        } catch (e) {
          resolve(false);
        }
      } else {
        log('自动刷新失败:', (err || out).trim().slice(-300));
        resolve(false);
      }
    });
  });
  return ok;
}

// ───────────────────────────── NDJSON 解析 ─────────────────────────────

function mergeText(acc, frag) {
  acc = acc || '';
  frag = frag || '';
  if (!frag) return acc;
  if (frag.startsWith(acc)) return frag;
  if (acc.startsWith(frag)) return acc;
  return acc + frag;
}

function cleanText(input) {
  if (input == null) return '';
  let out = String(input);
  out = out.replace(/<lang\b[^>]*>([\s\S]*?)<\/lang>/g, '$1');
  out = out.replace(/<lang\b[^>]*\/>/g, '');
  out = out.replace(/<\/?lang\b[^>]*>/g, '');
  return out.replace(/^\s*\n+/, '').trim();
}

class StreamState {
  constructor() {
    this.steps = new Map(); // idx -> { type, items: Map(itemIdx -> {type, content}) }
    this.nextStepIndex = 0;
    this.usage = null;
    this.errors = [];
    this.recordMapContent = null;
    this.recordMapThinking = '';
  }

  getStep(idx) {
    if (!this.steps.has(idx)) this.steps.set(idx, { type: '', items: new Map() });
    return this.steps.get(idx);
  }

  setItem(step, idx, block) {
    if (block == null) return;
    if (typeof block === 'string') {
      step.items.set(idx, { type: 'text', content: block });
      return;
    }
    if (typeof block !== 'object') return;
    step.items.set(idx, {
      type: String(block.type || ''),
      content: typeof block.content === 'string' ? block.content : '',
    });
  }

  getItem(step, idx) {
    if (!step.items.has(idx)) step.items.set(idx, { type: '', content: '' });
    return step.items.get(idx);
  }

  apply(rec) {
    if (!rec || typeof rec !== 'object') return;
    const type = rec.type;
    if (!type) {
      this.applyLegacy(rec);
      return;
    }
    if (type === 'error') {
      this.errors.push({ subType: rec.subType || '', message: rec.message || '' });
      return;
    }
    if (type === 'patch-start') {
      const initialSteps = (rec.data && rec.data.s) || [];
      this.nextStepIndex = initialSteps.length;
      for (const item of initialSteps) {
        if (item && item.type === 'error') {
          this.errors.push({ subType: item.subType || '', message: item.message || '' });
        }
      }
      return;
    }
    if (type === 'patch') {
      for (const op of rec.v || []) this.applyOp(op);
      return;
    }
    if (type === 'record-map') {
      this.applyRecordMap(rec.recordMap);
      return;
    }
    if (type === 'markdown-chat' && typeof rec.value === 'string') {
      this.recordMapContent = cleanText(rec.value);
      return;
    }
    if (type === 'agent-inference') {
      const step = this.getStep(-1);
      (Array.isArray(rec.value) ? rec.value : []).forEach((it, i) => this.setItem(step, i, it));
      return;
    }
  }

  applyLegacy(rec) {
    if (Array.isArray(rec.value)) {
      const text = rec.value
        .map((seg) => (Array.isArray(seg) && typeof seg[0] === 'string' ? seg[0] : ''))
        .join('');
      if (text) {
        const cleaned = cleanText(text);
        if (!this.recordMapContent || cleaned.length >= this.recordMapContent.length) {
          this.legacyContent = cleaned;
        }
      }
    }
  }

  applyOp(op) {
    if (!op || op.p == null) return;
    const o = op.o || '';
    const v = op.v;
    let p = Array.isArray(op.p) ? op.p.join('/') : String(op.p);
    if (!p.startsWith('/')) p = '/' + p;

    // 追加一个全新 step：/s/-（服务端不知道 index，由客户端按顺序编号）
    if (p === '/s/-' || p === '/s') {
      const idx = this.nextStepIndex++;
      const step = this.getStep(idx);
      if (v && typeof v === 'object') {
        if (v.type) step.type = String(v.type);
        if (Array.isArray(v.value)) v.value.forEach((it, i) => this.setItem(step, i, it));
      }
      return;
    }

    const stepMatch = p.match(/^\/s\/(\d+)(?:\/(.*))?$/);
    if (!stepMatch) return;
    const idx = Number(stepMatch[1]);
    const rest = stepMatch[2] || '';
    const step = this.getStep(idx);

    if (o === 'a') {
      if (rest === '' || rest === '-') {
        if (v && typeof v === 'object') {
          if (v.type) step.type = String(v.type);
          if (Array.isArray(v.value)) v.value.forEach((it, i) => this.setItem(step, i, it));
          if (typeof v.content === 'string' && v.content) {
            step.items.set(step.items.size, { type: String(v.type || 'text'), content: v.content });
          }
        }
        return;
      }
      const valueMatch = rest.match(/^value\/(\d+)(?:\/(.*))?$/);
      if (valueMatch) {
        const itemIdx = Number(valueMatch[1]);
        const sub = valueMatch[2] || '';
        if (!sub) {
          this.setItem(step, itemIdx, v);
        } else if (sub === 'content') {
          const item = this.getItem(step, itemIdx);
          item.content = mergeText(item.content, typeof v === 'string' ? v : String(v == null ? '' : v));
        } else if (sub === 'type') {
          this.getItem(step, itemIdx).type = String(v || '');
        }
        return;
      }
      if (rest === 'value/-') {
        const itemIdx = step.items.size;
        this.setItem(step, itemIdx, v);
        return;
      }
      this.applyStepField(step, rest, v, 'a');
      return;
    }

    if (o === 'x' || o === 'p') {
      const contentMatch = rest.match(/^value\/(\d+)\/content$/);
      if (contentMatch) {
        const item = this.getItem(step, Number(contentMatch[1]));
        const frag = typeof v === 'string' ? v : String(v == null ? '' : v);
        item.content = o === 'x' ? mergeText(item.content, frag) : frag;
        return;
      }
      this.applyStepField(step, rest, v, o);
    }
  }

  applyStepField(step, field, value, op) {
    const usageFields = ['inputTokens', 'outputTokens', 'cachedTokensRead', 'cachedTokensCreated'];
    if (usageFields.includes(field)) {
      this.usage = this.usage || {};
      if (typeof value === 'number') this.usage[field] = value;
      return;
    }
    if (field === 'type' && typeof value === 'string') {
      step.type = value;
    }
  }

  derive() {
    let body = '';
    let thinking = '';
    const orderedSteps = [...this.steps.entries()].sort((a, b) => a[0] - b[0]);
    for (const [, step] of orderedSteps) {
      const stepType = (step.type || '').toLowerCase();
      const items = [...step.items.entries()].sort((a, b) => a[0] - b[0]);
      for (const [, item] of items) {
        const itemType = (item.type || '').toLowerCase();
        if (itemType === 'thinking' || itemType === 'reasoning') {
          thinking += item.content || '';
        } else if (
          stepType === 'agent-inference' ||
          stepType === 'markdown-chat' ||
          itemType === 'text' ||
          itemType === 'markdown-chat'
        ) {
          body += item.content || '';
        }
      }
    }
    return { body: cleanText(body), thinking: cleanText(thinking) };
  }

  applyRecordMap(rm) {
    if (!rm || typeof rm !== 'object') return;
    let finalStepId = null;
    let usage = null;
    for (const t of Object.values(rm.thread || {})) {
      const data = t && t.value && t.value.value && t.value.value.data;
      if (!data) continue;
      if (data.last_turn_outcome && data.last_turn_outcome.final_step_id) {
        finalStepId = data.last_turn_outcome.final_step_id;
      }
      if (data.usage_summary) usage = data.usage_summary;
    }
    const candidates = [];
    for (const [mid, m] of Object.entries(rm.thread_message || {})) {
      const step = m && m.value && m.value.value && m.value.value.step;
      if (!step) continue;
      const stepType = String(step.type || '').toLowerCase();
      if (!['agent-inference', 'markdown-chat', 'text'].includes(stepType)) continue;
      const items = Array.isArray(step.value) ? step.value : [];
      let body = '';
      let think = '';
      for (const it of items) {
        if (!it || typeof it !== 'object') continue;
        const itemType = String(it.type || '').toLowerCase();
        if (itemType === 'thinking' || itemType === 'reasoning') think += it.content || '';
        else if (itemType === 'text' || itemType === 'markdown-chat') body += it.content || '';
        else if (typeof it.content === 'string' && it.content) body += it.content;
      }
      const cleaned = cleanText(body);
      if (cleaned) {
        candidates.push({ id: mid, body: cleaned, think: cleanText(think), stepType });
      }
    }
    if (candidates.length) {
      const rank = (c) => (c.stepType === 'markdown-chat' || c.stepType === 'text' ? 0 : 1);
      let pick = finalStepId ? candidates.find((c) => c.id === finalStepId) : null;
      if (!pick) {
        pick = candidates.sort((a, b) => rank(a) - rank(b) || b.body.length - a.body.length)[0];
      }
      this.recordMapContent = pick.body;
      this.recordMapThinking = pick.think;
    }
    if (usage) {
      this.usage = {
        inputTokens: usage.input_tokens,
        outputTokens: usage.output_tokens,
        cachedTokensRead: usage.cached_input_tokens_read,
        cachedTokensCreated: usage.cached_input_tokens_created,
      };
    }
  }

  result() {
    const derived = this.derive();
    const content =
      this.recordMapContent != null ? this.recordMapContent : derived.body || this.legacyContent || '';
    let thinking = this.recordMapThinking || derived.thinking || '';
    // 部分模型会把正文同时塞进 thinking，避免客户端把同一段文字显示两遍
    if (thinking && content && thinking.trim() === content.trim()) thinking = '';
    return { content, thinking, usage: this.usage, errors: this.errors };
  }
}

// ───────────────────────────── 请求体构建 ─────────────────────────────

function normalizeMessages(rawMessages) {
  const out = [];
  for (const m of Array.isArray(rawMessages) ? rawMessages : []) {
    if (!m || typeof m !== 'object') continue;
    let text = '';
    if (typeof m.content === 'string') text = m.content;
    else if (Array.isArray(m.content)) {
      text = m.content
        .map((part) => {
          if (typeof part === 'string') return part;
          if (part && typeof part === 'object') return part.text || part.content || '';
          return '';
        })
        .join('');
    }
    out.push({ role: m.role || 'user', content: text });
  }
  return out;
}

// ───────────────────────────── Notion 会话复用（省会话数） ─────────────────────────────
// threadMode = "new"    ：每个请求 createThread:true，Notion 里新建一个对话（默认）
// threadMode = "single" ：复用同一个 Notion 会话，追加提问；到轮数/上下文上限后自动换新会话
// 可通过请求体 thread_mode / 请求头 X-Notion-Thread-Mode 覆盖（批量脚本用 single）。

let threadStateCache = null;

function threadState() {
  if (!threadStateCache) {
    try {
      threadStateCache = fs.existsSync(PATHS.threadState) ? readJson(PATHS.threadState) : {};
    } catch (_) {
      threadStateCache = {};
    }
  }
  return threadStateCache;
}

function saveThreadState() {
  try {
    writeJson(PATHS.threadState, threadState());
  } catch (e) {
    log('保存会话状态失败:', e.message);
  }
}

function resetThreadState() {
  threadState().single = null;
  saveThreadState();
}

function resolveThreadMode(body, req) {
  const raw = String(
    (body && (body.thread_mode || (body.metadata && body.metadata.thread_mode))) ||
      (req && req.headers['x-notion-thread-mode']) ||
      cfg.threadMode ||
      'new'
  ).toLowerCase();
  return raw === 'single' ? 'single' : 'new';
}

function acquireThread(mode) {
  if (mode !== 'single') return { mode: 'new', threadId: null, createThread: true, reused: false };
  const st = threadState().single;
  const maxTurns = Number(cfg.threadReuseMaxTurns) || 30;
  const maxTokens = Number(cfg.threadReuseMaxInputTokens) || 150000;
  if (st && st.threadId && (st.turns || 0) < maxTurns && (st.lastInputTokens || 0) < maxTokens) {
    log(`复用 Notion 会话 ${mask(st.threadId)}（第 ${(st.turns || 0) + 1} 轮）`);
    return { mode: 'single', threadId: st.threadId, createThread: false, reused: true };
  }
  const threadId = uuid();
  log(`新建 Notion 会话 ${mask(threadId)}（single 模式）`);
  threadState().single = {
    threadId,
    turns: 0,
    lastInputTokens: 0,
    createdAt: new Date().toISOString(),
  };
  saveThreadState();
  return { mode: 'single', threadId, createThread: true, reused: false };
}

function recordThreadResult(thread, usage) {
  if (!thread || thread.mode !== 'single' || !thread.threadId) return;
  const st = threadState().single;
  if (!st || st.threadId !== thread.threadId) return;
  st.turns = (st.turns || 0) + 1;
  if (usage && typeof usage.inputTokens === 'number') st.lastInputTokens = usage.inputTokens;
  st.lastUsedAt = new Date().toISOString();
  saveThreadState();
}

// single 模式下串行化请求，避免两个请求同时往同一个 Notion 会话里追加
let singleThreadLock = Promise.resolve();
async function withSingleThreadLock(fn) {
  const prev = singleThreadLock;
  let release;
  singleThreadLock = new Promise((r) => {
    release = r;
  });
  try {
    await prev;
    return await fn();
  } finally {
    release();
  }
}

async function runWithThread({ entry, messages, threadType, webSearch, reasoningEffort, threadId, mode, attachments }) {
  const atts = Array.isArray(attachments) ? attachments : [];
  const spaceId = state.workspace.spaceId;
  const stepsFor = (tid) => prepareAttachmentSteps(atts, spaceId, tid);

  // 显式指定 threadId：直接追加到该会话（高级用法，不做状态管理）
  if (threadId) {
    const attachmentSteps = atts.length ? await stepsFor(threadId) : [];
    const payload = buildPayload({
      entry,
      messages,
      threadId,
      threadType,
      webSearch,
      reasoningEffort,
      createThread: false,
      reuse: false,
      attachmentSteps,
    });
    const parsed = await executeInference(payload, null, { threadId });
    return { parsed, thread: { mode: 'custom', threadId } };
  }

  if (mode !== 'single') {
    if (!atts.length) {
      const payload = buildPayload({
        entry,
        messages,
        threadId: null,
        threadType,
        webSearch,
        reasoningEffort,
        createThread: true,
        reuse: false,
      });
      const parsed = await executeInference(payload, null, { threadId: null });
      return { parsed, thread: { mode: 'new', threadId: null } };
    }
    // 带附件：thread 必须先存在才能上传 -> 先建空 thread record，再上传，最后 createThread:false
    const newThreadId = uuid();
    await createThreadRecord(newThreadId);
    const attachmentSteps = await stepsFor(newThreadId);
    const payload = buildPayload({
      entry,
      messages,
      threadId: newThreadId,
      threadType,
      webSearch,
      reasoningEffort,
      createThread: false,
      reuse: false,
      attachmentSteps,
    });
    const parsed = await executeInference(payload, null, { threadId: newThreadId });
    return { parsed, thread: { mode: 'new', threadId: newThreadId } };
  }

  return withSingleThreadLock(async () => {
    let thread = acquireThread('single');
    try {
      let attachmentSteps = [];
      if (atts.length) {
        // single 模式首次请求时该会话在 Notion 端还不存在，先建 record 再上传
        if (thread.createThread) await createThreadRecord(thread.threadId);
        attachmentSteps = await stepsFor(thread.threadId);
      }
      const payload = buildPayload({
        entry,
        messages,
        threadId: thread.threadId,
        threadType,
        webSearch,
        reasoningEffort,
        createThread: atts.length ? false : thread.createThread,
        reuse: thread.reused,
        attachmentSteps,
      });
      const parsed = await executeInference(payload, null, {
        threadId: atts.length || !thread.createThread ? thread.threadId : null,
      });
      recordThreadResult(thread, parsed.usage);
      return { parsed, thread };
    } catch (e) {
      // 会话可能已失效/被删除：换一个新会话重试一次
      if (thread.reused && e instanceof HttpError && [400, 404, 502, 503].includes(e.status)) {
        log('复用会话失败，改为新建会话重试:', e.message);
        resetThreadState();
        thread = acquireThread('single');
        let attachmentSteps = [];
        if (atts.length) {
          await createThreadRecord(thread.threadId);
          attachmentSteps = await stepsFor(thread.threadId);
        }
        const payload = buildPayload({
          entry,
          messages,
          threadId: thread.threadId,
          threadType,
          webSearch,
          reasoningEffort,
          createThread: atts.length ? false : true,
          reuse: false,
          attachmentSteps,
        });
        const parsed = await executeInference(payload, null, {
          threadId: atts.length ? thread.threadId : null,
        });
        recordThreadResult(thread, parsed.usage);
        return { parsed, thread };
      }
      throw e;
    }
  });
}

function buildPayload({ entry, messages, threadId, threadType, webSearch, reasoningEffort, createThread, reuse, attachmentSteps }) {
  const spaceId = state.workspace.spaceId;
  const userId = state.cred.notion_user_id || state.workspace.userId || '';
  const now = new Date().toISOString();
  const sysTexts = messages.filter((m) => m.role === 'system' && m.content).map((m) => m.content);
  const dialog = messages.filter((m) => m.role !== 'system' && m.content);
  // 复用已有 Notion 会话时只发最新一条 user 消息（历史在服务端会话里）
  const sendDialog = reuse ? dialog.slice(-1) : dialog;
  const steps = Array.isArray(attachmentSteps) ? attachmentSteps.filter(Boolean) : [];
  // 附件 step 需要紧跟在其所属的（最后一条）user 消息前面
  let lastUserIndex = -1;
  for (let i = sendDialog.length - 1; i >= 0; i--) {
    if (sendDialog[i].role === 'user') {
      lastUserIndex = i;
      break;
    }
  }

  const configValue = {
    type: threadType,
    model: entry.code,
    modelFromUser: true,
    useWebSearch: !!webSearch,
    searchScopes: [{ type: 'everything' }],
  };
  if (reasoningEffort) configValue.reasoningEffort = reasoningEffort;
  // 有文本类附件（computer-file）时，让 AI 能用 computer 工具读取文件
  if (steps.some((s) => s && s.type === 'computer-file')) configValue.enableComputer = true;

  const transcript = [
    { id: uuid(), type: 'config', value: configValue },
    {
      id: uuid(),
      type: 'context',
      value: {
        timezone: 'Asia/Shanghai',
        currentDatetime: now,
        userId,
        spaceId,
        surface: threadType === 'markdown-chat' ? 'ai_module' : 'workflows',
      },
    },
  ];
  // system prompt 处理模式：
  //   prefix  —— 直接拼到第一条 user 消息前面（默认，绕开 Notion 的注入检测启发式）
  //   context —— 作为独立 context transcript 块发送（贴近 Notion 原生结构，待实测）
  const systemMode = cfg.systemPromptMode || 'prefix';
  if (systemMode === 'context' && sysTexts.length) {
    transcript.push({ id: uuid(), type: 'context', value: [[sysTexts.join('\n\n')]] });
  }

  let firstUser = true;
  sendDialog.forEach((msg, index) => {
    if (msg.role === 'assistant') {
      transcript.push({
        id: uuid(),
        type: 'agent-inference',
        value: [{ type: 'text', content: msg.content }],
      });
      return;
    }
    // 最后一条 user 消息前插入附件 step（没有附件时 steps 为空数组）
    if (index === lastUserIndex && steps.length) transcript.push(...steps);
    let content = msg.content;
    if (firstUser && systemMode === 'prefix' && sysTexts.length) {
      content = sysTexts.join('\n\n') + '\n\n' + content;
    }
    firstUser = false;
    transcript.push({
      id: uuid(),
      type: 'user',
      value: [[content]],
      userId,
      createdAt: now,
    });
  });

  const isNewThread = createThread !== undefined ? !!createThread : !threadId;
  return {
    traceId: uuid(),
    spaceId,
    threadId: threadId || uuid(),
    threadType,
    createThread: isNewThread,
    generateTitle: false,
    saveAllThreadOperations: true,
    setUnreadState: true,
    isPartialTranscript: !isNewThread,
    asPatchResponse: true,
    isUserInAnySalesAssistedSpace: false,
    isSpaceSalesAssisted: false,
    threadParentPointer: { table: 'space', id: spaceId, spaceId },
    transcript,
    debugOverrides: {
      emitAgentSearchExtractedResults: true,
      cachedInferences: {},
      annotationInferences: {},
      emitInferences: false,
    },
  };
}

// ───────────────────────────── 上游执行（含重试） ─────────────────────────────

function isRetryableInBand(errors) {
  return errors.some((e) => {
    const sub = String(e.subType || '').toLowerCase();
    const msg = String(e.message || '').toLowerCase();
    return (
      sub.includes('temporarily') ||
      sub.includes('unavailable') ||
      sub.includes('timeout') ||
      sub.includes('overloaded') ||
      msg.includes('try again')
    );
  });
}

async function executeInference(payload, onRecord, { threadId } = {}) {
  let lastError = null;
  const attempts = Math.max(0, Number(cfg.retries) || 0) + 1;
  for (let attempt = 0; attempt < attempts; attempt++) {
    if (attempt > 0) {
      const delay = 800 + attempt * 600;
      log(`重试第 ${attempt} 次（${delay}ms 后）...`);
      await sleep(delay);
      if (threadId) {
        // 复用已有 thread 重试：不能再次 createThread
        payload.createThread = false;
        payload.isPartialTranscript = true;
      }
    }
    const streamState = new StreamState();
    const res = await callNotion({
      pathname: '/api/v3/runInferenceTranscript',
      payload,
      onRecord: (rec) => {
        streamState.apply(rec);
        if (onRecord) onRecord(rec, streamState);
      },
    });

    if (res.status === 401) {
      if (attempt === 0 && (await refreshCookie())) continue;
      state.lastAuthError = Date.now();
      throw new HttpError(
        401,
        'Notion 登录态已失效。请运行 `python tools\\cdp_cookie_extract.py` 刷新后重试（需要 Edge 开着调试端口）。'
      );
    }
    if (res.status === 403) {
      const snippet = (res.records.length ? JSON.stringify(res.records[0]) : res.stderr).slice(0, 300);
      if (isCloudflareBlock(snippet)) {
        throw new HttpError(403, 'Notion 边缘节点触发了 Cloudflare 校验，请稍后重试或更换网络出口。');
      }
      throw new HttpError(403, `Notion 返回 403：${snippet}`);
    }
    if (res.status === 429) {
      lastError = new HttpError(429, 'Notion 触发限流（429），请稍等 10-30 秒再试。');
      continue;
    }
    if (res.status >= 500 || res.status === -1 || res.status === 0) {
      lastError = new HttpError(502, `Notion 上游异常（HTTP ${res.status}${res.stderr ? ': ' + res.stderr.slice(0, 200) : ''}）。`);
      continue;
    }
    if (res.status !== 200) {
      const bodyText = res.records.length ? JSON.stringify(res.records[0]).slice(0, 300) : res.stderr.slice(0, 300);
      throw new HttpError(res.status, `Notion 返回 HTTP ${res.status}：${bodyText}`);
    }

    const parsed = streamState.result();
    if (cfg.debugSaveRaw) {
      try {
        const dir = path.join(ROOT, 'secret', 'debug');
        fs.mkdirSync(dir, { recursive: true });
        fs.writeFileSync(
          path.join(dir, `stream-${Date.now()}.ndjson`),
          res.records.map((r) => JSON.stringify(r)).join('\n'),
          'utf8'
        );
        log('调试流已保存；解析结果:', JSON.stringify({ contentLen: parsed.content.length, thinkingLen: parsed.thinking.length, usage: parsed.usage }));
      } catch (_) {
        /* ignore */
      }
    }
    if (parsed.content) {
      return { ...parsed, rawRecords: res.records.length };
    }
    if (isRetryableInBand(parsed.errors)) {
      lastError = new HttpError(503, `Notion 暂时不可用（${parsed.errors[0].subType || 'temporarily-unavailable'}），已重试仍失败。`);
      continue;
    }
    if (parsed.errors.length) {
      throw new HttpError(502, `Notion 返回错误：${parsed.errors.map((e) => e.message || e.subType).join('; ')}`);
    }
    lastError = new HttpError(502, 'Notion 返回 200 但正文为空（可能是模型不支持或接口变动）。');
  }
  throw lastError || new HttpError(502, 'Notion 请求失败');
}

// ───────────────────────────── HTTP 服务 ─────────────────────────────

function corsHeaders() {
  return {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'authorization, content-type, x-api-key',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  };
}

function sendJson(res, status, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
    ...corsHeaders(),
  });
  res.end(body);
}

function sendError(res, status, message, extra) {
  sendJson(res, status, {
    error: {
      message,
      type: status === 401 ? 'authentication_error' : status === 429 ? 'rate_limit_error' : 'invalid_request_error',
      code: status,
      ...(extra || {}),
    },
  });
}

function readBody(req, limit) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > limit) {
        reject(new HttpError(413, `请求体过大（> ${Math.round(limit / 1024 / 1024)}MB）`));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

function checkAuth(req) {
  if (!cfg.apiKey) return true;
  const auth = String(req.headers['authorization'] || '');
  const key =
    auth.replace(/^Bearer\s+/i, '').trim() ||
    String(req.headers['x-api-key'] || '') ||
    String(req.headers['api-key'] || '');
  return key === cfg.apiKey;
}

let activeRequests = 0;
const waiters = [];

async function acquireSlot() {
  const max = Math.max(1, Number(cfg.maxConcurrent) || 1);
  if (activeRequests < max) {
    activeRequests += 1;
    return;
  }
  await new Promise((resolve) => waiters.push(resolve));
  activeRequests += 1;
}

function releaseSlot() {
  activeRequests = Math.max(0, activeRequests - 1);
  const next = waiters.shift();
  if (next) next();
}

function usageFromState(usage, promptText, completionText) {
  const estimate = (text) => Math.max(1, Math.round(String(text || '').length / 4));
  const prompt = usage && typeof usage.inputTokens === 'number' ? usage.inputTokens : estimate(promptText);
  const completion =
    usage && typeof usage.outputTokens === 'number' ? usage.outputTokens : estimate(completionText);
  return {
    prompt_tokens: prompt,
    completion_tokens: completion,
    total_tokens: prompt + completion,
  };
}

function sseWrite(res, obj) {
  res.write(`data: ${JSON.stringify(obj)}\n\n`);
}

async function handleChatCompletions(req, res) {
  const raw = await readBody(req, Number(cfg.maxBodyBytes) || DEFAULT_CONFIG.maxBodyBytes);
  let body;
  try {
    body = JSON.parse(raw);
  } catch (e) {
    throw new HttpError(400, `请求体不是合法 JSON：${e.message}`);
  }
  const messages = normalizeMessages(body.messages);
  if (!messages.length) throw new HttpError(400, 'messages 不能为空');
  const dialog = messages.filter((m) => m.role !== 'system');
  if (!dialog.length) throw new HttpError(400, '至少需要一条 user 消息');
  if (dialog[dialog.length - 1].role !== 'user') {
    throw new HttpError(400, '最后一条消息必须是 user（Notion 的对话流要求以用户输入结束）');
  }
  const { entry, requested, fallback } = resolveModel(body.model);
  if (fallback) log(`模型 "${body.model}" 未找到，回退到 ${entry.id}（${entry.code}）`);
  const stream = !!body.stream;
  const threadType =
    body.thread_type ||
    (cfg.markdownChatModels.includes(entry.code) ? 'markdown-chat' : cfg.defaultThreadType);
  const webSearch =
    body.web_search !== undefined ? !!body.web_search : !!cfg.useWebSearch;
  let reasoningEffort = body.reasoning_effort || body.reasoning?.effort;
  if (reasoningEffort && entry.efforts.length && !entry.efforts.includes(reasoningEffort)) {
    log(`模型 ${entry.id} 不支持 reasoning_effort=${reasoningEffort}，忽略`);
    reasoningEffort = null;
  }
  const threadId = body.thread_id || (body.metadata && (body.metadata.thread_id || body.metadata.notion_thread_id)) || null;
  const threadMode = resolveThreadMode(body, req);
  const attachments = extractOpenAIAttachments(body.messages);
  if (attachments.length && !cfg.enableAttachments) {
    throw new HttpError(400, '附件支持已关闭（config.enableAttachments=false）');
  }

  await acquireSlot();
  const startedAt = Date.now();
  let parsed;
  try {
    ({ parsed } = await runWithThread({
      entry,
      messages,
      threadType,
      webSearch,
      reasoningEffort,
      threadId,
      mode: threadMode,
      attachments,
    }));
  } finally {
    releaseSlot();
  }
  const duration = ((Date.now() - startedAt) / 1000).toFixed(1);
  log(
    `chat model=${body.model || entry.id}(${entry.code}) stream=${stream} ` +
      `thread=${threadMode}${attachments.length ? ` attachments=${attachments.length}` : ''} ` +
      `content=${parsed.content.length}字 耗时=${duration}s`
  );

  const responseId = `chatcmpl-${uuid()}`;
  const created = Math.floor(Date.now() / 1000);
  const modelName = body.model || entry.id;
  const usage = usageFromState(parsed.usage, raw, parsed.content);

  if (!stream) {
    const message = { role: 'assistant', content: parsed.content };
    if (cfg.includeReasoning && parsed.thinking) message.reasoning_content = parsed.thinking;
    sendJson(res, 200, {
      id: responseId,
      object: 'chat.completion',
      created,
      model: modelName,
      choices: [{ index: 0, message, finish_reason: 'stop', logprobs: null }],
      usage,
    });
    return;
  }

  res.writeHead(200, {
    'Content-Type': 'text/event-stream; charset=utf-8',
    'Cache-Control': 'no-cache, no-transform',
    Connection: 'keep-alive',
    'X-Accel-Buffering': 'no',
    ...corsHeaders(),
  });
  const chunk = (delta, finishReason) => ({
    id: responseId,
    object: 'chat.completion.chunk',
    created,
    model: modelName,
    choices: [{ index: 0, delta, finish_reason: finishReason || null, logprobs: null }],
    ...(finishReason ? { usage } : {}),
  });
  sseWrite(res, chunk({ role: 'assistant', content: '' }, null));
  if (cfg.includeReasoning && parsed.thinking) {
    sseWrite(res, chunk({ reasoning_content: parsed.thinking }, null));
  }
  if (parsed.content) {
    sseWrite(res, chunk({ content: parsed.content }, null));
  }
  sseWrite(res, chunk({}, 'stop'));
  res.write('data: [DONE]\n\n');
  res.end();
}

// ───────────────────── Anthropic Messages 兼容（Claude Code 用） ─────────────────────
// Claude Code 走 /v1/messages（Anthropic 协议），这里把它翻译成 Notion 请求，
// 再把结果按 Anthropic 的 message / SSE 事件格式返回。

function extractAnthropicText(content) {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content
      .map((block) => {
        if (!block || typeof block !== 'object') return '';
        if (block.type === 'text') return block.text || '';
        if (block.type === 'tool_result') return extractAnthropicText(block.content);
        if (block.type === 'tool_use') return `[tool_use ${block.name || ''}]`;
        if (block.type === 'image') return '[image]';
        return '';
      })
      .filter(Boolean)
      .join('\n');
  }
  return '';
}

function anthropicToOpenAIMessages(body) {
  const messages = [];
  const systemText = extractAnthropicText(body.system);
  if (systemText) messages.push({ role: 'system', content: systemText });
  for (const m of Array.isArray(body.messages) ? body.messages : []) {
    const text = extractAnthropicText(m && m.content);
    if (!text) continue;
    messages.push({ role: m.role === 'assistant' ? 'assistant' : 'user', content: text });
  }
  return messages;
}

function anthropicUsage(usage) {
  return {
    input_tokens: usage.prompt_tokens || 0,
    output_tokens: usage.completion_tokens || 0,
  };
}

function anthropicErrorMessage(status, message) {
  return {
    type: 'error',
    error: {
      type: status === 429 ? 'rate_limit_error' : status === 401 ? 'authentication_error' : 'api_error',
      message,
    },
  };
}

async function handleAnthropicMessages(req, res) {
  const raw = await readBody(req, Number(cfg.maxBodyBytes) || DEFAULT_CONFIG.maxBodyBytes);
  let body;
  try {
    body = JSON.parse(raw);
  } catch (e) {
    throw new HttpError(400, `请求体不是合法 JSON：${e.message}`);
  }
  const messages = anthropicToOpenAIMessages(body);
  if (!messages.length) throw new HttpError(400, 'messages 不能为空');
  const dialog = messages.filter((m) => m.role !== 'system');
  if (!dialog.length) throw new HttpError(400, '至少需要一条 user 消息');
  const { entry, fallback } = resolveModel(body.model);
  if (fallback) log(`模型 "${body.model}" 未找到，回退到 ${entry.id}（${entry.code}）`);
  const stream = !!body.stream;
  const threadType = cfg.markdownChatModels.includes(entry.code)
    ? 'markdown-chat'
    : cfg.defaultThreadType;
  const webSearch = body.web_search !== undefined ? !!body.web_search : !!cfg.useWebSearch;
  const threadId = body.thread_id || (body.metadata && body.metadata.thread_id) || null;
  const threadMode = resolveThreadMode(body, req);
  const attachments = extractAnthropicAttachments(body.messages);
  if (attachments.length && !cfg.enableAttachments) {
    throw new HttpError(400, '附件支持已关闭（config.enableAttachments=false）');
  }

  await acquireSlot();
  let parsed;
  try {
    ({ parsed } = await runWithThread({
      entry,
      messages,
      threadType,
      webSearch,
      reasoningEffort: null,
      threadId,
      mode: threadMode,
      attachments,
    }));
  } finally {
    releaseSlot();
  }
  const responseModel = body.model || entry.id;
  const msgId = `msg_${uuid().replace(/-/g, '')}`;
  const usage = usageFromState(parsed.usage, raw, parsed.content);
  log(
    `anthropic model=${responseModel}(${entry.code}) stream=${stream} content=${parsed.content.length}字`
  );

  if (!stream) {
    sendJson(res, 200, {
      id: msgId,
      type: 'message',
      role: 'assistant',
      model: responseModel,
      content: [{ type: 'text', text: parsed.content }],
      stop_reason: 'end_turn',
      stop_sequence: null,
      usage: anthropicUsage(usage),
    });
    return;
  }

  res.writeHead(200, {
    'Content-Type': 'text/event-stream; charset=utf-8',
    'Cache-Control': 'no-cache, no-transform',
    Connection: 'keep-alive',
    'X-Accel-Buffering': 'no',
    ...corsHeaders(),
  });
  const sendEvent = (event, data) => res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
  sendEvent('message_start', {
    type: 'message_start',
    message: {
      id: msgId,
      type: 'message',
      role: 'assistant',
      model: responseModel,
      content: [],
      stop_reason: null,
      stop_sequence: null,
      usage: { input_tokens: usage.prompt_tokens, output_tokens: 0 },
    },
  });
  sendEvent('content_block_start', {
    type: 'content_block_start',
    index: 0,
    content_block: { type: 'text', text: '' },
  });
  if (parsed.content) {
    sendEvent('content_block_delta', {
      type: 'content_block_delta',
      index: 0,
      delta: { type: 'text_delta', text: parsed.content },
    });
  }
  sendEvent('content_block_stop', { type: 'content_block_stop', index: 0 });
  sendEvent('message_delta', {
    type: 'message_delta',
    delta: { stop_reason: 'end_turn', stop_sequence: null },
    usage: { output_tokens: usage.completion_tokens },
  });
  sendEvent('message_stop', { type: 'message_stop' });
  res.end();
}

async function handleAnthropicCountTokens(req, res) {
  const raw = await readBody(req, Number(cfg.maxBodyBytes) || DEFAULT_CONFIG.maxBodyBytes);
  let body = {};
  try {
    body = JSON.parse(raw);
  } catch (_) {
    /* 忽略解析失败，按原文估算 */
  }
  const approx = Math.max(1, Math.ceil(JSON.stringify(body).length / 4));
  sendJson(res, 200, { input_tokens: approx });
}

// ───────────────────── OpenAI Responses API 兼容（Codex CLI 用） ─────────────────────
// Codex 0.147+ 只支持 wire_api="responses"，这里把 Responses 请求翻译成 Notion 请求，
// 再按 Responses 的 message / SSE 事件格式返回。

function responsesInputToMessages(body) {
  const messages = [];
  const instructions = typeof body.instructions === 'string' ? body.instructions : '';
  if (instructions) messages.push({ role: 'system', content: instructions });
  const input = body.input;
  if (typeof input === 'string') {
    if (input) messages.push({ role: 'user', content: input });
  } else if (Array.isArray(input)) {
    for (const item of input) {
      if (!item || typeof item !== 'object') continue;
      if (item.type === 'message' || item.role) {
        const role = item.role === 'assistant' ? 'assistant' : 'user';
        let text = '';
        if (typeof item.content === 'string') text = item.content;
        else if (Array.isArray(item.content)) {
          text = item.content
            .map((part) => {
              if (typeof part === 'string') return part;
              if (!part || typeof part !== 'object') return '';
              if (part.type === 'input_text' || part.type === 'output_text' || part.type === 'text') {
                return part.text || '';
              }
              if (part.type === 'input_image' || part.type === 'image') return '[image]';
              if (part.type === 'tool_result') return extractAnthropicText(part.content);
              return '';
            })
            .filter(Boolean)
            .join('\n');
        }
        if (text) messages.push({ role, content: text });
      } else if (item.type === 'function_call_output') {
        const out = typeof item.output === 'string' ? item.output : JSON.stringify(item.output || '');
        if (out) messages.push({ role: 'user', content: `[tool result]\n${out}` });
      }
    }
  }
  return messages;
}

function responsesObject({ id, model, status, output, usage }) {
  return {
    id,
    object: 'response',
    created_at: Math.floor(Date.now() / 1000),
    status,
    model,
    output: output || [],
    usage: usage || null,
    parallel_tool_calls: false,
    tool_choice: 'auto',
    tools: [],
  };
}

function responsesMessageItem(id, text, status) {
  return {
    id,
    type: 'message',
    status,
    role: 'assistant',
    content: status === 'completed' ? [{ type: 'output_text', text, annotations: [] }] : [],
  };
}

async function handleResponses(req, res) {
  const raw = await readBody(req, Number(cfg.maxBodyBytes) || DEFAULT_CONFIG.maxBodyBytes);
  let body;
  try {
    body = JSON.parse(raw);
  } catch (e) {
    throw new HttpError(400, `请求体不是合法 JSON：${e.message}`);
  }
  const messages = responsesInputToMessages(body);
  if (!messages.length) throw new HttpError(400, 'input 不能为空');
  const dialog = messages.filter((m) => m.role !== 'system');
  if (!dialog.length) throw new HttpError(400, '至少需要一条 user 消息');
  const { entry, fallback } = resolveModel(body.model);
  if (fallback) log(`模型 "${body.model}" 未找到，回退到 ${entry.id}（${entry.code}）`);
  const stream = !!body.stream;
  const threadType = cfg.markdownChatModels.includes(entry.code)
    ? 'markdown-chat'
    : cfg.defaultThreadType;
  const webSearch = body.web_search !== undefined ? !!body.web_search : !!cfg.useWebSearch;
  const threadMode = resolveThreadMode(body, req);
  const payload = null; // 由 runWithThread 统一构建

  await acquireSlot();
  let parsed;
  try {
    ({ parsed } = await runWithThread({
      entry,
      messages,
      threadType,
      webSearch,
      reasoningEffort: null,
      threadId: null,
      mode: threadMode,
    }));
  } finally {
    releaseSlot();
  }
  const responseModel = body.model || entry.id;
  const respId = `resp_${uuid().replace(/-/g, '')}`;
  const itemId = `msg_${uuid().replace(/-/g, '')}`;
  const usage = usageFromState(parsed.usage, raw, parsed.content);
  const respUsage = {
    input_tokens: usage.prompt_tokens,
    output_tokens: usage.completion_tokens,
    total_tokens: usage.total_tokens,
  };
  log(`responses model=${responseModel}(${entry.code}) stream=${stream} content=${parsed.content.length}字`);

  if (!stream) {
    sendJson(res, 200, {
      ...responsesObject({
        id: respId,
        model: responseModel,
        status: 'completed',
        output: [responsesMessageItem(itemId, parsed.content, 'completed')],
        usage: respUsage,
      }),
    });
    return;
  }

  res.writeHead(200, {
    'Content-Type': 'text/event-stream; charset=utf-8',
    'Cache-Control': 'no-cache, no-transform',
    Connection: 'keep-alive',
    'X-Accel-Buffering': 'no',
    ...corsHeaders(),
  });
  const sendEvent = (event, data) => res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
  sendEvent('response.created', {
    type: 'response.created',
    response: responsesObject({ id: respId, model: responseModel, status: 'in_progress', output: [], usage: null }),
  });
  sendEvent('response.in_progress', {
    type: 'response.in_progress',
    response: responsesObject({ id: respId, model: responseModel, status: 'in_progress', output: [], usage: null }),
  });
  sendEvent('response.output_item.added', {
    type: 'response.output_item.added',
    output_index: 0,
    item: responsesMessageItem(itemId, '', 'in_progress'),
  });
  sendEvent('response.content_part.added', {
    type: 'response.content_part.added',
    item_id: itemId,
    output_index: 0,
    content_index: 0,
    part: { type: 'output_text', text: '', annotations: [] },
  });
  if (parsed.content) {
    sendEvent('response.output_text.delta', {
      type: 'response.output_text.delta',
      item_id: itemId,
      output_index: 0,
      content_index: 0,
      delta: parsed.content,
    });
  }
  sendEvent('response.output_text.done', {
    type: 'response.output_text.done',
    item_id: itemId,
    output_index: 0,
    content_index: 0,
    text: parsed.content,
  });
  sendEvent('response.content_part.done', {
    type: 'response.content_part.done',
    item_id: itemId,
    output_index: 0,
    content_index: 0,
    part: { type: 'output_text', text: parsed.content, annotations: [] },
  });
  sendEvent('response.output_item.done', {
    type: 'response.output_item.done',
    output_index: 0,
    item: responsesMessageItem(itemId, parsed.content, 'completed'),
  });
  sendEvent('response.completed', {
    type: 'response.completed',
    response: responsesObject({
      id: respId,
      model: responseModel,
      status: 'completed',
      output: [responsesMessageItem(itemId, parsed.content, 'completed')],
      usage: respUsage,
    }),
  });
  res.end();
}

function handleModels(req, res) {
  const ua = String(req.headers['user-agent'] || '');
  if (/codex/i.test(ua)) {
    // Codex CLI 会用自己的一套模型目录 schema 请求 /v1/models（要求 `models` 字段 +
    // 大量内部元数据），本代理不提供该元数据，返回空数组让它走 fallback metadata，
    // 避免它在终端打印一大段解析错误。
    sendJson(res, 200, { models: [] });
    return;
  }
  const created = Math.floor(state.startedAt / 1000);
  const data = state.registry.entries
    .filter((e) => !e.disabled)
    .map((e) => ({
      id: e.id,
      object: 'model',
      created,
      owned_by: e.family ? `notion-${e.family}` : 'notion',
    }));
  sendJson(res, 200, { object: 'list', data });
}

function handleHealth(req, res) {
  sendJson(res, 200, {
    ok: true,
    upstream: cfg.upstreamHost,
    spaceId: state.workspace ? mask(state.workspace.spaceId) : null,
    models: state.registry ? state.registry.entries.filter((e) => !e.disabled).length : 0,
    cookieOk: !state.lastAuthError,
    uptimeSeconds: Math.round((Date.now() - state.startedAt) / 1000),
  });
}

const server = http.createServer(async (req, res) => {
  const startedAt = Date.now();
  const url = new URL(req.url, `http://${req.headers.host || '127.0.0.1'}`);
  const pathname = url.pathname;
  try {
    if (req.method === 'OPTIONS') {
      res.writeHead(204, corsHeaders());
      res.end();
      return;
    }
    if (pathname === '/health' && req.method === 'GET') {
      handleHealth(req, res);
      return;
    }
    if ((pathname === '/v1/models' || pathname === '/models') && req.method === 'GET') {
      if (!checkAuth(req)) return sendError(res, 401, '本地 API key 不正确');
      handleModels(req, res);
      return;
    }
    if (pathname === '/v1/messages/count_tokens' && req.method === 'POST') {
      if (!checkAuth(req)) return sendJson(res, 401, anthropicErrorMessage(401, '本地 API key 不正确'));
      await handleAnthropicCountTokens(req, res);
      return;
    }
    if (pathname === '/v1/messages' && req.method === 'POST') {
      if (!checkAuth(req)) return sendJson(res, 401, anthropicErrorMessage(401, '本地 API key 不正确'));
      await handleAnthropicMessages(req, res);
      return;
    }
    if (pathname === '/v1/responses' && req.method === 'POST') {
      if (!checkAuth(req)) return sendError(res, 401, '本地 API key 不正确');
      await handleResponses(req, res);
      return;
    }
    if (
      (pathname === '/v1/chat/completions' || pathname === '/chat/completions') &&
      req.method === 'POST'
    ) {
      if (!checkAuth(req)) return sendError(res, 401, '本地 API key 不正确');
      await handleChatCompletions(req, res);
      return;
    }
    sendError(res, 404, `未知路径 ${req.method} ${pathname}`);
  } catch (e) {
    const status = e instanceof HttpError ? e.status : 500;
    if (status >= 500) log('请求失败:', e.message);
    if (res.headersSent) {
      try {
        res.end();
      } catch (_) {
        /* ignore */
      }
      return;
    }
    if (pathname.startsWith('/v1/messages')) {
      sendJson(res, status, anthropicErrorMessage(status, e.message));
    } else {
      sendError(res, status, e.message, e.extra);
    }
  } finally {
    if (pathname.startsWith('/v1/')) {
      const dur = Date.now() - startedAt;
      if (dur > 3000) log(`${req.method} ${pathname} ${dur}ms`);
    }
  }
});

// ───────────────────────────── 启动 ─────────────────────────────

function parseSpaces(data) {
  let spaceId = '';
  let userId = '';
  let spaceViewId = '';
  const nestedValue = (record) => {
    if (!record || typeof record !== 'object') return {};
    const outer = record.value || {};
    return outer.value || {};
  };
  for (const v of Object.values(data || {})) {
    if (!v || typeof v !== 'object') continue;
    for (const [, sv] of Object.entries(v.space || {})) {
      const val = nestedValue(sv);
      if (val && val.id) {
        spaceId = val.id;
        break;
      }
    }
    for (const [, vv] of Object.entries(v.space_view || {})) {
      const val = nestedValue(vv);
      if (val && val.id) {
        spaceViewId = val.id;
        break;
      }
    }
    for (const [, uv] of Object.entries(v.notion_user || {})) {
      const val = nestedValue(uv);
      if (val && val.id) userId = val.id;
    }
  }
  return { spaceId, userId, spaceViewId };
}

async function discoverWorkspace() {
  const res = await callNotion({
    pathname: '/api/v3/getSpaces',
    payload: {},
    timeoutMs: 60000,
  });
  if (res.status === 401) {
    if (await refreshCookie()) return discoverWorkspace();
    throw new Error('Notion 登录态无效（401）。请运行 python tools\\cdp_cookie_extract.py 刷新登录态。');
  }
  if (res.status !== 200) {
    const detail = res.records.length ? JSON.stringify(res.records[0]).slice(0, 300) : res.stderr.slice(0, 300);
    throw new Error(`getSpaces 失败（HTTP ${res.status}）：${detail}`);
  }
  const spacesData = (() => {
    try {
      return JSON.parse(res.raw || '{}');
    } catch (_) {
      const first = res.records.find((r) => r && typeof r === 'object' && !r.type);
      return first || {};
    }
  })();
  const parsed = parseSpaces(spacesData);
  if (!parsed.spaceId) throw new Error('无法从 getSpaces 响应中解析 spaceId');
  const ws = {
    host: cfg.upstreamHost,
    spaceId: parsed.spaceId,
    userId: parsed.userId || (state.cred && state.cred.notion_user_id) || '',
    spaceViewId: parsed.spaceViewId || '',
    clientVersion: cfg.clientVersion,
    userAgent: cfg.userAgent,
    defaultModel: 'almond-croissant-low',
    models: [],
    probedAt: new Date().toISOString(),
  };
  try {
    const modelsRes = await callNotion({
      pathname: '/api/v3/getAvailableModels',
      payload: { spaceId: ws.spaceId },
      timeoutMs: 60000,
    });
    if (modelsRes.status === 200) {
      let payload = {};
      try {
        payload = JSON.parse(modelsRes.raw || '{}');
      } catch (_) {
        payload = modelsRes.records.find((r) => r && typeof r === 'object' && !r.type) || {};
      }
      const list = payload.models || payload.availableModels || [];
      ws.models = list
        .filter((m) => m && m.model)
        .map((m) => ({
          model: m.model,
          display: m.modelMessage || m.modelName || m.name || m.model,
          family: m.modelFamily || m.family || '',
          disabled: !!m.isDisabled,
          efforts: (m.modelConfiguration && m.modelConfiguration.supportedReasoningEfforts) || [],
        }));
      log(`自动发现 ${ws.models.length} 个模型`);
    } else {
      log(`getAvailableModels 返回 ${modelsRes.status}，模型列表将使用已知别名`);
    }
  } catch (e) {
    log('获取模型列表失败:', e.message);
  }
  writeJson(PATHS.workspace, ws);
  return ws;
}

async function validateSession() {
  const res = await callNotion({ pathname: '/api/v3/getSpaces', payload: {}, timeoutMs: 60000 });
  if (res.status === 200) return true;
  if (res.status === 401) return false;
  log(`登录态校验返回 HTTP ${res.status}`);
  return res.status < 500;
}

async function bootstrap() {
  try {
    state.cred = loadCredential();
  } catch (e) {
    if (fs.existsSync(PATHS.credential)) throw e;
    log('未找到 Notion 登录态，尝试从浏览器调试端口自动读取...');
    state.cred = await extractCredentialViaCdp();
  }
  if (fs.existsSync(PATHS.workspace)) {
    try {
      state.workspace = readJson(PATHS.workspace);
    } catch (e) {
      log('workspace.json 解析失败，将重新发现:', e.message);
    }
  }
  if (!state.workspace || !state.workspace.spaceId) {
    log('首次启动：正在从 Notion 发现工作区信息...');
    state.workspace = await discoverWorkspace();
  }
  state.registry = buildRegistry(state.workspace);

  if (cfg.validateOnStart) {
    log('校验 Notion 登录态...');
    let ok = await validateSession();
    if (!ok) {
      if (await refreshCookie()) {
        ok = await validateSession();
      }
    }
    if (!ok) {
      throw new Error('Notion 登录态无效。请运行 python tools\\cdp_cookie_extract.py 刷新登录态后重试。');
    }
    log('登录态有效');
  }

  const modelCount = state.registry.entries.filter((e) => !e.disabled).length;
  log(`就绪 | space=${mask(state.workspace.spaceId)} | 模型 ${modelCount} 个 | 接口 http://${cfg.bind}:${cfg.port}/v1`);
  if (!cfg.apiKey) log('本地 API 未设置密钥（config.json.apiKey），任何 key 均可调用');
}

async function main() {
  const args = process.argv.slice(2);
  if (args.includes('--port')) {
    const idx = args.indexOf('--port');
    cfg.port = Number(args[idx + 1]) || cfg.port;
  }
  if (args.includes('--help')) {
    console.log(
      '用法: notion-proxy [--port 8787] [--check] [--refresh]\n' +
        '  --check    只校验登录态并打印模型列表，然后退出\n' +
        '  --refresh  从浏览器调试端口重新读取 Notion 登录态（写 secret/credential.json）\n' +
        '  启动后接口: GET /v1/models, POST /v1/chat/completions, POST /v1/messages, POST /v1/responses, GET /health'
    );
    return;
  }
  if (args.includes('--refresh')) {
    try {
      await extractCredentialViaCdp();
      console.log('登录态刷新成功');
    } catch (e) {
      console.error('登录态刷新失败:', e.message);
      process.exitCode = 1;
    }
    return;
  }
  if (args.includes('--parse')) {
    const file = args[args.indexOf('--parse') + 1];
    if (!file || !fs.existsSync(file)) {
      console.error('用法: node notion-proxy.js --parse <ndjson-file>');
      process.exitCode = 1;
      return;
    }
    const st = new StreamState();
    for (const line of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
      const text = line.trim();
      if (!text) continue;
      let rec;
      try {
        rec = JSON.parse(text);
      } catch (_) {
        continue;
      }
      st.apply(rec);
    }
    console.log(JSON.stringify(st.result(), null, 2));
    return;
  }
  try {
    await bootstrap();
  } catch (e) {
    console.error('\n启动失败:', e.message);
    if (!fs.existsSync(PATHS.credential)) {
      console.error('\n需要先准备 Notion 登录态：');
      console.error('  1) Edge 用调试端口启动：msedge.exe --remote-debugging-port=9222');
      console.error('  2) 在这个 Edge 里登录 https://app.notion.com');
      console.error('  3) 重新运行本程序（会自动读取登录态），或运行 notion-proxy --refresh');
    }
    process.exitCode = 1;
    return;
  }
  if (args.includes('--check')) {
    const models = state.registry.entries.filter((e) => !e.disabled);
    console.log(`\n可用模型（${models.length} 个）：`);
    for (const m of models) console.log(`  - ${m.id}  (${m.code})  ${m.display}`);
    return;
  }
  server.listen(cfg.port, cfg.bind, () => {
    log(`监听 http://${cfg.bind}:${cfg.port}`);
  });
  const shutdown = () => {
    log('正在退出...');
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 1500).unref();
  };
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}

main();
