"""第2周验证：远程指令的下发、幂等防抖、领取协议、状态机、超时与异常路径。

跑法： cd server && python -m pytest test_commands.py -v

时间一律用可注入的假时钟（clock fixture 换掉 commands.now_ms），不 sleep：
超时逻辑必须能被确定性地断言。靠 sleep 等出来的测试既慢又抖，而且会掩盖本项目
的一个设计事实——超时不是后台线程判的，是在"读"的那一刻惰性结算的。
"""

import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
import app as app_module  # noqa: E402
import commands  # noqa: E402
import db  # noqa: E402

MAC = "94:A9:90:1C:6F:D4"
MAC2 = "AA:BB:CC:DD:EE:FF"
BOOT = "BOOT0001"
BOOT2 = "BOOT0002"
FW = "0.2.0"


class Clock:
    """假时钟。advance() 就是"时间过去了"，测试因此不必真的等。"""

    def __init__(self):
        self.t = int(time.time() * 1000)

    def ms(self):
        return self.t

    def advance(self, ms):
        self.t += int(ms)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def dbpath(tmp_path):
    return str(tmp_path / "t.db")


@pytest.fixture
def client(dbpath, monkeypatch, clock):
    monkeypatch.setattr(app_module, "DB_PATH", dbpath)
    monkeypatch.setattr(commands, "now_ms", clock.ms)
    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture
def conn(client, dbpath):
    """直连同一个库：用来断言"表里到底有几行"、以及直接驱动底层状态机。"""
    c = db.connect(dbpath)
    yield c
    c.close()


# ------------------------------------------------------------------ 工具
def post_cmd(client, op="capture", params=None, token=None, mac=MAC, note=None,
             headers=None):
    body = {"device_mac": mac, "op": op}
    if params is not None:
        body["params"] = params
    if token is not None:
        body["client_token"] = token
    if note is not None:
        body["note"] = note
    return client.post("/api/v1/commands", json=body, headers=headers or {})


def rid_of(resp):
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["command"]["request_id"]


def do_claim(client, mac=MAC, boot=BOOT, fw=FW, headers=None):
    return client.post("/api/v1/commands/claim",
                       content="%s|%s|%s" % (mac, boot, fw), headers=headers or {})


def claim_dict(client, **kw):
    r = do_claim(client, **kw)
    assert r.status_code == 200, r.text
    return commands.decode_claim(r.text)


def post_result(client, rid, state, boot=BOOT, mac=MAC, headers=None, **kw):
    body = {"state": state, "boot_id": boot, "device_mac": mac}
    body.update(kw)
    return client.post("/api/v1/commands/%s/result" % rid, json=body,
                       headers=headers or {})


def get_cmd(client, rid):
    r = client.get("/api/v1/commands/%s" % rid)
    assert r.status_code == 200, r.text
    return r.json()["command"]


def ingest(client, rid=None, seqs=(0, 1, 2), boot=BOOT, mac=MAC, t0=1000,
           headers=None):
    body = {
        "device_mac": mac,
        "boot_id": boot,
        "fw_version": FW,
        "ntp_synced": True,
        "ntp_sync_age_s": 12,
        "t_device_ntp_ms": int(time.time() * 1000),
        "dropped_since_last": 0,
        "readings": [
            {"seq": s, "t_device_ms": t0 + i * 50, "ax": 0.01 * i, "ay": 0.0,
             "az": 1.0, "spl_db": 50.0 + i}
            for i, s in enumerate(seqs)
        ],
    }
    if rid is not None:
        body["request_id"] = rid
    return client.post("/api/v1/ingest", json=body, headers=headers or {})


# ================================================================== request_id 规则
def test_request_id_format():
    rid = commands.new_request_id(1_800_000_000_000)
    assert commands.REQUEST_ID_RE.match(rid), rid
    assert rid.startswith("req_") and len(rid) == 20


def test_request_id_unique_within_same_millisecond():
    """同一毫秒内并发下发也不能撞：随机片就是为此存在的。"""
    now = 1_800_000_000_000
    ids = {commands.new_request_id(now) for _ in range(5000)}
    assert len(ids) == 5000


def test_request_id_sorts_chronologically():
    """定宽 base36 时间片 => 字典序即时间序，列表不必先解析再排序。"""
    base = 1_800_000_000_000
    parts = [commands.new_request_id(base + i * 1000).split("_")[1] for i in range(50)]
    assert parts == sorted(parts)
    assert len(set(len(t) for t in parts)) == 1


def test_request_id_time_slice_decodes_to_creation_ms():
    """id 里的时间片是给人看的线索；权威时间仍是 t_created_ms 那一列。"""
    now = 1_800_000_000_000
    assert int(commands.new_request_id(now).split("_")[1], 36) == now


def test_b36_padding():
    assert commands._to_b36(0, 9) == "000000000"
    assert commands._to_b36(35, 9) == "00000000z"
    assert commands._to_b36(36, 9) == "000000010"


