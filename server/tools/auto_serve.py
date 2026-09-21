#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""插上开发板、打开网页就有数据 —— 服务端守护 + 看板自动弹出。

为什么需要它
------------
用 file:// 直接双击 server/static/index.html 时，页面会去连
http://127.0.0.1:8000，但 uvicorn 服务端不会自己跑起来，于是页面报
“无法连接服务端”。浏览器里的 JS 出于安全沙箱无法启动本地进程，所以
“自动”只能由一个本地常驻的守护来做。

这个脚本做三件事
----------------
1) 保证服务端在 0.0.0.0:8000 上跑着（没起就起、崩了就重启、已在跑就接管）。
   绑 0.0.0.0 而不是 127.0.0.1，板子才能从手机热点/局域网把数据传进来。
2) 盯着开发板（USB VID_303A & PID_1001，ESP32-S3 原生 USB-Serial/JTAG）。
   板子从“没插”变成“插上”时，自动用默认浏览器打开 http://localhost:8000/。
3) 可注册到 Windows 启动项（--install-startup），开机即在后台守护——
   这样“插上板子打开网页自动就有数据”才真正成立（服务端永远在线）。

它不碰防火墙、不烧固件、不改任何系统设置（除非你显式 --install-startup）。
日志写在 server/data/auto_serve.log，服务端输出写在 server/data/uvicorn.log。
"""
from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[2]
SERVER_DIR = ROOT / "server"
DATA_DIR = SERVER_DIR / "data"
LOG_FILE = DATA_DIR / "auto_serve.log"
PID_FILE = DATA_DIR / "auto_serve.pid"

BOARD_VID = 0x303A          # Espressif
BOARD_PID = 0x1001          # ESP32-S3 USB-Serial/JTAG
DEFAULT_PORT = 8000
HEALTH_PATH = "/health"

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

STARTUP_NAME = "ESP32-S3-EYE-守护.vbs"


def log(msg: str) -> None:
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def health_ok(port: int, timeout: float = 2.0) -> bool:
    url = "http://127.0.0.1:%d%s" % (port, HEALTH_PATH)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= getattr(r, "status", 200) < 300
    except Exception:
        return False


def port_listening(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    except Exception:
        return False
    finally:
        s.close()


def board_present() -> bool:
    """开发板是否插在 USB 上。pyserial 不在就当作‘检测不可用’，返回 False。"""
    try:
        import serial.tools.list_ports as lp
    except Exception:
        return False
    try:
        for p in lp.comports():
            if p.vid == BOARD_VID and p.pid == BOARD_PID:
                return True
    except Exception:
        return False
    return False


def uvicorn_cmd(port: int) -> list:
    cmd = [sys.executable, "-m", "uvicorn", "app:app",
           "--host", "0.0.0.0", "--port", str(port)]
    if (SERVER_DIR / ".env").exists():
        cmd += ["--env-file", ".env"]
    return cmd


def spawn_uvicorn(port: int, detached: bool, logfh):
    cmd = uvicorn_cmd(port)
    log("启动服务端: " + " ".join(cmd) + "   (cwd=%s)" % SERVER_DIR)
    flags = 0
    if os.name == "nt":
        flags = (DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP) if detached else CREATE_NO_WINDOW
    return subprocess.Popen(
        cmd, cwd=str(SERVER_DIR), creationflags=flags,
        stdout=logfh, stderr=subprocess.STDOUT,
    )


def open_browser(port: int) -> None:
    url = "http://localhost:%d/" % port
    log("打开看板: " + url)
    try:
        webbrowser.open(url)
    except Exception as e:
        log("打开浏览器失败: %s（可手动访问 %s）" % (e, url))


def write_pid() -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass


def read_pid():
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def pid_alive(pid) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        out = subprocess.run('tasklist /FI "PID eq %d" /NH' % pid, shell=True,
                             capture_output=True, text=True)
        return str(pid) in (out.stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def kill_tree(pid) -> None:
    if not pid:
        return
    try:
        if os.name == "nt":
            subprocess.run("taskkill /PID %d /T /F" % pid, shell=True,
                           capture_output=True)
        else:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass


def stop_supervisor() -> int:
    pid = read_pid()
    if not pid or not pid_alive(pid):
        log("没有正在运行的守护进程。")
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        return 0
    log("停止守护进程 PID=%d 及其子进程..." % pid)
    kill_tree(pid)
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    return 0


def startup_dir():
    if os.name != "nt":
        return None
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft/Windows/Start Menu/Programs/Startup"


def install_startup(port: int) -> int:
    sd = startup_dir()
    if not sd:
        log("仅支持 Windows 启动项。")
        return 1
    pyw = Path(sys.executable).with_name("pythonw.exe")
    py = str(pyw) if pyw.exists() else sys.executable
    script = str(Path(__file__).resolve())
    vbs = (
        'Set ws = CreateObject("WScript.Shell")\r\n'
        'ws.Run """%s"" ""%s"" --watch --no-browser-on-start --port %d", 0, False\r\n'
        % (py, script, port)
    )
    target = sd / STARTUP_NAME
    try:
        sd.mkdir(parents=True, exist_ok=True)
        target.write_text(vbs, encoding="utf-8")
        log("已注册开机自启: %s" % target)
        log("开机后会在后台守护服务端；插上板子自动弹看板。卸载用 --uninstall-startup。")
        return 0
    except Exception as e:
        log("注册启动项失败: %s" % e)
        return 1


