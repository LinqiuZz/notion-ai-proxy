@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal

echo ============================================================
echo  Notion AI 本地代理 - 启动
echo  启动后请保持此窗口打开；关闭窗口 = 停止代理
echo ============================================================
echo.

where node >nul 2>nul
if errorlevel 1 (
    echo [错误] 找不到 node 命令。请先安装 Node.js 18 或更高版本：
    echo        https://nodejs.org/
    echo.
    pause
    exit /b 1
)

if not exist "notion-proxy.js" (
    echo [错误] 当前目录找不到 notion-proxy.js：
    echo        %CD%
    echo        请确认本脚本与 notion-proxy.js 放在同一个文件夹里。
    echo.
    pause
    exit /b 1
)

echo 正在启动代理（首次启动会自动发现 spaceId 和模型列表，请稍候）...
echo 提示：看到“就绪”后，这个窗口不要关；关闭窗口代理就停止了。
echo.

node notion-proxy.js
set "CODE=%errorlevel%"

echo.
if "%CODE%"=="0" (
    echo [提示] 代理已停止。
) else (
    echo [提示] 代理异常退出，退出码 %CODE%。
    echo        请把上面的错误信息发给维护者，或查看 README.md 的“常见问题”。
)
echo.
pause
endlocal
