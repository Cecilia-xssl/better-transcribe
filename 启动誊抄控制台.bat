@echo off
chcp 936 >nul
title 写字机誊抄控制台（关闭本窗口=停止服务）
set "PY=python"
set "ROOT=%~dp0"
set "URL=http://127.0.0.1:8765"

rem 已在运行 -> 只开网页
powershell -NoProfile -Command "try { Invoke-WebRequest -Uri '%URL%/status' -UseBasicParsing -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }"
if not errorlevel 1 goto already

echo 誊抄控制台启动中…
echo 服务在本窗口运行（关闭本窗口=停止服务），浏览器将自动打开 %URL%
echo.
set "KENJOY_OPEN_BROWSER=1"
"%PY%" -u "%ROOT%scripts\kenjoy_server.py"
echo.
echo 服务已退出。上方窗口有日志与报错信息可截图反馈。
pause
exit /b 1

:already
start "" %URL%
echo 服务已在运行，已打开网页。
echo 若只是想看看界面，可以关掉本窗口（不影响服务）。
timeout /t 3 >nul
exit /b 0