# ================================================================== 下发与防抖
def test_create_returns_pending_with_derived_fields(client, clock):
    r = post_cmd(client, "capture", {"n": 20, "interval_ms": 50}, token="t1")
    assert r.status_code == 201, r.text
    assert r.json()["deduped"] is False and r.headers["x-deduped"] == "0"
    c = r.json()["command"]
    assert c["state"] == "pending" and c["is_terminal"] is False
    assert c["device_mac"] == MAC
    assert c["params"] == {"n": 20, "interval_ms": 50, "duration_ms": 1000}
    assert c["attempts"] == 0 and c["age_ms"] == 0
    assert c["queue_ms"] is None and c["exec_ms"] is None
    assert c["deadline_ms"] == clock.ms() + c["ttl_ms"]
    assert c["remaining_ms"] == c["ttl_ms"]
    assert commands.REQUEST_ID_RE.match(c["request_id"])


def test_duplicate_client_token_is_deduped(client):
    """防抖的服务端那一半：同一次意图重复提交只产生一条指令。"""
    a = post_cmd(client, "ping", token="same")
    b = post_cmd(client, "ping", token="same")
    assert a.status_code == 201 and b.status_code == 200
    assert rid_of(a) == rid_of(b)
    assert b.json()["deduped"] is True and b.headers["x-deduped"] == "1"


def test_dedupe_leaves_only_one_row(conn, client):
    for _ in range(5):
        post_cmd(client, "ping", token="dup")
    assert conn.execute("SELECT COUNT(*) AS n FROM commands").fetchone()["n"] == 1


def test_different_token_creates_new_command(client):
    assert rid_of(post_cmd(client, "ping", token="t-a")) != \
           rid_of(post_cmd(client, "ping", token="t-b"))


def test_no_token_means_no_dedupe(client):
    """不传幂等键就是"我确实要再来一条"，服务端不替用户猜。"""
    assert rid_of(post_cmd(client, "ping")) != rid_of(post_cmd(client, "ping"))


def test_dedupe_is_scoped_by_op_and_mac(client):
    ids = {
        rid_of(post_cmd(client, "ping", token="k")),
        rid_of(post_cmd(client, "selftest", token="k")),
        rid_of(post_cmd(client, "ping", token="k", mac=MAC2)),
    }
    assert len(ids) == 3


def test_client_token_validated(client):
    for bad in ("", "x" * 65, "a b", "tok|1", "tok;drop"):
        assert post_cmd(client, "ping", token=bad).status_code in (400, 422), bad


def test_unknown_op_404(client):
    r = post_cmd(client, "reboot")
    assert r.status_code == 404 and r.json()["error"]["code"] == "unknown_op"


def test_op_charset_validated(client):
    for bad in ("", "PING", "rm -rf", "a|b", "op;drop", "1abc"):
        r = post_cmd(client, bad)
        assert r.status_code == 400, (bad, r.text)
        assert r.json()["error"]["code"] == "bad_op"


def test_capture_param_range_enforced(client):
    for params in ({"n": 0}, {"n": 201}, {"interval_ms": 9}, {"interval_ms": 1001},
                   {"n": 1.5}, {"n": "20"}, {"n": True}):
        r = post_cmd(client, "capture", params)
        assert r.status_code == 400, (params, r.text)
        assert r.json()["error"]["code"] == "bad_param"


def test_capture_unknown_param_rejected(client):
    r = post_cmd(client, "capture", {"n": 10, "evil": 1})
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_param"


def test_capture_duration_capped(client):
    """上限来自硬件事实：采集期间板子不上传，全靠 12 秒环形缓冲扛着。"""
    r = post_cmd(client, "capture", {"n": 200, "interval_ms": 1000})
    assert r.status_code == 400 and r.json()["error"]["code"] == "capture_too_long"
    assert post_cmd(client, "capture", {"n": 200, "interval_ms": 50}).status_code == 201


def test_capture_timeout_grows_with_params(client):
    small = rid_of(post_cmd(client, "capture", {"n": 5, "interval_ms": 20}, token="s"))
    big = rid_of(post_cmd(client, "capture", {"n": 200, "interval_ms": 50}, token="b"))
    s, b = get_cmd(client, small), get_cmd(client, big)
    assert s["timeout_ms"] == 20_000 + 2 * 100
    assert b["timeout_ms"] == 20_000 + 2 * 10_000


def test_defaults_applied_when_params_omitted(client):
    c = get_cmd(client, rid_of(post_cmd(client, "capture", token="d")))
    assert c["params"] == {"n": 40, "interval_ms": 50, "duration_ms": 2000}


def test_mac_normalized_and_validated(client):
    ok = post_cmd(client, "ping", mac=" 94:a9:90:1c:6f:d4 ", token="m")
    assert ok.status_code == 201 and ok.json()["command"]["device_mac"] == MAC
    for bad in ("", "94:A9:90", "hello", "94-A9-90-1C-6F-D4"):
        assert post_cmd(client, "ping", mac=bad, token="m").status_code == 400


def test_too_many_live_commands_rejected(client):
    for i in range(commands.MAX_LIVE_PER_DEVICE):
        assert post_cmd(client, "ping", token="t%d" % i).status_code == 201
    r = post_cmd(client, "ping", token="overflow")
    assert r.status_code == 429 and r.json()["error"]["code"] == "too_many_live"


