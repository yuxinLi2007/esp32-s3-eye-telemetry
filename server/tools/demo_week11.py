"""第11周：根据多源证据组织交互上下文 —— 三层模型现场演示（对着 127.0.0.1:8001）。

用法：python tools/demo_week11.py [--origin http://127.0.0.1:8001] [--token X]

演示序列（真实 HTTP：/ingest 事件 + 图像采集/分析 + /api/v1/context + 语音意图）：
  A1 三层模型：静止 + 新鲜位置 + 网络在线 → "已知事实"齐全，无待确认；
  A2 摄像头遮挡（模型推断）→ 只说"画面被遮挡"，"危险"只以否定形式出现，
      并追问"重拍确认"（遮挡 ≠ 危险）；
  A3 陈旧坐标 → "上次报告的位置（X 前）"，绝不"当前位置"，并追问是否在原地；
  A4 推断与观测明确区分：同一条答复里 camera=推断、movement/position=观测，
      各自带来源与更新时间；
  A5 冲突输入：移动中（观测）但本次 GNSS 无有效解（no_fix）→ 两边都保留，
      位置列为待确认，绝不吐出"当前位置"；
  A6 缺失输入：GNSS 从未上报 → 无位置事实，列为待确认（不补造位置）；
  B1 语音"现在什么情况" → context 只读意图，答复 = 已知＋待确认＋规则版本；
  B2 三层数据对照（L1 原始观测 / L2 派生状态 / L3 交互决策）打印。

退出码 0 = 全部通过。
"""
import argparse
import sys
import time

import httpx

