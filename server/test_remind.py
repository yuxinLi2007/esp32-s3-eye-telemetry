"""第12周：让用户纠错影响后续提醒 —— 主动提醒触发/去重/冷却/显式偏好。

对应当堂验证：
  1. 同一事件重放不重复触发提醒（fire_count 只增一次，重放只更新 last_seen）；
  2. 用户关闭（"这次不用提醒"）后不继续打扰（pref_off 拦住新提醒）；
  3. 能查看并清除本轮偏好（GET prefs 匿名可查；restore / 语音"恢复默认提醒"
     清除并留下版本回退记录）。

另覆盖：有限反馈（确认/纠正/忽略/稍后/恢复默认）全部落 feedback 表（带规则版本
与配置版本）；"保存反馈 ≠ 在线训练"——偏好只调显式规则，不碰模型；CONTROL_TOKEN
鉴权口径与 ask_ignore/cancel 同级。

跑法： cd server && python -m pytest test_remind.py -v
"""
import pytest

import app as app_module
import remind
import vision
from test_vision import analyze, chat, conn_of, seed_batch, snap_flow

MAC = "94:A9:90:1C:6F:D4"


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
    monkeypatch.setattr(vision, "VISION_REPLAY_LABEL", "occluded")
    monkeypatch.setattr(vision, "VISION_ASK_COOLDOWN_MS", 300_000)
    yield


def _open_ask(client):
    """遮挡画面 → 分析 → 触发询问。返回 (snapshot_id, analyze_json)。"""
    seed_batch(client)
    sid, _rid = snap_flow(client)
    r = analyze(client, sid)
    assert r.status_code == 200, r.text
    return sid, r.json()


# ---------------------------------------------------------------- 单元：门与偏好
def test_default_on_and_gate_fires(client):
    g = remind.gate(conn_of(client), MAC, "vision_ask", "snapshot:1", now=1_000_000)
    assert g["fire"] is True and g["reason"] == "ok"
    assert remind.get_pref(conn_of(client), MAC, "vision_ask") is None   # 无记录 = 默认

    remind.mark_fired(conn_of(client), MAC, "vision_ask", "snapshot:1", now=1_000_000)
    ev = conn_of(client).execute(
        "SELECT * FROM reminder_events WHERE event_key='snapshot:1'"
    ).fetchone()
    assert ev["fire_count"] == 1


def test_pref_off_blocks_and_restore_clears(client):
    out = remind.apply_pref(conn_of(client), MAC, "vision_ask", "off",
                            source="api", now=1_000_000)
    assert out["value"] == "off" and out["config_version"] == 1
    g = remind.gate(conn_of(client), MAC, "vision_ask", "snapshot:2", now=1_000_001)
    assert g["fire"] is False and g["reason"] == "pref_off"

    out2 = remind.apply_pref(conn_of(client), MAC, "vision_ask", "restore",
                             source="api", now=1_000_002)
    assert out2["value"] == "on" and out2["restored"] is True
    assert remind.get_pref(conn_of(client), MAC, "vision_ask") is None    # 回到默认
    g2 = remind.gate(conn_of(client), MAC, "vision_ask", "snapshot:3", now=1_000_003)
    assert g2["fire"] is True


def test_snooze_blocks_until_expiry(client):
    t0 = 1_000_000
    remind.apply_pref(conn_of(client), MAC, "vision_ask", "later", source="api", now=t0)
    assert remind.get_pref(conn_of(client), MAC, "vision_ask")["value"] == "snooze"
    g = remind.gate(conn_of(client), MAC, "vision_ask", "snapshot:4",
                    now=t0 + remind.REMIND_LATER_MS - 1, dedup_ms=0)
    assert g["fire"] is False and g["reason"] == "snooze_until"
    g2 = remind.gate(conn_of(client), MAC, "vision_ask", "snapshot:4",
                     now=t0 + remind.REMIND_LATER_MS + 1, dedup_ms=0)
    assert g2["fire"] is True                            # 到期恢复默认规则


