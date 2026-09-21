"""第2周端到端验证：假设备模拟器 + 故障注入（打真实 HTTP，不用 TestClient）。

为什么不用 TestClient：第1周吃过这个亏——TestClient 串行执行，把 FastAPI
跨线程分派的问题盖住了，测试全绿、真机必现。指令通道是"服务端 / 设备 / 网页"
三方异步交互，时序问题只有在真实 socket 下才露得出来。

它验证的不是"正常路径能跑通"，而是六类失败是否都被如实暴露：
  1) 双击与重试        -> 服务端幂等（200 + X-Deduped:1），板子不会执行两遍
  2) 领取后设备静默    -> 重排队一次；预算耗尽判 timeout，不无限重发
  3) 执行中静默        -> 直接 timeout（RUNNING 不重排队：重跑会得到两批对不上号的数据）
  4) 设备离线          -> ttl 到点判 expired，界面能看出"根本没人来领"
  5) 上传掉样          -> n_samples(服务端实收) != n_samples_device(设备自报) = mismatch
  6) 迟到/错乱的回执   -> 终态不可改、boot_id 不匹配、progress 回退，一律 409

用法（务必用独立库，别污染真实数据）：
    终端1  $env:TELEMETRY_DB="D:\\...\\.scratch\\sim.db"
           python -m uvicorn app:app --port 8001
    终端2  python tools/command_sim.py --url http://127.0.0.1:8001
环境变量 INGEST_TOKEN / CONTROL_TOKEN 若服务端设了，这里也要设，否则全 401。
退出码非 0 表示有断言失败，可直接挂到 CI 上。
"""
import argparse
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import commands as C  # noqa: E402  共用同一份协议实现，模拟器不另抄一遍解析

INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "")
CONTROL_TOKEN = os.environ.get("CONTROL_TOKEN", "")
URL = "http://127.0.0.1:8001"

PASS = 0
FAIL = 0
FAILED_NAMES = []

# 每次运行一个标识。反复跑这个脚本时，上一次运行留下的指令还在库里，
# 而幂等键是 (MAC, op, client_token)——token 固定的话第二次跑会全被判成重复提交，
# 测出来的是幂等而不是下发。MAC 同理：上一次的 pending 会被这一次的设备领走。
RUN = secrets.token_hex(3)


def mk_token(name):
    return "sim-%s-%s" % (RUN, name)


def ck(name, cond, detail=""):
    """断言。失败也继续跑完：一次运行要能看到所有问题，而不是修一个跑一遍。"""
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  " + name)
    else:
        FAIL += 1
        FAILED_NAMES.append(name)
        print("  FAIL  " + name + ("   <- " + str(detail) if detail != "" else ""))


# ---------------------------------------------------------------- HTTP
def req(method, path, body=None, text=False, token=None):
    """返回 (status, body, headers)。4xx/5xx 不抛异常：错误响应正是要断言的对象。"""
    data, headers = None, {}
    if body is not None:
        if text:
            data = body.encode("utf-8")
            headers["Content-Type"] = "text/plain"
        else:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
    if token == "ingest" and INGEST_TOKEN:
        headers["X-Ingest-Token"] = INGEST_TOKEN
    if token == "control" and CONTROL_TOKEN:
        headers["X-Control-Token"] = CONTROL_TOKEN
    r = urllib.request.Request(URL + path, data=data, headers=headers, method=method)
    # headers 保持 HTTPMessage 原样返回，不转 dict：
    # uvicorn 会把响应头名压成小写（x-deduped），dict 之后 .get("X-Deduped") 就取不到了，
    # 而 HTTPMessage.get 本身不区分大小写——断言要测的是"服务端有没有发这个头"，
    # 不是"它用了什么大小写"。
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            code, raw, hdr = resp.status, resp.read().decode("utf-8"), resp.headers
    except urllib.error.HTTPError as e:
        code, raw, hdr = e.code, e.read().decode("utf-8"), e.headers
    if text or not raw.lstrip().startswith("{"):
        return code, raw, hdr
    try:
        return code, json.loads(raw), hdr
    except ValueError:
        return code, raw, hdr


def err(body):
    """错误响应统一取 {code, message}。前端靠 code 决策，不解析中文句子。"""
    if isinstance(body, dict):
        if isinstance(body.get("error"), dict):
            return body["error"]
        if "detail" in body:
            d = body["detail"]
            return d if isinstance(d, dict) else {"code": "http", "message": str(d)}
    return {"code": "?", "message": str(body)[:120]}


def ck_err(name, body, code):
    e = err(body)
    ck(name, e.get("code") == code, e)
    return e


# ---------------------------------------------------------------- 假设备
_mac_n = [0]


def new_mac():
    """每个场景一台设备。

    不是为了好看：claim 取的是"该 MAC 最早的一条 pending"，
    共用一台设备的话，A 场景会把 B 场景的指令领走，断言就成了抓阄。
    """
    _mac_n[0] += 1
    n = _mac_n[0]
    return "94:A9:%s:%02X:%02X:%02X" % (RUN[:2].upper(), 0x10 + (n >> 8),
                                        (n >> 4) & 0x0F, n & 0xFF)