def test_live_budget_freed_after_completion(client):
    ids = [rid_of(post_cmd(client, "ping", token="t%d" % i))
           for i in range(commands.MAX_LIVE_PER_DEVICE)]
    assert post_cmd(client, "ping", token="x").status_code == 429
    claim_dict(client)
    post_result(client, ids[0], "done", result={"uptime_ms": 1})
    assert post_cmd(client, "ping", token="x").status_code == 201


def test_create_requires_control_token(client, monkeypatch):
    monkeypatch.setattr(app_module, "CONTROL_TOKEN", "sekret")
    assert post_cmd(client, "ping").status_code == 401
    assert post_cmd(client, "ping", headers={"X-Control-Token": "nope"}).status_code == 401
    assert post_cmd(client, "ping",
                    headers={"X-Control-Token": "sekret"}).status_code == 201


def test_ops_catalog_is_single_source_for_frontend(client):
    body = client.get("/api/v1/commands/ops").json()
    assert {o["op"] for o in body["ops"]} == {"ping", "selftest", "capture"}
    cap = [o for o in body["ops"] if o["op"] == "capture"][0]
    assert {p["name"] for p in cap["params"]} == {"n", "interval_ms"}
    assert body["max_capture_duration_ms"] == commands.MAX_CAPTURE_DURATION_MS
    assert body["max_live_per_device"] == commands.MAX_LIVE_PER_DEVICE


def test_no_collect_op_so_control_log_stays_single_source():
    """采集开关的事实来源是 control_log。指令通道若也能改它，就有两份事实会漂移。"""
    assert "set_collect" not in commands.OPS
    assert all(commands.OP_RE.match(op) for op in commands.OPS)

# ================================================================== 领取协议
def test_claim_marks_claimed_and_returns_text_protocol(client, clock):
    rid = rid_of(post_cmd(client, "capture", {"n": 10, "interval_ms": 20}, token="c"))
    r = do_claim(client)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    parts = r.text.split("|")
    assert parts[0] == rid and parts[1] == "capture"
    assert "n=10" in parts[2] and "interval_ms=20" in parts[2]
    assert "duration_ms" not in parts[2]      # 派生值不下发，两边各算就成了两份事实
    assert int(parts[3]) == get_cmd(client, rid)["timeout_ms"]
    c = get_cmd(client, rid)
    assert c["state"] == "claimed" and c["attempts"] == 1
    assert c["claimed_boot_id"] == BOOT and c["claimed_fw"] == FW
    assert c["queue_ms"] == 0


def test_claim_protocol_roundtrips(client):
    rid_of(post_cmd(client, "capture", {"n": 7, "interval_ms": 30}, token="c"))
    got = claim_dict(client)
    assert got["op"] == "capture"
    assert got["params"] == {"n": 7, "interval_ms": 30}
    assert got["timeout_ms"] == 20_000 + 2 * 210


def test_claim_of_paramless_op_keeps_four_fields_with_empty_params(client):
    """无参数指令的领取应答必须仍是 4 段，且第 3 段为空串。

    这是和固件之间的硬契约：firmware/src/command.cpp 的 split_claim 按 '|' 切出
    固定 4 段。真机联调时固件旧版要求 4 段全部非空，于是 ping/selftest 领到手里
    就解析失败、永远不回执，在服务端只能等成 timeout——而服务端自己的
    decode_claim 对空参数段是宽松的，所以 104 个单测和仿真器全绿，问题只在真机上
    暴露。这条测试把线格式钉死，任何一侧改动都会当场红。
    """
    for op in ("ping", "selftest"):
        rid = rid_of(post_cmd(client, op, token="p-" + op))
        r = do_claim(client)
        assert r.status_code == 200, r.text
        parts = r.text.split("|")
        assert len(parts) == 4, (op, r.text)          # 不多不少 4 段
        assert parts[0] == rid and parts[1] == op
        assert parts[2] == "", (op, r.text)           # 参数段为空串，不是省略
        assert int(parts[3]) >= 1000                  # 固件会拒收 <1000 的 timeout
        # 除参数段外任何一段为空，固件都当成应答被截断而拒收
        assert parts[0] and parts[1] and parts[3]
        got = commands.decode_claim(r.text)
        assert got["op"] == op and got["params"] == {}
        # 收尾，免得残留指令影响后面的用例
        post_result(client, rid, "done", result={})


def test_claim_none_when_nothing_pending(client):
    r = do_claim(client)
    assert r.status_code == 200 and r.text == "none"


def test_claim_is_idempotent_per_boot(client):
    """板子重发领取请求既不能把 attempts 加两次，也不能顺手领走第二条指令。"""
    a = rid_of(post_cmd(client, "ping", token="a"))
    rid_of(post_cmd(client, "ping", token="b"))
    assert claim_dict(client)["request_id"] == a
    for _ in range(3):
        assert claim_dict(client)["request_id"] == a
    assert get_cmd(client, a)["attempts"] == 1


def test_claim_hands_out_oldest_first(client):
    a = rid_of(post_cmd(client, "ping", token="a"))
    rid_of(post_cmd(client, "ping", token="b"))
    assert claim_dict(client)["request_id"] == a


def test_claim_is_scoped_by_device_mac(client):
    rid_of(post_cmd(client, "ping", mac=MAC, token="a"))
    rid_of(post_cmd(client, "ping", mac=MAC2, token="b"))
    assert do_claim(client, mac=MAC2, boot=BOOT).status_code == 200
    assert do_claim(client, mac=MAC2, boot=BOOT2).text == "none"   # 只有一条，已领走


