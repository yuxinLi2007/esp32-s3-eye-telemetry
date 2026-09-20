#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""插上开发板之后，网页端没数据？跑这个。

    python server/tools/doctor.py            # 只诊断
    python server/tools/doctor.py --port 8000

它按"从板子到网页"的实际数据流向逐项体检，每一项给出 PASS / WARN / FAIL，
FAIL 会直接附上可复制粘贴的修复命令。全部通过时退出码 0。

为什么要这个脚本：真机联调时"网页没数据"这一个现象，背后可能是六七个完全不同的
原因（没通电、secrets.h 缺失、SERVER_URL 填了 127.0.0.1、防火墙拦入站、服务端只绑
了 127.0.0.1、固件没烧新版……）。人肉排查每次都要把这套顺序重走一遍，而且最容易
漏的恰恰是最隐蔽的那条（防火墙 Block 规则优先级高于 Allow）。把顺序固化成代码，
下次插上板子跑一次就知道卡在哪。

设计原则：只读诊断，不改系统。所有修复动作都以"建议你执行的命令"形式打印出来，
由人决定要不要跑——诊断工具悄悄改防火墙，比没有诊断工具更危险。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import urllib.request
import winreg

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SECRETS = os.path.join(ROOT, "firmware", "include", "secrets.h")
CONFIG_H = os.path.join(ROOT, "firmware", "include", "config.h")
ENV_FILE = os.path.join(ROOT, "server", ".env")

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_results: list[tuple[str, str, str]] = []


def record(name: str, status: str, detail: str = "", fix: str = "") -> str:
    _results.append((name, status, detail))
    mark = {"PASS": "[ ok ]", "WARN": "[warn]", "FAIL": "[FAIL]"}[status]
    print("%s %-34s %s" % (mark, name, detail))
    if fix and status != PASS:
        for line in fix.strip().splitlines():
            print("         -> %s" % line.strip())
    return status


def run(cmd: str, timeout: int = 25) -> str:
    """跑一条命令拿 stdout。失败不抛异常，返回空串——诊断脚本自己不能先崩。"""
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, timeout=timeout)
        out = p.stdout.decode("utf-8", "replace")
        if not out.strip():
            out = p.stderr.decode("utf-8", "replace")
        return out
    except Exception:
        return ""


def read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


# ------------------------------------------------------------------ 各项检查
def local_ipv4s() -> list[str]:
    """本机所有 IPv4。用 ipconfig 而不是 Get-NetIPAddress：后者在非管理员的
    受限 shell 里会被拒绝访问，而 ipconfig 谁都能跑。"""
    out = run("ipconfig")
    return re.findall(r"IPv4[^\d]*(\d+\.\d+\.\d+\.\d+)", out)


def check_secrets(port: int) -> tuple[str, str]:
    src = read_text(SECRETS)
    if not src:
        record("secrets.h 存在", FAIL, "找不到 %s" % SECRETS, """
            copy firmware\\include\\secrets.example.h firmware\\include\\secrets.h
            然后填 WIFI_SSID / WIFI_PASSWORD / SERVER_URL / INGEST_TOKEN
            """)
        return "", ""

    record("secrets.h 存在", PASS, os.path.relpath(SECRETS, ROOT))

    m = re.search(r'#define\s+WIFI_SSID\s+"([^"]*)"', src)
    ssid = m.group(1) if m else ""
    if not ssid or "你的" in ssid:
        record("WIFI_SSID 已填写", FAIL, repr(ssid), "编辑 secrets.h 填真实热点名")
    else:
        record("WIFI_SSID 已填写", PASS, ssid)

    m = re.search(r'#define\s+SERVER_URL\s+"([^"]*)"', src)
    url = m.group(1) if m else ""
    m2 = re.search(r"#define\s+INGEST_TOKEN\s+\"([^\"]*)\"", src)
    fw_token = m2.group(1) if m2 else ""
    return url, fw_token


def check_server_url(url: str, port: int) -> str | None:
    """返回服务端应该被填进去的正确地址；None 表示当前值就是对的。"""
    if not url:
        record("SERVER_URL 已填写", FAIL, "secrets.h 里没有 SERVER_URL")
        return None

    host = re.sub(r"^https?://", "", url).split(":")[0].split("/")[0]
    if host in ("127.0.0.1", "localhost", "0.0.0.0"):
        record("SERVER_URL 不是回环地址", FAIL, url,
               "127.0.0.1 指向板子自己。必须填本机的局域网 IP，见下一项。")
        return None

    ips = local_ipv4s()
    if host in ips:
        record("SERVER_URL 指向本机网卡", PASS, "%s（本机 IP 之一：%s）" % (url, ", ".join(ips)))
        return None

    record("SERVER_URL 指向本机网卡", FAIL,
           "填的是 %s，但本机现在的 IP 是 %s" % (host, ", ".join(ips) or "（没查到）"),
           "手机热点重连后 IP 会变。改 secrets.h：\n"
           '#define SERVER_URL "http://%s:%d"\n改完要重新烧录：pio run -t upload'
           % (ips[0] if ips else "<本机IP>", port))
    return "http://%s:%d" % (ips[0], port) if ips else None


