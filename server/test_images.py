"""第7周验证：按需环境快照——意图分类、图像指令通道、诚实性铁律与最少保留。

对应当堂验证：
  1. 上次成功图像不得被标为本次采集成功——一次请求一个 request_id 一张图，
     done 回执必须先传图，迟到/重放的图像被终态守卫 409 拒收；两次采集各自
     拿到不同的图像记录与采集时刻；
  2. 采集失败保持明确状态——failed + error_code（occluded / frame_unavailable
     / camera_error）在指令详情与自然语言状态回答里都如实可见，不消失。

另覆盖：图像意图与关键字优先级（拍照 > 查看/查询，取消仍优先于一切）、
图像上传幂等、体积上限、非图像指令拒收、上下文 image_issued/取消、CONTROL_TOKEN。

跑法： cd server && python -m pytest test_images.py -v
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

MAC = "94:A9:90:1C:6F:D4"
BOOT = "BOOT0001"

REQ_ID_RE = re.compile(r"^req_[0-9a-z]{9}_[0-9a-f]{6}$")

IMG_A = b"\xff\xd8\xff\xe0" + b"A" * 256      # 两张不同的"环境图"样例
IMG_B = b"\xff\xd8\xff\xe0" + b"B" * 256


@pytest.fixture(autouse=True)
def isolated_snapshots(tmp_path, monkeypatch):
    """图像本体一律写进临时目录，不碰仓库；保留策略默认放开，个别测试再收紧。"""
    monkeypatch.setattr(snapshots, "SNAPSHOT_DIR", str(tmp_path / "snaps"))
    monkeypatch.setattr(snapshots, "SNAPSHOT_KEEP_MAX", 100)
    monkeypatch.setattr(snapshots, "SNAPSHOT_KEEP_MS", 24 * 3600 * 1000)
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


def seed_batch(client, mac=MAC, boot="boot-aaa", seqs=(0, 1, 2)):
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
    return client.post("/api/v1/ingest", json=body)


def chat(client, text, context_id=None):
    body = {"text": text}
    if context_id:
        body["context_id"] = context_id
    return client.post("/api/v1/assistant/chat", json=body)


def create_image(client, text="拍张照", context_id=None):
    j = chat(client, text, context_id).json()
    assert j["intent"]["action"] == "image", j
    assert j["command"] is not None and j["command"]["op"] == "image"
    return j


def claim(client, rid, mac=MAC, boot=BOOT):
    r = client.post("/api/v1/commands/claim", content="%s|%s|0.1.0" % (mac, boot))
    assert r.status_code == 200, r.text
    assert rid in r.text


def upload(client, rid, data, mac=MAC, boot=BOOT, **headers):
    h = {"X-Device-Mac": mac, "X-Boot-Id": boot, "Content-Type": "image/jpeg"}
    h.update(headers)
    return client.post("/api/v1/commands/%s/image" % rid, content=data, headers=h)


def result(client, rid, state="done", boot=BOOT, **kw):
    body = {"state": state, "boot_id": boot}
    body.update(kw)
    return client.post("/api/v1/commands/%s/result" % rid, json=body)


def full_image_flow(client, data, source="camera", t_captured_ms=123456,
                    error=None):
    """下发 -> 领取 ->（可选传图）-> 回执。返回 (j, snapshot_id)。"""
    j = create_image(client)
    rid = j["command"]["request_id"]
    claim(client, rid)
    if error:
        r = result(client, rid, "failed",
                   error_code=error, error_message="camera report")
        assert r.status_code == 200, r.text
        return j, None
    ru = upload(client, rid, data,
                **{"X-Source": source,
                   "X-Captured-At-Ms": str(t_captured_ms),
                   "X-Width": "640", "X-Height": "480"})
    assert ru.status_code == 200, ru.text
    snap_id = ru.json()["snapshot"]["id"]
    rr = result(client, rid, "done")
    assert rr.status_code == 200, rr.text
    return j, snap_id


# ---------------------------------------------------------------- 意图与优先级
def test_image_intent_creates_image_command(client, conn):
    seed_batch(client)
    j = chat(client, "拍张照").json()
    assert j["intent"]["action"] == "image"
    assert "已受理" in j["answer"]
    assert "成功" not in j["answer"]                # 没拿到图绝不声称成功
    c = j["command"]
    assert c["op"] == "image" and c["state"] == "pending"
    assert REQ_ID_RE.match(c["request_id"])
    assert conn.execute("SELECT op FROM commands").fetchone()["op"] == "image"


def test_image_keyword_precedence(client):
    seed_batch(client)
    assert chat(client, "看看环境").json()["intent"]["action"] == "image"   # 图像词压过"看看"
    assert chat(client, "查看画面").json()["intent"]["action"] == "image"
    assert chat(client, "查看最新数据").json()["intent"]["action"] == "query"
    assert chat(client, "重新采集").json()["intent"]["action"] == "collect"
    assert chat(client, "取消").json()["intent"]["action"] == "cancel"
    assert chat(client, "不要了").json()["intent"]["action"] == "cancel"    # 取消仍优先于一切
    # "采集环境的图像数据"这类又采又拍的口径留给澄清，不擅自二选一
    assert chat(client, "采集环境图像数据").json()["intent"]["action"] == "clarify"


def test_image_requires_known_device(client):
    j = chat(client, "拍张照").json()         # 没有 seed_batch → 无已知设备
    assert j["intent"]["action"] == "refuse"
    assert "还没有见过任何设备" in j["answer"]


def test_image_via_llm_path(client, monkeypatch):
    seed_batch(client)
    monkeypatch.setattr(assistant, "_ask_llm",
                        lambda text: {"action": "image"})
    j = chat(client, "把现场拍下来").json()    # 无关键字命中 → 走模型路径
    assert j["intent"]["action"] == "image"
    assert j["command"]["op"] == "image"


# ---------------------------------------------------------------- 完整流程
def test_image_full_flow_success(client, conn):
    seed_batch(client)
    j = create_image(client)
    rid = j["command"]["request_id"]
    claim(client, rid)
    r = result(client, rid, "running", progress=60)          # 采集中心跳
    assert r.status_code == 200
    assert r.json()["command"]["state"] == "running"
    assert r.json()["command"]["progress"] == 60
    ru = upload(client, rid, IMG_A, **{"X-Source": "camera",
                                       "X-Captured-At-Ms": "987654321",
                                       "X-Width": "640", "X-Height": "480"})
    assert ru.status_code == 200, ru.text
    meta = ru.json()["snapshot"]
    assert meta["source"] == "camera" and meta["n_bytes"] == len(IMG_A)
    rr = result(client, rid, "done")
    assert rr.status_code == 200, rr.text
    c = rr.json()["command"]
    assert c["state"] == "done"
    res = c["result"]
    assert res["image_id"] == meta["id"]
    assert res["captured_at_ms"] == meta["t_server_recv_ms"]       # 服务端接收时刻，权威
    assert res["t_captured_ms"] == 987654321                        # 设备声称，参考
    assert res["source"] == "camera" and res["sha256"] == meta["sha256"]
    # 图像本体可回取，且与上传字节一致
    g = client.get("/api/v1/snapshots/%d/image" % meta["id"])
    assert g.status_code == 200
    assert g.content == IMG_A
    assert g.headers["x-source"] == "camera"
    # 磁盘文件存在
    row = snapshots.by_request(conn, rid)
    assert row is not None and snapshots.path_for(row).is_file()
    # 元数据列表可见（含采集时间，不暴露路径）
    lst = client.get("/api/v1/snapshots").json()["snapshots"]
    assert len(lst) == 1 and lst[0]["request_id"] == rid
    assert "file_name" not in lst[0]


def test_image_done_without_upload_is_rejected(client):
    seed_batch(client)
    j = create_image(client)
    rid = j["command"]["request_id"]
    claim(client, rid)
    r = result(client, rid, "done")            # 没传图就报成功 = 说谎，服务端 409
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "image_not_uploaded"
    c = client.get("/api/v1/commands/%s" % rid).json()["command"]
    assert c["state"] == "claimed"             # 状态没被改坏


def test_image_failed_state_stays_visible(client, conn):
    seed_batch(client)
    j, _ = full_image_flow(client, IMG_A, error="occluded")
    rid = j["command"]["request_id"]
    c = client.get("/api/v1/commands/%s" % rid).json()["command"]
    assert c["state"] == "failed"
    assert c["error_code"] == "occluded"
    assert snapshots.by_request(conn, rid) is None
    # 自然语言状态回答把原因翻译成人话
    j2 = chat(client, "图像采集完成了吗").json()
    assert j2["intent"]["action"] == "status"
    assert "镜头遮挡" in j2["answer"]


# ---------------------------------------------------------------- 当堂验证 1：旧图不得冒充本次成功
def test_two_requests_get_two_distinct_images(client):
    seed_batch(client)
    j1, s1 = full_image_flow(client, IMG_A, t_captured_ms=111)
    j2, s2 = full_image_flow(client, IMG_B, t_captured_ms=222)
    assert s1 != s2                             # 两次采集是两张不同的图
    r1 = client.get("/api/v1/commands/%s" % j1["command"]["request_id"]).json()["command"]
    r2 = client.get("/api/v1/commands/%s" % j2["command"]["request_id"]).json()["command"]
    assert r1["result"]["captured_at_ms"] != r2["result"]["captured_at_ms"]
    assert r1["result"]["image_ref"] != r2["result"]["image_ref"]
    # 第一次的图绝不是第二次的图
    g1 = client.get("/api/v1/snapshots/%d/image" % s1)
    g2 = client.get("/api/v1/snapshots/%d/image" % s2)
    assert g1.content == IMG_A and g2.content == IMG_B and g1.content != g2.content
    # 按 request_id 过滤互不串
    only1 = client.get("/api/v1/snapshots?request_id=%s" % j1["command"]["request_id"])
    assert only1.json()["count"] == 1


def test_late_image_after_terminal_rejected(client):
    seed_batch(client)
    # 已失败终态之后补传的旧图：409，不许翻案
    j, _ = full_image_flow(client, IMG_A, error="camera_error")
    rid = j["command"]["request_id"]
    assert upload(client, rid, IMG_B).status_code == 409
    # 已撤销之后补传的旧图：409，不许"复活"
    j2 = chat(client, "拍张照").json()
    rid2 = j2["command"]["request_id"]
    r = client.post("/api/v1/commands/%s/cancel" % rid2)     # pending 可撤
    assert r.status_code == 200, r.text
    assert upload(client, rid2, IMG_B).status_code == 409


def test_upload_to_non_image_command_rejected(client):
    seed_batch(client)
    r = chat(client, "重新采集")
    assert r.json()["intent"]["action"] == "collect"
    rid = r.json()["command"]["request_id"]
    claim(client, rid)
    assert upload(client, rid, IMG_A).status_code == 409
    assert upload(client, rid, IMG_A).json()["error"]["code"] == "not_image_op"


def test_upload_unknown_rid_404(client):
    assert upload(client, "req_000000000_000000", IMG_A).status_code == 404


def test_upload_too_large_413(client, monkeypatch):
    seed_batch(client)
    j = create_image(client)
    rid = j["command"]["request_id"]
    claim(client, rid)
    monkeypatch.setattr(snapshots, "MAX_IMAGE_BYTES", 1024)
    big = b"\xff\xd8" + b"0" * 4096
    r = upload(client, rid, big)
    assert r.status_code == 413, r.text


def test_upload_idempotent_single_snapshot(client):
    seed_batch(client)
    j = create_image(client)
    rid = j["command"]["request_id"]
    claim(client, rid)
    r1 = upload(client, rid, IMG_A)
    r2 = upload(client, rid, IMG_A)            # 断网重发
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["snapshot"]["id"] == r2.json()["snapshot"]["id"]
    lst = client.get("/api/v1/snapshots").json()
    assert lst["count"] == 1


# ---------------------------------------------------------------- 最少保留
def test_retention_keeps_newest_and_prunes_files(client, conn, monkeypatch):
    monkeypatch.setattr(snapshots, "SNAPSHOT_KEEP_MAX", 2)
    seed_batch(client)
    j1, s1 = full_image_flow(client, IMG_A, t_captured_ms=1)
    j2, s2 = full_image_flow(client, IMG_A, t_captured_ms=2)
    j3, s3 = full_image_flow(client, IMG_B, t_captured_ms=3)
    lst = client.get("/api/v1/snapshots").json()["snapshots"]
    assert len(lst) == 2                       # 第 1 张已被最少保留策略清掉
    ids = {s["id"] for s in lst}
    assert ids == {s2, s3} and s1 not in ids
    assert client.get("/api/v1/snapshots/%d/image" % s1).status_code == 404
    row = snapshots.by_request(conn, j1["command"]["request_id"])
    assert row is None
    # 磁盘文件也一并清掉
    leftovers = list(Path(snapshots.SNAPSHOT_DIR).glob("*.jpg"))
    assert len(leftovers) == 2


def test_retention_by_age(client, conn, monkeypatch):
    """时间上限：超龄图像在下一次入库时被清（存储层可控时间，不睡真实时钟）。"""
    monkeypatch.setattr(snapshots, "SNAPSHOT_KEEP_MS", 60_000)
    monkeypatch.setattr(snapshots, "SNAPSHOT_KEEP_MAX", 100)
    seed_batch(client)
    rids = []
    for _ in range(2):
        j = create_image(client)
        rids.append(j["command"]["request_id"])
    snapshots.store(conn, rids[0], IMG_A, device_mac=MAC, now=1_000_000)
    snapshots.store(conn, rids[1], IMG_B, device_mac=MAC,
                    now=1_000_000 + 70_000)     # 70 秒后入库
    assert snapshots.by_request(conn, rids[0]) is None    # 入库当时即被清
    assert snapshots.by_request(conn, rids[1]) is not None


# ---------------------------------------------------------------- 会话上下文与鉴权
def test_image_context_issued_and_cancel(client, conn):
    seed_batch(client)
    import uuid
    ctx = uuid.uuid4().hex[:24]
    j = create_image(client, context_id=ctx)
    rid = j["command"]["request_id"]
    assert j["context"]["active"]["op"] == "image"
    events = [e["event"] for e in j["context"]["log"]]
    assert "image_issued" in events            # 关联合法的图像请求，日志可反查
    jc = chat(client, "取消", context_id=ctx).json()     # pending 图像指令可撤销
    assert "已撤销" in jc["answer"]
    assert conn.execute("SELECT state FROM commands WHERE request_id=?",
                        (rid,)).fetchone()["state"] == "cancelled"
    assert jc["context"]["cancelled"] is True


def test_image_chat_requires_control_token(client, monkeypatch):
    monkeypatch.setattr(app_module, "CONTROL_TOKEN", "sekrit")
    seed_batch(client)
    r = chat(client, "拍张照")
    assert r.status_code == 401
    rr = client.post("/api/v1/assistant/chat", json={"text": "拍张照"},
                     headers={"X-Control-Token": "sekrit"})
    assert rr.status_code == 200 and rr.json()["intent"]["action"] == "image"


def test_status_answer_reports_image_done_time(client):
    seed_batch(client)
    full_image_flow(client, IMG_A, t_captured_ms=555)
    j = chat(client, "图像采集完成了吗").json()
    assert j["intent"]["action"] == "status"
    assert "图像采集完成" in j["answer"]
    assert "服务端" in j["answer"]            # 答案交代"采到的是哪一时刻"