def test_config_version_and_rollback_log(client):
    remind.apply_pref(conn_of(client), MAC, "vision_ask", "off", source="api", now=1)
    remind.apply_pref(conn_of(client), MAC, "vision_ask", "later", source="api", now=2)
    remind.apply_pref(conn_of(client), MAC, "vision_ask", "restore",
                      source="api", now=3)
    assert remind.config_version(conn_of(client)) == 3            # 单调递增
    log = remind.pref_history(conn_of(client))
    assert log[0]["action"] == "restore"
    assert log[0]["from_value"] == "snooze" and log[0]["to_value"] == "on"
    assert log[0]["prev_version"] == 2 and log[0]["config_version"] == 3


def test_replay_same_event_does_not_refire(client):
    t0 = 1_000_000
    g1 = remind.gate(conn_of(client), MAC, "vision_ask", "snapshot:9", now=t0)
    assert g1["fire"] is True
    remind.mark_fired(conn_of(client), MAC, "vision_ask", "snapshot:9", now=t0)
    # 重放（窗口内再次看到同一事件）：不触发，只更新 last_seen
    g2 = remind.gate(conn_of(client), MAC, "vision_ask", "snapshot:9",
                     now=t0 + 1_000)
    assert g2["fire"] is False and g2["reason"] == "dedup_replay"
    ev = conn_of(client).execute(
        "SELECT * FROM reminder_events WHERE event_key='snapshot:9'"
    ).fetchone()
    assert ev["fire_count"] == 1
    assert ev["last_seen_ms"] == t0 + 1_000               # 重放被记录
    # 窗口过期后再次出现 → 允许再次触发（fire_count 增加）
    remind.mark_fired(conn_of(client), MAC, "vision_ask", "snapshot:9",
                      now=t0 + remind.REMIND_DEDUP_MS + 1)
    ev2 = conn_of(client).execute(
        "SELECT * FROM reminder_events WHERE event_key='snapshot:9'"
    ).fetchone()
    assert ev2["fire_count"] == 2


def test_feedback_recorded_with_rule_and_config_version(client):
    remind.apply_pref(conn_of(client), MAC, "vision_ask", "off", source="api", now=1)
    remind.record_feedback(conn_of(client), MAC, "confirm", round_id="ask:1",
                           detail="确认重拍", now=2)
    remind.record_feedback(conn_of(client), MAC, "correct", round_id="snapshot:1",
                           detail="usable", now=3)
    rows = remind.list_feedback(conn_of(client))
    assert [r["verb"] for r in rows] == ["correct", "confirm", "off"]
    assert rows[0]["rule_version"] == remind.RULE_VERSION
    assert rows[0]["config_version"] == 1                 # 变更后的配置版本
    with pytest.raises(ValueError):
        remind.record_feedback(conn_of(client), MAC, "retrain")   # 有限反馈集合之外拒绝


def test_global_fallback_scope(client):
    """设备级记录优先；未覆盖的设备回落到全局默认。"""
    remind.apply_pref(conn_of(client), "AA:BB:CC:DD:EE:01", "vision_ask", "off",
                      source="api", now=1)
    assert remind.get_pref(conn_of(client), "AA:BB:CC:DD:EE:01", "vision_ask")["value"] == "off"
    assert remind.get_pref(conn_of(client), MAC, "vision_ask") is None      # 其它设备默认


# ---------------------------------------------------------------- 端点：只读/鉴权
def test_prefs_endpoint_anonymous_and_write_requires_token(client, monkeypatch):
    r = client.get("/api/v1/reminders/prefs")
    assert r.status_code == 200
    j = r.json()
    assert j["config_version"] == 0 and j["default"] == "on"
    assert j["channels"]["vision_ask"]["value"] == "on"

    monkeypatch.setattr(app_module, "CONTROL_TOKEN", "sekrit")
    assert client.post("/api/v1/reminders/prefs",
                       json={"action": "off"}).status_code == 401
    r2 = client.post("/api/v1/reminders/prefs", json={"action": "off"},
                     headers={"X-Control-Token": "sekrit"})
    assert r2.status_code == 200
    assert r2.json()["value"] == "off"
    assert client.get("/api/v1/reminders/prefs").json()["channels"]["vision_ask"]["value"] == "off"
    # 用户可见、可撤回：restore 清除
    r3 = client.post("/api/v1/reminders/prefs", json={"action": "restore"},
                     headers={"X-Control-Token": "sekrit"})
    assert r3.status_code == 200 and r3.json()["restored"] is True


