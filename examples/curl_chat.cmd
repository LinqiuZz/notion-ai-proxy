@echo off
chcp 65001 >nul
setlocal

rem ============================================================
rem  Notion AI 本地代理 - curl.exe 调用示例（含中文请求）
rem  用法：双击本文件，或在命令行运行：
rem      examples\curl_chat.cmd
rem      examples\curl_chat.cmd http://127.0.0.1:8787
rem  说明：本脚本把控制台临时切到 UTF-8（chcp 65001），
rem        这样中文请求体不会乱码。
rem ============================================================

set "ROOT=%~1"
if "%ROOT%"=="" set "ROOT=http://127.0.0.1:8787"

rem 允许用户把地址写成 .../v1 或 .../v1/，自动去掉末尾的 /v1
if "%ROOT:~-3%"=="/v1" set "ROOT=%ROOT:~0,-3%"
if "%ROOT:~-1%"=="/" set "ROOT=%ROOT:~0,-1%"

set "BASE=%ROOT%/v1"
set "MODEL=claude-sonnet4.6"
if not "%NOTION_MODEL%"=="" set "MODEL=%NOTION_MODEL%"

where curl.exe >nul 2>nul
if errorlevel 1 (
    echo [错误] 找不到 curl.exe。Windows 10/11 一般自带 curl，请确认系统版本。
    echo.
    pause
    exit /b 1
)

echo ============================================================
echo  Notion AI 本地代理 - curl 调用示例
echo  代理地址: %ROOT%
echo  模型:     %MODEL%
echo ============================================================
echo.

echo [1/3] 健康检查: GET %ROOT%/health
curl.exe -sS --noproxy "*" "%ROOT%/health"
echo.
echo.

echo [2/3] 模型列表: GET %BASE%/models
curl.exe -sS --noproxy "*" "%BASE%/models"
echo.
echo.

echo [3/3] 对话: POST %BASE%/chat/completions （中文请求）
curl.exe -sS --noproxy "*" -X POST "%BASE%/chat/completions" ^
  -H "Content-Type: application/json" ^
  -H "Authorization: Bearer sk-local" ^
  -d "{\"model\":\"%MODEL%\",\"messages\":[{\"role\":\"user\",\"content\":\"你好，请用一句中文介绍一下你自己，并说明你现在用的是哪个模型。\"}],\"stream\":false}"
echo.
echo.

echo 提示：
echo   - 如果返回 401：登录态过期，运行 update-cookie.cmd 后重试。
echo   - 如果返回 403 / 429 / 连接失败：请查看 README.md 的“常见问题”。
echo   - 想换模型：先 set NOTION_MODEL=claude-opus5 再运行本脚本。
echo.
pause
endlocal
