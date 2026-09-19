"""服务端验证：入库、查询、可信度分级、缺口检测、失败计数可见性。

跑法： cd server && python -m pytest test_ingest.py -v
"""

import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
import app as app_module  # noqa: E402
import db  # noqa: E402

MAC = "94:A9:90:1C:6F:D4"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DB_PATH", str(tmp_path / "t.db"))
    with TestClient(app_module.app) as c:
        yield c


def batch(boot_id="boot-aaa", seqs=(0, 1, 2), ntp=True, age=10,
          dropped=0, t0=1000, step=100):
    return {
        "device_mac": MAC,
        "boot_id": boot_id,
        "fw_version": "0.1.0",
        "ntp_synced": ntp,
        "ntp_sync_age_s": age if ntp else None,
        "t_device_ntp_ms": int(time.time() * 1000) if ntp else None,
        "dropped_since_last": dropped,
        "readings": [
            {
                "seq": s,
                "t_device_ms": t0 + i * step,
                "ax": 0.01 * i,
                "ay": -0.02 * i,
                "az": 1.0,
                "spl_db": 40.0 + i,
            }
            for i, s in enumerate(seqs)
        ],
    }


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_ingest_then_query_roundtrip(client):
    r = client.post("/api/v1/ingest", json=batch(seqs=(0, 1, 2)))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["n_readings"] == 3
    assert body["batch_id"] == 1

    q = client.get("/api/v1/readings").json()
    assert q["count"] == 3
    assert [x["seq"] for x in q["readings"]] == [0, 1, 2]
    assert q["readings"][0]["ax"] == 0.0
    assert q["readings"][2]["spl_db"] == 42.0
    assert q["gaps"] == []
    # 溯源信息必须随样本一起返回
    assert q["readings"][0]["device_mac"] == MAC
    assert q["readings"][0]["source_ip"] == "testclient"
    assert q["readings"][0]["boot_id"] == "boot-aaa"


def test_trust_fresh_ntp(client):
    client.post("/api/v1/ingest", json=batch(ntp=True, age=10))
    q = client.get("/api/v1/readings").json()
    assert q["trust"]["latest"]["t_trust"] == "ntp_fresh"
    assert q["trust"]["latest"]["ntp_synced"] is True
    assert q["trust"]["counts"] == {"ntp_fresh": 3}
    # 设备时间 - 服务端时间，偏差应该很小（两者都取的真实 now）
    assert abs(q["trust"]["latest"]["device_clock_skew_ms"]) < 5000


def test_trust_stale_and_expired_ntp(client):
    client.post("/api/v1/ingest", json=batch(seqs=(0,), age=1800))
    assert client.get("/api/v1/readings").json()["trust"]["latest"]["t_trust"] == "ntp_stale"
    client.post("/api/v1/ingest", json=batch(boot_id="b2", seqs=(0,), age=7200))
    assert client.get("/api/v1/readings").json()["trust"]["latest"]["t_trust"] == "ntp_expired"


def test_trust_unsynced_is_labelled(client):
    client.post("/api/v1/ingest", json=batch(ntp=False))
    q = client.get("/api/v1/readings").json()
    assert q["trust"]["latest"]["t_trust"] == "unsynced"
    assert q["trust"]["latest"]["device_clock_skew_ms"] is None
    assert q["trust"]["counts"] == {"unsynced": 3}
    assert client.get("/api/v1/status").json()["last_batch"]["t_trust"] == "unsynced"


def test_trust_counts_survive_mixed_window(client):
    """同一窗口内混有已同步/未同步批次时，必须分别计数而不是只报最后一条。"""
    client.post("/api/v1/ingest", json=batch(seqs=(0, 1), ntp=True, age=5))
    client.post("/api/v1/ingest", json=batch(seqs=(2,), ntp=False, t0=5000))
    q = client.get("/api/v1/readings").json()
    assert q["trust"]["counts"] == {"ntp_fresh": 2, "unsynced": 1}
    assert q["trust"]["latest"]["t_trust"] == "unsynced"


def test_gap_detection_within_same_boot(client):
    client.post("/api/v1/ingest", json=batch(seqs=(0, 1, 2)))
    # 模拟中间丢了 7 条（seq 3..9 没到）
    client.post("/api/v1/ingest", json=batch(seqs=(10, 11), t0=5000))
    q = client.get("/api/v1/readings").json()
    assert q["count"] == 5
    assert len(q["gaps"]) == 1
    g = q["gaps"][0]
    assert g["after_seq"] == 2 and g["before_seq"] == 10
    assert g["missing"] == 7
    assert g["boot_id"] == "boot-aaa"


