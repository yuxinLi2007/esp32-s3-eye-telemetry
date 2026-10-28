"""第9周：视觉事件触发主动询问（"画面不可用 → 要重拍吗？"）+ 确认/忽略 + 冷却。

当堂验证点：
  - 误判能够纠正（纠正为"可用"后同一画面不再提示；询问被作废 superseded）；
  - 用户拒绝（忽略）后不反复打扰（同画面只问一次 + 冷却期内新画面不再问）；
  - 事件触发与反馈优先级合一：画面不可用 + 设备空闲 + 没问过 + 不在冷却，
    才发起询问（vision.ask_eligibility 一处说清）。
"""
import json

import pytest

import app as app_module
import vision
from test_vision import (analyze, chat, claim, conn_of, create_image, seed_batch,
                         snap_flow)


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(app_module, "DB_PATH", str(tmp_path / "t.db"))
    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    import snapshots as snapshots_mod
    monkeypatch.setattr(snapshots_mod, "SNAPSHOT_DIR", str(tmp_path / "snaps"))
    monkeypatch.setattr(snapshots_mod, "SNAPSHOT_KEEP_MAX", 10)
    monkeypatch.setattr(snapshots_mod, "SNAPSHOT_KEEP_MS", 10_000_000)
    for attr in ("VISION_BASE_URL", "VISION_API_KEY", "VISION_MODEL"):
        monkeypatch.setattr(vision, attr, "")
    monkeypatch.setattr(vision, "VISION_LOW_SCORE", 0.45)
    monkeypatch.setattr(vision, "VISION_REPLAY_SCORE", 0.55)
    monkeypatch.setattr(vision, "VISION_REPLAY_LABEL", "")
    monkeypatch.setattr(vision, "VISION_ASK_COOLDOWN_MS", 300_000)


def _label(client, label, score=0.55):
    """分析前先固定回放标签（环境变量级，测试替身）、分数。"""
    import snapshots as snapshots_mod
    # 用 monkeypatch 能力由上层 call 者决定；这里直接改 vision 模块属性
    vision.VISION_REPLAY_LABEL = label
    vision.VISION_REPLAY_SCORE = score


def open_ask_via_analyze(client, label="occluded", score=0.55):
    """采集 → 分析（回放判定为不可用）→ 返回 (sid, rid, analyze_json)。"""
    _label(client, label, score)
    seed_batch(client)
    sid, rid = snap_flow(client)
    r = analyze(client, sid)
    assert r.status_code == 200, r.text
    return sid, rid, r.json()


def recapture_confirm(client, sid):
    return client.post("/api/v1/snapshots/%d/ask" % sid,
                       json={"decision": "confirm"})


# ---------------------------------------------------------------- 事件触发
def test_ask_auto_opens_on_unusable(client):
    sid, _rid, j = open_ask_via_analyze(client, label="occluded")
    assert j["ask"] is not None and j["ask"]["state"] == "open"
    assert j["ask"]["snapshot_id"] == sid
    lst = client.get("/api/v1/asks?pending=1").json()
    assert lst["count"] == 1 and lst["asks"][0]["snapshot_id"] == sid
    assert lst["asks"][0]["captured_ms"] > 0          # 连快照元数据（横幅用）
    assert j["inference"]["label"] == "occluded"      # 事件来源：画面不可用


def test_no_ask_when_usable(client):
    sid, _rid, j = open_ask_via_analyze(client, label="usable", score=0.8)
    assert j.get("ask") is None
    assert j["ask_note"] == "usable"
    assert client.get("/api/v1/asks?pending=1").json()["count"] == 0


def test_ask_dedupe_same_snapshot(client):
    sid, _rid, j = open_ask_via_analyze(client, label="blurred")
    assert j["ask"]["state"] == "open"
    r2 = analyze(client, sid, force=True)             # 重跑分析
    assert r2.status_code == 200
    assert r2.json()["ask"]["id"] == j["ask"]["id"]   # 仍是同一条询问
    assert client.get("/api/v1/asks?pending=1").json()["count"] == 1


def test_no_ask_when_rejected(client, monkeypatch):
    """拒识（未知）同样属于"画面不可用"，触发询问。"""
    def fake(data, task, client=None):
        return {"task": task, "label": "unknown", "score": 0.1,
                "rejected": True,
                "model_version": "teacher-baseline-1"}
    monkeypatch.setattr(vision, "VISION_BASE_URL", "http://teacher.invalid")
    monkeypatch.setattr(vision, "_call_model", fake)
    seed_batch(client)
    sid, _ = snap_flow(client)
    r = analyze(client, sid)
    assert r.status_code == 200, r.text
    assert r.json()["inference"]["status"] == "rejected"
    assert r.json()["ask"] is not None and r.json()["ask"]["state"] == "open"