def test_concurrent_claim_only_one_winner(client):
    """同一条指令被两个 boot 抢：只有一个能领到，另一个必须看到 none。"""
    rid_of(post_cmd(client, "ping", token="race"))
    got = [x.text for x in (do_claim(client, boot=BOOT), do_claim(client, boot=BOOT2))
           if x.text != "none"]
    assert len(got) == 1


def test_expired_command_is_not_handed_out(client, clock):
    rid_of(post_cmd(client, "ping", token="old"))
    clock.advance(commands.OPS["ping"]["ttl_ms"] + 1)
    assert do_claim(client).text == "none"
    assert client.get("/api/v1/commands").json()["commands"][0]["state"] == "expired"


def test_claim_body_validated(client):
    for bad in ("", "AA", "%s|%s" % (MAC, BOOT), "zz:zz|%s|%s" % (BOOT, FW),
                "%s|%s|" % (MAC, BOOT), "%s|%s|%s" % (MAC, "B" * 40, FW),
                "%s|%s|%s|%s" % (MAC, BOOT, FW, "x")):
        r = client.post("/api/v1/commands/claim", content=bad)
        assert r.status_code == 400, (bad, r.text)


def test_claim_requires_ingest_token(client, monkeypatch):
    monkeypatch.setattr(app_module, "INGEST_TOKEN", "ing")
    rid_of(post_cmd(client, "ping", token="a"))
    assert do_claim(client).status_code == 401
    assert do_claim(client, headers={"X-Ingest-Token": "ing"}).status_code == 200


def test_decode_claim_rejects_garbage():
    assert commands.decode_claim("") is None
    assert commands.decode_claim("none") is None
    for bad in ("a|b", "req_x|op|k=v|1", "req_000000000_ffffff|op|k=v|x|y",
                "req_000000000_ffffff|op|novalue|12"):
        with pytest.raises(commands.CommandError):
            commands.decode_claim(bad)


# ================================================================== 回执与状态机
def test_result_running_updates_progress(client):
    rid = rid_of(post_cmd(client, "capture", {"n": 100, "interval_ms": 50}, token="r"))
    claim_dict(client)
    r = post_result(client, rid, "running", progress=40)
    assert r.status_code == 200, r.text
    c = r.json()["command"]
    assert c["state"] == "running" and c["progress"] == 40 and c["is_terminal"] is False
    assert post_result(client, rid, "running",
                       progress=80).json()["command"]["progress"] == 80


def test_progress_regression_rejected(client):
    """进度回退 = 回执乱序或设备在重放旧数据，两者都要暴露，不能被悄悄覆盖。"""
    rid = rid_of(post_cmd(client, "capture", {"n": 100, "interval_ms": 50}, token="r"))
    claim_dict(client)
    post_result(client, rid, "running", progress=60)
    r = post_result(client, rid, "running", progress=30)
    assert r.status_code == 409 and r.json()["error"]["code"] == "progress_regressed"
    assert get_cmd(client, rid)["progress"] == 60


def test_progress_range_validated(client):
    rid = rid_of(post_cmd(client, "capture", {"n": 100, "interval_ms": 50}, token="r"))
    claim_dict(client)
    for bad in (-1, 101):
        assert post_result(client, rid, "running", progress=bad).status_code == 422


def test_result_done_is_terminal_and_records_timing(client, clock):
    rid = rid_of(post_cmd(client, "ping", token="p"))
    claim_dict(client)
    clock.advance(1234)
    r = post_result(client, rid, "done", result={"uptime_ms": 99, "rssi": -52})
    assert r.status_code == 200
    c = r.json()["command"]
    assert c["state"] == "done" and c["is_terminal"] is True
    assert c["result"] == {"uptime_ms": 99, "rssi": -52}
    assert c["exec_ms"] == 1234 and c["age_ms"] >= 1234
    assert c["progress"] == 100 and c["error_code"] is None


def test_result_failed_requires_error_code(client):
    rid = rid_of(post_cmd(client, "selftest", token="s"))
    claim_dict(client)
    assert post_result(client, rid, "failed").status_code == 400
    r = post_result(client, rid, "failed", error_code="sensor_i2c",
                   error_message="accel 无应答", result={"accel_ok": False})
    assert r.status_code == 200
    c = r.json()["command"]
    assert c["state"] == "failed" and c["error_code"] == "sensor_i2c"
    assert c["error_message"] == "accel 无应答" and c["result"] == {"accel_ok": False}


def test_result_state_validated(client):
    rid = rid_of(post_cmd(client, "ping", token="p"))
    claim_dict(client)
    for bad in ("weird", "PENDING", "done ", ""):
        assert post_result(client, rid, bad).status_code == 400, bad