def test_reboot_does_not_count_as_gap(client):
    client.post("/api/v1/ingest", json=batch(boot_id="boot-1", seqs=(0, 1, 2)))
    client.post("/api/v1/ingest", json=batch(boot_id="boot-2", seqs=(0, 1)))
    q = client.get("/api/v1/readings").json()
    assert q["count"] == 5
    assert q["gaps"] == [], "跨 boot_id 是重启，不应被当成丢数据"
    st = client.get("/api/v1/status").json()
    assert st["total_boots"] == 2


def test_dropped_counter_surfaces(client):
    client.post("/api/v1/ingest", json=batch(seqs=(0,), dropped=0))
    client.post("/api/v1/ingest", json=batch(seqs=(1,), dropped=3, t0=2000))
    client.post("/api/v1/ingest", json=batch(seqs=(2,), dropped=2, t0=3000))

    st = client.get("/api/v1/status").json()
    assert st["total_dropped"] == 5, "本机累计丢失必须可见"
    assert st["last_batch"]["dropped_since_last"] == 2

    q = client.get("/api/v1/readings").json()
    assert q["readings"][1]["dropped_since_last"] == 3


def test_batches_endpoint_gives_provenance(client):
    client.post("/api/v1/ingest", json=batch(seqs=(0, 1)))
    b = client.get("/api/v1/batches").json()
    assert b["count"] == 1
    row = b["batches"][0]
    assert row["seq_first"] == 0 and row["seq_last"] == 1
    assert row["n_readings"] == 2
    assert row["source_ip"] == "testclient"
    assert row["t_server_recv_ms"] > 0


def test_status_reports_liveness(client):
    client.post("/api/v1/ingest", json=batch(seqs=(0, 1, 2)))
    st = client.get("/api/v1/status").json()
    assert st["total_batches"] == 1
    assert st["total_readings"] == 3
    assert st["readings_last_60s"] == 3
    assert st["last_batch"]["age_ms"] < 10_000
    assert st["last_batch"]["n_readings"] == 3


def test_empty_batch_rejected(client):
    b = batch(seqs=())
    r = client.post("/api/v1/ingest", json=b)
    assert r.status_code == 422


def test_seq_range_derived_server_side(client):
    """客户端谎报 seq 范围不影响服务端推导出的真实范围。"""
    b = batch(seqs=(5, 6, 7))
    b["seq_first"] = 0
    b["seq_last"] = 999
    r = client.post("/api/v1/ingest", json=b)
    assert r.status_code == 201, "多余字段应被忽略而不是报错"
    row = client.get("/api/v1/batches").json()["batches"][0]
    assert row["seq_first"] == 5 and row["seq_last"] == 7


def test_trust_level_pure_function():
    assert db.trust_level(False, None) == "unsynced"
    assert db.trust_level(True, 0) == "ntp_fresh"
    assert db.trust_level(True, 300) == "ntp_fresh"
    assert db.trust_level(True, 301) == "ntp_stale"
    assert db.trust_level(True, 3600) == "ntp_stale"
    assert db.trust_level(True, 3601) == "ntp_expired"


# ---- 入库鉴权 ----
# 没有鉴权时，任何能访问该端口的人都能以任意 MAC 注入数据，"来源可追溯"就是空话。

@pytest.fixture
def auth_client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DB_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setattr(app_module, "INGEST_TOKEN", "s3cr3t-token")
    with TestClient(app_module.app) as c:
        yield c


def test_ingest_open_when_token_unset(client, monkeypatch):
    """未配置令牌时不拦截：本地开发与现有行为保持一致。"""
    monkeypatch.setattr(app_module, "INGEST_TOKEN", None)
    assert client.post("/api/v1/ingest", json=batch()).status_code == 201


def test_ingest_rejects_missing_token(auth_client):
    r = auth_client.post("/api/v1/ingest", json=batch())
    assert r.status_code == 401
    assert auth_client.get("/api/v1/status").json()["total_readings"] == 0, \
        "被拒的请求不能留下任何数据"


def test_ingest_rejects_wrong_token(auth_client):
    r = auth_client.post("/api/v1/ingest", json=batch(),
                         headers={"X-Ingest-Token": "s3cr3t-tokeX"})
    assert r.status_code == 401
    assert auth_client.get("/api/v1/status").json()["total_readings"] == 0


def test_ingest_accepts_correct_token(auth_client):
    r = auth_client.post("/api/v1/ingest", json=batch(),
                         headers={"X-Ingest-Token": "s3cr3t-token"})
    assert r.status_code == 201
    assert auth_client.get("/api/v1/status").json()["total_readings"] == 3