def uninstall_startup() -> int:
    sd = startup_dir()
    if not sd:
        return 0
    target = sd / STARTUP_NAME
    try:
        if target.exists():
            target.unlink()
            log("已移除开机自启: %s" % target)
        else:
            log("启动项不存在，无需移除。")
        return 0
    except Exception as e:
        log("移除启动项失败: %s" % e)
        return 1


def once(port: int, browser: bool) -> int:
    """确保服务端起来并打开看板，然后退出（不守护）。"""
    if health_ok(port):
        log("服务端已在运行（端口 %d）。" % port)
    elif port_listening(port):
        log("端口 %d 被占用但 /health 不通——可能有别的程序占着，或服务端还在启动。" % port)
    else:
        log("服务端未运行，后台拉起...")
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            logfh = open(DATA_DIR / "uvicorn.log", "a", encoding="utf-8")
        except Exception:
            logfh = subprocess.DEVNULL
        spawn_uvicorn(port, detached=True, logfh=logfh)
        for _ in range(40):
            if health_ok(port):
                break
            time.sleep(0.5)
    if health_ok(port):
        log("服务端就绪：http://localhost:%d/" % port)
        if browser:
            open_browser(port)
        return 0
    log("服务端未能就绪，请查看 %s" % (DATA_DIR / "uvicorn.log"))
    return 1


def watch(port: int, interval: float, browser: bool, browser_on_start: bool) -> int:
    write_pid()
    child = None
    logfh = None
    board_was = board_present()
    opened_start = False
    unhealthy_streak = 0
    log("守护开始：端口 %d，间隔 %.1fs，开发板当前%s"
        % (port, interval, "已插入" if board_was else "未插入"))
    if board_was and browser:
        log("（板子已插着，待服务端就绪后弹看板）")

    def cleanup(*_):
        if child is not None and child.poll() is None:
            log("终止服务端子进程...")
            kill_tree(child.pid)
        if logfh not in (None, subprocess.DEVNULL):
            try:
                logfh.close()
            except Exception:
                pass
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    try:
        signal.signal(signal.SIGINT, lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
        try:
            signal.signal(signal.SIGBREAK, lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
        except Exception:
            pass
    except Exception:
        pass

    try:
        while True:
            healthy = health_ok(port)
            if healthy:
                unhealthy_streak = 0
                if not opened_start:
                    opened_start = True
                    if browser and browser_on_start:
                        open_browser(port)
            else:
                unhealthy_streak += 1
                if child is not None and child.poll() is None:
                    pass    # 我们拉起的子进程还活着，多半还在启动中
                elif child is not None and child.poll() is not None:
                    log("服务端进程退出（code=%s），准备重启..." % child.returncode)
                    child = None
                elif port_listening(port):
                    if unhealthy_streak in (1, 10, 30):
                        log("端口 %d 有进程在听但 /health 不通：可能别的服务端正在启动，或被别的程序占用。" % port)
                else:
                    try:
                        if logfh not in (None, subprocess.DEVNULL):
                            logfh.close()
                        DATA_DIR.mkdir(parents=True, exist_ok=True)
                        logfh = open(DATA_DIR / "uvicorn.log", "a", encoding="utf-8")
                    except Exception:
                        logfh = subprocess.DEVNULL
                    child = spawn_uvicorn(port, detached=False, logfh=logfh)

            board_now = board_present()
            if board_now and not board_was:
                log("检测到开发板插入 (VID_303A&PID_1001)。")
                if browser:
                    if healthy:
                        open_browser(port)
                    else:
                        log("服务端尚未就绪，待其起来后再弹看板。")
            elif board_was and not board_now:
                log("开发板已拔出。")
            board_was = board_now

            time.sleep(interval)
    except KeyboardInterrupt:
        log("收到中断，停止守护。")
    finally:
        cleanup()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="ESP32-S3-EYE 服务端守护 + 看板自动弹出")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--interval", type=float, default=2.0,
                    help="守护轮询间隔秒数（默认 2.0）")
    ap.add_argument("--once", action="store_true",
                    help="确保服务端起来并打开看板后退出（不守护）")
    ap.add_argument("--watch", action="store_true",
                    help="守护模式（默认行为）")
    ap.add_argument("--stop", action="store_true",
                    help="停止正在运行的守护进程及其服务端")
    ap.add_argument("--install-startup", action="store_true",
                    help="注册开机自启（后台守护，不在开机时弹浏览器）")
    ap.add_argument("--uninstall-startup", action="store_true",
                    help="移除开机自启")
    ap.add_argument("--no-browser", action="store_true",
                    help="完全不打开浏览器（无头/测试用）")
    ap.add_argument("--no-browser-on-start", action="store_true",
                    help="启动时不弹看板，仅在检测到插板时弹")
    args = ap.parse_args(argv)

    if args.stop:
        return stop_supervisor()
    if args.install_startup:
        return install_startup(args.port)
    if args.uninstall_startup:
        return uninstall_startup()

    browser = not args.no_browser
    browser_on_start = not args.no_browser_on_start
    if args.once:
        return once(args.port, browser)
    return watch(args.port, args.interval, browser, browser_on_start)


if __name__ == "__main__":
    raise SystemExit(main())
