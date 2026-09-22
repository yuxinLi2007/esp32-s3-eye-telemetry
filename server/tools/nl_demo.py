"""第4周：自然语言助手端到端演示 / 验收脚本。

对**真实 HTTP 服务**发请求，不需要真实板子（假设备扮演板端领取+回执），
也不需要 OPENAI_API_KEY（规则引擎离线可用）。

用法：
  # 终端1：起服务（可指定独立库，别碰你正在用的那份）
  set TELEMETRY_DB=D:\esp32-s3-eye-telemetry\.scratch\wk4.db
  python -m uvicorn app:app --port 8012
  # 终端2：
  python tools/nl_demo.py --url http://127.0.0.1:8012

覆盖本周两组必测场景：
  1. 新旧数据：旧数据 -> "查看上次"保留旧时间戳；"重新采集" -> 新样本。
  2. 异常指令：越界设备 / 含糊指令 / 旧数据查询 / 设备无响应 / 模型不可用。
"""

import argparse
import io
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

MAC = "94:A9:90:1C:6F:D4"
OTHER = "AA:BB:CC:DD:EE:FF"
BOOT = "BOOTDEMO"

# 服务端 .env 里的令牌（若设了 INGEST_TOKEN / CONTROL_TOKEN）。http() 会自动带上，
# 不设时为空 dict——本地零配置实例照样能跑。
AUTH = {}


def load_env_file(path):
    """从 .env 读取两个令牌。脚本自动读取 server/.env，于是 --url 之外零参数可用。"""
    out = {}
    try:
        with io.open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out

PASS = []
FAIL = []


