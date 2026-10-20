@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal

echo ============================================================
echo  Claude Code - 使用本地 Notion AI 代理（模型 opus-5.5）
echo ============================================================
echo.

where claude >nul 2>nul
if errorlevel 1 (
    echo [错误] 找不到 claude 命令。请先安装 Claude Code：
    echo        npm install -g @anthropic-ai/claude-code
    echo.
    pause
    exit /b 1
)

if not exist "claude-code-settings.json" (
    echo [错误] 缺少 claude-code-settings.json（应与本脚本在同一目录）。
    echo.
    pause
    exit /b 1
)

echo 提示：本脚本会用 --settings 覆盖你的 ANTHROPIC_BASE_URL，
echo       只对本次 Claude Code 会话生效，不会改动 ~/.claude/settings.json。
echo       参数会原样传给 claude：不带参数=交互模式，加 -p "问题"=单次问答。
echo.

claude --settings "%~dp0claude-code-settings.json" %*
set "CODE=%errorlevel%"
endlocal & exit /b %CODE%
