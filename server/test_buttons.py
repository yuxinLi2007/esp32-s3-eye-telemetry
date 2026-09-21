"""第3周验证：按键事件上报、幂等、Web 回应/取消生成 notify 指令、指令状态回显。

跑法： cd server && python -m pytest test_buttons.py -v

沿用 test_commands.py 的口径：假时钟注入、不 sleep；超时/过期必须能被确定性断言。
"""

import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
import app as app_module  # noqa: E402
import buttons  # noqa: E402
import commands  # noqa: E402
import db  # noqa: E402

MAC = "94:A9:90:1C:6F:D4"
BOOT = "BOOT0001"
BOOT2 = "BOOT0002"
FW = "0.3.0"


class Clock:
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
    # buttons 模块用 from-import 拿的 now_ms，必须单独换掉，
    # 否则事件行会用真实时间而指令用假时间，断言对不上。
    monkeypatch.setattr(buttons, "now_ms", clock.ms)
    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture
def conn(client, dbpath):
    c = db.connect(dbpath)
    yield c
    c.close()


# ------------------------------------------------------------------ 工具
def press(client, seq=0, mac=MAC, boot=BOOT, headers=None, **kw):
    body = {"device_mac": mac, "boot_id": boot, "press_seq": seq,
            "fw_version": FW, "t_press_uptime_ms": 12345 + seq,
            "ntp_synced": True, "ntp_sync_age_s": 30}
    body.update(kw)
    return client.post("/api/v1/button", json=body, headers=headers or {})


def events(client, **kw):
    return client.get("/api/v1/button/events", params=kw or None)


def respond(client, event_id, decision="ack", token=None, headers=None):
    body = {"decision": decision}
    if token is not None:
        body["client_token"] = token
    return client.post("/api/v1/button/events/%d/respond" % event_id,
                       json=body, headers=headers or {})


def claim(client, mac=MAC, boot=BOOT, fw=FW):
    return client.post("/api/v1/commands/claim",
                       content="%s|%s|%s" % (mac, boot, fw),
                       headers={"Content-Type": "text/plain"})


# ------------------------------------------------------------------ 上报与幂等
def test_press_creates_event(client):
    r = press(client, 0)
    assert r.status_code == 201
    assert r.headers["x-deduped"] == "0"
    j = r.json()
    assert j["ok"] and not j["deduped"]
    e = j["event"]
    assert e["state"] == "received" and e["state_text"] == "待回应"
    assert e["device_mac"] == MAC and e["boot_id"] == BOOT and e["press_seq"] == 0
    assert e["decision"] is None and e["request_id"] is None
    assert e["t_server_ms"] > 0 and e["ntp_synced"] is True


def test_press_retry_is_idempotent(client):
    a = press(client, 3)
    b = press(client, 3)          # 板端重发同一次按键
    assert a.status_code == 201
    assert b.status_code == 200 and b.headers["x-deduped"] == "1"
    assert a.json()["event"]["id"] == b.json()["event"]["id"]
    assert events(client).json()["stats"]["total"] == 1


def test_press_seq_restarts_with_new_boot(client):
    press(client, 0, boot=BOOT)
    r = press(client, 0, boot=BOOT2)   # 重启后 seq 归零，但 boot_id 变了就是新事件
    assert r.status_code == 201
    assert events(client).json()["stats"]["total"] == 2


def test_press_bad_mac(client):
    r = press(client, 0, mac="not-a-mac")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_mac"


def test_press_bad_boot(client):
    r = press(client, 0, boot="bad boot!!")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_boot_id"


def test_press_negative_seq_rejected(client):
    r = client.post("/api/v1/button",
                    json={"device_mac": MAC, "boot_id": BOOT, "press_seq": -1})
    assert r.status_code == 422       # pydantic 挡在业务层之前


def test_press_requires_ingest_token(client, monkeypatch):
    monkeypatch.setattr(app_module, "INGEST_TOKEN", "sekret")
    assert press(client, 0).status_code == 401
    r = press(client, 0, headers={"X-Ingest-Token": "sekret"})
    assert r.status_code == 201


