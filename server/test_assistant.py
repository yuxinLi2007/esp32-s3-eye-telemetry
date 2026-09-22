"""第4周验证：自然语言助手 —— 意图翻译、两个动作分流、越界与异常路径。

跑法： cd server && python -m pytest test_assistant.py -v

两组必须通过的测试（对应本周任务书）：
  1. 新旧数据测试：同一份旧数据，"查看上次"必须保留旧 t_server_recv_ms；
     "重新采集"必须等到一条**新**样本（t_server_recv_ms 晚于指令下发）。
  2. 异常指令测试：越界设备、含糊指令、旧数据查询、设备无响应、模型出错，
     全部返回结构化 ok=false + error.code，进程不许崩。

测试全程不开网络、不需要 OPENAI_API_KEY：
默认 engine=rules 或给 parse_llm 打桩，模型是"被注入的依赖"而不是测试前提。
"""

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
OTHER = "AA:BB:CC:DD:EE:FF"
BOOT = "BOOTAAAA"


# ------------------------------------------------------------------ fixtures
@pytest.fixture
def dbpath(tmp_path):
    return str(tmp_path / "assistant.db")


@pytest.fixture
def client(dbpath, monkeypatch):
    monkeypatch.setattr(app_module, "DB_PATH", dbpath)
    monkeypatch.delenv("DEVICE_ALLOWLIST", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture
def conn(client, dbpath):
    c = db.connect(dbpath)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _reset_hook():
    yield
    assistant._TEST_POLL_HOOK = None


def ingest(client, *, rid=None, seqs=(0, 1, 2), boot=BOOT, mac=MAC, t0=1_000_000,
           ntp_synced=True, age=12, headers=None):
    body = {
        "device_mac": mac, "boot_id": boot, "fw_version": "0.3.1",
        "ntp_synced": ntp_synced, "ntp_sync_age_s": age,
        "t_device_ntp_ms": int(time.time() * 1000), "dropped_since_last": 0,
        "readings": [
            {"seq": s, "t_device_ms": t0 + i * 50, "ax": 0.01 * i, "ay": 0.02,
             "az": 1.0, "spl_db": 50.0 + i}
            for i, s in enumerate(seqs)
        ],
    }
    if rid is not None:
        body["request_id"] = rid
    return client.post("/api/v1/ingest", json=body, headers=headers or {})


def ask(client, text, **kw):
    return client.post("/api/v1/assistant/ask", json={"text": text, **kw})


def claim(client, mac=MAC, boot=BOOT, fw="0.3.1"):
    return client.post("/api/v1/commands/claim", content="%s|%s|%s" % (mac, boot, fw))


def post_result(client, rid, state, **kw):
    body = {"state": state, "boot_id": BOOT, "device_mac": MAC}
    body.update(kw)
    return client.post("/api/v1/commands/%s/result" % rid, json=body)


# ================================================================== 1. 新旧数据
def test_query_history_keeps_old_timestamp(client):
    """问"查看上次"：必须读旧数据，且 t_server_recv_ms 原样保留，绝不下发指令。"""
    assert ingest(client, seqs=(0, 1, 2), t0=500_000).status_code == 201
    with db.connect(app_module.DB_PATH) as _c:
        old_ms = _c.execute("SELECT t_server_recv_ms FROM batches ORDER BY id DESC"
                            ).fetchone()["t_server_recv_ms"]

    r = ask(client, "帮我查看上次的数据")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True
    assert j["intent"] == "query_history"
    assert j["action"]["tool"] == "readings"
    assert j["data"]["is_new_sample"] is False
    assert j["data"]["count"] == 1            # "上次" = 最新那一条
    assert j["data"]["latest"]["t_server_recv_ms"] == old_ms
    # 只读动作：库里不许冒出任何指令
    rows = db.connect(app_module.DB_PATH).execute("SELECT COUNT(*) n FROM commands").fetchone()
    assert rows["n"] == 0


def test_query_history_no_data_is_structured_not_crash(client):
    """旧数据查询：设备已知但库里一条样本都没有 -> no_data，而不是 500，也不是假数据。"""
    # 造一个"设备被见过、但 readings 为空"的库：这是"查旧数据，但没有旧数据"的真实情形
    assert ingest(client).status_code == 201
    with db.connect(app_module.DB_PATH) as c:
        c.execute("DELETE FROM readings")
        c.commit()

    r = ask(client, "看看历史数据")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is False
    assert j["intent"] == "query_history"
    assert j["data"]["count"] == 0
    assert j["error"]["code"] == "no_data"


def test_query_on_empty_db_is_unknown_device(client):
    """全新库：连设备都没见过 -> unknown_device，结构化提示，不崩。"""
    r = ask(client, "看看历史数据")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is False
    assert j["intent"] == "reject"
    assert j["error"]["code"] == "unknown_device"


def test_request_capture_waits_for_new_sample(client):
    """问"重新采集"：必须下发 capture、等设备回执，再拿到一条**新**样本。

    测试用 _TEST_POLL_HOOK 扮演设备：首次轮询领取，第二次回执并入库。
    这样"等新结果"是可确定性断言的，不靠 sleep 撞运气。
    """
    ingest(client, seqs=(0, 1, 2), t0=500_000)
    with db.connect(app_module.DB_PATH) as c:
        old_ms = c.execute("SELECT MAX(t_server_recv_ms) m FROM batches").fetchone()["m"]

    state = {"rides": 0}

    def hook(conn, rid, attempt):
        if state["rides"] == 0:
            r = claim(client)
            assert r.status_code == 200
            assert r.text != "none"
            assert r.text.split("|")[0] == rid
            assert r.text.split("|")[1] == "capture"
            state["rides"] = 1
        elif state["rides"] == 1:
            rows = [{"seq": i, "t_device_ms": 900_000 + i * 50, "ax": 0.5,
                     "ay": 0.0, "az": 1.0, "spl_db": 60.0 + i} for i in range(5)]
            r = client.post("/api/v1/ingest", json={
                "device_mac": MAC, "boot_id": BOOT, "fw_version": "0.3.1",
                "request_id": rid, "ntp_synced": True, "ntp_sync_age_s": 5,
                "t_device_ntp_ms": int(time.time() * 1000), "dropped_since_last": 0,
                "readings": rows,
            })
            assert r.status_code == 201, r.text
            batch_id = r.json()["batch_id"]
            rr = post_result(client, rid, "done", samples_batch_id=batch_id,
                             n_samples=5, progress=100)
            assert rr.status_code == 200, rr.text
            state["rides"] = 2

    assistant._TEST_POLL_HOOK = hook
    r = ask(client, "帮我重新采集一次", wait_ms=5000)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True, j.text
    assert j["intent"] == "request_capture"
    assert j["action"]["endpoint"] == "/api/v1/commands"
    assert j["action"]["op"] == "capture"
    assert j["action"]["state"] == "done"
    assert j["data"]["is_new_sample"] is True
    assert j["data"]["count"] == 5
    assert j["data"]["latest"]["t_server_recv_ms"] > old_ms
    assert j["data"]["latest"]["request_id"] == j["action"]["request_id"]


def test_new_vs_old_are_distinct_actions(client):
    """"查看上次"与"重新采集"必须映射到两个不同的接口，不能混。"""
    ingest(client)
    q = ask(client, "查看上次数据").json()
    c = ask(client, "重新采集一次", wait_ms=0).json()
    assert q["intent"] == "query_history" and q["action"]["method"] == "GET"
    assert c["intent"] == "request_capture" and c["action"]["method"] == "POST"
    assert q["action"]["endpoint"] != c["action"]["endpoint"]


# ================================================================== 2. 异常指令
def test_forbidden_device_is_rejected_and_no_command_created(client):
    """越界：控制别人的设备 -> 结构化 forbidden_device，且指令表里一行都不能多。"""
    ingest(client, mac=MAC)
    r = ask(client, "把设备 %s 重新采集一次" % OTHER)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is False
    assert j["intent"] == "reject"
    assert j["error"]["code"] == "forbidden_device"
    assert j["error"]["level"] == "warn"
    assert j["error"]["requested_device"] == OTHER
    n = db.connect(app_module.DB_PATH).execute(
        "SELECT COUNT(*) n FROM commands").fetchone()["n"]
    assert n == 0


def test_forbidden_cross_device_query_also_rejected(client):
    """越界不区分读/写：点名别人的设备，查询也拒绝（白名单是硬边界）。"""
    ingest(client, mac=MAC)
    r = ask(client, "查一下 %s 最近的数据" % OTHER)
    j = r.json()
    assert j["ok"] is False and j["error"]["code"] == "forbidden_device"


def test_ambiguous_request_asks_for_clarification(client):
    """含糊：'帮我弄一下' -> clarify，且不产生任何指令/数据变化。"""
    ingest(client)
    r = ask(client, "帮我弄一下")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is False
    assert j["intent"] == "clarify"
    assert j["error"]["code"] == "ambiguous_request"
    assert j["error"]["level"] == "info"
    assert j["action"] is None and j["data"] is None
    n = db.connect(app_module.DB_PATH).execute(
        "SELECT COUNT(*) n FROM commands").fetchone()["n"]
    assert n == 0


def test_bare_capture_word_asks_instead_of_guessing(client):
    """只说了"采集"：两种解释都对，必须澄清，绝不能默认执行。"""
    ingest(client)
    j = ask(client, "采集").json()
    assert j["intent"] == "clarify"
    assert j["error"]["code"] == "ambiguous_request"
    n = db.connect(app_module.DB_PATH).execute(
        "SELECT COUNT(*) n FROM commands").fetchone()["n"]
    assert n == 0


def test_unsupported_write_action_rejected(client):
    """白名单外的写操作（停采集/重启）-> reject，绝不放行到命令通道。"""
    ingest(client)
    for text in ["帮我停止采集", "重启一下设备", "把数据清空"]:
        j = ask(client, text).json()
        assert j["ok"] is False, text
        assert j["intent"] == "reject", text
        assert j["error"]["code"] in ("unsupported_action", "forbidden_device"), text
    n = db.connect(app_module.DB_PATH).execute(
        "SELECT COUNT(*) n FROM commands").fetchone()["n"]
    assert n == 0


def test_device_unreachable(client):
    """设备无响应（超期未领取）-> device_unreachable，结构化且不崩。

    用 wait_ms=0 触发"窗口立即用完"，再直接把指令判过期，
    模拟"板子离线，指令没人领"。
    """
    ingest(client)
    j = ask(client, "重新采集一次", wait_ms=0).json()
    assert j["ok"] is False
    assert j["intent"] == "request_capture"
    assert j["error"]["code"] in ("device_no_response", "device_unreachable")
    rid = j["action"]["request_id"]

    # 把 ttl 推到过去，下一次读接口就会惰性结算成 expired
    with db.connect(app_module.DB_PATH) as c:
        c.execute("UPDATE commands SET t_created_ms=t_created_ms-? WHERE request_id=?",
                  (commands.OPS["capture"]["ttl_ms"] + 1000, rid))
        c.commit()
        row = commands.get_command(c, rid)
    assert row["state"] == "expired"

    j2 = ask(client, "重新采集一次", wait_ms=0).json()
    assert j2["ok"] is False
    assert j2["error"]["code"] in ("device_no_response", "too_many_live")
    assert j2["action"] is not None


def test_device_timeout_after_claim(client):
    """设备领了却一直不回 -> timeout，结构化 device_timeout。"""
    ingest(client)

    def hook(conn, rid, attempt):
        if attempt == 0:
            claim(client)
        else:
            with db.connect(app_module.DB_PATH) as c:
                c.execute("UPDATE commands SET t_claimed_ms=t_claimed_ms-? WHERE request_id=?",
                          (commands.OPS["capture"]["timeout_ms"] + 5000, rid))
                c.commit()

    assistant._TEST_POLL_HOOK = hook
    j = ask(client, "重新采集一次", wait_ms=3000).json()
    assert j["ok"] is False
    assert j["error"]["code"] in ("device_timeout", "device_no_response")
    assert j["action"]["request_id"].startswith("req_")


def test_llm_failure_degrades_to_rules(client, monkeypatch):
    """模型不可用：降级到规则引擎，功能不变，且把失败原因如实写进 trace。"""
    ingest(client)

    def boom(*a, **k):
        raise RuntimeError("连接被重置")

    monkeypatch.setattr(assistant, "parse_llm", boom)
    r = ask(client, "查看上次数据", engine="auto")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True
    assert j["intent"] == "query_history"
    assert j["trace"]["engine_used"] == "rules"


def test_llm_hallucinated_device_cannot_write(client, monkeypatch):
    """模型越权：返回白名单外的设备 MAC，也必须在落地前被拒。"""
    ingest(client)

    def evil(text, *, devices, default_mac):
        return ({"intent": "request_capture", "device_mac": OTHER, "slots": {},
                 "confidence": 0.99, "error": None, "notes": [],
                 "llm_reason": "模型开始乱编"}, None)

    monkeypatch.setattr(assistant, "parse_llm", evil)
    j = ask(client, "重新采集一次", engine="llm").json()
    assert j["ok"] is False
    assert j["intent"] == "reject"
    assert j["error"]["code"] == "forbidden_device"
    n = db.connect(app_module.DB_PATH).execute(
        "SELECT COUNT(*) n FROM commands").fetchone()["n"]
    assert n == 0


def test_llm_vs_rules_conflict_forces_clarification(client, monkeypatch):
    """模型与规则给出相反动作 -> 强制澄清，绝不让任一方替用户拍板。"""
    ingest(client)

    def flip(text, *, devices, default_mac):
        return ({"intent": "request_capture", "device_mac": MAC, "slots": {},
                 "confidence": 0.99, "error": None, "notes": [], "llm_reason": "翻转"}, None)

    monkeypatch.setattr(assistant, "parse_llm", flip)
    j = ask(client, "查看上次的数据", engine="auto").json()
    assert j["ok"] is False
    assert j["intent"] == "clarify"
    assert j["error"]["code"] == "ambiguous_request"


def test_llm_invalid_json_degrades(client, monkeypatch):
    """模型返回非法 JSON/结构：parse_llm 返回 None + 原因，规则兜底。"""
    monkeypatch.setattr(assistant, "parse_llm", lambda *a, **k: (None, "JSONDecodeError: 垃圾输出"))
    ingest(client)
    j = ask(client, "重新采集一次", wait_ms=0, engine="auto").json()
    assert j["intent"] == "request_capture"
    assert j["trace"]["engine_used"] == "rules"


def test_no_known_device(client):
    """白名单为空 -> unknown_device，结构化提示，不崩。"""
    j = ask(client, "查看上次数据").json()
    assert j["ok"] is False
    assert j["intent"] == "reject" or j["intent"] == "clarify"
    assert j["error"]["code"] in ("unknown_device", "need_device")


def test_need_device_when_multiple(client):
    """多台设备且没点名 -> need_device，要求澄清。"""
    ingest(client, mac=MAC, boot="BOOTAAAA")
    ingest(client, mac="11:22:33:44:55:66", boot="BOOTBBBB")
    j = ask(client, "查看一下数据").json()
    assert j["ok"] is False
    assert j["error"]["code"] == "need_device"
    assert len(j["error"]["allowed_devices"]) == 2


def test_allowlist_env_is_authoritative(client, monkeypatch):
    """DEVICE_ALLOWLIST 一旦设置就是权威白名单，库里见过别的设备也不算。"""
    ingest(client, mac=MAC)
    monkeypatch.setenv("DEVICE_ALLOWLIST", OTHER)
    # 白名单只认 OTHER：助手只会对 OTHER 说话，MAC 这点不能被库里的历史带偏
    j = ask(client, "查看一下数据").json()
    # 唯一白名单设备是 OTHER，所以助手解析出来的目标只能是 OTHER（不是历史里的 MAC）
    assert j["device_mac"] == OTHER
    assert j["error"]["code"] == "no_data"
    # 而点名 MAC（已被白名单排除）必须拒绝
    j2 = ask(client, "把设备 %s 采集一次" % MAC).json()
    assert j2["ok"] is False
    assert j2["intent"] == "reject"
    assert j2["error"]["code"] == "forbidden_device"


def test_explicit_device_mac_allowed(client):
    """显式指定白名单内的设备：允许。"""
    ingest(client, mac=MAC)
    j = ask(client, "查看上次数据", device_mac=MAC).json()
    assert j["ok"] is True
    assert j["device_mac"] == MAC


def test_explicit_device_mac_bad_format(client):
    """device_mac 格式非法 -> 结构化错误，不崩。"""
    ingest(client, mac=MAC)
    j = ask(client, "查看上次数据", device_mac="not-a-mac").json()
    assert j["ok"] is False
    assert j["error"]["code"] == "forbidden_device"


# ================================================================== 参数夹取
def test_capture_params_clamped_and_reported():
    """越界参数（500 次）应夹进 OPS 范围，并把调整写进 adjustments。"""
    p, adj = assistant.fit_capture_params({"n": 500})
    assert p["n"] == commands.OPS["capture"]["params"]["n"]["hi"]
    assert adj and "n=500" in adj[0]
    p2, adj2 = assistant.fit_capture_params({"n": 500, "interval_ms": 1000})
    assert p2["n"] * p2["interval_ms"] <= commands.MAX_CAPTURE_DURATION_MS


def test_capture_params_from_language(client):
    """整句里带参数：'重新采集 40 次，间隔 50 毫秒' 要抽到 n/interval_ms。"""
    ingest(client)

    def hook(conn, rid, attempt):
        with db.connect(app_module.DB_PATH) as c:
            row = commands.get_row(c, rid)
            assert assistant.fit_capture_params({})[0]["n"] > 0
            c.execute("UPDATE commands SET state='done', t_finished_ms=?, error_code=NULL"
                      " WHERE request_id=?", (int(time.time() * 1000), rid))
            c.commit()

    assistant._TEST_POLL_HOOK = hook
    j = ask(client, "重新采集 40 次，间隔 50 毫秒", wait_ms=3000).json()
    # 设备被钩子直接判 done 但没有样本，会走 device_failed；关键是参数必须被抽到
    assert j["action"]["params"]["n"] == 40
    assert j["action"]["params"]["interval_ms"] == 50


# ================================================================== 端点契约
def test_assistant_info(client):
    ingest(client)
    j = client.get("/api/v1/assistant/info").json()
    assert j["ok"] is True
    assert {a["intent"] for a in j["actions"]} == {"query_history", "request_capture"}
    assert j["llm"]["enabled"] is False or j["llm"]["configured"] is False
    assert MAC in j["allowed_devices"]
    assert any(e["code"] == "forbidden_device" for e in j["error_codes"])


def test_ask_always_200_and_same_envelope(client):
    """无论什么输入都返回 200 + 统一信封，前端不必处理协议级错误。"""
    ingest(client)
    for text in ["", "???", "帮我弄一下", "查看上次", "重启设备",
                 "把设备 %s 采集一次" % OTHER, "a" * 500]:
        r = ask(client, text)
        assert r.status_code == 200, (text, r.status_code, r.text)
        j = r.json()
        for key in ("ok", "intent", "engine", "confidence", "answer",
                    "action", "data", "error"):
            assert key in j, key


def test_internal_error_is_caught(client, monkeypatch):
    """内部异常兜底成 internal_error，绝不抛 500。"""
    ingest(client)

    def boom(*a, **k):
        raise ValueError("模拟内部故障")

    monkeypatch.setattr(assistant, "allowed_devices", boom)
    j = ask(client, "查看上次数据").json()
    assert j["ok"] is False
    assert j["error"]["code"] == "internal_error"