def test_terminal_state_cannot_be_overwritten(client, clock):
    """超时之后姗姗来迟的"成功"，不能把界面刚显示过的超时改成成功。"""
    rid = rid_of(post_cmd(client, "ping", token="p"))
    tmo = commands.OPS["ping"]["timeout_ms"]
    claim_dict(client, boot=BOOT)
    clock.advance(tmo + 1)                      # 第一次静默：还在重试预算内 -> 重新入队
    assert get_cmd(client, rid)["state"] == "pending"
    claim_dict(client, boot=BOOT2)
    clock.advance(tmo + 1)                      # 预算用尽 -> timeout（终态）
    assert get_cmd(client, rid)["state"] == "timeout"
    r = post_result(client, rid, "done", boot=BOOT2, result={"late": True})
    assert r.status_code == 409 and r.json()["error"]["code"] == "already_terminal"
    assert get_cmd(client, rid)["state"] == "timeout"


def test_boot_mismatch_rejected(client):
    """设备重启后补发的回执属于上一次启动，必须拒收，否则"谁执行的"就不可信。"""
    rid = rid_of(post_cmd(client, "ping", token="p"))
    claim_dict(client, boot=BOOT)
    r = post_result(client, rid, "done", boot=BOOT2, result={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "boot_mismatch"


def test_mac_mismatch_rejected(client):
    rid = rid_of(post_cmd(client, "ping", token="p"))
    claim_dict(client)
    r = post_result(client, rid, "done", mac=MAC2, result={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "mac_mismatch"


def test_result_for_unknown_command_404(client):
    r = post_result(client, "req_000000000_000000", "done", result={})
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_result_before_claim_rejected(client):
    rid = rid_of(post_cmd(client, "ping", token="p"))
    r = post_result(client, rid, "done", result={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_claimed"


def test_result_requires_ingest_token(client, monkeypatch):
    monkeypatch.setattr(app_module, "INGEST_TOKEN", "ing")
    rid = rid_of(post_cmd(client, "ping", token="p"))
    do_claim(client, headers={"X-Ingest-Token": "ing"})
    assert post_result(client, rid, "done", result={}).status_code == 401
    assert post_result(client, rid, "done", result={},
                       headers={"X-Ingest-Token": "ing"}).status_code == 200


def test_transition_table_is_closed():
    known = set(commands.TRANSITIONS) | commands.TERMINAL | commands.LIVE
    for src, dsts in commands.TRANSITIONS.items():
        assert src in known and dsts <= known
    for t in commands.TERMINAL:
        assert commands.TRANSITIONS[t] == set(), "终态不允许再迁移"
    assert commands.can_transition("pending", "claimed")
    assert commands.can_transition("running", "running")      # 进度心跳
    assert not commands.can_transition("done", "pending")
    assert not commands.can_transition("pending", "done")     # 没领取就不能直接成功


def test_illegal_transition_raises_from_inside(conn, client):
    rid = rid_of(post_cmd(client, "ping", token="p"))
    claim_dict(client)
    post_result(client, rid, "done", result={})
    row = commands.get_row(conn, rid)
    with pytest.raises(commands.CommandError) as ei:
        commands._move(conn, row, commands.PENDING, "test", commands.now_ms())
    assert ei.value.code == "bad_transition"


def test_event_trail_covers_every_transition(client):
    rid = rid_of(post_cmd(client, "capture", {"n": 100, "interval_ms": 50}, token="p"))
    claim_dict(client)
    post_result(client, rid, "running", progress=50)
    post_result(client, rid, "done", result={"n_ok": 100}, n_samples=100)
    ev = get_cmd(client, rid)["events"]
    assert [(e["from_state"], e["to_state"]) for e in ev] == [
        (None, "pending"), ("pending", "claimed"),
        ("claimed", "running"), ("running", "done"),
    ]
    assert ev[0]["actor"] == "testclient"
    assert ev[1]["actor"] == "device:" + BOOT
    assert ev[2]["detail"]["progress"] == 50
    assert all(e["t_server_ms"] for e in ev)
    assert [e["t_server_ms"] for e in ev] == sorted(e["t_server_ms"] for e in ev)

# ================================================================== 超时 / 过期 / 重试
def test_pending_expires_after_ttl(client, clock):
    rid = rid_of(post_cmd(client, "ping", token="p"))
    clock.advance(commands.OPS["ping"]["ttl_ms"] - 1)
    assert get_cmd(client, rid)["state"] == "pending"
    clock.advance(2)
    c = get_cmd(client, rid)
    assert c["state"] == "expired" and c["is_terminal"] is True
    assert c["events"][-1]["to_state"] == "expired"
    assert "没有设备领取" in str(c["events"][-1]["detail"])


def test_claimed_silence_requeues_while_attempts_remain(client, clock):
    """领了却静默 = 设备掉线/重启。在重试预算内放回队列，而不是直接判死。"""
    rid = rid_of(post_cmd(client, "ping", token="p"))
    claim_dict(client, boot=BOOT)
    clock.advance(commands.OPS["ping"]["timeout_ms"] + 1)
    c = get_cmd(client, rid)
    assert c["state"] == "pending" and c["attempts"] == 1 and c["requeues"] == 1
    assert c["claimed_boot_id"] is None and c["t_claimed_ms"] is None
    assert c["events"][-1]["detail"]["lost_boot_id"] == BOOT


def test_requeued_command_times_out_after_budget_used(client, clock):
    rid = rid_of(post_cmd(client, "ping", token="p"))
    claim_dict(client, boot=BOOT)
    clock.advance(commands.OPS["ping"]["timeout_ms"] + 1)          # -> pending
    assert claim_dict(client, boot=BOOT2)["request_id"] == rid      # 第二次启动领到
    clock.advance(commands.OPS["ping"]["timeout_ms"] + 1)          # 预算用尽
    c = get_cmd(client, rid)
    assert c["state"] == "timeout" and c["attempts"] == 2
    assert c["events"][-1]["detail"]["lost_boot_id"] == BOOT2


def test_progress_heartbeat_extends_deadline(client, clock):
    """超时锚点是"最后一次有动静"，不是"领取时刻"：
    一次合法的长采集只要还在报进度，就不该被判超时。"""
    rid = rid_of(post_cmd(client, "capture", {"n": 200, "interval_ms": 50}, token="h"))
    tmo = get_cmd(client, rid)["timeout_ms"]
    claim_dict(client)
    for pct in (25, 50, 75):
        clock.advance(tmo - 1000)
        assert post_result(client, rid, "running", progress=pct).status_code == 200
        assert get_cmd(client, rid)["state"] == "running"
    clock.advance(tmo + 1)
    assert get_cmd(client, rid)["state"] == "timeout"


def test_sweep_is_idempotent_and_leaves_terminal_alone(conn, client, clock):
    rid = rid_of(post_cmd(client, "ping", token="p"))
    clock.advance(commands.OPS["ping"]["ttl_ms"] + 1)
    assert [(f, t) for _, f, t in commands.sweep(conn, clock.ms())] == \
        [("pending", "expired")]
    assert commands.sweep(conn, clock.ms()) == []
    n0 = conn.execute("SELECT COUNT(*) AS n FROM command_events").fetchone()["n"]
    commands.sweep(conn, clock.ms() + 10_000)
    n1 = conn.execute("SELECT COUNT(*) AS n FROM command_events").fetchone()["n"]
    assert n0 == n1
    assert get_cmd(client, rid)["state"] == "expired"


def test_deadline_anchor_depends_on_state(client):
    rid = rid_of(post_cmd(client, "capture", {"n": 10, "interval_ms": 20}, token="d"))
    c = get_cmd(client, rid)
    assert c["deadline_ms"] == c["t_created_ms"] + c["ttl_ms"]
    claim_dict(client)
    c2 = get_cmd(client, rid)
    assert c2["deadline_ms"] == c2["t_claimed_ms"] + c2["timeout_ms"]
    assert c2["remaining_ms"] > 0
    post_result(client, rid, "done", result={})
    assert get_cmd(client, rid)["deadline_ms"] is None    # 终态没有截止时刻


def test_list_state_filter_validated(client):
    rid_of(post_cmd(client, "ping", token="p"))
    assert client.get("/api/v1/commands?state=pending").status_code == 200
    assert client.get("/api/v1/commands?state=pending,done").status_code == 200
    r = client.get("/api/v1/commands?state=bogus")
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_state"


def test_list_filters_by_mac_and_op(client):
    rid_of(post_cmd(client, "ping", mac=MAC, token="a"))
    rid_of(post_cmd(client, "capture", {"n": 5, "interval_ms": 20}, mac=MAC2, token="b"))
    assert len(client.get("/api/v1/commands?device_mac=%s" % MAC2).json()["commands"]) == 1
    assert len(client.get("/api/v1/commands?op=capture").json()["commands"]) == 1
    assert len(client.get("/api/v1/commands?op=selftest").json()["commands"]) == 0


# ================================================================== 撤销
def test_cancel_pending(client):
    rid = rid_of(post_cmd(client, "ping", token="p"))
    r = client.post("/api/v1/commands/%s/cancel" % rid)
    assert r.status_code == 200 and r.json()["command"]["state"] == "cancelled"
    assert do_claim(client).text == "none"


def test_cancel_claimed_is_refused(client):
    """已领取的指令不能撤：板子可能正在采集，中途抽走会让结果和指令对不上号。"""
    rid = rid_of(post_cmd(client, "capture", {"n": 10, "interval_ms": 20}, token="p"))
    claim_dict(client)
    r = client.post("/api/v1/commands/%s/cancel" % rid)
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_cancellable"


def test_cancel_unknown_404(client):
    assert client.post("/api/v1/commands/req_000000000_000000/cancel").status_code == 404


def test_cancel_needs_control_token(client, monkeypatch):
    monkeypatch.setattr(app_module, "CONTROL_TOKEN", "sekret")
    rid = rid_of(post_cmd(client, "ping", token="p",
                          headers={"X-Control-Token": "sekret"}))
    assert client.post("/api/v1/commands/%s/cancel" % rid).status_code == 401
    assert client.post("/api/v1/commands/%s/cancel" % rid,
                       headers={"X-Control-Token": "sekret"}).status_code == 200


# ================================================================== 与样本流的联动
def test_ingest_with_request_id_links_samples(client):
    rid = rid_of(post_cmd(client, "capture", {"n": 3, "interval_ms": 50}, token="i"))
    claim_dict(client)
    r = ingest(client, rid=rid, seqs=(0, 1, 2))
    assert r.status_code == 201, r.text
    assert r.json()["request_id"] == rid
    c = get_cmd(client, rid)
    assert c["n_samples"] == 3 and c["samples_batch_id"] == r.json()["batch_id"]
    post_result(client, rid, "done", result={"n_ok": 3}, n_samples=3,
                samples_batch_id=r.json()["batch_id"])
    c = get_cmd(client, rid)
    assert c["n_samples_device"] == 3 and c["sample_count_mismatch"] is False


def test_sample_count_mismatch_is_visible(client):
    """设备说采了 5 条、库里只有 3 条 = 上传掉了。state=done 不足以证明成功。"""
    rid = rid_of(post_cmd(client, "capture", {"n": 5, "interval_ms": 50}, token="i"))
    claim_dict(client)
    ingest(client, rid=rid, seqs=(0, 1, 2))
    post_result(client, rid, "done", result={"n_ok": 5}, n_samples=5)
    c = get_cmd(client, rid)
    assert c["sample_count_mismatch"] is True
    assert c["n_samples"] == 3 and c["n_samples_device"] == 5


def test_ingest_with_unknown_request_id_rejected(client):
    r = ingest(client, rid="req_000000000_000000")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "unknown_request_id"


def test_ingest_with_malformed_request_id_rejected(client):
    r = ingest(client, rid="'; DROP TABLE readings; --")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "bad_request_id"
    assert client.get("/api/v1/status").json()["total_readings"] == 0


def test_readings_exclude_command_samples_by_default_and_say_so(client):
    rid = rid_of(post_cmd(client, "capture", {"n": 3, "interval_ms": 50}, token="i"))
    claim_dict(client)
    ingest(client, seqs=(0, 1, 2))                      # 连续流
    ingest(client, rid=rid, seqs=(0, 1, 2), t0=5000)    # 指令采集
    q = client.get("/api/v1/readings").json()
    assert q["count"] == 3 and q["excluded_command_samples"] == 3
    assert all(r["request_id"] is None for r in q["readings"])


def test_readings_request_id_filter(client):
    rid = rid_of(post_cmd(client, "capture", {"n": 2, "interval_ms": 50}, token="i"))
    claim_dict(client)
    ingest(client, seqs=(0, 1))
    ingest(client, rid=rid, seqs=(0, 1), t0=5000)
    q = client.get("/api/v1/readings?request_id=%s" % rid).json()
    assert q["count"] == 2 and all(r["request_id"] == rid for r in q["readings"])
    assert q["excluded_command_samples"] == 0    # 指名要它，就谈不上"被过滤"


def test_readings_include_command_samples(client):
    rid = rid_of(post_cmd(client, "capture", {"n": 2, "interval_ms": 50}, token="i"))
    claim_dict(client)
    ingest(client, seqs=(0, 1))
    ingest(client, rid=rid, seqs=(0, 1), t0=5000)
    q = client.get("/api/v1/readings?include_command_samples=true").json()
    assert q["count"] == 4 and q["excluded_command_samples"] == 0


def test_capture_samples_do_not_create_false_gaps(client):
    """指令采集的 seq 自成一套。混进连续流算缺口，会把一次正常采集报成丢样。"""
    rid = rid_of(post_cmd(client, "capture", {"n": 3, "interval_ms": 50}, token="i"))
    claim_dict(client)
    ingest(client, seqs=(0, 1, 2), t0=1000)
    ingest(client, rid=rid, seqs=(0, 1, 2), t0=5000)   # seq 回到 0，属于另一个流
    ingest(client, seqs=(3, 4, 5), t0=9000)
    q = client.get("/api/v1/readings").json()
    assert q["gaps"] == [] and q["count"] == 6


def test_real_gap_still_detected_next_to_capture(client):
    rid = rid_of(post_cmd(client, "capture", {"n": 2, "interval_ms": 50}, token="i"))
    claim_dict(client)
    ingest(client, seqs=(0, 1), t0=1000)
    ingest(client, rid=rid, seqs=(0, 1), t0=3000)
    ingest(client, seqs=(9, 10), t0=5000)              # 连续流真的丢了 2..8
    q = client.get("/api/v1/readings").json()
    assert len(q["gaps"]) == 1 and q["gaps"][0]["missing"] == 7


def test_status_carries_command_stats(client, clock):
    rid = rid_of(post_cmd(client, "ping", token="p"))
    st = client.get("/api/v1/status").json()
    assert st["commands"]["pending"] == 1 and st["commands"]["live"] == 1
    claim_dict(client)
    post_result(client, rid, "done", result={})
    st = client.get("/api/v1/status").json()
    assert st["commands"]["done"] == 1 and st["commands"]["live"] == 0
    # status 轮询本身就会结算超时，不需要后台线程
    rid2 = rid_of(post_cmd(client, "ping", token="q"))
    clock.advance(commands.OPS["ping"]["ttl_ms"] + 1)
    assert client.get("/api/v1/status").json()["commands"]["expired"] == 1
    assert get_cmd(client, rid2)["state"] == "expired"


# ================================================================== 端到端
def test_full_capture_flow_end_to_end(client, clock):
    """一次完整的"点按钮 -> 板子真采 -> 结果回到界面"。"""
    created = post_cmd(client, "capture", {"n": 5, "interval_ms": 20},
                       token="click-1", note="界面手动采集")
    assert created.status_code == 201
    rid = rid_of(created)

    got = claim_dict(client)
    assert got["request_id"] == rid and got["params"]["n"] == 5

    batch = ingest(client, rid=rid, seqs=tuple(range(5)), t0=2000).json()
    post_result(client, rid, "running", progress=100)
    clock.advance(120)
    c = post_result(client, rid, "done", n_samples=5,
                    samples_batch_id=batch["batch_id"],
                    result={"n_ok": 5, "n_failed": 0, "spl_avg_db": 51.2,
                            "mag_avg": 1.001, "accel_ok": True,
                            "mic_ok": True}).json()["command"]
    assert c["state"] == "done" and c["progress"] == 100
    assert c["n_samples"] == 5 and c["sample_count_mismatch"] is False
    assert c["result"]["spl_avg_db"] == 51.2 and c["exec_ms"] == 120
    assert c["note"] == "界面手动采集"

    q = client.get("/api/v1/readings?request_id=%s" % rid).json()
    assert q["count"] == 5 and q["readings"][0]["boot_id"] == BOOT
    # 事件流里混着两类记录：状态迁移，以及 ingest 挂样本时的"同状态注记"。
    # 只看迁移才能断言状态机走位；注记单独断言，它必须存在（样本从哪来要可追溯）。
    assert [e["to_state"] for e in c["events"]
            if e["from_state"] != e["to_state"]] == \
        ["pending", "claimed", "running", "done"]
    assert [(e["actor"], e["detail"]["n_samples"]) for e in c["events"]
            if e["from_state"] == e["to_state"]] == [("ingest", 5)]


def test_device_failure_path_is_visible(client, clock):
    """传感器读不出来时，界面必须看到"失败 + 原因"，而不是永远转圈。"""
    rid = rid_of(post_cmd(client, "selftest", token="s"))
    claim_dict(client)
    c = post_result(client, rid, "failed", error_code="mic_no_output",
                   error_message="i2s_read 连续 3 次返回 0 帧",
                   result={"accel_ok": True, "mic_ok": False}).json()["command"]
    assert c["state"] == "failed" and c["error_code"] == "mic_no_output"
    assert client.get("/api/v1/commands?state=failed").json()["stats"]["failed"] == 1


def test_offline_device_ends_in_expired_not_silence(client, clock):
    """设备根本不在线时，指令必须自己走到 expired —— 这是"失败可见"的底线。"""
    rid = rid_of(post_cmd(client, "capture", {"n": 10, "interval_ms": 50},
                          token="offline"))
    clock.advance(commands.OPS["capture"]["ttl_ms"] + 1)
    assert get_cmd(client, rid)["state"] == "expired"
    assert client.get("/api/v1/commands").json()["stats"]["expired"] == 1


def test_migration_adds_request_id_to_week1_database(tmp_path):
    """旧库升级：第1周的库没有 request_id 列，启动时必须自动补上且不丢数据。"""
    path = str(tmp_path / "old.db")
    conn = db.connect(path)
    conn.executescript("""
    CREATE TABLE batches (
        id INTEGER PRIMARY KEY AUTOINCREMENT, device_mac TEXT NOT NULL,
        boot_id TEXT NOT NULL, fw_version TEXT, seq_first INTEGER NOT NULL,
        seq_last INTEGER NOT NULL, n_readings INTEGER NOT NULL,
        dropped_since_last INTEGER NOT NULL DEFAULT 0, ntp_synced INTEGER NOT NULL,
        ntp_sync_age_s INTEGER, t_device_ntp_ms INTEGER,
        t_server_recv_ms INTEGER NOT NULL, source_ip TEXT, source_ua TEXT);
    CREATE TABLE readings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
        device_mac TEXT NOT NULL, boot_id TEXT NOT NULL, seq INTEGER NOT NULL,
        t_device_ms INTEGER NOT NULL, ax REAL, ay REAL, az REAL, spl_db REAL);
    """)
    # 用裸 SQL 写入：这一刻模拟的是"第1周的 db.insert_batch"，
    # 它根本不知道 request_id 这一列的存在。走新代码的 insert_batch 就测不到迁移了。
    t_recv = int(time.time() * 1000)
    cur = conn.execute(
        "INSERT INTO batches (device_mac, boot_id, fw_version, seq_first, seq_last,"
        " n_readings, dropped_since_last, ntp_synced, ntp_sync_age_s,"
        " t_device_ntp_ms, t_server_recv_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (MAC, BOOT, "0.1.0", 0, 1, 2, 0, 1, 5, None, t_recv))
    bid = cur.lastrowid
    conn.executemany(
        "INSERT INTO readings (batch_id, device_mac, boot_id, seq, t_device_ms,"
        " ax, ay, az, spl_db) VALUES (?,?,?,?,?,?,?,?,?)",
        [(bid, MAC, BOOT, 0, 1, 0, 0, 1, 50), (bid, MAC, BOOT, 1, 2, 0, 0, 1, 51)])
    conn.commit()
    conn.close()

    conn = db.init_db(path)                       # 升级路径
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(batches)")}
    assert "request_id" in cols
    rows = db.query_readings(conn)
    assert len(rows) == 2 and rows[0]["request_id"] is None
    assert db.count_command_samples(conn) == 0
    conn.close()