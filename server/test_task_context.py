"""第6周验证：语音/文字任务的会话上下文——澄清与重新输入、取消与停止、
迟到结果隔离、request_id 关联日志与计时。

对应当堂验证：
  1. 取消后迟到结果不得重新播放或触发动作（指令层：终态拒收迟到回执 409；
     会话层：取消标记 + epoch 递增，前端据此丢弃迟到应答）；
  2. 新任务不被旧任务结果覆盖（两次请求各自独立，旧 rid 的完成不影响新任务）。
另覆盖：澄清有限多轮（选设备）、轮数上限强制重开、上下文有界、计时字段。

跑法： cd server && python -m pytest test_task_context.py -v
"""

import re
import sys
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
import app as app_module  # noqa: E402
import commands  # noqa: E402
import db  # noqa: E402
import task_context  # noqa: E402

MAC = "94:A9:90:1C:6F:D4"
MAC2 = "AA:BB:CC:DD:EE:FF"
BOOT = "BOOT0001"

REQ_ID_RE = re.compile(r"^req_[0-9a-z]{9}_[0-9a-f]{6}$")


@pytest.fixture(autouse=True)
def fresh_contexts():
    task_context.reset()
    yield
    task_context.reset()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DB_PATH", str(tmp_path / "t.db"))
    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture
def conn(client, tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    yield c
    c.close()


def cid() -> str:
    return uuid.uuid4().hex[:24]


def chat(client, text, context_id=None):
    body = {"text": text}
    if context_id:
        body["context_id"] = context_id
    return client.post("/api/v1/assistant/chat", json=body)


def seed_batch(client, mac=MAC, boot="boot-aaa", seqs=(0, 1, 2), **kw):
    body = {
        "device_mac": mac,
        "boot_id": boot,
        "fw_version": "0.1.0",
        "ntp_synced": True,
        "ntp_sync_age_s": 10,
        "t_device_ntp_ms": int(time.time() * 1000),
        "dropped_since_last": 0,
        "readings": [
            {"seq": s, "t_device_ms": 1000 + i * 100,
             "ax": 0.01 * i, "ay": -0.02 * i, "az": 1.0, "spl_db": 40.0 + i}
            for i, s in enumerate(seqs)
        ],
    }
    body.update(kw)
    return client.post("/api/v1/ingest", json=body)


def claim(client, rid, mac=MAC, boot=BOOT):
    r = client.post("/api/v1/commands/claim", content="%s|%s|0.1.0" % (mac, boot))
    assert r.status_code == 200, r.text
    assert rid in r.text


def result(client, rid, state, boot=BOOT, **kw):
    body = {"state": state, "boot_id": boot}
    body.update(kw)
    return client.post("/api/v1/commands/%s/result" % rid, json=body)


def state_of(conn, rid):
    row = conn.execute("SELECT state FROM commands WHERE request_id=?", (rid,)).fetchone()
    return row["state"] if row else None


def events_of(j):
    return [e["event"] for e in j["context"]["log"]]


# ---------------------------------------------------------------- 会话基础
def test_without_context_id_keeps_legacy_shape(client):
    seed_batch(client)
    r = chat(client, "查看最新数据")
    assert r.status_code == 200
    j = r.json()
    assert j["context"] is None          # 无上下文 = 第 4/5 周行为
    assert j["intent"]["action"] == "query"


def test_context_view_returned_and_turn_recorded(client):
    seed_batch(client)
    ctx = cid()
    j = chat(client, "查看最新数据", ctx).json()
    assert j["context"]["context_id"] == ctx
    assert j["context"]["epoch"] == 0
    assert j["context"]["turns"][0]["role"] == "user"
    assert "chat_in" in events_of(j) and "chat_out" in events_of(j)


def test_request_id_correlated_log(client):
    seed_batch(client)
    ctx = cid()
    j = chat(client, "重新采集", ctx).json()
    rid = j["command"]["request_id"]
    assert REQ_ID_RE.match(rid)
    issued = [e for e in j["context"]["log"] if e["event"] == "collect_issued"]
    assert issued and issued[0]["request_id"] == rid


# ---------------------------------------------------------------- 澄清与重新输入
def test_clarify_pick_device_then_resolve(client):
    seed_batch(client, mac=MAC)
    seed_batch(client, mac=MAC2)
    ctx = cid()
    j1 = chat(client, "重新采集", ctx).json()
    assert j1["intent"]["action"] == "clarify"
    assert "多台" in j1["answer"]
    assert j1["context"]["pending"]["kind"] == "pick_device"
    assert j1["context"]["pending"]["rounds_left"] == 2
    j2 = chat(client, "第一台", ctx).json()
    assert j2["intent"]["action"] == "collect"
    assert j2["command"]["device_mac"] == MAC          # 排序后第一台
    assert j2["context"]["pending"] is None
    assert "clarify_resolved" in events_of(j2)


def test_clarify_exhausts_after_two_wrong_answers(client):
    seed_batch(client, mac=MAC)
    seed_batch(client, mac=MAC2)
    ctx = cid()
    chat(client, "重新采集", ctx)
    r2 = chat(client, "第三台", ctx).json()
    assert "再确认一次" in r2["answer"]
    assert r2["context"]["pending"]["rounds_left"] == 1
    j3 = chat(client, "随便", ctx).json()
    assert "重新完整说一遍" in j3["answer"]
    assert j3["context"]["pending"] is None
    assert "clarify_exhausted" in events_of(j3)


def test_new_full_intent_during_pending_starts_new_task(client):
    seed_batch(client, mac=MAC)
    seed_batch(client, mac=MAC2)
    ctx = cid()
    chat(client, "重新采集", ctx)
    j2 = chat(client, "查看最新数据", ctx).json()
    assert j2["intent"]["action"] == "query"
    assert j2["context"]["pending"] is None


# ---------------------------------------------------------------- 取消与迟到隔离
def test_cancel_pending_collect(client, conn):
    seed_batch(client)
    ctx = cid()
    rid = chat(client, "重新采集", ctx).json()["command"]["request_id"]
    j = chat(client, "取消", ctx).json()
    assert j["intent"]["action"] == "cancel"
    assert "已撤销" in j["answer"]
    assert state_of(conn, rid) == "cancelled"
    assert j["context"]["cancelled"] is True
    cancelled = [e for e in j["context"]["log"] if e["event"] == "task_cancelled"]
    assert cancelled and cancelled[0]["request_id"] == rid
    assert j["context"]["epoch"] >= 1


def test_cancel_without_active_task(client):
    seed_batch(client)
    ctx = cid()
    j = chat(client, "取消", ctx).json()
    assert "没有进行中的任务" in j["answer"]
    assert j["context"]["cancelled"] is True


def test_cancel_claimed_running_stops_following(client, conn):
    seed_batch(client)
    ctx = cid()
    rid = chat(client, "重新采集", ctx).json()["command"]["request_id"]
    claim(client, rid)
    result(client, rid, "running")
    j = chat(client, "取消", ctx).json()
    assert "停止关注" in j["answer"]
    assert state_of(conn, rid) == "running"      # 指令本体不动，会话停止关注
    assert j["context"]["cancelled"] is True


def test_late_result_rejected_after_cancel(client, conn):
    seed_batch(client)
    ctx = cid()
    rid = chat(client, "重新采集", ctx).json()["command"]["request_id"]
    chat(client, "取消", ctx)
    r = result(client, rid, "done")              # 迟到回执
    assert r.status_code == 409                  # 终态拒收：不会把"已撤销"覆盖成"成功"
    assert r.json()["error"]["code"] == "already_terminal"


def test_new_task_not_overwritten_by_old(client, conn):
    seed_batch(client)
    ctx = cid()
    rid1 = chat(client, "重新采集", ctx).json()["command"]["request_id"]
    rid2 = chat(client, "重新采集", ctx).json()["command"]["request_id"]
    assert rid2 != rid1
    claim(client, rid1)
    result(client, rid1, "done", n_samples=3)    # 旧任务完成 —— 迟到
    j = chat(client, "查看最新数据", ctx).json()
    assert j["context"]["active"]["request_id"] == rid2   # 上下文仍盯着新任务
    assert state_of(conn, rid1) == "done"
    assert state_of(conn, rid2) == "pending"


# ---------------------------------------------------------------- 时序与计时
def test_ttff_recorded_on_each_turn(client):
    seed_batch(client)
    ctx = cid()
    j = chat(client, "查看最新数据", ctx).json()
    assert j["context"]["timing"]["ttff_ms"] is not None
    assert j["context"]["timing"]["ttff_ms"] >= 0


def test_terminal_timing_recorded(client, conn):
    seed_batch(client)
    ctx = cid()
    rid = chat(client, "重新采集", ctx).json()["command"]["request_id"]
    claim(client, rid)
    result(client, rid, "done", n_samples=3)
    j = chat(client, "查看最新数据", ctx).json()
    t = j["context"]["timing"]
    assert t["t_done_ms"] is not None
    assert t["latency_ms"] is not None and t["latency_ms"] >= 0
    assert "task_terminal" in events_of(j)


def test_context_endpoint_finalizes_timing(client):
    seed_batch(client)
    ctx = cid()
    rid = chat(client, "重新采集", ctx).json()["command"]["request_id"]
    claim(client, rid)
    result(client, rid, "done", n_samples=3)
    r = client.get("/api/v1/voice/context", params={"context_id": ctx})
    assert r.status_code == 200
    t = r.json()["context"]["timing"]
    assert t["latency_ms"] is not None and t["latency_ms"] >= 0


def test_context_endpoint_404_unknown(client):
    r = client.get("/api/v1/voice/context", params={"context_id": "nope"})
    assert r.status_code == 404


# ---------------------------------------------------------------- 有界性
def test_max_turns_forces_restart(client):
    seed_batch(client)
    ctx = cid()
    for _ in range(task_context.MAX_TURNS):
        chat(client, "查看最新数据", ctx)
    j = chat(client, "查看最新数据", ctx).json()
    assert "轮数已到上限" in j["answer"]
    j2 = chat(client, "查看最新数据", ctx).json()     # 重开 = 新会话
    assert len(j2["context"]["turns"]) == 1


def test_context_store_bounded():
    for i in range(40):
        task_context.get("c%08d" % i, create=True)
    with task_context._lock:
        n = len(task_context._store)
    assert n <= task_context.MAX_CONTEXTS


def test_bad_context_id_ignored(client):
    seed_batch(client)
    j = chat(client, "查看最新数据", "bad context id!").json()
    assert "context" not in j