def test_read_endpoints_stay_public(auth_client):
    """鉴权只保护写入。读接口公开，否则界面会在浏览器里被拦下。"""
    assert auth_client.get("/api/v1/readings").status_code == 200
    assert auth_client.get("/api/v1/status").status_code == 200


# ---- 采集开关 ----
# 停止后界面会安静下来，和"设备失联"长得一模一样。所以开关的历史必须留下来，
# 且开关本身要能鉴权——否则外人可以远程把采集停掉，受害者完全察觉不到。

@pytest.fixture
def control_client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DB_PATH", str(tmp_path / "ctl.db"))
    monkeypatch.setattr(app_module, "CONTROL_TOKEN", None)
    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture
def control_auth_client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DB_PATH", str(tmp_path / "ctlauth.db"))
    monkeypatch.setattr(app_module, "CONTROL_TOKEN", "ctl-s3cr3t")
    with TestClient(app_module.app) as c:
        yield c


def test_control_defaults_to_collecting(control_client):
    """旧库升级上来没有控制记录，必须按"照常采集"处理，不能凭空停机。"""
    d = control_client.get("/api/v1/control").json()
    assert d["collect"] is True
    assert d["t_server_ms"] is None
    assert d["history"] == []
    assert control_client.get("/api/v1/control/state").text == "1"


def test_control_stop_and_start_roundtrip(control_client):
    r = control_client.post("/api/v1/control", json={"collect": False, "note": "手动停止"})
    assert r.status_code == 200
    d = r.json()
    assert d["collect"] is False
    assert d["note"] == "手动停止"
    assert d["changed_ago_ms"] < 10_000
    assert control_client.get("/api/v1/control/state").text == "0"

    d = control_client.post("/api/v1/control", json={"collect": True}).json()
    assert d["collect"] is True
    assert control_client.get("/api/v1/control/state").text == "1"


def test_control_state_is_bare_plaintext(control_client):
    """设备端按纯文本解析，多一个换行或 JSON 引号都会让固件认不出来。"""
    r = control_client.get("/api/v1/control/state")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert r.text == "1"

    control_client.post("/api/v1/control", json={"collect": False})
    r = control_client.get("/api/v1/control/state")
    assert r.text == "0"
    assert r.text in ("0", "1")


def test_control_repeat_is_not_logged(control_client):
    """状态没变就不该写历史。否则设备每 2 秒轮询一次也会塞满历史，
    "变更历史"就不再是变更历史，界面也就无法据此解释空白。"""
    control_client.post("/api/v1/control", json={"collect": False})
    control_client.post("/api/v1/control", json={"collect": False})
    control_client.post("/api/v1/control", json={"collect": False})
    assert len(control_client.get("/api/v1/control").json()["history"]) == 1


def test_control_history_explains_blank_periods(control_client):
    """历史要给界面提供"停止→恢复"的时间区间。"""
    control_client.post("/api/v1/control", json={"collect": False, "note": "停一下"})
    control_client.post("/api/v1/control", json={"collect": True, "note": "继续"})
    control_client.post("/api/v1/control", json={"collect": False, "note": "再停"})

    h = control_client.get("/api/v1/control").json()["history"]
    assert [x["collect"] for x in h] == [False, True, False], "应按时间升序返回"
    assert [x["note"] for x in h] == ["停一下", "继续", "再停"]
    assert all(x["t_server_ms"] > 0 for x in h)
    assert h[0]["t_server_ms"] <= h[1]["t_server_ms"] <= h[2]["t_server_ms"]


def test_control_rejects_bad_token(control_auth_client):
    r = control_auth_client.post("/api/v1/control", json={"collect": False})
    assert r.status_code == 401
    assert control_auth_client.get("/api/v1/control/state").text == "1", \
        "被拒的请求不能真的把采集停掉"

    r = control_auth_client.post("/api/v1/control", json={"collect": False},
                                 headers={"X-Control-Token": "ctl-s3cr3X"})
    assert r.status_code == 401
    assert control_auth_client.get("/api/v1/control/state").text == "1"


def test_control_accepts_correct_token(control_auth_client):
    r = control_auth_client.post("/api/v1/control", json={"collect": False},
                                 headers={"X-Control-Token": "ctl-s3cr3t"})
    assert r.status_code == 200
    assert control_auth_client.get("/api/v1/control/state").text == "0"


def test_control_state_readable_without_token(control_auth_client):
    """设备轮询接口刻意不鉴权：板子把"取不到"当成"照常采集"，
    配错令牌会让它静默不停采——那种失败在界面上看不出来。"""
    assert control_auth_client.get("/api/v1/control/state").status_code == 200
    assert control_auth_client.get("/api/v1/control").status_code == 200