def check_token(fw_token: str, port: int) -> None:
    env = read_text(ENV_FILE)
    m = re.search(r"^\s*INGEST_TOKEN\s*=\s*(.+?)\s*$", env, re.M)
    srv_token = m.group(1).strip().strip('"').strip("'") if m else ""
    if not srv_token:
        record("入库令牌两侧一致", WARN, "server/.env 里没设 INGEST_TOKEN（服务端不校验）")
        return
    if fw_token == srv_token:
        record("入库令牌两侧一致", PASS, "secrets.h 与 server/.env 相同")
    else:
        record("入库令牌两侧一致", FAIL,
               "固件=%s… / 服务端=%s…" % (fw_token[:8], srv_token[:8]),
               "把 secrets.h 的 INGEST_TOKEN 改成和 server/.env 一致，然后重新烧录。\n"
               "症状：板子串口刷 401，或 [cmd] 领取被拒 401。")


def check_listener(port: int) -> bool:
    out = run("netstat -ano -p tcp")
    listening = [ln for ln in out.splitlines()
                 if "LISTENING" in ln and re.search(r":%d\s" % port, ln)]
    if not listening:
        record("服务端在监听 %d" % port, FAIL, "没有进程监听",
               "启动服务端：\n"
               "  cd server && python -m uvicorn app:app --host 0.0.0.0 --port %d\n"
               "或直接双击根目录的 start_server.bat" % port)
        return False

    addrs = [ln.split()[1] for ln in listening]
    bound_all = any(a.startswith("0.0.0.0:") or a.startswith(":::") for a in addrs)
    if not bound_all:
        record("服务端在监听 %d" % port, FAIL,
               "只绑了 %s —— 板子从局域网连不进来" % ", ".join(addrs),
               "必须用 --host 0.0.0.0 启动，不能用默认的 127.0.0.1")
        return False

    record("服务端在监听 %d" % port, PASS, "0.0.0.0:%d（局域网可达）" % port)
    return True


def check_firewall(port: int) -> None:
    """这是最容易漏、也最隐蔽的一项。

    Windows 防火墙里 Block 规则的优先级高于 Allow：只要存在一条覆盖本次连接的
    Block，加多少条 8000 放行都没用。所以先查有没有"拦全部入站"的规则，
    再查有没有针对 8000 的放行。
    """
    out = run("netsh advfirewall firewall show rule name=all")
    if not out.strip():
        record("防火墙放行入站 %d" % port, WARN, "读不到防火墙规则（可能需要管理员）",
               "用管理员身份重跑，或手工确认端口 %d 入站已放行" % port)
        return

    blocks = re.split(r"\n(?=规则名称:|Rule Name:)", out)
    blanket = []
    for b in blocks:
        enabled = re.search(r"(已启用|Enabled):\s*(是|Yes)", b)
        direction = re.search(r"(方向|Direction):\s*(入|In)\b", b)
        action = re.search(r"(操作|Action):\s*(阻止|Block)", b)
        name = re.search(r"(规则名称|Rule Name):\s*(.+)", b)
        if not (enabled and direction and action):
            continue
        proto = re.search(r"(协议|Protocol):\s*(\S+)", b)
        lport = re.search(r"(本地端口|LocalPort):\s*(\S+)", b)
        # 协议 Any + 端口 Any = 拦住一切入站，这种规则会盖掉所有放行
        broad = (not proto or proto.group(2).lower() in ("any", "任何")) and \
                (not lport or lport.group(2).lower() in ("any", "任何"))
        if broad and name:
            blanket.append(name.group(2).strip())

    if blanket:
        record("无全局入站拦截规则", FAIL,
               "存在拦住全部入站的 Block 规则：%s" % ", ".join(blanket),
               "Block 优先于 Allow，这条不关，放行 %d 也没用。管理员 PowerShell：\n"
               '  netsh advfirewall firewall set rule name="%s" new enable=no\n'
               "（或直接运行 server\tools\\fix_firewall.ps1，会自动请求管理员权限）"
               % (port, blanket[0]))
    else:
        record("无全局入站拦截规则", PASS)

    allow = False
    for b in blocks:
        if not re.search(r"(已启用|Enabled):\s*(是|Yes)", b):
            continue
        if not re.search(r"(方向|Direction):\s*(入|In)\b", b):
            continue
        if not re.search(r"(操作|Action):\s*(允许|Allow)", b):
            continue
        lport = re.search(r"(本地端口|LocalPort):\s*(\S+)", b)
        if lport and (lport.group(2) == str(port) or lport.group(2).lower() in ("any", "任何")):
            allow = True
            break
    if allow:
        record("端口 %d 入站已放行" % port, PASS)
    else:
        record("端口 %d 入站已放行" % port, FAIL, "没有针对 %d 的入站放行规则" % port,
               "管理员 PowerShell：\n"
               '  netsh advfirewall firewall add rule name="Telemetry %d" dir=in '
               'action=allow protocol=TCP localport=%d profile=any\n'
               "（或直接运行 server\tools\\fix_firewall.ps1）" % (port, port))


