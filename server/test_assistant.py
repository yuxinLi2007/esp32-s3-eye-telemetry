"""第4周验证：自然语言入口的查询/采集分离、参数校验、设备范围、歧义与诚实性。

跑法： cd server && python -m pytest test_assistant.py -v

诚实性是本周最重要的验收点：任何一条回答都只由真实结果驱动，
"无设备完成证据时不得回复已采集成功"必须有直接断言。
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

MAC = "94:A9:90:1C:6F:D4"
MAC2 = "AA:BB:CC:DD:EE:FF"
BOOT = "BOOT0001"

REQ_ID_RE = re.compile(r"^req_[0-9a-z]{9}_[0-9a-f]{6}$")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DB_PATH", str(tmp_path / "t.db"))
    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture
def conn(client, tmp_path, monkeypatch):
    c = db.connect(str(tmp_path / "t.db"))
    yield c
    c.close()


def chat(client, text):
    return client.post("/api/v1/assistant/chat", json={"text": text})


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


def count_commands(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM commands").fetchone()["n"]


def claim_and_finish(client, rid, mac=MAC, boot=BOOT, n_device=None, with_ingest=True):
    """设备领取并把指令执行完。with_ingest=True 时把样本挂到指令上（服务端实收）。"""
    r = client.post("/api/v1/commands/claim", content="%s|%s|0.1.0" % (mac, boot))
    assert r.status_code == 200, r.text
    body = {"state": "done", "boot_id": boot, "result": {"ok": 1}}
    if n_device is not None:
        body["n_samples"] = n_device
    r = client.post("/api/v1/commands/%s/result" % rid, json=body)
    assert r.status_code == 200, r.text
    if with_ingest:
        r = seed_batch(client, mac=mac, boot=boot, seqs=(100, 101, 102), request_id=rid)
        assert r.status_code == 201, r.text


# ---------------------------------------------------------------- 查询 / 采集分离
def test_query_reads_data_and_does_not_create_command(client, conn):
    seed_batch(client)
    r = chat(client, "查看最新数据")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["intent"]["action"] == "query"
    assert MAC in j["answer"]
    assert "接收" in j["answer"]
    assert count_commands(conn) == 0          # 查询绝不下发指令


def test_collect_creates_capture_and_answer_never_claims_success(client, conn):
    seed_batch(client)
    r = chat(client, "重新采集一次")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["intent"]["action"] == "collect"
    assert "已采集成功" not in j["answer"]
    assert "已受理" in j["answer"]            # 指令刚下发只能答"已受理/等待执行"
    c = j["command"]
    assert c is not None and c["op"] == "capture"
    assert REQ_ID_RE.match(c["request_id"])
    assert c["state"] == "pending"
    assert count_commands(conn) == 1
    # 复查服务端那条指令，与回答里的 request_id 一致
    assert conn.execute("SELECT op FROM commands").fetchone()["op"] == "capture"


# ---------------------------------------------------------------- 诚实性
def test_success_reported_only_with_server_received_samples(client):
    seed_batch(client)
    rid = chat(client, "重新采集").json()["command"]["request_id"]
    claim_and_finish(client, rid, n_device=5, with_ingest=True)   # 服务端实收 3 条
    r = chat(client, "上次采集结果怎么样")
    assert r.status_code == 200, r.text
    ans = r.json()["answer"]
    assert "采集完成" in ans and "实收 3 条" in ans
    assert "已采集成功" not in ans            # 措辞是"实收 N 条"，不是空口"成功"


def test_done_without_samples_is_not_success(client):
    seed_batch(client)
    rid = chat(client, "重新采集").json()["command"]["request_id"]
    claim_and_finish(client, rid, n_device=5, with_ingest=False)  # 设备自报 5 条，服务端 0 条
    r = chat(client, "采集结果")
    ans = r.json()["answer"]
    assert "采集完成" not in ans
    assert "服务端没有收到任何新样本" in ans


def test_pending_status_is_honest(client):
    seed_batch(client)
    chat(client, "重新采集")
    r = chat(client, "完成没有")
    assert "等待设备领取" in r.json()["answer"]
    assert "成功" not in r.json()["answer"]


# ---------------------------------------------------------------- 参数校验
def test_params_out_of_range_rejected(client, conn):
    seed_batch(client)
    r = chat(client, "重新采集 1000 个")      # n=1000 超过上限 200
    assert r.status_code == 200, r.text
    ans = r.json()["answer"]
    assert "参数无法执行" in ans
    assert count_commands(conn) == 0          # 校验不通过，一条指令都不许发


# ---------------------------------------------------------------- 设备范围
def test_unknown_device_refused(client, conn):
    seed_batch(client)                         # 系统只见过 MAC
    r = chat(client, "对 AA:BB:CC:DD:EE:FF 重新采集")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["intent"]["action"] == "refuse"
    assert "不在已知设备" in j["answer"]
    assert count_commands(conn) == 0


def test_multi_device_ambiguous(client, conn):
    seed_batch(client, mac=MAC)
    seed_batch(client, mac=MAC2)
    r = chat(client, "重新采集")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["intent"]["action"] == "clarify"
    assert MAC in j["answer"] and MAC2 in j["answer"]
    assert count_commands(conn) == 0          # 歧义时不猜测、不默认、不随便选


# ---------------------------------------------------------------- 歧义处理
def test_mixed_intent_clarify(client, conn):
    seed_batch(client)
    r = chat(client, "查看数据并重新采集")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["intent"]["action"] == "clarify"
    assert count_commands(conn) == 0


def test_nonsense_clarify(client, conn):
    seed_batch(client)
    r = chat(client, "今天天气怎么样")
    assert r.status_code == 200, r.text
    assert r.json()["intent"]["action"] == "clarify"
    assert count_commands(conn) == 0


# ---------------------------------------------------------------- 语言模型路径
def test_llm_structured_json_used(client, conn, monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://fake.invalid")
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_MODEL", "m")
    monkeypatch.setattr(assistant, "_ask_llm",
                        lambda text: {"action": "collect", "device_mac": None,
                                      "n": 60, "interval_ms": 100})
    seed_batch(client)
    r = chat(client, "帮我测一下")
    j = r.json()
    assert j["intent"]["action"] == "collect"
    params = j["command"]["params"]
    assert params["n"] == 60 and params["interval_ms"] == 100


def test_llm_failure_falls_back_to_rules(client, monkeypatch):
    def boom(_text):
        raise RuntimeError("llm down")
    monkeypatch.setattr(assistant, "_ask_llm", boom)
    seed_batch(client)
    r = chat(client, "查看数据")               # 模型挂了也必须能答
    assert r.status_code == 200, r.text
    assert r.json()["intent"]["action"] == "query"


def test_llm_opposite_to_keywords_is_overruled(client, conn, monkeypatch):
    """安全护栏：词面明确"查看"时，模型猜成 collect 也不许发指令。"""
    monkeypatch.setattr(assistant, "_ask_llm",
                        lambda text: {"action": "collect"})
    seed_batch(client)
    r = chat(client, "查看上次数据")
    j = r.json()
    assert j["intent"]["action"] == "query"
    assert count_commands(conn) == 0


# ---------------------------------------------------------------- 鉴权 / 陈旧数据
def test_collect_requires_control_token_when_set(client, monkeypatch):
    monkeypatch.setattr(app_module, "CONTROL_TOKEN", "s3cret")
    seed_batch(client)
    assert chat(client, "重新采集").status_code == 401          # 没令牌：不发指令
    assert chat(client, "重新采集").status_code == 401          # 令牌错误也一样
    r = chat(client, "重新采集")
    assert r.status_code == 401
    r = client.post("/api/v1/assistant/chat", json={"text": "重新采集"},
                    headers={"X-Control-Token": "s3cret"})
    assert r.status_code == 200, r.text
    assert r.json()["command"] is not None


def test_query_works_without_token_even_when_set(client, monkeypatch):
    monkeypatch.setattr(app_module, "CONTROL_TOKEN", "s3cret")
    seed_batch(client)
    r = chat(client, "查看数据")               # 查询不改设备行为，无需令牌
    assert r.status_code == 200, r.text
    assert r.json()["intent"]["action"] == "query"


def test_stale_data_not_current(client):
    # 直接把一条 5 分钟前的批次写进库，回答必须说明"不能当作当前状态"
    conn = db.connect(str(Path(app_module.DB_PATH)))
    old = int(time.time() * 1000) - 5 * 60 * 1000
    db.insert_batch(conn, {
        "device_mac": MAC, "boot_id": "OLD0001", "fw_version": "0.1.0",
        "seq_first": 0, "seq_last": 2, "dropped_since_last": 0,
        "ntp_synced": True, "ntp_sync_age_s": 10,
        "t_device_ntp_ms": old - 800, "t_server_recv_ms": old,
        "source_ip": "127.0.0.1", "source_ua": "test",
        "readings": [
            {"seq": i, "t_device_ms": old + i * 100,
             "ax": 0.0, "ay": 0.0, "az": 1.0, "spl_db": 40.0}
            for i in range(3)
        ],
    })
    conn.close()
    r = chat(client, "查看数据")
    ans = r.json()["answer"]
    assert "不能当作当前状态" in ans
    assert "分钟" in ans                       # 必须说出有多旧