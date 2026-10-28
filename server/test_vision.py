"""第8周验证：把视觉推理结果变成可理解反馈。

对应当堂验证：
  1. 未经校准的模型分数不写成现实正确率——任何答复/界面把 score 表述为
     "正确率=…" 即失败；正确做法是固定口径"分数未校准，不代表现实正确率"；
  2. 场景类别不转换为精确位置或人身安全判断—— scene 答案固定声明
     "不代表精确位置，也不构成任何安全结论"；
  3. 回放路径明确标注、不计入真实推理证据（source=replay + 答复前缀"回放标注"）。

另覆盖：结构化解析（模型输出校验/越界/缺字段显式报错）、幂等与 force 重跑、
人工纠正入口、重拍（端点 + 语音意图"重拍"）、识别结果问答、输入-结果-反馈
三者对应（推理行即使图像被最少保留清理仍可反查）、CONTROL_TOKEN 鉴权。

跑法： cd server && python -m pytest test_vision.py -v
"""

import re
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
import app as app_module  # noqa: E402
import assistant  # noqa: E402
import commands  # noqa: E402
import db  # noqa: E402
import snapshots  # noqa: E402
import vision  # noqa: E402

MAC = "94:A9:90:1C:6F:D4"
BOOT = "BOOT0001"

REQ_ID_RE = re.compile(r"^req_[0-9a-z]{9}_[0-9a-f]{6}$")

IMG_A = b"\xff\xd8\xff\xe0" + b"A" * 256      # 两张不同的"环境图"样例
IMG_B = b"\xff\xd8\xff\xe0" + b"B" * 256


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """图像本体与推理配置全部隔离；回放路径参数固定（不受宿主机环境影响）。"""
    monkeypatch.setattr(snapshots, "SNAPSHOT_DIR", str(tmp_path / "snaps"))
    monkeypatch.setattr(snapshots, "SNAPSHOT_KEEP_MAX", 100)
    monkeypatch.setattr(snapshots, "SNAPSHOT_KEEP_MS", 24 * 3600 * 1000)
    monkeypatch.setattr(vision, "VISION_BASE_URL", "")
    monkeypatch.setattr(vision, "VISION_API_KEY", "")
    monkeypatch.setattr(vision, "VISION_MODEL", "")
    monkeypatch.setattr(vision, "VISION_LOW_SCORE", 0.45)
    monkeypatch.setattr(vision, "VISION_REPLAY_SCORE", 0.55)
    monkeypatch.setattr(vision, "VISION_REPLAY_LABEL", "")
    yield


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


def seed_batch(client, mac=MAC):
    body = {
        "device_mac": mac,
        "boot_id": "boot-aaa",
        "fw_version": "0.1.0",
        "ntp_synced": True,
        "ntp_sync_age_s": 10,
        "t_device_ntp_ms": int(time.time() * 1000),
        "dropped_since_last": 0,
        "readings": [
            {"seq": i, "t_device_ms": 1000 + i * 100,
             "ax": 0.01 * i, "ay": -0.02 * i, "az": 1.0, "spl_db": 40.0 + i}
            for i in range(3)
        ],
    }
    return client.post("/api/v1/ingest", json=body)


def chat(client, text, context_id=None):
    body = {"text": text}
    if context_id:
        body["context_id"] = context_id
    return client.post("/api/v1/assistant/chat", json=body)


def create_image(client):
    j = chat(client, "拍张照").json()
    assert j["intent"]["action"] == "image", j
    return j


def claim(client, rid):
    r = client.post("/api/v1/commands/claim", content="%s|%s|0.1.0" % (MAC, BOOT))
    assert r.status_code == 200, r.text
    assert rid in r.text


def upload(client, rid, data):
    r = client.post("/api/v1/commands/%s/image" % rid, content=data,
                    headers={"X-Device-Mac": MAC, "X-Boot-Id": BOOT,
                             "Content-Type": "image/jpeg"})
    assert r.status_code == 200, r.text
    return r.json()["snapshot"]


