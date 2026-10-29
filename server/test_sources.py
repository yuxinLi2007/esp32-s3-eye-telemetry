"""第10周：区分传感器观测与当前状态 —— 多源独立状态（未尝试/失败/成功/陈旧）。

当堂验证点（每条一个以上用例）：
  - 补传数据按原观测时间解释（陈旧不冒充当前）；
  - GNSS 无有效解不直接写成设备故障（no_fix ≠ device fault）；
  - 无证据时不补造位置（从未上报 → 未尝试，不编坐标）。
"""
import time

import pytest

import app as app_module
import db
import sources
from test_vision import MAC, seed_batch


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(app_module, "DB_PATH", str(tmp_path / "t.db"))
    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture
def conn(client, tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    yield c
    c.close()


def ingest(client, mac=MAC, boot="boot-w10", sources_=None,
           readings_n=2, t_device_ms=None, ntp_synced=True):
    t0 = t_device_ms if t_device_ms is not None else int(time.time() * 1000)
    body = {
        "device_mac": mac,
        "boot_id": boot,
        "fw_version": "0.10.0",
        "ntp_synced": ntp_synced,
        "ntp_sync_age_s": 5,
        "t_device_ntp_ms": t0,
        "dropped_since_last": 0,
        "readings": [
            {"seq": i, "t_device_ms": t0 + i * 100,
             "ax": 0.01 * i, "ay": -0.02 * i, "az": 1.0, "spl_db": 40.0 + i}
            for i in range(readings_n)
        ],
    }
    if sources_ is not None:
        body["sources"] = sources_
    return client.post("/api/v1/ingest", json=body)


def gnss(state="ok", t_obs_ms=None, raw=None, replay=False):
    return {"source": "gnss", "state": state,
            "t_obs_ms": t_obs_ms if t_obs_ms is not None
            else int(time.time() * 1000),
            "raw": raw, "replay": replay}


def net(state="ok", t_obs_ms=None, raw=None, replay=False):
    return {"source": "net", "state": state,
            "t_obs_ms": t_obs_ms if t_obs_ms is not None
            else int(time.time() * 1000),
            "raw": raw, "replay": replay}


def get_sources(client, mac=MAC):
    r = client.get("/api/v1/devices/%s/sources" % mac)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- 未尝试
def test_absent_sources_are_not_attempted(client):
    ingest(client, sources_=None)         # 老板子：不带上 sources
    j = get_sources(client)
    # 事件源（gnss/net）未上报 = 未尝试；imu 由本次 readings 派生 = 正常
    assert j["source_states"]["gnss"]["status"] == "not_attempted"
    assert j["source_states"]["net"]["status"] == "not_attempted"
    assert j["source_states"]["imu"]["status"] == "success"
    ans = j["answer"]
    assert "未尝试" in ans and "不假定硬件已上线" in ans
    assert "lat" not in ans and "lon" not in ans     # 无证据不补造位置
    chat = client.post("/api/v1/assistant/chat",
                       json={"text": "各源状态"}).json()
    assert chat["intent"]["action"] == "source_status"
    assert "多源状态" in chat["answer"]


# ---------------------------------------------------------------- 成功（新鲜）
def test_gnss_ok_fresh_success(client):
    raw = {"lat": 31.2304, "lon": 121.4737, "sats": 7}
    t0 = int(time.time() * 1000)
    ingest(client, sources_=[gnss("ok", t_obs_ms=t0 - 1000, raw=raw)])
    j = get_sources(client)
    g = j["source_states"]["gnss"]
    assert g["status"] == "success" and g["reason"] is None
    assert 0 <= g["age_ms"] <= 3000
    assert g["raw"]["lat"] == 31.2304              # 原始观测原样保留
    ans = j["answer"]
    assert "GNSS 定位：正常" in ans


# ---------------------------------------------------------------- no_fix ≠ 设备故障
def test_no_fix_is_failure_not_device_fault(client):
    t0 = int(time.time() * 1000)
    ingest(client, sources_=[gnss("ok", t_obs_ms=t0 - 60_000,
                                  raw={"lat": 31.2, "lon": 121.4})])
    ingest(client, sources_=[gnss("no_fix", t_obs_ms=t0 - 5_000)])
    j = get_sources(client)
    g = j["source_states"]["gnss"]
    assert g["status"] == "failed" and g["reason"] == "no_fix"
    assert g["last_success_obs_ms"] is not None    # 有旧位置
    ans = j["answer"]
    assert "不是设备故障" in ans
    assert "有旧位置但本次 no_fix" in ans
    assert "不冒充当前位置" in ans
    # 答复里的"设备故障"只以否定形式出现
    assert ans.count("设备故障") == ans.count("不是设备故障")


def test_no_fix_without_history_no_position(client):
    t0 = int(time.time() * 1000)
    ingest(client, sources_=[gnss("no_fix", t_obs_ms=t0 - 5_000)])
    j = get_sources(client)
    g = j["source_states"]["gnss"]
    assert g["status"] == "failed" and g["reason"] == "no_fix"
    assert g["last_success_obs_ms"] is None
    ans = j["answer"]
    assert "不补造位置" in ans and "从未有过有效定位" in ans
    assert "旧位置" not in ans


# ---------------------------------------------------------------- 补传按原观测时间解释
def test_backfill_interpreted_by_observation_time(client):
    """补传：观测时刻在 15 分钟前、服务端此刻才收到 —— 状态必须"陈旧"不是"正常"。"""
    now_ms = int(time.time() * 1000)
    t_obs = now_ms - 15 * 60_000                       # 原观测时间：15 分钟前
    ingest(client, sources_=[gnss("ok", t_obs_ms=t_obs,
                                  raw={"lat": 31.2, "lon": 121.4})])
    j = get_sources(client)
    g = j["source_states"]["gnss"]
    assert g["status"] == "stale", g                    # 不冒充当前
    assert g["age_ms"] > 14 * 60_000                    # 年龄按观测时刻算
    recv = g["last_recv_ms"]
    assert recv is not None and (now_ms - recv) < 5000  # 接收时刻是刚刚
    ans = j["answer"]
    assert "数据陈旧" in ans and "不冒充当前状态" in ans


# ---------------------------------------------------------------- 陈旧阈值（单元级，固定 now）
def test_stale_thresholds_per_source(client, conn):
    now = int(time.time() * 1000)
    sources.record(conn, device_mac=MAC, boot_id="b", source="gnss",
                   state="ok", t_obs_ms=now - 65_000, t_recv_ms=now)   # 65s > 60s
    sources.record(conn, device_mac=MAC, boot_id="b", source="net",
                   state="ok", t_obs_ms=now - 80_000, t_recv_ms=now)   # 80s < 90s
    db.insert_batch(conn, {
        "device_mac": MAC, "boot_id": "b", "fw_version": "0.10",
        "seq_first": 0, "seq_last": 0, "ntp_synced": 1, "ntp_sync_age_s": 5,
        "t_device_ntp_ms": now, "t_server_recv_ms": now,
        "readings": [{"seq": 0, "t_device_ms": now - 35_000,   # 35s > 30s
                      "ax": 0.1, "ay": 0.2, "az": 1.0}],
    })
    sts = sources.snapshot(conn, MAC, now=now)
    assert sts["gnss"]["status"] == "stale"
    assert sts["net"]["status"] == "success"
    assert sts["imu"]["status"] == "stale"
    # 观测时刻回拨到"都还新鲜"的时间点重算（年龄按观测时刻，与接收无关）
    sts2 = sources.snapshot(conn, MAC, now=now - 30_000)
    assert sts2["gnss"]["status"] == "success"      # 65-30=35s < 60s
    assert sts2["imu"]["status"] == "success"       # 35-30=5s < 30s


# ---------------------------------------------------------------- 案例：网络在线但位置未更新
def test_case_net_online_but_gnss_stale(client):
    t0 = int(time.time() * 1000)
    ingest(client, sources_=[gnss("ok", t_obs_ms=t0 - 5 * 60_000,
                                  raw={"lat": 31.2, "lon": 121.4})])
    ingest(client, sources_=[net("ok", t_obs_ms=t0 - 2000, raw={"rssi": -55})])
    j = get_sources(client)
    assert j["source_states"]["net"]["status"] == "success"
    assert j["source_states"]["gnss"]["status"] == "stale"
    ans = j["answer"]
    assert "网络在线但位置未更新" in ans
    assert "不会把旧位置说成当前位置" in ans


# ---------------------------------------------------------------- 回放标注
def test_replay_marked_clearly(client):
    t0 = int(time.time() * 1000)
    ingest(client, sources_=[gnss("ok", t_obs_ms=t0 - 2000, replay=True,
                                  raw={"lat": 31.2, "lon": 121.4})])
    j = get_sources(client)
    assert j["source_states"]["gnss"]["replay"] is True
    assert "回放数据，非真实上报" in j["answer"]


# ---------------------------------------------------------------- 端点与鉴权口径
def test_devices_endpoint_lists_devices(client):
    ingest(client, mac=MAC)
    ingest(client, mac="AA:BB:CC:DD:EE:FF", boot="b2")
    r = client.get("/api/v1/devices")
    assert r.status_code == 200
    body = r.json()
    macs = {d["device_mac"] for d in body["devices"]}
    assert macs == {MAC, "AA:BB:CC:DD:EE:FF"}
    assert body["devices"][0]["last_recv_ms"] >= body["devices"][1]["last_recv_ms"]


def test_source_status_readonly_even_with_control_token(client, monkeypatch):
    monkeypatch.setattr(app_module, "CONTROL_TOKEN", "sekrit")
    ingest(client)
    r = client.get("/api/v1/devices/%s/sources" % MAC)
    assert r.status_code == 200                     # 匿名只读
    j = client.post("/api/v1/assistant/chat", json={"text": "定位状态"}).json()
    assert j["intent"]["action"] == "source_status"
    assert j.get("ok") is True                      # 只读，不需要令牌


def test_invalid_source_event_rejected(client):
    t0 = int(time.time() * 1000)
    bad = {"source": "wifi", "state": "ok", "t_obs_ms": t0}
    r = client.post("/api/v1/ingest", json={
        "device_mac": MAC, "boot_id": "b", "ntp_synced": True,
        "readings": [{"seq": 0, "t_device_ms": t0}], "sources": [bad],
    })
    assert r.status_code == 422                     # 来源白名单拦截
    bad2 = {"source": "gnss", "state": "locked", "t_obs_ms": t0}
    r2 = client.post("/api/v1/ingest", json={
        "device_mac": MAC, "boot_id": "b", "ntp_synced": True,
        "readings": [{"seq": 0, "t_device_ms": t0}], "sources": [bad2],
    })
    assert r2.status_code == 422


def test_sources_absent_creates_no_events(client, conn):
    t0 = int(time.time() * 1000)
    seed_batch(client)
    n = conn.execute("SELECT COUNT(*) AS n FROM source_events").fetchone()["n"]
    assert n == 0