def test_feedback_events_report_endpoints_anonymous(client):
    assert client.get("/api/v1/reminders/feedback").status_code == 200
    assert client.get("/api/v1/reminders/events").status_code == 200
    j = client.get("/api/v1/reminders/report").json()
    assert j["report"]["fired_before_pref_change"] == 0
    assert "asks" in j["report"] and "feedback" in j["report"]


def test_bad_action_422(client, monkeypatch):
    monkeypatch.setattr(app_module, "CONTROL_TOKEN", "sekrit")
    for action in ("retrain", "never", ""):
        assert client.post("/api/v1/reminders/prefs", json={"action": action},
                           headers={"X-Control-Token": "sekrit"}).status_code == 422


# ---------------------------------------------------------------- 集成：提醒门接进 analyze
def test_analyze_fires_ask_and_counts_event(client):
    sid, j = _open_ask(client)
    assert j["ask"] is not None and j["ask"]["state"] == "open"
    ev = client.get("/api/v1/reminders/events").json()["events"]
    hit = [e for e in ev if e["event_key"] == "snapshot:%d" % sid]
    assert len(hit) == 1 and hit[0]["fire_count"] == 1


def test_replay_analyze_same_ask_no_refire(client):
    """当堂验证1：同一事件重放（同图 force 重跑）不重复触发提醒。"""
    sid, j = _open_ask(client)
    assert j["ask"]["state"] == "open"
    r2 = analyze(client, sid, force=True)
    assert r2.status_code == 200
    assert r2.json()["ask"]["id"] == j["ask"]["id"]       # 仍是同一条询问
    ev = client.get("/api/v1/reminders/events").json()["events"]
    hit = [e for e in ev if e["event_key"] == "snapshot:%d" % sid][0]
    assert hit["fire_count"] == 1                          # 没有新触发
    rep = client.get("/api/v1/reminders/report").json()["report"]
    assert rep["replays_not_refired"] == 1                 # 重放被记但不重触发


def test_pref_off_stops_future_reminders(client):
    """当堂验证2：用户关闭后不继续打扰。"""
    sid1, j1 = _open_ask(client)
    assert j1["ask"] is not None
    client.post("/api/v1/reminders/prefs",
                json={"device_mac": MAC, "action": "off"})
    # 新画面（也遮挡）→ 不再发起询问
    sid2, j2 = _open_ask(client)
    assert j2.get("ask") is None
    assert j2["ask_note"] == "pref_off"
    # 之前那条未表态的询问仍在（关闭的是"以后"的提醒，不是用户还没答的问题）
    lst = client.get("/api/v1/asks?pending=1").json()
    assert lst["count"] == 1 and lst["asks"][0]["snapshot_id"] == sid1
    ev = client.get("/api/v1/reminders/events").json()["events"]
    hit = [e for e in ev if e["event_key"] == "snapshot:%d" % sid2][0]
    assert hit["fire_count"] == 0
    # 恢复默认 → 新画面又提醒
    client.post("/api/v1/reminders/prefs",
                json={"device_mac": MAC, "action": "restore"})
    sid3, j3 = _open_ask(client)
    assert j3["ask"] is not None


def test_snooze_blocks_new_reminder(client):
    _open_ask(client)
    client.post("/api/v1/reminders/prefs",
                json={"device_mac": MAC, "action": "later"})
    sid, j = _open_ask(client)
    assert j.get("ask") is None
    assert j["ask_note"] == "snooze_until"


# ---------------------------------------------------------------- 语音/文字意图
def test_chat_remind_review_read_only(client):
    j = chat(client, "提醒设置").json()
    assert j["intent"]["action"] == "remind_review"
    assert "规则版本" in j["answer"] and "配置版本" in j["answer"]