def check_com_ports() -> None:
    """直接读注册表枚举串口，不依赖 pyserial，也不需要管理员。"""
    ports = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM") as k:
            i = 0
            while True:
                try:
                    _, val, _ = winreg.EnumValue(k, i)
                    ports.append(val)
                    i += 1
                except OSError:
                    break
    except OSError:
        pass
    if ports:
        record("检测到串口", PASS, ", ".join(sorted(ports)))
    else:
        record("检测到串口", FAIL, "系统里没有任何串口",
               "板子没插好 / 没通电 / 缺 USB 驱动。\n"
               "插上后在设备管理器里应出现 USB-SERIAL / USB Serial Device。")


def check_expected_fw() -> str:
    src = read_text(CONFIG_H)
    m = re.search(r'#define\s+FW_VERSION\s+"([^"]*)"', src)
    return m.group(1) if m else ""


def check_device(port: int, expected_fw: str) -> None:
    """最终裁决：数据到底进没进库。前面全 PASS 而这里 FAIL，说明问题在板子侧。"""
    url = "http://127.0.0.1:%d/api/v1/status" % port
    try:
        with urllib.request.urlopen(url, timeout=6) as r:
            st = json.loads(r.read())
    except Exception as e:
        record("设备数据正在入库", FAIL, "问不到服务端：%s" % e)
        return

    last = st.get("last_batch") or {}
    age = st.get("readings_last_60s", 0)
    age_ms = last.get("age_ms")
    fw = last.get("fw_version") or "?"

    if age > 0 and age_ms is not None and age_ms < 15000:
        record("设备数据正在入库", PASS,
               "近60秒 %d 条，最新批次延迟 %.1f 秒，boot=%s fw=%s"
               % (age, age_ms / 1000.0, last.get("boot_id"), fw))
    elif age > 0:
        record("设备数据正在入库", FAIL,
               "近60秒 %d 条，但最新批次已是 %.1f 分钟前（fw=%s ip=%s）"
               % (age, (age_ms or 0) / 60000.0, fw, last.get("source_ip")),
               "板子掉线了。看串口日志定位：\n"
               "  pio device monitor -p COM5 -b 115200\n"
               "常见：热点名/密码变了、热点没开、板子没通电。")
    else:
        record("设备数据正在入库", FAIL, "库里一条近期数据都没有",
               "先看串口有没有在刷 [uplink] OK：\n"
               "  pio device monitor -p COM5 -b 115200\n"
               "[uplink] HTTP -1 = 连不上服务端（查防火墙）；401 = 令牌不一致。")

    if expected_fw and fw != expected_fw and fw != "?":
        record("固件是最新版", WARN,
               "板子跑的是 %s，代码里写的是 %s" % (fw, expected_fw),
               "重新烧录：cd firmware && pio run -t upload --upload-port COM5")
    elif expected_fw:
        record("固件是最新版", PASS, fw)


# ------------------------------------------------------------------ 入口
def main() -> int:
    ap = argparse.ArgumentParser(description="开发板连不上/网页无数据 的一键体检")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    args = ap.parse_args()

    print("=" * 72)
    print("ESP32-S3-EYE 遥测链路体检    端口 %d    仓库 %s" % (args.port, ROOT))
    print("=" * 72)
    print("\n--- 1. 固件配置（板子知道该连谁吗）---")
    url, fw_token = check_secrets(args.port)
    check_server_url(url, args.port)
    check_token(fw_token, args.port)

    print("\n--- 2. 本机（板子连得进来吗）---")
    ips = local_ipv4s()
    record("本机局域网 IP", PASS if ips else WARN, ", ".join(ips) or "没查到")
    check_listener(args.port)
    check_firewall(args.port)

    print("\n--- 3. 物理连接 ---")
    check_com_ports()

    print("\n--- 4. 数据是否真的进库 ---")
    check_device(args.port, check_expected_fw())

    print("\n" + "=" * 72)
    n_fail = sum(1 for _, s, _ in _results if s == FAIL)
    n_warn = sum(1 for _, s, _ in _results if s == WARN)
    if n_fail == 0 and n_warn == 0:
        print("全部 %d 项通过。网页端应该有实时数据；没有就强制刷新（Ctrl+F5）。" % len(_results))
    elif n_fail == 0:
        print("%d 项通过，%d 项警告。数据应该在流，警告项建议顺手处理。" % (len(_results) - n_warn, n_warn))
    else:
        print("有 %d 项 FAIL —— 按上面 -> 提示逐条修，修完重跑本脚本。" % n_fail)
        print("排查顺序很重要：先修靠前的项，后面的项可能是被它连累的。")
    print("=" * 72)
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())