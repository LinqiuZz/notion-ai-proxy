@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal

echo ============================================================
echo  刷新 Notion 登录态
echo  从本机 Edge 的调试端口读取 cookie，写入 secret\credential.json
echo ============================================================
echo.
echo 前置条件：
echo   1. Edge 必须完全关闭后，用调试端口重新启动，例如在“运行”里执行：
echo      "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --remote-debugging-port=9222
echo   2. 在这个 Edge 里已经登录 https://app.notion.com
echo   3. 本脚本只在本机读取 cookie，不会打印 token，也不会上传任何东西。
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 找不到 python 命令。请先安装 Python 3，并在安装时勾选
    echo        Add python.exe to PATH。
    echo.
    pause
    exit /b 1
)

if not exist "tools\cdp_cookie_extract.py" (
    echo [错误] 找不到 tools\cdp_cookie_extract.py。
    echo        请确认本脚本与 tools 文件夹都放在 ai-proxy 目录里。
    echo.
    pause
    exit /b 1
)

echo 正在读取本机 Edge 的 Notion cookie ...
echo.
python "tools\cdp_cookie_extract.py"
set "CODE=%errorlevel%"
echo.

if "%CODE%"=="0" (
    echo [成功] 登录态已刷新，写入了 secret\credential.json。
    echo        回到代理窗口重新试一次即可；代理在收到 401 时也会自动尝试刷新一次。
) else (
    echo [失败] 刷新失败，退出码 %CODE%。请依次检查：
    echo   1. Edge 是否以 --remote-debugging-port=9222 启动（可用 netstat -ano ^| findstr :9222 确认）
    echo   2. Edge 里是否已经登录 Notion（打开 https://app.notion.com 确认）
    echo   3. 是否安装依赖：pip install requests websocket-client
    echo   4. 如果还是不行，关掉所有 Edge 窗口后按第 1 步重新启动 Edge
)
echo.
pause
endlocal