def test_queue_dropped_is_visible(client):
    # 板端队列溢出丢弃的按键数必须随下一次成功上传被看见，不能静默
    r = press(client, 1, queue_dropped=2)
    assert r.json()["event"]["queue_dropped"] == 2


# ------------------------------------------------------------------ 列表
def test_events_list_order_and_filter(client):
    press(client, 0)
    press(client, 1)
    j = events(client).json()
    assert j["ok"] and len(j["events"]) == 2
    assert j["events"][0]["press_seq"] == 1        # 新的在前
    j2 = events(client, device_mac="aa:bb:cc:dd:ee:ff").json()
    assert len(j2["events"]) == 0
    j3 = events(client, state="received").json()
    assert len(j3["events"]) == 2
    assert events(client, state="bogus").status_code == 400


# ------------------------------------------------------------------ 回应/取消
def test_respond_ack_creates_notify_command(client, conn):
    eid = press(client, 0).json()["event"]["id"]
    r = respond(client, eid, "ack")
    assert r.status_code == 201 and r.headers["x-deduped"] == "0"
    j = r.json()
    e, c = j["event"], j["command"]
    assert e["state"] == "acked" and e["decision"] == "ack"
    assert e["respond_count"] == 1 and e["request_id"] == c["request_id"]
    assert c["op"] == "notify" and c["state"] == "pending"
    assert c["params"] == {"decision": "ack", "event_id": eid}
    # 指令走的是第2周那一份状态机：审计事件也必须存在
    row = commands.get_row(conn, c["request_id"])
    assert row is not None
    evs = commands.events_of(conn, c["request_id"])
    assert evs[0]["to_state"] == "pending"


def test_respond_cancel(client):
    eid = press(client, 0).json()["event"]["id"]
    r = respond(client, eid, "cancel")
    assert r.status_code == 201
    j = r.json()
    assert j["event"]["state"] == "cancelled" and j["event"]["decision"] == "cancel"
    assert j["command"]["params"]["decision"] == "cancel"


def test_respond_bad_decision(client):
    eid = press(client, 0).json()["event"]["id"]
    r = respond(client, eid, "maybe")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_decision"


def test_respond_missing_event(client):
    assert respond(client, 999999).status_code == 404


def test_respond_requires_control_token(client, monkeypatch):
    monkeypatch.setattr(app_module, "CONTROL_TOKEN", "ctl-sekret")
    eid = press(client, 0).json()["event"]["id"]
    assert respond(client, eid).status_code == 401
    assert respond(client, eid, headers={"X-Control-Token": "ctl-sekret"}
                   ).status_code == 201


def test_respond_http_retry_is_idempotent_but_reclick_is_not(client):
    eid = press(client, 0).json()["event"]["id"]
    a = respond(client, eid, "ack", token="web-fixed-1")
    b = respond(client, eid, "ack", token="web-fixed-1")   # 同一次意图的网络重发
    assert a.status_code == 201 and b.status_code == 200
    assert b.json()["deduped"] is True
    assert a.json()["command"]["request_id"] == b.json()["command"]["request_id"]
    assert b.json()["event"]["respond_count"] == 1          # 重发不重复计数

    c = respond(client, eid, "ack")                          # 用户再次点击（重发）
    assert c.status_code == 201
    assert c.json()["command"]["request_id"] != a.json()["command"]["request_id"]
    assert c.json()["event"]["respond_count"] == 2


def test_respond_decision_can_be_overridden(client):
    # 先回应再取消：以最后一次为准，两次回应各对应一条指令，历史都在 commands 表里
    eid = press(client, 0).json()["event"]["id"]
    respond(client, eid, "ack")
    r = respond(client, eid, "cancel")
    e = r.json()["event"]
    assert e["state"] == "cancelled" and e["decision"] == "cancel"
    assert e["respond_count"] == 2


