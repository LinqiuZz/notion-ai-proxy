@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal

echo ============================================================
echo  Codex CLI - 使用本地 Notion AI 代理（模型 opus-5.5）
echo ============================================================
echo.

where codex >nul 2>nul
if errorlevel 1 (
    echo [错误] 找不到 codex 命令。请先安装 Codex CLI：
    echo        npm install -g @openai/codex
    echo.
    pause
    exit /b 1
)

if not exist "codex-notion.config.toml" (
    echo [错误] 缺少 codex-notion.config.toml（应与本脚本在同一目录）。
    echo.
    pause
    exit /b 1
)

rem 代理默认不校验 key，这里随便给一个非空值即可
set "NOTION_PROXY_KEY=sk-local"
set "PROFILE=%USERPROFILE%\.codex\notion.config.toml"

if not exist "%PROFILE%" (
    if not exist "%USERPROFILE%\.codex" mkdir "%USERPROFILE%\.codex" 2>nul
    copy /Y "%~dp0codex-notion.config.toml" "%PROFILE%" >nul
    echo [提示] 已安装 Codex profile 到 %PROFILE%
)

echo 提示：不带参数=交互模式；加 exec "问题"=单次问答。
echo       本代理不支持工具调用，Codex 只能当聊天用，不能自动改文件/跑命令。
echo       出现 model metadata 警告属正常现象，不影响回答。
echo.

codex -p notion %*
set "CODE=%errorlevel%"
endlocal & exit /b %CODE%
