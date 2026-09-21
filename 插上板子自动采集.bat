@echo off
chcp 65001 >nul
title ESP32-S3-EYE 自动采集守护
rem ============================================================
rem  插上开发板、打开网页就有数据 —— 一键守护
rem  双击本文件即可：
rem    1) 自动在 0.0.0.0:8000 起服务端（已在跑就接管，不重复起）
rem    2) 自动用默认浏览器打开看板 http://localhost:8000/
rem    3) 持续守护：服务端崩了自动重启；开发板插上时自动弹看板
rem  窗口开着 = 守护在跑。关窗口即停（也可另开命令行跑：
rem    python server\tools\auto_serve.py --stop ）
rem  想开机自动后台守护：python server\tools\auto_serve.py --install-startup
rem ============================================================
cd /d "%~dp0"

set "PY=D:\anaconda3\python.exe"
if not exist "%PY%" set "PY=python"

echo.
echo   正在启动守护（服务端 + 看板自动弹出）...
echo   看板地址： http://localhost:8000/
echo   按 Ctrl+C 或直接关窗口即可停止。
echo.

"%PY%" -X utf8 "%~dp0server\tools\auto_serve.py" --watch
pause