def test_chat_off_later_restore_cycle(client):
    j1 = chat(client, "这次不用提醒").json()
    assert j1["intent"]["action"] == "remind_off"
    assert "不再主动提醒" in j1["answer"]
    assert client.get("/api/v1/reminders/prefs").json()["channels"]["vision_ask"]["value"] == "off"
    assert any(r["verb"] == "off" for r in client.get(
        "/api/v1/reminders/feedback").json()["feedback"])

    j2 = chat(client, "稍后再说").json()
    assert j2["intent"]["action"] == "remind_later"
    assert client.get("/api/v1/reminders/prefs").json()["channels"]["vision_ask"]["value"] == "snooze"

    j3 = chat(client, "恢复默认提醒").json()
    assert j3["intent"]["action"] == "remind_restore"
    assert "回退记录" in j3["answer"]
    assert client.get("/api/v1/reminders/prefs").json()["channels"]["vision_ask"]["value"] == "on"


def test_chat_remind_actions_need_token(client, monkeypatch):
    monkeypatch.setattr(app_module, "CONTROL_TOKEN", "sekrit")
    assert chat(client, "这次不用提醒").status_code == 401
    assert chat(client, "稍后再说").status_code == 401
    assert chat(client, "恢复默认提醒").status_code == 401
    # 只读查看不需要令牌
    assert chat(client, "提醒设置").status_code == 200
    r = chat(client, "这次不用提醒")
    assert r.status_code == 401          # 未带令牌仍被拦
    r2 = client.post("/api/v1/assistant/chat", json={"text": "这次不用提醒"},
                     headers={"X-Control-Token": "sekrit"})
    assert r2.status_code == 200


def test_ask_and_corrections_record_feedback(client, monkeypatch):
    # 忽略会进入冷却：本测试要连续走完"纠正→忽略→确认"三轮，把冷却窗口设 0
    monkeypatch.setattr(vision, "VISION_ASK_COOLDOWN_MS", 0)
    # 纠正 → feedback correct + ask superseded（提醒依据有误，作废）
    sid1, j1 = _open_ask(client)
    assert j1["ask"]["state"] == "open"
    r1 = client.post("/api/v1/snapshots/%d/corrections" % sid1,
                     json={"kind": "label", "value": "usable", "note": "人工核对"})
    assert r1.status_code == 200
    assert any(fb["verb"] == "correct" for fb in
               client.get("/api/v1/reminders/feedback").json()["feedback"])
    assert vision.get_ask(conn_of(client), sid1)["state"] == "superseded"

    # 忽略（语音"忽略"）→ feedback ignore
    sid2, _ = _open_ask(client)
    j2 = chat(client, "忽略").json()
    assert j2["intent"]["action"] == "ask_ignore"
    assert any(fb["verb"] == "ignore" for fb in
               client.get("/api/v1/reminders/feedback").json()["feedback"])

    # 确认 → feedback confirm + 重拍指令（放到最后：避免 pending 指令干扰下一段采集）
    sid3, j3 = _open_ask(client)
    assert j3["ask"]["state"] == "open"
    r3 = client.post("/api/v1/snapshots/%d/ask" % sid3, json={"decision": "confirm"})
    assert r3.status_code == 200
    assert r3.json()["recapture"]["op"] == "image"
    assert any(fb["verb"] == "confirm" for fb in
               client.get("/api/v1/reminders/feedback").json()["feedback"])


# ---------------------------------------------------------------- 前后对照（实践2）
def test_report_contrast_before_after(client):
    sid1, _ = _open_ask(client)                     # 修改前：1 次提醒
    client.post("/api/v1/reminders/prefs",
                json={"device_mac": MAC, "action": "off"})
    sid2, _ = _open_ask(client)                     # 修改后：被拦住，0 次新提醒
    assert _open_ask(client)[1].get("ask") is None
    rep = client.get("/api/v1/reminders/report?device_mac=%s" % MAC).json()["report"]
    assert rep["fired_before_pref_change"] == 1
    assert rep["fired_after_pref_change"] == 0
    assert rep["feedback"].get("off") == 1