# ---------------------------------------------------------------- 确认 → 重拍
def test_confirm_creates_recapture(client):
    sid, _rid, j = open_ask_via_analyze(client, label="occluded")
    assert j["ask"]["state"] == "open"
    r = recapture_confirm(client, sid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ask"]["state"] == "confirmed"
    rc = body["recapture"]
    assert rc["op"] == "image" and rc["state"] == "pending"
    assert rc["request_id"] != _rid            # 新请求 = 新图，旧图不冒充
    assert client.get("/api/v1/asks?pending=1").json()["count"] == 0
    # 重复表态被拒
    assert recapture_confirm(client, sid).status_code == 409


# ---------------------------------------------------------------- 拒绝 → 冷却
def test_ignore_closes_ask_and_cooldown_blocks(client):
    sid1, _r1, j = open_ask_via_analyze(client, label="occluded")
    assert j["ask"]["state"] == "open"
    r = client.post("/api/v1/snapshots/%d/ask" % sid1,
                    json={"decision": "ignore"})
    assert r.status_code == 200
    assert r.json()["ask"]["state"] == "ignored"
    assert client.get("/api/v1/asks?pending=1").json()["count"] == 0

    # 冷却期内：新画面（也不可用）不再自动询问 —— 拒绝后不反复打扰
    sid2, _r2, j2 = open_ask_via_analyze(client, label="occluded")
    assert j2.get("ask") is None
    assert j2["ask_note"] == "cooldown"

    # 单元级验证冷却过期后恢复资格（不真的等 5 分钟）
    snap = conn_of(client).execute(
        "SELECT * FROM snapshots WHERE id=?", (sid2,)).fetchone()
    last_ign = vision.last_ignored_ms(conn_of(client))
    elig, why, _a = vision.ask_eligibility(
        conn_of(client), dict(snap), now=last_ign + vision.VISION_ASK_COOLDOWN_MS + 1)
    assert elig and why == "not_usable:occluded@0.55"


def test_ignore_via_voice(client):
    sid, _rid, _j = open_ask_via_analyze(client, label="occluded")
    res = chat(client, "忽略")
    assert res.status_code == 200
    body = res.json()
    assert body["intent"]["action"] == "ask_ignore"
    assert "冷却" in body["answer"] and "5 分钟" in body["answer"]  # 300_000ms
    ask = vision.get_ask(conn_of(client), sid)
    assert ask["state"] == "ignored"
    # 再"忽略"没有待确认的询问了
    res2 = chat(client, "忽略")
    assert "没有待确认" in res2.json()["answer"]


# ---------------------------------------------------------------- 反馈优先级
def test_no_ask_when_task_busy(client):
    """设备上有进行中的采集/图像指令 → 不打断任务，不发起询问。"""
    _label(client, "occluded")
    seed_batch(client)
    sid, rid = snap_flow(client)
    # 先造一条 pending 的采集指令（模拟正在执行别的任务）
    j = chat(client, "重新采集").json()
    pending = j["command"]["request_id"]
    assert j["command"]["state"] == "pending"
    r = analyze(client, sid)
    assert r.status_code == 200
    body = r.json()
    assert body.get("ask") is None
    assert body["ask_note"] == "task_busy"
    assert pending != rid


# ---------------------------------------------------------------- 误判可纠正
def test_correction_usable_supersedes_ask(client):
    sid, _rid, j = open_ask_via_analyze(client, label="occluded")
    assert j["ask"]["state"] == "open"
    r = client.post("/api/v1/snapshots/%d/corrections" % sid,
                    json={"kind": "label", "value": "usable", "note": "人工核对"})
    assert r.status_code == 200
    ask = vision.get_ask(conn_of(client), sid)
    assert ask["state"] == "superseded"          # 纠正后不再提示
    assert client.get("/api/v1/asks?pending=1").json()["count"] == 0
    # 状态回答也不再带询问
    status = chat(client, "图像采集完成了吗").json()["answer"]
    assert "要重拍一张吗" not in status


# ---------------------------------------------------------------- 主动询问入状态回答
def test_status_answer_includes_question_when_ask_pending(client):
    sid, _rid, _j = open_ask_via_analyze(client, label="occluded")
    status = chat(client, "图像采集完成了吗").json()["answer"]
    assert "要重拍一张吗" in status and "重拍" in status and "忽略" in status
    # 之后走确认，状态回答恢复正常
    recapture_confirm(client, sid)
    status2 = chat(client, "图像采集完成了吗").json()["answer"]
    assert "要重拍一张吗" not in status2


# ---------------------------------------------------------------- 鉴权
def test_ask_endpoint_and_ignore_need_control_token(client, monkeypatch):
    sid, _rid, j = open_ask_via_analyze(client, label="occluded")  # 先建流，再上令牌门槛
    assert j["ask"]["state"] == "open"
    monkeypatch.setattr(app_module, "CONTROL_TOKEN", "sekrit")
    # 写操作都要令牌
    assert client.post("/api/v1/snapshots/%d/ask" % sid,
                       json={"decision": "ignore"}).status_code == 401
    assert chat(client, "忽略").status_code == 401
    assert recapture_confirm(client, sid).status_code == 401
    # 只读的询问列表匿名可用
    assert client.get("/api/v1/asks?pending=1").status_code == 200
    rr = client.post("/api/v1/assistant/chat", json={"text": "忽略"},
                     headers={"X-Control-Token": "sekrit"})
    assert rr.status_code == 200
    assert vision.get_ask(conn_of(client), sid)["state"] == "ignored"


def test_ask_not_found_and_bad_decision(client):
    seed_batch(client)
    sid, _ = snap_flow(client)
    assert client.post("/api/v1/snapshots/%d/ask" % sid,
                       json={"decision": "ignore"}).status_code == 404
    _label(client, "occluded")
    sid2, _rid, _j = open_ask_via_analyze(client, label="occluded")
    r = client.post("/api/v1/snapshots/%d/ask" % sid2,
                    json={"decision": "later"})
    assert r.status_code == 422                     # pydantic pattern 先拦

    # 详情里能看到询问状态（匿名只读）
    det = client.get("/api/v1/snapshots/%d" % sid2).json()
    assert det["ask"] is not None and det["ask"]["state"] == "open"