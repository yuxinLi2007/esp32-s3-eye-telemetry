@echo off
chcp 65001 >nul
rem 双击这个文件即可启动服务端。窗口开着 = 服务端在跑，网页才有数据可看。
rem 关掉窗口服务端就停了——数据不会丢，都在 server\data\telemetry.db 里。
cd /d "%~dp0server"

rem 优先用本机已知的 Python，找不到就退回 PATH 里的 python
set "PY=D:\anaconda3\python.exe"
if not exist "%PY%" set "PY=python"

rem .env 里放着 INGEST_TOKEN 等密钥。文件不存在时不传这个参数，
rem 否则 uvicorn 会因为读不到文件直接退出。
set "ENVARGS="
if exist ".env" set "ENVARGS=--env-file .env"
if not exist ".env" echo [提示] 没有 server\.env，INGEST_TOKEN / CONTROL_TOKEN 均未设置。

echo.
echo   本机打开：   http://localhost:8000/
echo   直接双击 index.html 打开时，网页默认连 http://127.0.0.1:8000，也是同一台。
echo   若服务端跑在别的机器上，用 http://地址:8000/?api=http://服务端地址:8000 打开。
echo.
echo   按 Ctrl+C 或直接关窗口即可停止。
echo.

"%PY%" -m uvicorn app:app --host 0.0.0.0 --port 8000 %ENVARGS%
pause