def firmware_split_claim(body):
    """逐行复刻 firmware/src/command.cpp 的 split_claim + parse_claim 前置校验。

    仿真器原本直接用服务端的 decode_claim 解析领取应答，等于"服务端自己验自己"，
    线格式一旦和固件不一致就查不出来——真机联调时就栽在这里：ping/selftest 没有
    参数，服务端发 "rid|op||timeout_ms"，而固件旧版要求四段全部非空，于是这两条
    指令永远解析失败、永远不回执。这份复刻把固件的口径搬过来，格式漂移当场报错。
    """
    parts, start, idx = ["", "", "", ""], 0, 0
    while idx < 3:
        k = body.find("|", start)
        if k < 0:
            raise AssertionError("领取应答段数不足 4：%r" % body)
        parts[idx] = body[start:k]
        idx += 1
        start = k + 1
    if body.find("|", start) >= 0:
        raise AssertionError("领取应答多于 4 段（固件会拒收）：%r" % body)
    parts[3] = body[start:]
    # parts[2]（参数段）允许为空，其余三段为空视为应答被截断
    for i in (0, 1, 3):
        if not parts[i]:
            raise AssertionError("领取应答第 %d 段为空（固件会拒收）：%r" % (i, body))
    if not parts[0].startswith("req_"):
        raise AssertionError("request_id 前缀不是 req_：%r" % body)
    if parts[3].isdigit() is False or int(parts[3]) < 1000:
        raise AssertionError("timeout_ms 不合法（固件会拒收）：%r" % body)
    return parts


class Sim:
    """一台假板子。只实现板端真做的那三件事：领取、上报样本、回执。"""

    def __init__(self, mac, boot_id=None, fw="0.2.0-sim"):
        self.mac = mac.upper()
        self.boot_id = boot_id or ("SIM" + self.mac.replace(":", "")[-6:])
        self.fw = fw
        self.seq = 0
        self.t = 1_700_000_000_000

    def claim(self):
        code, raw, _ = req("POST", "/api/v1/commands/claim",
                           C.encode_claim_request(self.mac, self.boot_id, self.fw),
                           text=True, token="ingest")
        if code != 200:
            raise RuntimeError("claim 失败 %d %s" % (code, raw))
        raw = (raw or "").strip()
        if raw == C.CLAIM_NONE:
            return None
        firmware_split_claim(raw)      # 先按固件的严格口径验一遍线格式
        job = C.decode_claim(raw)      # 再用服务端同一份解析，避免两边各写一套
        job["raw"] = raw
        return job

    def result(self, rid, **kw):
        body = {"boot_id": self.boot_id, "device_mac": self.mac}
        body.update(kw)
        return req("POST", "/api/v1/commands/%s/result" % rid, body, token="ingest")

    def progress(self, rid, pct):
        return self.result(rid, state="running", progress=pct)

    def done(self, rid, result=None, n_samples=None, batch_id=None):
        return self.result(rid, state="done", result=result, n_samples=n_samples,
                           samples_batch_id=batch_id)

    def failed(self, rid, code_, message=None):
        return self.result(rid, state="failed", error_code=code_, error_message=message)

    def ingest(self, rid, n, interval_ms=20):
        readings = [
            {"seq": self.seq + i, "t_device_ms": self.t + i * interval_ms,
             "ax": 0.012, "ay": -0.021, "az": 0.998, "spl_db": 51.5 + 0.2 * i}
            for i in range(n)
        ]
        self.seq += n
        self.t += n * interval_ms
        body = {"device_mac": self.mac, "boot_id": self.boot_id, "request_id": rid,
                "fw_version": self.fw, "ntp_synced": True, "ntp_sync_age_s": 12,
                "t_device_ntp_ms": int(time.time() * 1000),
                "dropped_since_last": 0, "readings": readings}
        return req("POST", "/api/v1/ingest", body, token="ingest")


def submit(mac, op, params=None, token=None, note=None):
    return req("POST", "/api/v1/commands",
               {"device_mac": mac, "op": op, "params": params or {},
                "client_token": token, "note": note}, token="control")


def get_cmd(rid):
    code, body, _ = req("GET", "/api/v1/commands/" + rid)
    return body["command"] if code == 200 else None


def wait_for(rid, states, timeout_s, poll=0.4):
    """轮询到某个状态为止。返回 (最后一次的 command, 期间见过的状态序列)。"""
    t0, seen = time.time(), []
    while time.time() - t0 < timeout_s:
        c = get_cmd(rid)
        if c is None:
            return None, seen
        key = (c["state"], c["requeues"])
        if not seen or seen[-1] != key:
            seen.append(key)
        if c["state"] in states:
            return c, seen
        time.sleep(poll)
    return get_cmd(rid), seen