def snap_flow(client, data=IMG_A):
    """完整采集流程：拍一张 → 领取 → 传图 → done。返回 (snapshot_id, request_id)。"""
    j = create_image(client)
    rid = j["command"]["request_id"]
    claim(client, rid)
    snap = upload(client, rid, data)
    r = client.post("/api/v1/commands/%s/result" % rid,
                    json={"state": "done", "boot_id": BOOT})
    assert r.status_code == 200, r.text
    assert r.json()["command"]["state"] == "done"
    return snap["id"], rid


def analyze(client, sid, task="usability", force=False, token=None):
    h = {"X-Control-Token": token} if token else None
    return client.post("/api/v1/snapshots/%d/analyze" % sid,
                       json={"task": task, "force": force}, headers=h)


# ---------------------------------------------------------------- 结构化解析
def test_parse_model_json_validation():
    ok = vision._parse_model_json(
        '{"task":"usability","label":"occluded","score":0.9,'
        '"rejected":false,"model_version":"teacher-1"}')
    assert ok == {"task": "usability", "label": "occluded", "score": 0.9,
                  "rejected": False, "model_version": "teacher-1"}
    for bad in ("not json", '{"score":0.5}', '{"task":"usability","label":"x","score":"a"}',
                '{"task":"usability","label":"x","score":1.7}', '[]'):
        with pytest.raises(vision.VisionError) as ei:
            vision._parse_model_json(bad)
        assert ei.value.code == "bad_model_response"


# ---------------------------------------------------------------- 分析：回放路径
def test_analyze_replay_default(client):
    seed_batch(client)
    sid, rid = snap_flow(client)
    r = analyze(client, sid)
    assert r.status_code == 200, r.text
    inf = r.json()["inference"]
    assert inf["source"] == "replay" and inf["playback"] is True
    assert inf["model_version"] == "replay-rule-1"
    assert inf["task"] == "usability" and inf["label"] in vision.USABILITY_LABELS
    assert inf["request_id"] == rid          # 输入-结果对应：推理行带着请求 id
    # 幂等：同图同任务重复分析拿到同一条记录
    r2 = analyze(client, sid)
    assert r2.json()["inference"]["id"] == inf["id"]
    # force 重跑：旧行被替换（新 id），仍是同图同任务一条
    r3 = analyze(client, sid, force=True)
    inf3 = r3.json()["inference"]
    assert inf3["id"] != inf["id"] and inf3["snapshot_id"] == sid


def test_analyze_unknown_task_and_missing_snapshot(client):
    seed_batch(client)
    sid, _ = snap_flow(client)
    assert analyze(client, sid, task="face_detect").status_code == 400
    assert analyze(client, 99999).status_code == 404


def test_analyze_image_file_pruned_404(client):
    seed_batch(client)
    sid, _ = snap_flow(client)
    row = snapshots.get(conn_of(client), sid)
    snapshots.path_for(row).unlink()
    assert analyze(client, sid).status_code == 404


# ---------------------------------------------------------------- 分析：模型路径
def test_analyze_model_path(client, monkeypatch):
    seed_batch(client)
    sid, _ = snap_flow(client)
    seen = {}

    def fake_model(data, task, **kw):
        seen.update({"len": len(data), "task": task})
        return {"task": task, "label": "occluded", "score": 0.92,
                "rejected": False, "model_version": "teacher-baseline-1"}

    monkeypatch.setattr(vision, "VISION_BASE_URL", "http://model.example")
    monkeypatch.setattr(vision, "_call_model", fake_model)
    r = analyze(client, sid)
    assert r.status_code == 200, r.text
    inf = r.json()["inference"]
    assert inf["source"] == "model" and inf["playback"] is False
    assert inf["status"] == "ok"
    assert inf["label"] == "occluded" and inf["score"] == 0.92
    assert inf["model_version"] == "teacher-baseline-1"
    assert seen["task"] == "usability" and seen["len"] == len(IMG_A)


