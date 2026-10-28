"""第9周：视觉事件触发一次主动询问 —— 完整任务现场演示（对着 127.0.0.1:8001）。

用法：python tools/demo_week9.py [--origin http://127.0.0.1:8001] [--token X]

覆盖（对应当堂验证与提交要求）：
  S1 事件触发闭环：画面不可用(遮挡) → 系统主动询问"要重拍吗" → 确认重拍 →
     新图(可用) → 不再询问（同一画面只问一次、新画面按新事件处理）；
  S2 拒绝后不反复打扰：对询问说"忽略" → 询问关闭 + 冷却 5 分钟 →
     冷却期内新画面也不自动询问（不连环追问）；
  S3 误判能够纠正：把"遮挡"误判纠正为"可用" → 该画面询问立即作废(superseded)，
     状态回答不再追问；
  S4 反馈优先级：设备上有进行中的采集任务时，不打断、不发起询问（task_busy）；
  S5 运行位置清单：模型 / 设备 / 服务实际运行位置如实说明（提交项）。

退出码 0 = 全部通过。中间等价于"完整任务测试原始记录"，供 docs/week9-test-record.md。
"""
import argparse
import hashlib
import sys
import time

import httpx

BASE = "http://127.0.0.1:8001"
MAC = "94:A9:90:1C:6F:D4"


def _bytes_with(hash_fn):
    """找 sha256 满足 hash_fn(h) 的字节（回放规则稳定判类，现场可复现）。"""
    base = b"\xff\xd8\xff\xe0 " + b"W9-SCENE-"
    for i in range(4000):
        data = base + b"%04d" % i
        h = int(hashlib.sha256(data).hexdigest(), 16)
        if hash_fn(h):
            return data
    raise RuntimeError("找不到满足条件的样例字节")