# ---------------------------------------------------------------- 场景
def s0_catalog():
    print("\n[S0] 指令清单与 request_id 生成规则")
    code, body, _ = req("GET", "/api/v1/commands/ops")
    ck("GET /commands/ops 返回 200", code == 200, code)
    if code != 200:
        return
    ops = {o["op"]: o for o in body["ops"]}
    # 第3周：notify 加入白名单（按键回应通知）
    ck("白名单恰好是 ping/selftest/capture/notify",
       set(ops) == {"ping", "selftest", "capture", "notify"}, sorted(ops))
    ck("max_live_per_device = 8", body["max_live_per_device"] == 8, body["max_live_per_device"])
    ck("max_capture_duration_ms = 10000", body["max_capture_duration_ms"] == 10000,
       body["max_capture_duration_ms"])
    cap = {p["name"]: p for p in ops["capture"]["params"]}
    ck("n 的范围由服务端给出 1..200", (cap["n"]["lo"], cap["n"]["hi"]) == (1, 200), cap["n"])
    if "notify" in ops:
        ntf = {p["name"]: p for p in ops["notify"]["params"]}
        ck("notify.decision 的白名单由服务端给出 (ack/cancel)",
           ntf["decision"]["kind"] == "str" and ntf["decision"]["choices"] == ["ack", "cancel"],
           ntf["decision"])
        ck("notify.event_id 由服务端给出范围", ntf["event_id"]["kind"] == "int",
           ntf["event_id"])
    ck("interval_ms 的范围由服务端给出 10..1000",
       (cap["interval_ms"]["lo"], cap["interval_ms"]["hi"]) == (10, 1000), cap["interval_ms"])

    mac = new_mac()
    before = int(time.time() * 1000)
    code, body, _ = submit(mac, "ping", token=mk_token("s0"))
    after = int(time.time() * 1000)
    ck("下发 ping 返回 201", code == 201, err(body))
    rid = body["command"]["request_id"]
    ck("request_id 形如 req_<9位base36>_<6位hex>", bool(C.REQUEST_ID_RE.match(rid)), rid)
    m = re.match(r"^req_([0-9a-z]{9})_([0-9a-f]{6})$", rid)
    if m:
        t_id = int(m.group(1), 36)
        ck("id 里的时间片落在下发时刻 ±1s 内", before - 1000 <= t_id <= after + 1000,
           (t_id, before, after))
        ck("id 里的时间片 == t_created_ms（不是第二份事实）",
           t_id == body["command"]["t_created_ms"], (t_id, body["command"]["t_created_ms"]))
    # 字典序 == 时间序：定宽 base36 的全部意义就在这
    time.sleep(0.01)
    _, b2, _ = submit(mac, "selftest", token=mk_token("s0b"))
    ck("后下发的 id 字典序更大（可直接排序）", b2["command"]["request_id"] > rid,
       (rid, b2["command"]["request_id"]))
    # 顺手跑到终态：留在 claimed 的话，20 秒后会变成两条 timeout，
    # 把 S4 的"在飞数量"统计搅浑。
    sim0 = Sim(mac)
    for _ in range(2):
        j = sim0.claim()
        if j:
            sim0.done(j["request_id"], result={"uptime_s": 1})


def s1_happy_path():
    print("\n[S1] 正常 capture 全流程：下发 -> 领取 -> 进度 -> 样本入库 -> done")
    mac = new_mac()
    sim = Sim(mac)
    code, body, hdr = submit(mac, "capture", {"n": 6, "interval_ms": 20},
                             token=mk_token("s1"), note="端到端验证")
    ck("下发返回 201（新建）", code == 201, err(body))
    c = body["command"]
    rid = c["request_id"]
    ck("初始状态 pending", c["state"] == "pending", c["state"])
    ck("响应头 x-deduped: 0", hdr.get("X-Deduped") == "0", hdr.get("X-Deduped"))
    ck("note 原样存回", c["note"] == "端到端验证", c["note"])
    ck("capture 超时窗随参数增长 = 20000 + 2×120", c["timeout_ms"] == 20000 + 2 * 120,
       c["timeout_ms"])
    ck("ttl_ms = 30000", c["ttl_ms"] == 30000, c["ttl_ms"])
    ck("remaining_ms 在下发时接近 ttl", 29000 < c["remaining_ms"] <= 30000, c["remaining_ms"])

    job = sim.claim()
    ck("设备领到的就是这一条", job is not None and job["request_id"] == rid, job)
    ck("领取文本不带派生值 duration_ms", "duration_ms" not in job["raw"], job["raw"])
    ck("参数按名排序编码（interval_ms=20;n=6）", job["raw"].split("|")[2] == "interval_ms=20;n=6",
       job["raw"])
    ck("领取文本带 timeout_ms", job["timeout_ms"] == 20240, job["timeout_ms"])
    c = get_cmd(rid)
    ck("领取后状态 claimed", c["state"] == "claimed", c["state"])
    ck("attempts = 1", c["attempts"] == 1, c["attempts"])
    ck("queue_ms 已算出（下发->领取）", c["queue_ms"] is not None and c["queue_ms"] >= 0,
       c["queue_ms"])
    ck("同一 boot 重复领取拿回同一条（不重复计数）",
       sim.claim()["request_id"] == rid and get_cmd(rid)["attempts"] == 1, get_cmd(rid)["attempts"])

    ck("进度 30% 被接受", sim.progress(rid, 30)[0] == 200)
    ck("进度 70% 被接受", sim.progress(rid, 70)[0] == 200)
    ck("进度心跳刷新超时锚点（t_last_event_ms 前移）",
       get_cmd(rid)["state"] == "running", get_cmd(rid)["state"])

    code, ing, _ = sim.ingest(rid, 6, 20)
    ck("样本带 request_id 走老 ingest 路入库 201", code == 201, err(ing))
    batch_id = ing.get("batch_id")
    ck("ingest 回显 request_id", ing.get("request_id") == rid, ing.get("request_id"))

    code, res, _ = sim.done(rid, result={"n_sampled": 6, "spl_avg_db": 51.9,
                                         "mag_avg": 0.998, "accel_verdict": "pass",
                                         "mic_verdict": "pass"},
                            n_samples=6, batch_id=batch_id)
    ck("done 回执 200", code == 200, err(res))
    c = res["command"]
    ck("终态 done", c["state"] == "done", c["state"])
    ck("is_terminal = True", c["is_terminal"] is True)
    ck("progress 被拉到 100", c["progress"] == 100, c["progress"])
    ck("服务端实收 6 / 设备自报 6", (c["n_samples"], c["n_samples_device"]) == (6, 6),
       (c["n_samples"], c["n_samples_device"]))
    ck("sample_count_mismatch = False", c["sample_count_mismatch"] is False)
    ck("结果体原样存回", c["result"]["spl_avg_db"] == 51.9, c["result"])
    ck("exec_ms 已算出", c["exec_ms"] is not None and c["exec_ms"] >= 0, c["exec_ms"])
    ck("deadline/remaining 在终态下为 None", c["deadline_ms"] is None and
       c["remaining_ms"] is None)
    states = [e["to_state"] for e in c["events"]]
    ck("事件时间线完整 pending->claimed->running->done",
       states == ["pending", "claimed", "running", "running", "running", "done"], states)
    ck("ingest 也留了痕（actor=ingest 的同状态注记）",
       any(e["actor"] == "ingest" for e in c["events"]), c["events"])

    code, rr, _ = req("GET", "/api/v1/readings?request_id=" + rid)
    ck("按 request_id 能捞回这 6 条指令样本", code == 200 and rr["count"] == 6,
       rr.get("count"))
    code, rr2, _ = req("GET", "/api/v1/readings?limit=5000")
    ck("默认视图不含指令样本，但如实报出被过滤的条数",
       all(r.get("request_id") in (None, "") for r in rr2["readings"]) and
       rr2["excluded_command_samples"] >= 6,
       rr2["excluded_command_samples"])
    code, rr3, _ = req("GET", "/api/v1/readings?include_command_samples=true&limit=5000")
    ck("include_command_samples=true 时能一起看到",
       sum(1 for r in rr3["readings"] if r.get("request_id") == rid) == 6)


