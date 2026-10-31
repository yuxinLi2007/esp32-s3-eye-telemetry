"""第11周：根据多源证据组织交互上下文 —— 三层模型测试。

当堂验证点（每条一个以上用例）：
  - 摄像头遮挡不被解释为危险（推断标签 occluded → 只说"遮挡"，"危险"只以
    "不是危险信号"这种否定形式出现）；
  - 陈旧坐标不被包装成当前位置（陈旧 → "上次报告的位置（X 前）"，绝不"当前位置"）；
  - 模型推断与观测明确区分（每条事实带 观测/推断 标签与来源）。
另有：相互冲突输入（移动中但无当前定位解）、缺失输入（保留为待确认）、
规则版本与证据来源/更新时间的保留、三层对照索引 raw_refs。
"""
import time

import pytest

import app as app_module
import context
import vision
from test_vision import (BOOT, MAC, analyze, chat, claim, create_image,
                         seed_batch, snap_flow, upload)


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(app_module, "DB_PATH", str(tmp_path / "t.db"))
    with TestClient(app_module.app) as c:
        yield c


def ingest(client, mac=MAC, readings=None, sources_=None, t_obs=None):
    t0 = t_obs if t_obs is not None else int(time.time() * 1000)
    body = {
        "device_mac": mac,
        "boot_id": "boot-w11",
        "fw_version": "0.11.0",
        "ntp_synced": True,
        "ntp_sync_age_s": 5,
        "t_device_ntp_ms": t0,
        "dropped_since_last": 0,
        "readings": readings or [
            {"seq": i, "t_device_ms": t0 - (9 - i) * 1000,
             "ax": 0.01 * i, "ay": -0.02 * i, "az": 1.0, "spl_db": 40.0 + i}
            for i in range(10)
        ],
    }
    if sources_ is not None:
        body["sources"] = sources_
    r = client.post("/api/v1/ingest", json=body)
    assert r.status_code == 201, r.text
    return r


def stationary(now=None):
    """静止样本：az≈1g 恒定，幅度无波动。"""
    now = now or int(time.time() * 1000)
    return [
        {"seq": i, "t_device_ms": now - (9 - i) * 1000,
         "ax": 0.0, "ay": 0.0, "az": 1.0, "spl_db": 41.0}
        for i in range(10)
    ]


def moving(now=None):
    """移动样本：幅度明显波动。"""
    now = now or int(time.time() * 1000)
    return [
        {"seq": i, "t_device_ms": now - (9 - i) * 1000,
         "ax": 0.5 * (i % 3 - 1), "ay": 0.3 * (i % 2), "az": 1.0 + 0.4 * (i % 2),
         "spl_db": 42.0}
        for i in range(10)
    ]


def gnss(state="ok", lat=31.2304, lon=121.4737, t_obs=None, replay=False):
    e = {"source": "gnss", "state": state,
         "t_obs_ms": t_obs if t_obs is not None else int(time.time() * 1000)}
    if lat is not None:
        e["raw"] = {"lat": lat, "lon": lon}
    if replay:
        e["replay"] = True
    return e


def get_ctx(client, mac=MAC):
    r = client.get("/api/v1/context?device_mac=%s" % mac)
    assert r.status_code == 200, r.text
    return r.json()


def fact_by_key(ctx, key):
    return next((f for f in ctx["facts"] if f["key"] == key), None)


# ---------------------------------------------------------------- 空数据
def test_no_data_context(client):
    j = get_ctx(client, mac="AA:BB:CC:DD:EE:FF")
    assert j["ok"] is True and j["facts"] == []
    j2 = client.get("/api/v1/context").json()
    assert j2["device_mac"] is None and "还没有任何设备" in j2["answer"]
    c = chat(client, "现在什么情况").json()
    assert c["intent"]["action"] == "context"
    assert "无从谈起上下文" in c["answer"]


# ---------------------------------------------------------------- 静止 + 新鲜位置
def test_stationary_fresh_position(client):
    ingest(client, readings=stationary(),
           sources_=[gnss(t_obs=int(time.time() * 1000) - 2000)])
    ctx = get_ctx(client)
    mv = fact_by_key(ctx, "movement")
    assert mv and mv["value"] == "静止" and mv["layer"] == "observation"
    ps = fact_by_key(ctx, "position")
    assert ps and ps["extra"]["lat"] == 31.2304
    assert ps["value"] == "最近报告的位置" and "fresh" in ps["extra"]
    assert ctx["open_questions"] == []
    ans = ctx["answer"]
    assert "已知" in ans and "观测" in ans and "规则版本" in ans
    assert "仍未确认" not in ans.replace("仍需确认：", "")


# ---------------------------------------------------------------- 移动中
def test_moving_detected(client):
    ingest(client, readings=moving())
    ctx = get_ctx(client)
    mv = fact_by_key(ctx, "movement")
    assert mv and mv["value"] == "移动中"
    assert mv["extra"]["samples"] == 10


# ---------------------------------------------------------------- 遮挡 ≠ 危险（推断）
def test_occluded_never_danger(client, monkeypatch):
    monkeypatch.setattr(vision, "_replay_infer",
                        lambda *a, **k: {"task": "usability", "label": "occluded",
                                         "score": 0.9, "rejected": False,
                                         "model_version": "replay-1"})
    seed_batch(client)
    sid, _ = snap_flow(client)
    analyze(client, sid)
    ctx = get_ctx(client)
    cam = fact_by_key(ctx, "camera_view")
    assert cam and cam["value"] == "画面被遮挡"
    assert cam["layer"] == "inference"          # 模型推断，不是观测
    ans = ctx["answer"]
    assert "画面被遮挡" in ans
    # "危险"只允许以"不是危险信号"出现（遮挡被解释为遮挡，不是危险）
    assert ans.count("危险") == ans.count("不是危险信号")
    assert any("画面被遮挡" in q["question"] for q in ctx["open_questions"])