IMG_OCCLUDED = _bytes_with(lambda h: h % 7 == 0)          # 回放规则 → occluded
IMG_USABLE = _bytes_with(lambda h: h % 7 != 0 and h % 11 != 0)   # → usable


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--origin", default=BASE)
    ap.add_argument("--token", default="")
    args = ap.parse_args()
    origin = args.origin.rstrip("/")
    hdr = {"X-Control-Token": args.token} if args.token else {}
    failed = []
    total = 0

    def check(name, cond, extra=""):
        nonlocal total
        total += 1
        print(("PASS  " if cond else "FAIL  ") + name + ("  " + extra if extra else ""))
        if not cond:
            failed.append(name)

    c = httpx.Client(base_url=origin, timeout=10.0)
    check("服务端可达 /health", c.get("/health").status_code == 200)
    now = int(time.time() * 1000)
    assert c.post("/api/v1/ingest", json={
        "device_mac": MAC, "boot_id": "boot-w9", "fw_version": "0.9.0",
        "ntp_synced": True, "ntp_sync_age_s": 5, "t_device_ntp_ms": now,
        "dropped_since_last": 0,
        "readings": [{"seq": 0, "t_device_ms": now - 100, "ax": 0.1,
                      "ay": 0.2, "az": 1.0, "spl_db": 41.0}],
    }).status_code == 201

    def claim_upload(rid, data):
        """按目标指令领取（容忍残留 pending：循环领到 rid 为止）→ 传图 → done。"""
        got = None
        for _ in range(6):
            cl = c.post("/api/v1/commands/claim", content="%s|bootW9|0.9.0" % MAC)
            assert cl.status_code == 200, cl.text
            if cl.text.strip() == "none":
                break
            got = cl.text.split("|", 1)[0]
            if got == rid:
                break
        assert got == rid, "未领到目标指令 %s（最后 %r）" % (rid, got)
        up = c.post("/api/v1/commands/%s/image" % rid, content=data,
                    headers={"X-Device-Mac": MAC, "X-Boot-Id": "bootW9",
                             "Content-Type": "image/jpeg",
                             "X-Captured-At-Ms": str(now)})
        assert up.status_code == 200, up.text
        done = c.post("/api/v1/commands/%s/result" % rid,
                      json={"state": "done", "boot_id": "bootW9"})
        assert done.status_code == 200, done.text
        return up.json()["snapshot"]["id"]

    def snapflow(text, data):
        """拍一张 → 领取（容忍残留 pending）→ 传图 → done。返回 (sid, rid)。"""
        j = c.post("/api/v1/assistant/chat", headers=hdr,
                   json={"text": text}).json()
        assert j["intent"]["action"] == "image", j
        rid = j["command"]["request_id"]
        return claim_upload(rid, data), rid

    def analyze(sid, task="usability"):
        r = c.post("/api/v1/snapshots/%d/analyze" % sid, headers=hdr,
                   json={"task": task})
        assert r.status_code == 200, r.text
        return r.json()

    def ask_count():
        return c.get("/api/v1/asks?pending=1").json()["count"]

    # ------------------------------------------------------ S1 事件触发闭环
    print("\n=== S1 事件触发闭环：遮挡 → 主动询问 → 确认重拍 → 新图可用 ===")
    sid1, rid1 = snapflow("拍张照", IMG_OCCLUDED)
    j1 = analyze(sid1)
    check("S1 画面不可用（遮挡）", j1["inference"]["label"] == "occluded")
    check("S1 事件触发：系统主动询问", j1["ask"] is not None
          and j1["ask"]["state"] == "open", "ask#%d" % j1["ask"]["id"])
    st = c.post("/api/v1/assistant/chat", headers=hdr,
                json={"text": "图像采集完成了吗"}).json()["answer"]
    check("S1 状态回答携带询问", "要重拍一张吗" in st and "忽略" in st, st[:60])
    q = c.post("/api/v1/snapshots/%d/ask" % sid1, headers=hdr,
               json={"decision": "confirm"})
    rc = q.json()["recapture"]["request_id"]
    check("S1 确认 → 立即下发重拍指令", q.status_code == 200
          and q.json()["recapture"]["op"] == "image" and rc != rid1
          and q.json()["ask"]["state"] == "confirmed")
    sid2 = claim_upload(rc, IMG_USABLE)      # 设备领取的正是"确认重拍"这条指令
    j2 = analyze(sid2)
    check("S1 新图可用 → 不再询问", j2.get("ask") is None
          and j2["ask_note"] == "usable", j2["ask_note"])
    check("S1 同画面只问一次（无残留 open 询问）", ask_count() == 0)

    # ------------------------------------------------------ S3 误判能够纠正
    print("\n=== S3 误判能够纠正：遮挡误判 → 人工纠正为可用 → 询问作废 ===")
    sid3, rid3 = snapflow("拍张照", IMG_OCCLUDED)
    j3 = analyze(sid3)
    check("S3 误判（遮挡）触发询问", j3["ask"] is not None
          and j3["ask"]["state"] == "open")
    cr = c.post("/api/v1/snapshots/%d/corrections" % sid3, headers=hdr,
                json={"kind": "label", "value": "usable",
                      "note": "同学人工核对，实为可用"})
    check("S3 纠正后询问作废（superseded）",
          cr.status_code == 200
          and c.get("/api/v1/snapshots/%d" % sid3).json()["ask"]["state"]
          == "superseded")
    st3 = c.post("/api/v1/assistant/chat", headers=hdr,
                 json={"text": "图像采集完成了吗"}).json()["answer"]
    check("S3 默认回答不再追问", "要重拍一张吗" not in st3, st3[:60])

    # ------------------------------------------------------ S4 反馈优先级
    print("\n=== S4 反馈优先级：进行中的任务不被询问打断 ===")
    sid4, _rid4 = snapflow("拍张照", IMG_OCCLUDED)   # 先拍好一幅遮挡图（暂不分析）
    busy = c.post("/api/v1/assistant/chat", headers=hdr,
                  json={"text": "重新采集"}).json()["command"]
    check("S4 先挂一条进行中的采集", busy["state"] == "pending")
    claim = c.post("/api/v1/commands/claim",
                   content="%s|bootW9|0.9.0" % MAC).text
    check("S4 设备已领取（采集进行中）", claim.split("|", 1)[0] == busy["request_id"])
    j4 = analyze(sid4)                          # 此刻设备正忙，分析旧图
    check("S4 忙碌 → 不打断、不询问", j4.get("ask") is None
          and j4["ask_note"] == "task_busy", j4["ask_note"])
    fin = c.post("/api/v1/commands/%s/result" % busy["request_id"],
                 json={"state": "failed", "boot_id": "bootW9",
                       "error_code": "aborted_by_user"})
    check("S4 指令收尾（终态，演示清理）", fin.status_code == 200)

    # ------------------------------------------------------ S2 拒绝后不反复打扰
    print("\n=== S2 拒绝后不反复打扰：忽略 → 冷却 5 分钟 ===")
    sid5, _rid5 = snapflow("拍张照", IMG_OCCLUDED)
    j5 = analyze(sid5)
    check("S2 又一幅不可用画面 → 新询问", j5["ask"] is not None
          and j5["ask"]["state"] == "open")
    ign = c.post("/api/v1/assistant/chat", headers=hdr, json={"text": "忽略"})
    ig = ign.json()
    check("S2 说“忽略”→ 询问关闭 + 进入冷却",
          ig["intent"]["action"] == "ask_ignore"
          and "冷却" in ig["answer"] and "5 分钟" in ig["answer"], ig["answer"][:60])
    sid6, _rid6 = snapflow("拍张照", IMG_OCCLUDED)
    j6 = analyze(sid6)
    check("S2 冷却期内新画面不再自动询问",
          j6.get("ask") is None and j6["ask_note"] == "cooldown", j6["ask_note"])
    check("S2 冷却期内无任何 open 询问挂起", ask_count() == 0)

    # ------------------------------------------------------ 运行位置清单
    print("\n=== S5 运行位置清单（演示口径，如实说明） ===")
    lines = [
        "模型层   ：未配置 VISION_BASE_URL → 走“回放标注”（source=replay，"
        "replay-rule-1），不计入真实推理证据；接教师基线后同接口走真实模型",
        "设备层   ：本轮演示用 claim 模拟 ESP32-S3 领取/传图/done（fw 0.9.0）；"
        "实体板卡固件适配（第9周要求）列为下周端侧待办，未谎称已上板",
        "服务层   ：FastAPI 本机 127.0.0.1:8001（学生实现，DB=SQlite，图像最少保留）",
        "演示方式 ：python tools/demo_week9.py --origin http://127.0.0.1:8001",
    ]
    for ln in lines:
        print("  - " + ln)
    print()
    print("结果：%d 项检查，%d 项失败（完整原始记录见上方输出）" % (total, len(failed)))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()