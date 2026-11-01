"""第12周：让用户纠错影响后续提醒 —— 主动提醒与纠错闭环现场演示（127.0.0.1:8001）。

用法：python tools/demo_week12.py [--origin http://127.0.0.1:8001]

演示序列（真实 HTTP：图像采集/分析触发主动提醒 + 提醒门 + 有限反馈 + 前后对照）：
  A1 默认开启：遮挡帧 → "要重拍吗"提醒触发（fire #1）；
  A2 同一事件重放：同帧 force 重跑 → 仍同一条询问，不重复触发（fire_count=1）；
  A3 语音"这次不用提醒" → 偏好 off（配置版本 v1，反馈 off 记录）；
  A4 新遮挡帧 → 不再打扰（pref_off）；前后对照：变更前 1 次 / 变更后 0 次；
  A5 语音"稍后再说" → snooze（v2）；
  A6 新遮挡帧 → 稍后窗口拦截（snooze_until）；
  A7 语音"恢复默认提醒" → 配置版本回退 v3（from snooze → on，回退记录留档）；
  A8 新遮挡帧 → 恢复后又能提醒（fire #2）；
  A9 纠正（人工核对可用）→ feedback correct，询问作废；
  A10 确认重拍 → feedback confirm + 重拍指令（确认闭环）；
  A11 语音"忽略" → feedback ignore + 冷却（拒绝后不反复打扰）；
  A12 冷却窗口：新帧不再问（cooldown）；
  B1 查看偏好与反馈（用户可见）；B2 语音"提醒设置"（只读）；B3 触发/去重日志；
  B4 最终前后对照：变更前 1 次 / 变更后 3 次 + 反馈汇总。

退出码 0 = 全部通过。
"""
import argparse
import hashlib
import sys
import time

import httpx

BASE = "http://127.0.0.1:8001"
MAC = "94:A9:90:1C:6F:D4"
BOOT = "boot-w12"