def test_analyze_model_failure_is_visible_not_lied(client, monkeypatch):
    """模型不可达/输出非法 → status=error 的结果行（HTTP 200 但要如实呈现）。"""
    seed_batch(client)
    sid, _ = snap_flow(client)
    monkeypatch.setattr(vision, "VISION_BASE_URL", "http://model.example")

    def boom(data, task, **kw):
        raise vision.VisionError("模型服务不可达: Timeout", "model_unavailable", 503)

    monkeypatch.setattr(vision, "_call_model", boom)
    r = analyze(client, sid)
    assert r.status_code == 200, r.text
    inf = r.json()["inference"]
    assert inf["status"] == "error" and inf["error_code"] == "model_unavailable"
    j = chat(client, "识别结果").json()
    assert "识别失败" in j["answer"] and "模型服务不可达" in j["answer"]


def test_analyze_model_rejected(client, monkeypatch):
    seed_batch(client)
    sid, _ = snap_flow(client)
    monkeypatch.setattr(vision, "VISION_BASE_URL", "http://model.example")
    monkeypatch.setattr(
        vision, "_call_model",
        lambda data, task, **kw: {"task": task, "label": "unknown", "score": 0.1,
                                  "rejected": True, "model_version": "m-1"})
    inf = analyze(client, sid).json()["inference"]
    assert inf["status"] == "rejected" and inf["rejected"] is True
    j = chat(client, "识别结果").json()
    assert "拒识" in j["answer"] and "不确定" in j["answer"]


# ---------------------------------------------------------------- 当堂验证 1：分数不写成正确率
def test_answer_never_claims_accuracy(client):
    seed_batch(client)
    sid, _ = snap_flow(client)
    assert analyze(client, sid).status_code == 200
    j = chat(client, "识别结果").json()
    assert j["intent"]["action"] == "image_result" and j["command"] is None
    ans = j["answer"]
    assert "未校准" in ans and "不代表现实正确率" in ans
    for bad in ("正确率 55", "正确率55", "准确率"):
        assert bad not in ans


def test_low_score_feedback_suggests_recapture(client, monkeypatch):
    seed_batch(client)
    sid, _ = snap_flow(client)
    monkeypatch.setattr(vision, "VISION_REPLAY_LABEL", "usable")
    monkeypatch.setattr(vision, "VISION_REPLAY_SCORE", 0.3)   # 低分
    monkeypatch.setattr(vision, "VISION_LOW_SCORE", 0.45)
    assert analyze(client, sid).status_code == 200
    ans = chat(client, "识别结果").json()["answer"]
    assert "分数偏低" in ans and "重拍" in ans


# ---------------------------------------------------------------- 当堂验证 2：场景类别不越界
def test_scene_category_not_location_or_safety(client, monkeypatch):
    seed_batch(client)
    sid, _ = snap_flow(client)
    monkeypatch.setattr(vision, "VISION_BASE_URL", "http://model.example")
    monkeypatch.setattr(
        vision, "_call_model",
        lambda data, task, **kw: {"task": task, "label": "indoor_desk", "score": 0.81,
                                  "rejected": False, "model_version": "m-1"})
    r = analyze(client, sid, task="scene")
    assert r.status_code == 200
    ans = chat(client, "识别结果").json()["answer"]
    assert "场景粗分类" in ans and "不代表精确位置" in ans
    assert "不构成任何安全结论" in ans


# ---------------------------------------------------------------- 人工纠正
def test_correction_flows_through(client):
    seed_batch(client)
    sid, _ = snap_flow(client)
    assert analyze(client, sid).status_code == 200   # 回放默认可能是 occluded/usable
    r = client.post("/api/v1/snapshots/%d/corrections" % sid,
                    json={"kind": "label", "value": "usable", "note": "用户说看得清"})
    assert r.status_code == 200, r.text
    assert r.json()["correction"]["effective"] == "usable"
    det = client.get("/api/v1/snapshots/%d" % sid).json()
    assert len(det["corrections"]) == 1
    assert det["effective"][0] == "usable" and det["effective"][1] == "corrected"
    ans = chat(client, "识别结果").json()["answer"]
    assert "已按你的纠正显示" in ans and "可用" in ans