# ------------------------------------------------------------------ 板端领取协议
def test_notify_claim_text_protocol(client):
    eid = press(client, 0).json()["event"]["id"]
    rid = respond(client, eid, "cancel").json()["command"]["request_id"]
    r = claim(client)
    assert r.status_code == 200
    text = r.text
    assert text.split("|")[1] == "notify"
    d = commands.decode_claim(text)
    assert d["request_id"] == rid
    assert d["params"] == {"decision": "cancel", "event_id": eid}
    # 字符串参数值不含协议分隔符：白名单在 validate_params 就挡住了
    for ch in "|;=":
        assert ch not in d["params"]["decision"]


def test_notify_result_done(client, conn):
    eid = press(client, 0).json()["event"]["id"]
    rid = respond(client, eid, "ack").json()["command"]["request_id"]
    claim(client)
    r = client.post("/api/v1/commands/%s/result" % rid, json={
        "state": "done", "boot_id": BOOT, "device_mac": MAC, "progress": 100,
        "result": {"decision": "ack", "event_id": eid, "pattern_ms": 800},
    })
    assert r.status_code == 200
    c = r.json()["command"]
    assert c["state"] == "done" and c["result"]["decision"] == "ack"
    # 事件行不抄指令状态：request_id 关联过去现查
    ev = [e for e in events(client).json()["events"] if e["id"] == eid][0]
    assert ev["command"]["state"] == "done" and ev["command"]["is_terminal"]


def test_notify_expiry_is_visible_on_event(client, clock):
    # 设备一直不来领：ttl 到点判 expired，事件行上必须看得见——
    # "用户点了回应但设备根本没收到"绝不能被显示成成功。
    eid = press(client, 0).json()["event"]["id"]
    respond(client, eid, "ack")
    clock.advance(31_000)
    ev = events(client).json()["events"][0]
    assert ev["command"]["state"] == "expired"
    assert ev["command"]["is_terminal"] is True
    assert ev["state"] == "acked"        # 用户确实点过回应，这个事实不被改写


# ------------------------------------------------------------------ notify op 本身
def test_notify_params_validation():
    p = commands.validate_params("notify", None)
    assert p == {"decision": "ack", "event_id": 0}
    p = commands.validate_params("notify", {"decision": "cancel"})
    assert p["decision"] == "cancel"
    with pytest.raises(commands.CommandError) as e:
        commands.validate_params("notify", {"decision": "ACK"})
    assert e.value.code == "bad_param"
    with pytest.raises(commands.CommandError):
        commands.validate_params("notify", {"event_id": -1})
    with pytest.raises(commands.CommandError):
        commands.validate_params("notify", {"decision": "ack|evil=1"})


def test_ops_catalog_exposes_choices():
    cat = {o["op"]: o for o in commands.ops_catalog()}
    notify = cat["notify"]
    dec = [p for p in notify["params"] if p["name"] == "decision"][0]
    assert dec["kind"] == "str" and dec["choices"] == ["ack", "cancel"]
    eid = [p for p in notify["params"] if p["name"] == "event_id"][0]
    assert eid["kind"] == "int" and eid["choices"] is None


def test_notify_can_be_created_directly_from_web(client):
    # notify 也是普通指令：指令面板手动下发（event_id=0）应当照常工作
    r = client.post("/api/v1/commands",
                    json={"device_mac": MAC, "op": "notify",
                          "params": {"decision": "cancel"},
                          "client_token": "web-manual-1"})
    assert r.status_code == 201
    assert r.json()["command"]["params"] == {"decision": "cancel", "event_id": 0}


def test_live_limit_covers_notify(client):
    # 在飞上限对 notify 同样生效：连点回应不能把设备队列塞爆
    eid = press(client, 0).json()["event"]["id"]
    for i in range(commands.MAX_LIVE_PER_DEVICE):
        r = respond(client, eid, "ack", token="tok-%d" % i)
        assert r.status_code == 201, i
    r = respond(client, eid, "ack", token="tok-overflow")
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "too_many_live"