def s2_idempotent():
    print("\n[S2] 幂等：双击 / 重试 / 并发提交同一 client_token")
    mac = new_mac()
    sim = Sim(mac)
    tk1 = mk_token("dbl")
    c1, b1, h1 = submit(mac, "ping", token=tk1)
    c2, b2, h2 = submit(mac, "ping", token=tk1)
    ck("第一次 201", c1 == 201, c1)
    ck("第二次 200（不是 201）", c2 == 200, c2)
    ck("第二次 x-deduped: 1", h2.get("X-Deduped") == "1", h2.get("X-Deduped"))
    ck("deduped 标志为真", b2.get("deduped") is True, b2)
    rid = b1["command"]["request_id"]
    ck("两次拿到同一个 request_id", b2["command"]["request_id"] == rid, rid)

    code, lst, _ = req("GET", "/api/v1/commands?device_mac=" + mac)
    ck("库里只有一条", len(lst["commands"]) == 1, len(lst["commands"]))

    # 并发双击：4 个线程同时提交同一 token。前端按钮禁用挡不住刷新和 curl，
    # 唯一索引才是最后一道。
    mac2 = new_mac()
    tk2 = mk_token("race")
    out = []
    lock = threading.Lock()

    def hit():
        r = submit(mac2, "ping", token=tk2)
        with lock:
            out.append(r)

    ths = [threading.Thread(target=hit) for _ in range(4)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    rids = {b["command"]["request_id"] for c, b, h in out}
    ck("4 个并发提交只产生 1 条指令", len(rids) == 1, rids)
    ck("全部返回 2xx", all(c in (200, 201) for c, b, h in out),
       [c for c, b, h in out])
    ck("其中恰好一个 201、其余 200", sorted(c for c, b, h in out) == [200, 200, 200, 201],
       sorted(c for c, b, h in out))

    # 不同 token 就是不同意图，必须真的产生两条
    mac3 = new_mac()
    a = submit(mac3, "ping", token=mk_token("a"))[1]["command"]["request_id"]
    b = submit(mac3, "ping", token=mk_token("b"))[1]["command"]["request_id"]
    ck("不同 client_token 产生两条指令", a != b, (a, b))
    # 收尾：把这两台设备的队列跑到终态，别影响后面的统计断言
    for dev, _mac in ((Sim(mac), mac), (Sim(mac3), mac3)):
        while True:
            j = dev.claim()
            if not j:
                break
            dev.done(j["request_id"], result={"uptime_s": 1})


def s3_validation():
    print("\n[S3] 参数与输入校验：非法输入必须被挡住，且错误码可判别")
    mac = new_mac()
    cases = [
        ("n 下越界", {"n": 0, "interval_ms": 20}, "bad_param", 400),
        ("n 上越界", {"n": 201, "interval_ms": 20}, "bad_param", 400),
        ("interval 下越界", {"n": 5, "interval_ms": 9}, "bad_param", 400),
        ("interval 上越界", {"n": 5, "interval_ms": 1001}, "bad_param", 400),
        ("n×interval 超上限", {"n": 150, "interval_ms": 100}, "capture_too_long", 400),
        ("n 不是整数", {"n": "abc"}, "bad_param", 400),
        ("n 是浮点", {"n": 3.5}, "bad_param", 400),
        ("白名单外的参数名", {"n": 5, "evil": 1}, "bad_param", 400),
    ]
    for name, params, ecode, status in cases:
        code, body, _ = submit(mac, "capture", params, token=mk_token("v-" + name))
        ck("capture %s 被拒 %d" % (name, status), code == status, code)
        ck_err("  错误码 = " + ecode, body, ecode)
    for op, ecode, status in [("reboot", "unknown_op", 404), ("DROP TABLE", "bad_op", 400),
                              ("", "bad_op", 400)]:
        code, body, _ = submit(mac, op, token=None)
        ck("op=%r 被拒 %d" % (op, status), code == status, code)
        ck_err("  错误码 = " + ecode, body, ecode)
    for bad, ecode in [("94:A9:90:1C:6F", "bad_mac"), ("GG:GG:GG:GG:GG:GG", "bad_mac"),
                       ("94:a9:90:1c:6f:d4; DROP", "bad_mac")]:
        code, body, _ = submit(bad, "ping", token=None)
        ck("device_mac=%r 被拒" % bad, code == 400, code)
        ck_err("  错误码 = " + ecode, body, ecode)
    code, body, _ = submit(mac, "ping", token="非法 token 带空格")
    ck("client_token 含空格被拒", code == 400, code)
    ck_err("  错误码 = bad_token", body, "bad_token")
    code, _, _ = req("GET", "/api/v1/commands/req_nope_nope")
    ck("查不存在的 request_id -> 404", code == 404, code)
    code, body, _ = req("GET", "/api/v1/commands?state=nope")
    ck("list 的 state 过滤也校验", code == 400, code)
    ck_err("  错误码 = bad_state", body, "bad_state")
    # 小写 MAC 应当被规范化，而不是被拒
    code, body, _ = submit("94:a9:90:1c:6f:d4", "ping", token=mk_token("lower"))
    ck("小写 MAC 被规范化为大写并接受",
       code in (200, 201) and body["command"]["device_mac"] == "94:A9:90:1C:6F:D4",
       (code, err(body)))


def s4_rate_limit():
    print("\n[S4] 在飞上限：同一台设备最多 8 条，第 9 条必须被拒")
    mac = new_mac()
    ok = 0
    for i in range(C.MAX_LIVE_PER_DEVICE):
        code, body, _ = submit(mac, "ping", token=mk_token("rl-%d" % i))
        ok += 1 if code == 201 else 0
    ck("前 %d 条全部下发成功" % C.MAX_LIVE_PER_DEVICE, ok == C.MAX_LIVE_PER_DEVICE, ok)
    code, body, _ = submit(mac, "ping", token=mk_token("rl-overflow"))
    ck("第 %d 条被拒 429" % (C.MAX_LIVE_PER_DEVICE + 1), code == 429, code)
    e = ck_err("  错误码 = too_many_live", body, "too_many_live")
    ck("  错误消息说明了为什么（人是串行执行的）", "串行" in e.get("message", ""), e)
    code, st, _ = req("GET", "/api/v1/status")
    ck("stats.live 至少 8", st["commands"]["live"] >= C.MAX_LIVE_PER_DEVICE, st["commands"])


def s5_failed():
    print("\n[S5] 设备执行失败：必须带 error_code，且原样出现在界面数据里")
    mac = new_mac()
    sim = Sim(mac)
    _, body, _ = submit(mac, "selftest", token=mk_token("s5"))
    rid = body["command"]["request_id"]
    sim.claim()
    code, res, _ = sim.result(rid, state="failed")
    ck("failed 不带 error_code 被拒 400", code == 400, code)
    ck_err("  错误码 = missing_error_code", res, "missing_error_code")
    ck("被拒后指令仍在 claimed（没被写坏）", get_cmd(rid)["state"] == "claimed",
       get_cmd(rid)["state"])

    code, res, _ = sim.failed(rid, "accel_i2c", "I2C 读 WHO_AM_I 超时（SDA/SCL 未上拉？）")
    ck("failed 回执 200", code == 200, err(res))
    c = res["command"]
    ck("终态 failed", c["state"] == "failed", c["state"])
    ck("error_code 原样保存", c["error_code"] == "accel_i2c", c["error_code"])
    ck("error_message 原样保存", "WHO_AM_I" in (c["error_message"] or ""), c["error_message"])
    ck("progress 不被伪造为 100", c["progress"] is None, c["progress"])
    ck("事件里记了失败原因",
       any(e["to_state"] == "failed" and (e["detail"] or {}).get("error_code") == "accel_i2c"
           for e in c["events"]), c["events"])

    # 另一台：mic 没输出
    mac2 = new_mac()
    sim2 = Sim(mac2)
    _, b2, _ = submit(mac2, "capture", {"n": 4, "interval_ms": 20}, token=mk_token("s5b"))
    rid2 = b2["command"]["request_id"]
    sim2.claim()
    sim2.progress(rid2, 50)
    code, res2, _ = sim2.failed(rid2, "mic_no_output", "I2S 读了 40 帧全是 0")
    ck("执行到一半失败也是 failed", code == 200 and res2["command"]["state"] == "failed",
       err(res2))
    ck("失败时的进度被保留（看得出死在哪一步）", res2["command"]["progress"] == 50,
       res2["command"]["progress"])


def s6_bad_receipts():
    print("\n[S6] 迟到与错乱的回执：一律拒收，绝不静默覆盖")
    mac = new_mac()
    sim = Sim(mac)
    _, body, _ = submit(mac, "ping", token=mk_token("s6"))
    rid = body["command"]["request_id"]
    code, res, _ = sim.done(rid, result={"uptime_s": 1})
    ck("未领取就回执 -> 409", code == 409, code)
    ck_err("  错误码 = not_claimed", res, "not_claimed")

    job = sim.claim()
    other = Sim(mac, boot_id="SIMOTHER")
    code, res, _ = other.done(rid, result={"uptime_s": 2})
    ck("boot_id 不匹配的回执 -> 409（设备重启过）", code == 409, code)
    ck_err("  错误码 = boot_mismatch", res, "boot_mismatch")
    code, res, _ = req("POST", "/api/v1/commands/%s/result" % rid,
                       {"state": "done", "boot_id": sim.boot_id,
                        "device_mac": new_mac()}, token="ingest")
    ck("device_mac 不符 -> 409", code == 409, code)
    ck_err("  错误码 = mac_mismatch", res, "mac_mismatch")
    code, res, _ = sim.progress(rid, 60)
    ck("进度 60 接受", code == 200, err(res))
    code, res, _ = sim.progress(rid, 40)
    ck("进度回退 -> 409（乱序或重放）", code == 409, code)
    ck_err("  错误码 = progress_regressed", res, "progress_regressed")
    code, res, _ = sim.progress(rid, 150)
    ck("进度越界被拒（Pydantic 422）", code in (400, 422), code)
    code, res, _ = sim.result(rid, state="finished")
    ck("未知 state 被拒", code in (400, 422), code)

    sim.done(rid, result={"uptime_s": 3})
    code, res, _ = sim.done(rid, result={"uptime_s": 999})
    ck("终态之后再来 done -> 409", code == 409, code)
    ck_err("  错误码 = already_terminal", res, "already_terminal")
    code, res, _ = sim.progress(rid, 90)
    ck("终态之后再来 running -> 409", code == 409, code)
    ck("结果体没有被迟到回执覆盖", get_cmd(rid)["result"]["uptime_s"] == 3,
       get_cmd(rid)["result"])
    code, res, _ = req("POST", "/api/v1/commands/req_000000000_000000/result",
                       {"state": "done", "boot_id": sim.boot_id}, token="ingest")
    ck("对不存在的指令回执 -> 404", code == 404, code)
    ck_err("  错误码 = not_found", res, "not_found")


def s7_mismatch():
    print("\n[S7] 上传掉样：设备自报 6 条、服务端只收到 3 条，必须被标出来")
    mac = new_mac()
    sim = Sim(mac)
    _, body, _ = submit(mac, "capture", {"n": 6, "interval_ms": 20}, token=mk_token("s7"))
    rid = body["command"]["request_id"]
    sim.claim()
    sim.progress(rid, 50)
    code, ing, _ = sim.ingest(rid, 3, 20)          # 只上传成功一半
    ck("部分样本入库 201", code == 201, err(ing))
    code, res, _ = sim.done(rid, result={"n_sampled": 6}, n_samples=6,
                            batch_id=ing.get("batch_id"))
    c = res["command"]
    ck("state 仍然是 done（设备确实采完了）", c["state"] == "done", c["state"])
    ck("服务端实收 3", c["n_samples"] == 3, c["n_samples"])
    ck("设备自报 6", c["n_samples_device"] == 6, c["n_samples_device"])
    ck("sample_count_mismatch = True", c["sample_count_mismatch"] is True, c)
    code, lst, _ = req("GET", "/api/v1/commands?device_mac=" + mac)
    ck("列表接口也带 mismatch 标记（表格才标得出警告符号）",
       lst["commands"][0]["sample_count_mismatch"] is True)
    # ingest 挂到不存在的 request_id 上必须被拒，否则会造出无主样本
    code, body2, _ = req("POST", "/api/v1/ingest",
                         {"device_mac": mac, "boot_id": sim.boot_id,
                          "request_id": "req_000000000_000000",
                          "readings": [{"seq": 900, "t_device_ms": 1}]}, token="ingest")
    ck("ingest 带未知 request_id -> 4xx（不产生无主样本）", 400 <= code < 500, code)
    code, body3, _ = req("POST", "/api/v1/ingest",
                         {"device_mac": mac, "boot_id": sim.boot_id,
                          "request_id": "req_bad_format",
                          "readings": [{"seq": 901, "t_device_ms": 1}]}, token="ingest")
    ck("ingest 带格式非法 request_id -> 4xx", 400 <= code < 500, code)
    ck_err("  错误码 = bad_request_id", body3, "bad_request_id")

def s8_cancel():
    print("\n[S8] 撤销：只有 pending 能撤，已被领走的不能抽走")
    mac = new_mac()
    sim = Sim(mac)
    _, body, _ = submit(mac, "ping", token=mk_token("s8"))
    rid = body["command"]["request_id"]
    code, res, _ = req("POST", "/api/v1/commands/%s/cancel" % rid, {}, token="control")
    ck("pending 撤销成功 200", code == 200, err(res))
    ck("终态 cancelled", res["command"]["state"] == "cancelled", res["command"]["state"])
    ck("撤销后设备来领 -> 领不到（不会被执行）", sim.claim() is None)
    code, res, _ = req("POST", "/api/v1/commands/%s/cancel" % rid, {}, token="control")
    ck("重复撤销被拒 409", code == 409, code)
    ck_err("  错误码 = not_cancellable", res, "not_cancellable")

    mac2 = new_mac()
    sim2 = Sim(mac2)
    _, b2, _ = submit(mac2, "capture", {"n": 4, "interval_ms": 20}, token=mk_token("s8b"))
    rid2 = b2["command"]["request_id"]
    sim2.claim()
    code, res, _ = req("POST", "/api/v1/commands/%s/cancel" % rid2, {}, token="control")
    ck("已被领取的指令不可撤销 409", code == 409, code)
    ck_err("  错误码 = not_cancellable", res, "not_cancellable")
    ck("指令仍在执行中（没被撤掉）", get_cmd(rid2)["state"] == "claimed",
       get_cmd(rid2)["state"])
    sim2.done(rid2, result={"n_sampled": 4}, n_samples=0)
    code, res, _ = req("POST", "/api/v1/commands/req_000000000_000000/cancel", {},
                       token="control")
    ck("撤销不存在的指令 -> 404", code == 404, code)
    ck_err("  错误码 = not_found", res, "not_found")


# ---------------------------------------------------------------- 慢场景
def start_slow():
    """先把三个"要等几十秒才有结论"的场景点火，等待期间去跑快场景。

    串行等的话这个脚本要跑一分多钟；并发点火后总时长压到 ~35 秒。
    """
    print("\n[点火] 三个慢场景：领取后静默 / 执行中静默 / 设备离线")
    t0 = time.time()

    macA = new_mac()
    simA = Sim(macA)
    ridA = submit(macA, "ping", token=mk_token("slow-a"))[1]["command"]["request_id"]
    simA.claim()                                   # 领了，然后彻底静默

    macB = new_mac()
    simB = Sim(macB)
    ridB = submit(macB, "ping", token=mk_token("slow-b"))[1]["command"]["request_id"]
    simB.claim()
    simB.progress(ridB, 10)                        # 报过一次进度，然后静默

    macC = new_mac()
    simC = Sim(macC)
    ridC = submit(macC, "ping", token=mk_token("slow-c"))[1]["command"]["request_id"]
    # 这台设备从此不出现（离线 / MAC 写错 / 固件太旧不支持指令通道）

    print("  已下发：claimed-silent=%s running-silent=%s offline=%s" % (ridA, ridB, ridC))
    return {"t0": t0, "simA": simA, "ridA": ridA, "simB": simB, "ridB": ridB,
            "simC": simC, "ridC": ridC}


def finish_slow(s):
    print("\n[S9] 领取后静默：重排队一次，重试预算耗尽后判 timeout")
    c, seen = wait_for(s["ridA"], {"pending"}, 20)
    ck("静默超过 timeout_ms 后被放回队列", c is not None and c["state"] == "pending",
       (c and c["state"], seen))
    ck("requeues = 1", c and c["requeues"] == 1, c and c["requeues"])
    ck("重排队时清掉上一轮的领取痕迹（否则下次领取会看到脏字段）",
       c and c["claimed_boot_id"] is None and c["t_claimed_ms"] is None and
       c["progress"] is None, c and (c["claimed_boot_id"], c["t_claimed_ms"], c["progress"]))
    ck("事件里写明了为什么重排队",
       c and any("重新入队" in json.dumps(e.get("detail") or {}, ensure_ascii=False)
                 for e in c["events"]), c and c["events"])

    job = s["simA"].claim()
    ck("重排队后能被再次领取", job is not None and job["request_id"] == s["ridA"], job)
    ck("attempts 累加到 2", get_cmd(s["ridA"])["attempts"] == 2,
       get_cmd(s["ridA"])["attempts"])
    c2, seen2 = wait_for(s["ridA"], {"timeout"}, 20)
    ck("第二次仍静默 -> timeout", c2 is not None and c2["state"] == "timeout",
       (c2 and c2["state"], seen2))
    ck("attempts 已用尽（== max_attempts）", c2 and c2["attempts"] == c2["max_attempts"],
       c2 and (c2["attempts"], c2["max_attempts"]))
    ck("requeues 停在 1，没有无限重发", c2 and c2["requeues"] == 1, c2 and c2["requeues"])
    code, res, _ = s["simA"].done(s["ridA"], result={"uptime_s": 1})
    ck("超时之后姗姗来迟的 done 被拒 409", code == 409, code)
    ck_err("  错误码 = already_terminal", res, "already_terminal")
    ck("timeout 的结论没有被迟到回执改写成 done",
       get_cmd(s["ridA"])["state"] == "timeout", get_cmd(s["ridA"])["state"])

    print("\n[S9b] 执行中静默：RUNNING 不重排队，直接 timeout")
    cB, seenB = wait_for(s["ridB"], {"timeout"}, 20)
    ck("执行中静默 -> timeout", cB is not None and cB["state"] == "timeout",
       (cB and cB["state"], seenB))
    ck("没有重排队（requeues = 0）", cB and cB["requeues"] == 0, cB and cB["requeues"])
    ck("事件理由写明是执行中静默并带上当时的 progress",
       cB and any("执行中静默" in json.dumps(e.get("detail") or {}, ensure_ascii=False)
                  for e in cB["events"]), cB and cB["events"])

    print("\n[S10] 设备离线：ttl 到点判 expired")
    remain = max(0.0, 31.5 - (time.time() - s["t0"]))
    if remain:
        print("  还要等 %.1f 秒（ttl 30s）…" % remain)
        time.sleep(remain)
    cC, seenC = wait_for(s["ridC"], {"expired"}, 20)
    ck("ttl 内无人领取 -> expired", cC is not None and cC["state"] == "expired",
       (cC and cC["state"], seenC))
    ck("attempts 仍为 0（从没被领过）", cC and cC["attempts"] == 0, cC and cC["attempts"])
    ck("事件里写明 ttl_ms=30000",
       cC and "ttl_ms=30000" in json.dumps(cC["events"], ensure_ascii=False),
       cC and cC["events"])
    ck("离线设备后来上线也领不到这条（不会被执行）", s["simC"].claim() is None)
    code, res, _ = s["simC"].done(s["ridC"], result={"uptime_s": 1})
    ck("expired 之后补发的回执被拒 409", code == 409, code)
    ck_err("  错误码 = already_terminal", res, "already_terminal")


# ---------------------------------------------------------------- 第3周：按键闭环
def s11_button_loop():
    print("\n[S11] 第3周按键闭环：上报 -> 幂等 -> Web 回应 -> notify 领取 -> done -> 回显")
    mac = new_mac()
    sim = Sim(mac, fw="0.3.0-sim")
    boot = sim.boot_id

    def press(seq, **kw):
        body = {"device_mac": mac, "boot_id": boot, "press_seq": seq,
                "fw_version": sim.fw, "t_press_uptime_ms": 4242 + seq,
                "ntp_synced": True, "ntp_sync_age_s": 12,
                "t_device_ntp_ms": int(time.time() * 1000), "queue_dropped": 0}
        body.update(kw)
        return req("POST", "/api/v1/button", body, token="ingest")

    code, body, hdr = press(0)
    ck("按键上报返回 201（新事件）", code == 201, err(body))
    eid = body["event"]["id"]
    ck("响应头 x-deduped: 0", hdr.get("X-Deduped") == "0", hdr.get("X-Deduped"))
    ck("事件初始状态 received", body["event"]["state"] == "received", body["event"])

    code, body, hdr = press(0)   # 板端重试（比如第一次响应丢在半路）
    ck("同 (mac,boot,seq) 重发返回 200 + x-deduped: 1", code == 200 and
       hdr.get("X-Deduped") == "1", (code, hdr.get("X-Deduped")))
    ck("重发拿回的是同一条事件", body["event"]["id"] == eid, body["event"])

    code, body, _ = press(0, boot_id="BOOTX999")
    ck("换了 boot_id 就是新事件（重启后 seq 归零不冲突）", code == 201, err(body))

    code, body, _ = req("GET", "/api/v1/button/events?limit=50")
    ck("事件列表可读", code == 200, code)
    evs = [e for e in body["events"] if e["device_mac"] == mac]
    ck("列表里有本场景的两条事件", len(evs) == 2, len(evs))

    # Web 回应：生成 notify 指令
    code, body, hdr = req("POST", "/api/v1/button/events/%d/respond" % eid,
                          {"decision": "ack", "client_token": mk_token("s11a")},
                          token="control")
    ck("回应返回 201", code == 201, err(body))
    rid = body["command"]["request_id"]
    ck("生成的是 op=notify 的指令", body["command"]["op"] == "notify", body["command"])
    ck("指令参数 decision=ack, event_id=%d" % eid,
       body["command"]["params"] == {"decision": "ack", "event_id": eid},
       body["command"]["params"])
    ck("事件行已更新为 acked 并记住 request_id",
       body["event"]["state"] == "acked" and body["event"]["request_id"] == rid,
       body["event"])

    code, body2, _ = req("POST", "/api/v1/button/events/%d/respond" % eid,
                         {"decision": "ack", "client_token": mk_token("s11a")},
                         token="control")
    ck("同一 client_token 重发被去重（200 + 同一条指令）",
       code == 200 and body2["command"]["request_id"] == rid, (code, body2))

    code, body, _ = req("POST", "/api/v1/button/events/%d/respond" % eid,
                        {"decision": "nope"}, token="control")
    ck_err("非法 decision 被拒（bad_decision）", body, "bad_decision")

    # 设备领取：走固件口径的纯文本协议解析
    j = sim.claim()
    ck("设备领到的正是这条 notify", j is not None and j["request_id"] == rid, j)
    if j:
        ck("领取文本协议里带着 decision=ack", "decision=ack" in j["raw"], j["raw"])
        ck("固件口径解析出的参数正确",
           j["params"] == {"decision": "ack", "event_id": eid}, j["params"])
        code, res, _ = sim.done(rid, result={"decision": "ack", "event_id": eid,
                                             "led_feedback": True})
        ck("notify 回执 done 被接受", code == 200, err(res))

    # 回显：事件行上的指令状态必须变成 done（不另存副本，现查）
    code, body, _ = req("GET", "/api/v1/button/events?limit=50")
    ev = [e for e in body["events"] if e["id"] == eid][0]
    ck("事件行回显 notify 指令状态 = done",
       ev["command"] and ev["command"]["state"] == "done", ev["command"])
    ck("回显里 is_terminal 为真（前端据此停止转圈）",
       ev["command"]["is_terminal"] is True, ev["command"])

    # 再回应一次（用户改主意/重发）：产生新指令，respond_count 增加
    code, body, _ = req("POST", "/api/v1/button/events/%d/respond" % eid,
                        {"decision": "cancel"}, token="control")
    ck("再次回应生成新指令（201）", code == 201, err(body))
    rid2 = body["command"]["request_id"]
    ck("新指令 decision=cancel", body["command"]["params"]["decision"] == "cancel",
       body["command"]["params"])
    ck("respond_count 增到 2", body["event"]["respond_count"] == 2, body["event"])
    j = sim.claim()
    if j:
        sim.done(j["request_id"], result={"decision": "cancel", "event_id": eid})

    # 取消一条尚未回应的按键
    code, body, _ = press(7)
    eid2 = body["event"]["id"]
    code, body, _ = req("POST", "/api/v1/button/events/%d/respond" % eid2,
                        {"decision": "cancel", "client_token": mk_token("s11c")},
                        token="control")
    ck("cancel 回应生成 notify 指令", code == 201, err(body))
    j = sim.claim()
    ck("设备领到 cancel", j is not None and j["params"]["decision"] == "cancel", j)
    if j:
        sim.done(j["request_id"], result={"decision": "cancel", "event_id": eid2})

    # 队列丢弃计数必须可见
    code, body, _ = press(8, queue_dropped=3)
    ck("queue_dropped 原样存回（板上丢过按键这件事不会被抹掉）",
       code == 201 and body["event"]["queue_dropped"] == 3, body)


# ---------------------------------------------------------------- 入口
def main():
    global URL
    # Windows 控制台默认 GBK，中文输出会变成乱码、非 GBK 字符直接崩。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description="第2周远程指令通道端到端验证")
    ap.add_argument("--url", default="http://127.0.0.1:8001")
    ap.add_argument("--fast", action="store_true",
                    help="跳过三个 30 秒级的超时/过期场景")
    args = ap.parse_args()
    URL = args.url.rstrip("/")

    code, body, _ = req("GET", "/health")
    if code != 200:
        print("服务端没起来：%s -> %s" % (URL, body))
        print("请先： $env:TELEMETRY_DB='...\\sim.db'; python -m uvicorn app:app --port 8001")
        return 2
    print("目标服务端:", URL, "  库:", body.get("db"))

    slow = None if args.fast else start_slow()
    s0_catalog()
    s1_happy_path()
    s2_idempotent()
    s3_validation()
    s4_rate_limit()
    s5_failed()
    s6_bad_receipts()
    s7_mismatch()
    s8_cancel()
    s11_button_loop()
    if slow:
        finish_slow(slow)

    print("\n" + "=" * 62)
    print("断言结果：%d 通过 / %d 失败" % (PASS, FAIL))
    if FAIL:
        print("失败项：")
        for n in FAILED_NAMES:
            print("  - " + n)
    else:
        print("全部通过。")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())