def test_correction_validation(client):
    seed_batch(client)
    sid, _ = snap_flow(client)
    # 空值 / 超长值被 pydantic 先拦（422）；kind 非法走到业务校验（400）
    assert client.post("/api/v1/snapshots/%d/corrections" % sid,
                       json={"kind": "label", "value": "  "}).status_code in (400, 422)
    assert client.post("/api/v1/snapshots/%d/corrections" % sid,
                       json={"kind": "label", "value": "x" * 40}).status_code in (400, 422)
    assert client.post("/api/v1/snapshots/%d/corrections" % sid,
                       json={"kind": "note", "value": "ok"}).status_code == 400


# ---------------------------------------------------------------- 重新采集
def test_recapture_endpoint_new_command(client):
    seed_batch(client)
    sid, rid = snap_flow(client)
    r = client.post("/api/v1/snapshots/%d/recapture" % sid, json={})
    assert r.status_code == 201, r.text
    c = r.json()["command"]
    assert c["op"] == "image" and c["request_id"] != rid
    assert REQ_ID_RE.match(c["request_id"])


def test_recapture_via_voice_intent(client):
    seed_batch(client)
    sid, rid = snap_flow(client)
    j = chat(client, "重拍").json()
    assert j["intent"]["action"] == "recapture", j["intent"]
    assert j["command"]["op"] == "image"
    assert REQ_ID_RE.match(j["command"]["request_id"])
    assert "已受理" in j["answer"] and "重拍" in j["answer"]


# ---------------------------------------------------------------- 输入-结果-反馈对应
def test_correspondence_survives_snapshot_prune(client, monkeypatch):
    """图像本体被最少保留清理后，推理与纠正（带 request_id）仍可反查。"""
    seed_batch(client)
    monkeypatch.setattr(snapshots, "SNAPSHOT_KEEP_MAX", 1)
    sid1, rid1 = snap_flow(client, IMG_A)
    analyze(client, sid1)
    client.post("/api/v1/snapshots/%d/corrections" % sid1,
                json={"kind": "label", "value": "occluded", "note": "第8周审计"})
    sid2, rid2 = snap_flow(client, IMG_B)          # 第二条入库即触发最少保留
    analyze(client, sid2)
    assert snapshots.get(conn_of(client), sid1) is None      # 旧图记录已清理
    lst = client.get("/api/v1/inferences",
                     params={"request_id": rid1}).json()
    assert lst["count"] == 1 and lst["inferences"][0]["request_id"] == rid1
    lst2 = client.get("/api/v1/inferences",
                      params={"request_id": rid2}).json()
    assert lst2["count"] == 1 and lst2["inferences"][0]["request_id"] == rid2


def conn_of(client):
    """与 client 同库的连接：通过 client fixture 建库路径重连。"""
    import db as _db
    return _db.connect(app_module.DB_PATH)


# ---------------------------------------------------------------- 鉴权
def test_control_token_guards_write_ops(client, monkeypatch):
    seed_batch(client)
    sid, _ = snap_flow(client)               # 先建图（此刻还没有令牌门槛）
    monkeypatch.setattr(app_module, "CONTROL_TOKEN", "sekrit")
    assert analyze(client, sid).status_code == 401
    assert analyze(client, sid, token="sekrit").status_code == 200
    assert client.post("/api/v1/snapshots/%d/corrections" % sid,
                       json={"kind": "label", "value": "usable"}).status_code == 401
    assert client.post("/api/v1/snapshots/%d/recapture" % sid,
                       json={}).status_code == 401
    assert client.get("/api/v1/snapshots/%d" % sid).status_code == 200   # 只读匿名
    assert chat(client, "重拍").status_code == 401
    rr = client.post("/api/v1/assistant/chat", json={"text": "重拍"},
                     headers={"X-Control-Token": "sekrit"})
    assert rr.status_code == 200 and rr.json()["intent"]["action"] == "recapture"
    assert chat(client, "识别结果").status_code == 200                     # 只读匿名


def test_image_result_no_inference_yet(client):
    seed_batch(client)
    sid, _ = snap_flow(client)          # 只拍未分析
    ans = chat(client, "识别结果").json()["answer"]
    assert "还没有任何图像识别结果" in ans