# ---------------------------------------------------------------- 陈旧坐标 ≠ 当前位置
def test_stale_position_not_current(client):
    now = int(time.time() * 1000)
    ingest(client, sources_=[gnss(t_obs=now - 5 * 60_000)])   # 陈旧位置
    ctx = get_ctx(client)
    ps = fact_by_key(ctx, "position")
    assert ps and ps["value"].startswith("上次报告的位置")
    assert "不作为当前位置" in ps["value"]
    assert ps["extra"]["fresh"] is False
    ans = ctx["answer"]
    assert "不作为当前位置" in ans
    assert "当前位置是" not in ans and "当前位置：" not in ans
    assert any("设备现在还在刚才报告的位置附近吗？" == q["question"]
               for q in ctx["open_questions"])


# ---------------------------------------------------------------- 推断与观测明确区分
def test_inference_and_observation_separated(client, monkeypatch):
    monkeypatch.setattr(vision, "_replay_infer",
                        lambda *a, **k: {"task": "usability", "label": "occluded",
                                         "score": 0.9, "rejected": False,
                                         "model_version": "replay-1"})
    ingest(client, readings=moving(),
           sources_=[gnss(t_obs=int(time.time() * 1000) - 3000)])
    seed_batch(client)
    sid, _ = snap_flow(client)
    analyze(client, sid)
    ctx = get_ctx(client)
    ans = ctx["answer"]
    assert "推断" in ans and "观测" in ans            # 两种层标注都出现
    assert "[推断]" not in ans                        # 用的是中文括号（观测）/（推断）
    cam = fact_by_key(ctx, "camera_view")
    mv = fact_by_key(ctx, "movement")
    assert cam["layer"] == "inference" and mv["layer"] == "observation"
    assert cam["evidence"][0]["source"] == "replay"   # 证据来源保留
    assert mv["evidence"][0]["source"].startswith("imu")


# ---------------------------------------------------------------- 冲突输入：移动中但无当前定位解
def test_conflicting_moving_with_no_position_fix(client):
    now = int(time.time() * 1000)
    ingest(client, readings=moving())
    ingest(client, sources_=[gnss("ok", t_obs=now - 3 * 60_000)])
    ingest(client, sources_=[gnss("no_fix", lat=None, lon=None,
                                  t_obs=now - 2000)])
    ctx = get_ctx(client)
    mv = fact_by_key(ctx, "movement")
    ps = fact_by_key(ctx, "position")
    assert mv["value"] == "移动中"                     # 观测冲突保留（不抹掉任何一边）
    assert ps["value"].startswith("上次有效定位")
    ans = ctx["answer"]
    assert "移动中" in ans and "上次有效定位" in ans and "重新获取位置" in ans
    assert "当前位置" not in ans       # 无当前解，绝不产出当前位置


# ---------------------------------------------------------------- 缺失输入保留为待确认
def test_missing_inputs_kept_pending(client):
    ingest(client, readings=stationary())              # 无 sources：gnss/net 未尝试
    j = get_ctx(client)
    assert fact_by_key(j, "position") is None          # 无位置事实（不补造）
    assert any("无定位证据" in q["question"] for q in j["open_questions"])
    assert fact_by_key(j, "network")["value"] == "网络状态未尝试"
    # imu 无惯性证据：删掉读数后重新解释 → 保留为待确认
    import db
    from pathlib import Path
    conn = db.connect(str(Path(app_module.DB_PATH)))
    conn.execute("DELETE FROM readings WHERE device_mac=?", (MAC,))
    conn.commit()
    conn.close()
    j2 = get_ctx(client)
    assert any("无惯性证据" in q["question"] for q in j2["open_questions"])


# ---------------------------------------------------------------- 只读：匿名可用，无需令牌
def test_context_readonly_even_with_control_token(client, monkeypatch):
    monkeypatch.setattr(app_module, "CONTROL_TOKEN", "sekrit")
    seed_batch(client)
    r = client.get("/api/v1/context")
    assert r.status_code == 200
    c = chat(client, "设备状态").json()
    assert c["intent"]["action"] == "context"
    assert c.get("ok") is True


# ---------------------------------------------------------------- 规则版本与三层对照索引
def test_rule_version_and_raw_refs(client):
    ingest(client, readings=stationary(),
           sources_=[gnss(t_obs=int(time.time() * 1000) - 3000)])
    ctx = get_ctx(client)
    assert ctx["rule_version"] == context.RULE_VERSION
    assert "规则版本" in ctx["answer"]
    refs = ctx["raw_refs"]
    assert {"imu", "gnss"} <= set(refs)
    assert refs["imu"]["kind"] == "observation"
    assert refs["gnss"]["kind"] == "observation"
    # 每条事实都带 updated_ms 与 evidence
    for f in ctx["facts"]:
        assert "updated_ms" in f
        assert isinstance(f["evidence"], list)
        assert f["rule"]


# ---------------------------------------------------------------- 回放标注保留
def test_replay_inference_marked(client, monkeypatch):
    monkeypatch.setattr(vision, "_replay_infer",
                        lambda *a, **k: {"task": "usability", "label": "usable",
                                         "score": 0.8, "rejected": False,
                                         "model_version": "replay-1"})
    seed_batch(client)
    sid, _ = snap_flow(client)
    analyze(client, sid)
    ctx = get_ctx(client)
    cam = fact_by_key(ctx, "camera_view")
    assert cam and cam["evidence"][0]["source"] == "replay"
    assert "回放标注" in ctx["answer"]