def occluded_bytes():
    """遮挡图像：确定性构造（sha256%7==0 → 回放规则判定 occluded）。"""
    seed = 0
    while True:
        seed += 1
        img = b"jpg-occluded-%d" % seed
        if int(hashlib.sha256(img).hexdigest(), 16) % 7 == 0:
            return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--origin", default=BASE)
    ap.add_argument("--token", default="")
    args = ap.parse_args()
    origin = args.origin.rstrip("/")
    failed, total = [], 0

    def check(name, cond, extra=""):
        nonlocal total
        total += 1
        print(("PASS  " if cond else "FAIL  ") + name + ("  " + extra if extra else ""))
        if not cond:
            failed.append(name)

    c = httpx.Client(base_url=origin, timeout=20.0)
    check("服务端可达 /health", c.get("/health").status_code == 200)
    now = int(time.time() * 1000)

    # 先登记设备（一次最小批次）：没有批次的设备不会被自然语言识别为可操作对象
    seed = c.post("/api/v1/ingest", json={
        "device_mac": MAC, "boot_id": BOOT, "fw_version": "0.12.0",
        "ntp_synced": True, "ntp_sync_age_s": 5,
        "t_device_ntp_ms": now, "dropped_since_last": 0,
        "readings": [{"seq": i, "t_device_ms": now - (2 - i) * 1000,
                      "ax": 0.0, "ay": 0.0, "az": 1.0, "spl_db": 41.0}
                     for i in range(3)],
    })
    check("设备登记批次上报", seed.status_code == 201, "batch_id=%s"
          % seed.json().get("batch_id"))

    def chat(text):
        return c.post("/api/v1/assistant/chat", json={"text": text}).json()

    def prefs(dev=None):
        r = c.get("/api/v1/reminders/prefs" +
                  ("?device_mac=%s" % dev if dev else ""))
        assert r.status_code == 200, r.text
        return r.json()

    def feedback():
        return c.get("/api/v1/reminders/feedback").json()["feedback"]

    def report(dev=None):
        r = c.get("/api/v1/reminders/report" +
                  ("?device_mac=%s" % dev if dev else ""))
        return r.json()["report"]

    def capture_snapshot():
        """拍一张遮挡图并完成分析，返回 (snapshot_id, analyze_json)。"""
        rid = chat("拍张照")["command"]["request_id"]
        c.post("/api/v1/commands/claim", content="%s|%s|0.12.0" % (MAC, BOOT))
        up = c.post("/api/v1/commands/%s/image" % rid, content=occluded_bytes(),
                    headers={"X-Device-Mac": MAC, "X-Boot-Id": BOOT,
                             "Content-Type": "image/jpeg"})
        assert up.status_code == 200, up.text
        sid = up.json()["snapshot"]["id"]
        c.post("/api/v1/commands/%s/result" % rid,
               json={"state": "done", "boot_id": BOOT})
        az = c.post("/api/v1/snapshots/%d/analyze" % sid,
                    json={"task": "usability", "force": False})
        assert az.status_code == 200, az.text
        return sid, az.json()

    # ------------------------------------------------------ A1 默认开启
    print("\n=== A1 默认开启：遮挡帧 → 主动提醒（要重拍吗）===")
    sid1, j1 = capture_snapshot()
    check("A1 询问打开", j1.get("ask") is not None and j1["ask"]["state"] == "open",
          "round ask#%d" % j1["ask"]["id"])
    ev = c.get("/api/v1/reminders/events").json()["events"]
    hit = [e for e in ev if e["event_key"] == "snapshot:%d" % sid1][0]
    check("A1 事件 fire_count=1", hit["fire_count"] == 1, hit["last_note"] or "")

    # ------------------------------------------------------ A2 同一事件重放
    print("\n=== A2 同一事件重放：不重复触发提醒 ===")
    az2 = c.post("/api/v1/snapshots/%d/analyze" % sid1,
                 json={"task": "usability", "force": True})
    check("A2 重放仍返回同一条询问", az2.status_code == 200
          and az2.json()["ask"]["id"] == j1["ask"]["id"])
    ev2 = [e for e in c.get("/api/v1/reminders/events").json()["events"]
           if e["event_key"] == "snapshot:%d" % sid1][0]
    check("A2 fire_count 仍为 1（不重复触发）", ev2["fire_count"] == 1
          and ev2["last_seen_ms"] > ev2["last_fired_ms"],
          "last_seen=%d last_fired=%d" % (ev2["last_seen_ms"], ev2["last_fired_ms"]))

    # ------------------------------------------------------ A3 关闭本轮提醒
    print("\n=== A3 语音「这次不用提醒」→ 偏好 off ===")
    j3 = chat("这次不用提醒")
    check("A3 意图 remind_off", j3["intent"]["action"] == "remind_off")
    check("A3 答复带配置版本", "配置版本 v1" in j3["answer"], j3["answer"][:50])
    p3 = prefs(MAC)["channels"]["vision_ask"]
    check("A3 偏好已关闭", p3["value"] == "off" and p3["config_version"] == 1)
    check("A3 反馈 off 已记录", any(f["verb"] == "off" for f in feedback()))

    # ------------------------------------------------------ A4 关闭后不打扰 + 前后对照
    print("\n=== A4 用户关闭后：新帧不再打扰（前/后对照）===")
    sid4, j4 = capture_snapshot()
    check("A4 不发起新询问", j4.get("ask") is None and j4["ask_note"] == "pref_off")
    pending = c.get("/api/v1/asks?pending=1").json()
    check("A4 旧询问仍在（未答），新帧不新增", pending["count"] == 1
          and pending["asks"][0]["snapshot_id"] == sid1)
    rep = report(MAC)
    check("A4 前后对照：变更前 1 次 / 变更后 0 次",
          rep["fired_before_pref_change"] == 1 and rep["fired_after_pref_change"] == 0,
          "before=%d after=%d" % (rep["fired_before_pref_change"],
                                  rep["fired_after_pref_change"]))

    # ------------------------------------------------------ A5 稍后再说
    print("\n=== A5 语音「稍后再说」→ 稍后窗口 ===")
    j5 = chat("稍后再说")
    check("A5 意图 remind_later", j5["intent"]["action"] == "remind_later"
          and "稍后" in j5["answer"])
    p5 = prefs(MAC)["channels"]["vision_ask"]
    check("A5 偏好 snooze + 版本 v2", p5["value"] == "snooze" and p5["config_version"] == 2
          and p5["until_ms"] is not None)

    # ------------------------------------------------------ A6 稍后窗口拦截
    print("\n=== A6 稍后窗口内：新帧拦截 ===")
    sid6, j6 = capture_snapshot()
    check("A6 被稍后窗口拦住", j6.get("ask") is None and j6["ask_note"] == "snooze_until")

    # ------------------------------------------------------ A7 恢复默认（配置版本回退）
    print("\n=== A7 语音「恢复默认提醒」→ 配置版本回退 ===")
    j7 = chat("恢复默认提醒")
    check("A7 意图 remind_restore + 回退记录", j7["intent"]["action"] == "remind_restore"
          and "回退记录" in j7["answer"] and "v2 → v3" in j7["answer"])
    p7v = prefs(MAC)
    p7 = p7v["channels"]["vision_ask"]
    check("A7 偏好回到 on（默认）", p7["value"] == "on"
          and p7v["config_version"] == 3)
    log = c.get("/api/v1/reminders/feedback").json()["feedback"]
    check("A7 反馈 restore_default 已记录",
          any(f["verb"] == "restore_default" for f in log))

    # ------------------------------------------------------ A8 恢复后又能提醒
    print("\n=== A8 恢复默认后：新帧又能提醒（fire #2）===")
    sid8, j8 = capture_snapshot()
    check("A8 询问重新打开", j8.get("ask") is not None and j8["ask"]["state"] == "open")
    check("A8 事件 fire_count=1（第 2 个事件）",
          [e for e in c.get("/api/v1/reminders/events").json()["events"]
           if e["event_key"] == "snapshot:%d" % sid8][0]["fire_count"] == 1)

    # ------------------------------------------------------ A9 纠正
    print("\n=== A9 纠正（人工核对：画面可用）→ 询问作废 + feedback correct ===")
    r9 = c.post("/api/v1/snapshots/%d/corrections" % sid8,
                json={"kind": "label", "value": "usable", "note": "人工核对"})
    check("A9 纠正成功", r9.status_code == 200)
    ask9 = c.get("/api/v1/snapshots/%d" % sid8).json()["ask"]
    check("A9 询问作废（superseded）", ask9["state"] == "superseded")
    check("A9 反馈 correct 已记录", any(f["verb"] == "correct" for f in feedback()))

    # ------------------------------------------------------ A10 确认重拍
    print("\n=== A10 确认重拍 → 重拍指令 + feedback confirm ===")
    sid10, j10 = capture_snapshot()
    r10 = c.post("/api/v1/snapshots/%d/ask" % sid10, json={"decision": "confirm"})
    check("A10 确认成功且下发重拍指令", r10.status_code == 200
          and r10.json()["ask"]["state"] == "confirmed"
          and r10.json()["recapture"]["op"] == "image")
    check("A10 反馈 confirm 已记录", any(f["verb"] == "confirm" for f in feedback()))
    # 领走重拍指令，恢复设备空闲（下一段演示不被 task_busy 干扰）
    rc = r10.json()["recapture"]["request_id"]
    claim = c.post("/api/v1/commands/claim", content="%s|%s|0.12.0" % (MAC, BOOT))
    check("A10 重拍指令可被设备领取", rc in claim.text, claim.text)
    c.post("/api/v1/commands/%s/image" % rc, content=occluded_bytes(),
           headers={"X-Device-Mac": MAC, "X-Boot-Id": BOOT,
                    "Content-Type": "image/jpeg"})
    c.post("/api/v1/commands/%s/result" % rc, json={"state": "done", "boot_id": BOOT})

    # ------------------------------------------------------ A11 忽略 + 冷却
    print("\n=== A11 语音「忽略」→ 关闭询问 + 冷却（拒绝后不反复打扰）===")
    sid11, j11 = capture_snapshot()
    check("A11 新提示已打开", j11.get("ask") is not None and j11["ask"]["state"] == "open")
    j11b = chat("忽略")
    check("A11 意图 ask_ignore + 冷却提示", j11b["intent"]["action"] == "ask_ignore"
          and "冷却" in j11b["answer"])
    check("A11 反馈 ignore 已记录", any(f["verb"] == "ignore" for f in feedback()))

    # ------------------------------------------------------ A12 冷却拦截
    print("\n=== A12 冷却窗口内：新帧不再询问 ===")
    sid12, j12 = capture_snapshot()
    check("A12 被冷却拦住", j12.get("ask") is None and j12["ask_note"] == "cooldown")

    # ------------------------------------------------------ B1 查看偏好（用户可见）
    print("\n=== B1 查看本轮偏好（用户可见、可撤回）===")
    pv = prefs(MAC)
    check("B1 显示偏好与配置版本", pv["channels"]["vision_ask"]["value"] == "on"
          and pv["config_version"] >= 3 and pv["rule_version"] == "week12-r1")
    fb = feedback()
    verbs = {f["verb"] for f in fb}
    check("B1 反馈可查（有限动词集合）", {"off", "later", "restore_default",
                                    "correct", "confirm", "ignore"} <= verbs,
          "verbs=" + ",".join(sorted(verbs)))

    # ------------------------------------------------------ B2 语音"提醒设置"
    print("\n=== B2 语音「提醒设置」→ remind_review 只读 ===")
    jb2 = chat("提醒设置")
    check("B2 意图 remind_review", jb2["intent"]["action"] == "remind_review")
    check("B2 答复带规则/配置版本与反馈记录",
          "规则版本" in jb2["answer"] and "配置版本" in jb2["answer"]
          and "反馈记录" in jb2["answer"], jb2["answer"][:70])

    # ------------------------------------------------------ B3 触发/去重日志
    print("\n=== B3 提醒触发与去重日志（重放不重复触发）===")
    evs = c.get("/api/v1/reminders/events").json()["events"]
    fired = [e for e in evs if e["fire_count"] > 0]
    check("B3 触发事件都只 fire 1 次（重放只更新 last_seen）",
          len(fired) >= 4 and all(e["fire_count"] == 1 for e in fired)
          and any(e["last_seen_ms"] > e["last_fired_ms"] for e in fired),
          "fired_events=%d" % len(fired))

    # ------------------------------------------------------ B4 最终前后对照
    print("\n=== B4 最终前后对照（实践2：重复提醒次数 + 任务结果 + 反馈汇总）===")
    repf = report(MAC)
    check("B4 变更前 1 次 / 变更后 3 次触发", repf["fired_before_pref_change"] == 1
          and repf["fired_after_pref_change"] == 3,
          "before=%d after=%d" % (repf["fired_before_pref_change"],
                                  repf["fired_after_pref_change"]))
    check("B4 重放未重复触发 ≥1", repf["replays_not_refired"] >= 1,
          "replays=%d" % repf["replays_not_refired"])
    check("B4 任务结果统计完整", repf["asks"]["confirmed"] >= 1
          and repf["asks"]["ignored"] >= 1 and repf["asks"]["superseded"] >= 1,
          "asks=%s" % repf["asks"])
    check("B4 反馈汇总 = 6 种动词", set(repf["feedback"]) ==
          {"off", "later", "restore_default", "correct", "confirm", "ignore"},
          "fb=%s" % sorted(repf["feedback"]))

    print("\n结果：%d 项检查，%d 项失败" % (total, len(failed)))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()