def http(url, method="GET", body=None, headers=None, raw=False):
    data = None
    h = dict(headers or {})
    h.update(AUTH)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        h["Content-Type"] = "application/json"
    elif raw:
        data = b""
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            text = r.read().decode("utf-8")
            return r.status, (text if not text.startswith(("{", "[")) or raw else json.loads(text)), text
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8")
        try:
            return e.code, json.loads(text), text
        except ValueError:
            return e.code, None, text


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + detail) if detail else ""))
    return cond


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8012")
    ap.add_argument("--ingest-token", default=None,
                    help="默认读 server/.env 的 INGEST_TOKEN；没有则空")
    ap.add_argument("--control-token", default=None,
                    help="默认读 server/.env 的 CONTROL_TOKEN；没有则空")
    ap.add_argument("--env-file",
                    default=os.path.join(os.path.dirname(os.path.dirname(
                        os.path.abspath(__file__))), ".env"))
    args = ap.parse_args()
    base = args.url.rstrip("/")

    env = load_env_file(args.env_file)
    ingest = args.ingest_token if args.ingest_token is not None else env.get("INGEST_TOKEN", "")
    control = args.control_token if args.control_token is not None else env.get("CONTROL_TOKEN", "")
    if ingest:
        AUTH["X-Ingest-Token"] = ingest
    if control:
        AUTH["X-Control-Token"] = control

    print("=" * 74)
    print("第4周 · 自然语言助手端到端验收  ->  " + base)
    print("=" * 74)

    st, j, _ = http(base + "/health")
    if st != 200:
        print("服务端不可达：%s" % base)
        return 2

    # ---------- 准备：注入一批"旧数据"
    old_readings = [{"seq": i, "t_device_ms": 500_000 + i * 50,
                     "ax": 0.01 * i, "ay": 0.0, "az": 1.0, "spl_db": 50.0 + i}
                    for i in range(3)]
    st, j, txt = http(base + "/api/v1/ingest", "POST", {
        "device_mac": MAC, "boot_id": BOOT, "fw_version": "0.3.1",
        "ntp_synced": True, "ntp_sync_age_s": 10,
        "t_device_ntp_ms": int(time.time() * 1000), "dropped_since_last": 0,
        "readings": old_readings,
    })
    if st != 201:
        print("注入旧数据失败：%s %s" % (st, txt))
        return 2
    with urllib.request.urlopen(base + "/api/v1/batches?limit=1") as r:
        old_ms = json.load(r)["batches"][0]["t_server_recv_ms"]
    print("\n旧数据已入库 t_server_recv_ms=%d\n" % old_ms)

    def ask(text, **kw):
        st, j, txt = http(base + "/api/v1/assistant/ask", "POST",
                          {"text": text, "engine": "rules", **kw})
        print("  > %s" % text)
        print("    intent=%s ok=%s error=%s" % (
            j.get("intent"), j.get("ok"), (j.get("error") or {}).get("code")))
        print("    answer=%s" % j.get("answer"))
        return j

    # ================= 1. 新旧数据测试 =================
    print("-" * 74)
    print("① 新旧数据测试")
    print("-" * 74)

    q = ask("帮我查看上次的数据")
    check("查看上次 -> query_history", q["intent"] == "query_history")
    check("查看上次 -> 走只读 /api/v1/readings", q["action"]["tool"] == "readings")
    check("查看上次 -> is_new_sample=False", q["data"]["is_new_sample"] is False)
    check("查看上次 -> 保留旧 t_server_recv_ms",
          q["data"]["latest"]["t_server_recv_ms"] == old_ms,
          "got=%s want=%s" % (q["data"]["latest"]["t_server_recv_ms"], old_ms))
    st, cmdlist, _ = http(base + "/api/v1/commands?limit=5")
    check("查看上次 -> 没有产生任何指令", cmdlist["stats"]["total"] == 0)

    # 扮演设备：在后台线程里领取 capture 指令、入库新样本、回执 done
    work = {"rid": None, "done": False}

    # claim 是 text/plain 请求体，单独实现
    def claim():
        req = urllib.request.Request(
            base + "/api/v1/commands/claim",
            data=("%s|%s|0.4.0" % (MAC, BOOT)).encode(), method="POST",
            headers={"Content-Type": "text/plain", **AUTH})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode().strip()

    def device_sim():
        for _ in range(400):
            claimed = claim()
            if claimed and claimed != "none":
                rid, op, params, _tmo = claimed.split("|")
                work["rid"] = rid
                n = 5
                rows = [{"seq": i, "t_device_ms": 900_000 + i * 50, "ax": 0.5,
                         "ay": 0.1, "az": 1.0, "spl_db": 60.0 + i} for i in range(n)]
                s2, b2, t2 = http(base + "/api/v1/ingest", "POST", {
                    "device_mac": MAC, "boot_id": BOOT, "fw_version": "0.4.0",
                    "request_id": rid, "ntp_synced": True, "ntp_sync_age_s": 3,
                    "t_device_ntp_ms": int(time.time() * 1000), "dropped_since_last": 0,
                    "readings": rows,
                })
                http(base + "/api/v1/commands/%s/result" % rid, "POST", {
                    "state": "done", "boot_id": BOOT, "device_mac": MAC,
                    "progress": 100, "samples_batch_id": b2.get("batch_id"),
                    "n_samples": n})
                work["done"] = True
                return
            time.sleep(0.2)

    t = threading.Thread(target=device_sim, daemon=True)
    t.start()
    c = ask("帮我重新采集一次", wait_ms=15000)
    t.join(timeout=5)
    check("重新采集 -> request_capture", c["intent"] == "request_capture")
    check("重新采集 -> 走写 /api/v1/commands(op=capture)",
          c["action"]["endpoint"] == "/api/v1/commands" and c["action"]["op"] == "capture")
    check("重新采集 -> 拿到新样本 is_new_sample=True",
          (c.get("data") or {}).get("is_new_sample") is True,
          "state=%s err=%s" % (c["action"].get("state"), (c.get("error") or {}).get("code")))
    if c.get("data"):
        check("重新采集 -> 新时间戳晚于旧时间戳",
              c["data"]["latest"]["t_server_recv_ms"] > old_ms,
              "%s > %s" % (c["data"]["latest"]["t_server_recv_ms"], old_ms))
        check("重新采集 -> 样本带本次 request_id",
              c["data"]["latest"]["request_id"] == c["action"]["request_id"])

    # ================= 2. 异常指令测试 =================
    print()
    print("-" * 74)
    print("② 异常指令测试")
    print("-" * 74)

    f = ask("把设备 %s 重新采集一次" % OTHER)
    check("越界设备 -> reject/forbidden_device",
          f["intent"] == "reject" and f["error"]["code"] == "forbidden_device")
    st, cmdlist, _ = http(base + "/api/v1/commands?limit=5")
    before = cmdlist["stats"]["total"]
    ask("把设备 %s 查一下" % OTHER)
    st, cmdlist2, _ = http(base + "/api/v1/commands?limit=5")
    check("越界设备 -> 不产生新指令", cmdlist2["stats"]["total"] == before)

    a = ask("帮我弄一下")
    check("含糊指令 -> clarify/ambiguous_request",
          a["intent"] == "clarify" and a["error"]["code"] == "ambiguous_request")

    b = ask("采集")
    check("裸动作词 -> 强制澄清，不默认采集", b["intent"] == "clarify")

    u = ask("帮我停止采集")
    check("白名单外写操作 -> reject",
          u["intent"] == "reject" and u["error"]["code"] == "unsupported_action")

    # 设备无响应：下发后 wait_ms=0（窗口立刻用完），指令仍 pending
    nr = ask("重新采集一次", wait_ms=0)
    check("设备无响应 -> 结构化 device_no_response，不崩",
          nr["ok"] is False and nr["error"]["code"] in
          ("device_no_response", "device_unreachable"), nr["error"]["code"])

    # 模型不可用降级
    st, j, _ = http(base + "/api/v1/assistant/ask", "POST",
                    {"text": "查看上次数据", "engine": "auto"})
    check("无 OPENAI_API_KEY -> 自动降级规则引擎",
          j["trace"]["engine_used"] == "rules", str(j["trace"].get("llm_error")))

    print()
    print("=" * 74)
    print("结果：%d 通过 / %d 失败" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：")
        for x in FAIL:
            print("  - " + x)
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