BASE = "http://127.0.0.1:8001"
MAC = "94:A9:90:1C:6F:D4"
MAC2 = "AA:BB:CC:DD:EE:0A"   # A6 用：全新设备，GNSS 从未上报


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--origin", default=BASE)
    ap.add_argument("--token", default="")
    ap.add_argument("--device", default=MAC)
    args = ap.parse_args()
    origin = args.origin.rstrip("/")
    mac = args.device.upper()
    failed, total = [], 0

    def check(name, cond, extra=""):
        nonlocal total
        total += 1
        print(("PASS  " if cond else "FAIL  ") + name + ("  " + extra if extra else ""))
        if not cond:
            failed.append(name)

    c = httpx.Client(base_url=origin, timeout=15.0)
    check("服务端可达 /health", c.get("/health").status_code == 200)
    now = int(time.time() * 1000)

    def ingest(readings, sources_=None, mac=MAC):
        body = {
            "device_mac": mac, "boot_id": "boot-w11", "fw_version": "0.11.0",
            "ntp_synced": True, "ntp_sync_age_s": 5,
            "t_device_ntp_ms": now, "dropped_since_last": 0,
            "readings": readings,
        }
        if sources_ is not None:
            body["sources"] = sources_
        r = c.post("/api/v1/ingest", json=body)
        assert r.status_code == 201, r.text

    def stationary():
        return [{"seq": i, "t_device_ms": now - (9 - i) * 1000,
                 "ax": 0.0, "ay": 0.0, "az": 1.0, "spl_db": 41.0}
                for i in range(10)]

    def moving():
        return [{"seq": i, "t_device_ms": now - (9 - i) * 1000,
                 "ax": 0.5 * (i % 3 - 1), "ay": 0.3 * (i % 2),
                 "az": 1.0 + 0.4 * (i % 2), "spl_db": 42.0}
                for i in range(10)]

    def gnss(state, t_obs, lat=31.2304, lon=121.4737):
        e = {"source": "gnss", "state": state, "t_obs_ms": t_obs}
        if lat is not None:
            e["raw"] = {"lat": lat, "lon": lon}
        return e

    def ctx(dev=mac):
        r = c.get("/api/v1/context?device_mac=%s" % dev)
        assert r.status_code == 200, r.text
        return r.json()

    # ------------------------------------------------------ A1 三层齐全
    print("\n=== A1 静止 + 新鲜位置 + 网络在线：已知事实齐全 ===")
    ingest(stationary(),
           [gnss("ok", now - 3000), {"source": "net", "state": "ok",
                                     "t_obs_ms": now - 1000,
                                     "raw": {"rssi": -55}}])
    j = ctx()
    keys = {f["key"] for f in j["facts"]}
    check("A1 有 movement/position/network 事实", {"movement", "position",
                                                   "network"} <= keys)
    check("A1 无待确认", j["open_questions"] == [], j["answer"][:60])
    print("A1 答复：%s" % j["answer"])

    # ------------------------------------------------------ A2 遮挡 ≠ 危险
    print("\n=== A2 摄像头遮挡（模型推断）：只解释为遮挡，不解释为危险 ===")
    rid = c.post("/api/v1/assistant/chat", json={"text": "拍张照"}).json()
    rid = rid["command"]["request_id"]
    c.post("/api/v1/commands/claim",
           content="%s|%s|0.11.0" % (mac, "boot-w11"))
    # 遮挡图像：demo_week8 同款确定性构造（sha256%7==0 → occluded）
    import hashlib
    img = b""
    h = -1
    seed = 0
    while h % 7 != 0:
        seed += 1
        img = b"jpg-occluded-%d" % seed
        h = int(hashlib.sha256(img).hexdigest(), 16)
    up = c.post("/api/v1/commands/%s/image" % rid, content=img,
           headers={"X-Device-Mac": mac, "X-Boot-Id": "boot-w11",
                    "Content-Type": "image/jpeg"})
    assert up.status_code == 200, up.text
    sid = up.json()["snapshot"]["id"]
    c.post("/api/v1/commands/%s/result" % rid,
           json={"state": "done", "boot_id": "boot-w11"})
    # 显式触发识别（回放路径：sha%7==0 → occluded）
    az = c.post("/api/v1/snapshots/%d/analyze" % sid,
                json={"task": "usability", "force": False})
    assert az.status_code == 200, az.text
    j = ctx()
    cam = next(f for f in j["facts"] if f["key"] == "camera_view")
    check("A2 画面被遮挡（推断层）", cam["value"] == "画面被遮挡"
          and cam["layer"] == "inference")
    check("A2 危险只以否定形式出现",
          j["answer"].count("危险") == j["answer"].count("不是危险信号"),
          j["answer"][:80])
    check("A2 追问重拍确认", any("重拍" in q["question"]
                                for q in j["open_questions"]))
    print("A2 答复：%s" % j["answer"])

    # ------------------------------------------------------ A3 陈旧坐标 ≠ 当前位置
    print("\n=== A3 陈旧坐标：不说当前位置，追问是否在原地 ===")
    ingest(stationary(), [gnss("ok", now - 5 * 60_000)])
    j = ctx()
    ps = next(f for f in j["facts"] if f["key"] == "position")
    check("A3 上次报告的位置 + 不作为当前位置", ps["value"].startswith("上次报告的位置")
          and "不作为当前位置" in ps["value"], ps["value"])
    check("A3 答复无“当前位置”表述",
          "当前位置是" not in j["answer"] and "当前位置：" not in j["answer"])
    check("A3 追问是否在原地", any("还在刚才报告的位置附近吗" in q["question"]
                                for q in j["open_questions"]))
    print("A3 答复：%s" % j["answer"])

    # ------------------------------------------------------ A4 推断与观测区分
    print("\n=== A4 推断与观测明确区分（同一条答复） ===")
    check("A4 同时出现 推断 与 观测 标注", "推断" in j["answer"]
          and "观测" in j["answer"])
    mov = next(f for f in j["facts"] if f["key"] == "movement")
    ev = mov["evidence"][0]
    check("A4 观测事实带来源与更新时间", mov["layer"] == "observation"
          and ev["source"] and ev["updated_ms"] is not None,
          "%s@%d" % (ev["source"], ev["updated_ms"] or 0))

    # ------------------------------------------------------ A5 冲突输入
    print("\n=== A5 冲突输入：移动中（观测）但本次 GNSS 无有效解 ===")
    ingest(moving(), [gnss("ok", now - 3 * 60_000)])
    ingest(moving(), [gnss("no_fix", now - 2000, lat=None, lon=None)])
    j = ctx()
    ans = j["answer"]
    check("A5 两边都保留（不抹掉任何一边）", "移动中" in ans and "上次有效定位" in ans)
    check("A5 位置列为待确认，无当前位置", "重新获取位置" in ans
          and "当前位置是" not in ans and "当前位置：" not in ans)
    print("A5 答复：%s" % ans)

    # ------------------------------------------------------ A6 缺失输入（第二台全新设备）
    print("\n=== A6 缺失输入：GNSS 从未上报（未尝试）—— 第二台设备 %s ===" % MAC2)
    ingest(stationary(), mac=MAC2)               # 不带 sources
    j = ctx(MAC2)
    check("A6 无位置事实（不补造）",
          next((f for f in j["facts"] if f["key"] == "position"), None) is None)
    check("A6 保留为待确认", any("无定位证据" in q["question"]
                                for q in j["open_questions"]))
    print("A6 答复：%s" % j["answer"])

    # ------------------------------------------------------ B1 语音
    print("\n=== B1 语音「现在什么情况」→ context 只读 ===")
    chat = c.post("/api/v1/assistant/chat", json={"text": "现在什么情况"}).json()
    check("B1 意图 = context", chat["intent"]["action"] == "context")
    check("B1 答复带 已知/仍需确认/规则版本",
          all(s in chat["answer"] for s in ("已知", "仍需确认", "规则版本")),
          chat["answer"][:60])

    # ------------------------------------------------------ B2 三层对照（A5 那台富上下文设备）
    print("\n=== B2 三层数据对照（同一时刻 /api/v1/context，设备 %s） ===" % MAC)
    j = ctx(mac)
    print("规则版本：%s" % j["rule_version"])
    print("L1 原始观测（引用索引）:")
    for k, v in (j["raw_refs"] or {}).items():
        print("  %-6s [%s] %s  age=%s %s"
              % (k, v.get("kind"), v.get("source"),
                 ("%d 秒" % (v.get("age_ms", 0) // 1000))
                 if v.get("age_ms") is not None else "—",
                 v.get("note") or v.get("state") or ""))
    print("L2 派生状态:")
    for f in j["facts"]:
        print("  %-12s %s [%s] 规则 %s 更新 %d (%s)"
              % (f["key"], f["value"], f["layer"], f["rule"],
                 (f.get("updated_ms") or 0), (f.get("extra") or {})))
    print("L3 交互决策:")
    for q in j["open_questions"]:
        print("  待确认：%s（%s）" % (q["question"], q["because"]))
    if not j["open_questions"]:
        print("  待确认：无，当前上下文一致")

    print("\n结果：%d 项检查，%d 项失败" % (total, len(failed)))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()