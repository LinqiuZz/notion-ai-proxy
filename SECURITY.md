# 安全说明

## 不要提交本机凭据

这个项目依赖 **你自己的 Notion 登录态**（`token_v2` Cookie）才能工作，它等同于账号登录凭据。

- `secret/` 目录（`credential.json` / `workspace.json` / `thread-state.json`）已在 `.gitignore` 中，**永远不要提交**。
- 不要截图、粘贴或在 issue 里贴出 `token_v2`、`notion_user_id`、`Cookie` 头或 `.tmp/` 里的调试日志。
- exe 是自构建未签名程序，仅从本仓库 `tools/build-exe.ps1` 构建，不要下载来路不明的 exe。

## 怀疑凭据泄露时

1. 在 Notion 里 **退出所有设备**（Settings → Sessions → Log out of all devices）；
2. 重新登录，然后运行 `update-cookie.cmd`（或 `notion-proxy.exe --refresh`）刷新本机凭据；
3. 如果曾把 `secret/` 推到公开仓库：立刻按上面步骤轮换凭据，并清理 git 历史（`git filter-repo`）。

## 报告安全问题

- 一般问题：开一个 issue（**不要**包含任何真实凭据）。
- 敏感问题（可导致凭据泄露、任意文件写入、命令执行绕过等）：请使用 GitHub 的
  [Security Advisories](https://github.com/LinqiuZz/notion-ai-proxy/security/advisories/new) 私密报告。

## 使用边界

本项目通过模拟网页请求使用 Notion AI，可能与 Notion 服务条款存在冲突，且接口随时可能失效。
请仅用于个人学习/自用、低频使用；不要对外提供服务、不要用于商业用途、不要用公司或共享账号。