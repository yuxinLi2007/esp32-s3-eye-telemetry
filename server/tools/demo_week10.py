"""第10周：区分传感器观测与当前状态 —— 多源状态现场演示（对着 127.0.0.1:8001）。

用法：python tools/demo_week10.py [--origin http://127.0.0.1:8001] [--token X]

演示序列（全部走真实 HTTP /ingest → /devices/{mac}/sources，数据来自脚本
"明确来源的回放"——标注 replay=True 的事件在答复里同样如实标注）：
  S1 未尝试：老板子不带上 sources → 事件源 = 未尝试，不假定硬件已上线（无证据不补造位置）；
  S2 有旧位置但本次 no_fix：先成功定位 → 再报 no_fix → "不是设备故障"，旧位置不冒充当前；
  S3 补传按原观测时间解释：观测于 15 分钟前、服务端此刻才收到 → 陈旧，不冒充当前；
  S4 案例对照：网络在线但位置未更新（net 新鲜 + gnss 陈旧）；
  S5 回放标注：replay=True 的事件在状态与答复里明确标注；
  S6 语音"各源状态" → 只读人话汇总（对应当堂"采集状态与用户提示对照"）。

退出码 0 = 全部通过。
"""
import argparse
import sys
import time

import httpx

BASE = "http://127.0.0.1:8001"
MAC = "94:A9:90:1C:6F:D4"


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

    def ingest(sources_, t_device_ms=None):
        body = {
            "device_mac": MAC, "boot_id": "boot-w10", "fw_version": "0.10.0",
            "ntp_synced": True, "ntp_sync_age_s": 5,
            "t_device_ntp_ms": t_device_ms or now,
            "dropped_since_last": 0,
            "readings": [{"seq": 0, "t_device_ms": (t_device_ms or now),
                          "ax": 0.1, "ay": 0.2, "az": 1.0, "spl_db": 41.0}],
        }
        if sources_ is not None:
            body["sources"] = sources_
        r = c.post("/api/v1/ingest", json=body)
        assert r.status_code == 201, r.text
        return r.json()["batch_id"]

    def srcs():
        r = c.get("/api/v1/devices/%s/sources" % MAC)
        assert r.status_code == 200, r.text
        return r.json()

    # ------------------------------------------------------ S1 未尝试
    print("\n=== S1 未尝试：老板子不带上 sources，不假定硬件已上线 ===")
    ingest(None)
    j = srcs()
    check("S1 GNSS/网络 = 未尝试", j["source_states"]["gnss"]["status"]
          == "not_attempted" and j["source_states"]["net"]["status"]
          == "not_attempted")
    check("S1 无证据不补造位置", "未尝试" in j["answer"]
          and "不假定硬件已上线" in j["answer"]
          and "lat" not in j["answer"] and "lon" not in j["answer"],
          j["answer"][:70])

    # ------------------------------------------------------ S2 有旧位置但本次 no_fix
    print("\n=== S2 有旧位置但本次 no_fix（GNSS 无有效解 ≠ 设备故障） ===")
    ingest([{"source": "gnss", "state": "ok", "t_obs_ms": now - 3 * 60_000,
             "raw": {"lat": 31.2304, "lon": 121.4737, "sats": 7}}])
    ingest([{"source": "gnss", "state": "no_fix", "t_obs_ms": now - 2000}])
    j = srcs()
    g = j["source_states"]["gnss"]
    check("S2 no_fix 是失败不是设备故障", g["status"] == "failed"
          and g["reason"] == "no_fix")
    check("S2 保留旧位置但不冒充当前", "不是设备故障" in j["answer"]
          and "有旧位置但本次 no_fix" in j["answer"]
          and "不冒充当前位置" in j["answer"], j["answer"][:80])
    check("S2 答复里设备故障只以否定出现",
          j["answer"].count("设备故障") == j["answer"].count("不是设备故障"))

    # ------------------------------------------------------ S3 补传按原观测时间解释
    print("\n=== S3 补传按原观测时间解释（陈旧不冒充当前） ===")
    t_obs_old = now - 15 * 60_000
    ingest([{"source": "gnss", "state": "ok", "t_obs_ms": t_obs_old,
             "raw": {"lat": 31.2304, "lon": 121.4737}},
            {"source": "net", "state": "ok", "t_obs_ms": now - 2000,
             "raw": {"rssi": -55}}], t_device_ms=t_obs_old)
    j = srcs()
    g = j["source_states"]["gnss"]
    recv_now = (now - j["source_states"]["gnss"]["last_recv_ms"]) < 10_000
    check("S3 观测 15 分钟前 → 陈旧（不是正常）", g["status"] == "stale"
          and g["age_ms"] > 14 * 60_000)
    check("S3 接收时刻是刚刚（两者分开）", recv_now)
    check("S3 答复明说陈旧、不冒充当前", "数据陈旧" in j["answer"]
          and "不冒充当前状态" in j["answer"], j["answer"][:80])

    # ------------------------------------------------------ S4 案例：网络在线但位置未更新
    print("\n=== S4 案例对照：网络在线但位置未更新 ===")
    j = srcs()
    check("S4 答复点出该案例", j["source_states"]["net"]["status"] == "success"
          and j["source_states"]["gnss"]["status"] == "stale"
          and "网络在线但位置未更新" in j["answer"]
          and "不会把旧位置说成当前位置" in j["answer"], j["answer"][-60:])

    # ------------------------------------------------------ S5 回放标注
    print("\n=== S5 回放数据明确标注（教学回放 ≠ 真实上报） ===")
    ingest([{"source": "net", "state": "ok", "t_obs_ms": now - 1000,
             "raw": {"rssi": -60, "replay_note": "脱敏回放"},
             "replay": True}])
    j = srcs()
    check("S5 状态与答复都标回放", j["source_states"]["net"]["replay"] is True
          and "回放数据，非真实上报" in j["answer"])

    # ------------------------------------------------------ S6 语音"各源状态"
    print("\n=== S6 语音只读查询：采集状态与用户提示对照 ===")
    chat = c.post("/api/v1/assistant/chat", headers=hdr,
                  json={"text": "各源状态"}).json()
    check("S6 意图 = source_status（只读）",
          chat["intent"]["action"] == "source_status", chat["intent"]["action"])
    check("S6 答复带多源状态人话", "多源状态" in chat["answer"]
          and "GNSS 定位" in chat["answer"] and "网络" in chat["answer"],
          chat["answer"][:80])

    print("\n=== 多源状态页数据（/devices + /devices/{mac}/sources） ===")
    devs = c.get("/api/v1/devices").json()["devices"]
    print("已知设备 %d 台：%s" % (len(devs), [d["device_mac"] for d in devs]))
    st = srcs()["source_states"]
    for k in ("gnss", "imu", "net"):
        s = st[k]
        print("  %-6s %-14s 观测 %s  接收 %s  年龄 %s  %s"
              % (k, ("正常" if s["status"] == "success" else
                     {"not_attempted": "未尝试", "failed": "失败",
                      "stale": "陈旧"}[s["status"]]),
                 time.strftime("%H:%M:%S", time.localtime(s["last_obs_ms"] / 1000))
                 if s["last_obs_ms"] else "—",
                 time.strftime("%H:%M:%S", time.localtime(s["last_recv_ms"] / 1000))
                 if s.get("last_recv_ms") else "—",
                 ("%d 秒前" % ((now - s["last_obs_ms"]) // 1000))
                 if s["last_obs_ms"] else "—",
                 s.get("detail") or ""))
    print()
    print("结果：%d 项检查，%d 项失败" % (total, len(failed)))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()