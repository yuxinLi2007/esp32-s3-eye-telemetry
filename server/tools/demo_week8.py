"""第8周当堂验证：视觉推理 → 可理解反馈的完整 HTTP 场景（对着 127.0.0.1:8001 跑）。

用法：
  python tools/demo_week8.py [--origin http://127.0.0.1:8001] [--token X]

覆盖（对应当堂验证与提交要求）：
  A. 完整闭环：采集 → 分析（结构化结果）→ "识别结果"人话反馈（看到了什么 +
     还有什么不能确定）→ 人工纠正 → 重拍新图对比（输入-结果-反馈三者对应）；
  B. 分数未校准口径：任何答复都不把 score 写成现实正确率（断言"未校准"与
     "不代表现实正确率"固定出现）；
  C. 场景类别不越界：scene 粗分类明确声明"不代表精确位置/不构成安全结论"；
  D. 回放路径标注：未配置真实模型时所有推理都标"回放"，不冒充实拍证据；
  E. 对照表输出：两图（正常 vs 被纠正误判）的 请求id/采集时刻/标签/分数 对照。

低分/拒识/模型错误三类样例由自动化注入模型输出覆盖（test_vision.py，
见 docs/week8-samples.md 对照表），本脚本不把静态标签冒充真实推理。
退出码 0 = 全部通过。
"""
import argparse
import hashlib
import sys
import time

import httpx

BASE = "http://127.0.0.1:8001"
MAC = "94:A9:90:1C:6F:D4"
IMG_A = b"\xff\xd8\xff\xe0 " + b"W8-NORMAL-A" * 6      # 正常样例


def _mk_occluded_bytes():
    """找一份 sha256%7==0 的字节：回放规则会稳定把它判成 'occluded'（可复现示范）。"""
    base = b"\xff\xd8\xff\xe0 " + b"W8-SEMI-COVERED-"
    for i in range(2000):
        data = base + b"%04d" % i
        h = int(hashlib.sha256(data).hexdigest(), 16)
        if h % 7 == 0:
            return data
    return base + b"FALLBACK"


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
        "device_mac": MAC, "boot_id": "boot-seed", "fw_version": "0.8.0",
        "ntp_synced": True, "ntp_sync_age_s": 5, "t_device_ntp_ms": now,
        "dropped_since_last": 0,
        "readings": [{"seq": 0, "t_device_ms": now - 100, "ax": 0.1,
                      "ay": 0.2, "az": 1.0, "spl_db": 42.0}],
    }).status_code == 201

    def snapflow(text, data):
        """拍一张 → 领取 → 传图 → done。返回 (snapshot_id, request_id)。

        领取按"最旧优先"：库里可能残留上一轮未领的 pending 指令（演示库不常清），
        循环领取直到拿到本轮的 request_id 为止（残留指令会被顺带领走、随后自然超时）。
        """
        j = c.post("/api/v1/assistant/chat", headers=hdr,
                   json={"text": text}).json()
        assert j["intent"]["action"] == "image", j
        rid = j["command"]["request_id"]
        got = None
        for _ in range(6):
            cl = c.post("/api/v1/commands/claim", content="%s|bootW8|0.8.0" % MAC)
            assert cl.status_code == 200, cl.text
            if cl.text.strip() == "none":
                break
            got = cl.text.split("|", 1)[0]
            if got == rid:
                break
        assert got == rid, "未领到目标指令 %s（最后拿到 %r）" % (rid, got)
        up = c.post("/api/v1/commands/%s/image" % rid, content=data,
                    headers={"X-Device-Mac": MAC, "X-Boot-Id": "bootW8",
                             "Content-Type": "image/jpeg",
                             "X-Captured-At-Ms": str(now)})
        assert up.status_code == 200, up.text
        done = c.post("/api/v1/commands/%s/result" % rid,
                      json={"state": "done", "boot_id": "bootW8"})
        assert done.status_code == 200, done.text
        return up.json()["snapshot"]["id"], rid

    def analyze(sid, task="usability"):
        r = c.post("/api/v1/snapshots/%d/analyze" % sid, headers=hdr,
                   json={"task": task})
        assert r.status_code == 200, r.text
        return r.json()["inference"]

    rows = []
    # ------------------------------------------------ 场景 A：正常样例闭环
    sid1, rid1 = snapflow("拍张照", IMG_A)
    inf1 = analyze(sid1)
    check("A1 结构化结果：标签+分数+版本", inf1["label"] and inf1["score"] is not None
          and inf1["model_version"], str(inf1))
    check("A1 回放路径如实标注", inf1["source"] == "replay" and inf1["playback"])
    ans1 = c.post("/api/v1/assistant/chat", headers=hdr,
                  json={"text": "识别结果"}).json()["answer"]
    check("A2 人话反馈：看到了什么",
          ("画面识别为" in ans1
           and any(w in ans1 for w in ("可用", "模糊", "遮挡", "拒识"))),
          ans1[:60])
    check("A2 分数未校准口径", "未校准" in ans1 and "不代表现实正确率" in ans1, ans1[:60])
    check("A2 不冒充真实推理", "回放标注" in ans1, ans1[:60])
    rows.append(("正常样例", rid1, sid1, inf1["label"], inf1["score"]))

    # ------------------------------------------------ 场景 C：scene 不越界
    infs = analyze(sid1, task="scene")
    ans_scene = c.post("/api/v1/assistant/chat", headers=hdr,
                       json={"text": "是什么场景"}).json()["answer"]
    check("C 场景粗分类边界", "场景粗分类" in ans_scene
          and "不代表精确位置" in ans_scene and "不构成任何安全结论" in ans_scene,
          ans_scene[:80])

    # ------------------------------------------------ 场景 E：误判 → 人工纠正 → 重拍
    occl = _mk_occluded_bytes()
    sid2, rid2 = snapflow("拍张照", occl)          # 再取一张（重拍意图在 E3 单独验）
    inf2 = analyze(sid2)
    check("E1 回放规则把样例判为遮挡（可复现）", inf2["label"] == "occluded", inf2["label"])
    cr = c.post("/api/v1/snapshots/%d/corrections" % sid2, headers=hdr,
                json={"kind": "label", "value": "usable", "note": "同学人工核对后纠正"})
    check("E2 纠错入口生效", cr.status_code == 200
          and cr.json()["correction"]["effective"] == "usable")
    det = c.get("/api/v1/snapshots/%d" % sid2).json()
    check("E2 反馈留痕可查", len(det["corrections"]) == 1
          and det["effective"][1] == "corrected")
    ans2 = c.post("/api/v1/assistant/chat", headers=hdr,
                  json={"text": "识别结果"}).json()["answer"]
    check("E2 答复按人工纠正显示", "已按你的纠正显示" in ans2 and "可用" in ans2,
          ans2[:60])
    rows.append(("误判样例（纠正后）", rid2, sid2, "usable(纠正)", inf2["score"]))

    rp = c.post("/api/v1/assistant/chat", headers=hdr, json={"text": "重拍"})
    rpj = rp.json()
    check("E3 重拍走 image 指令通道（新 id）", rpj["intent"]["action"] == "recapture"
          and rpj["command"]["op"] == "image"
          and rpj["command"]["request_id"] not in (rid1, rid2)
          and "已受理" in rpj["answer"], rpj["answer"][:50])

    # ------------------------------------------------ 输入-结果-反馈对照表
    print()
    print("=== 对照表：输入-结果-反馈三者对应（本周提交项） ===")
    print("%-16s %-22s %-8s %-16s %s" % ("样例", "request_id", "snapshot", "标签", "score"))
    for name, rid, sid, label, score in rows:
        meta = c.get("/api/v1/snapshots/%d" % sid).json()["snapshot"]
        print("%-16s %-22s %-8d %-16s %.2f（shot %s）"
              % (name, rid, sid, label, score,
                 time.strftime("%H:%M:%S", time.localtime(meta["t_server_recv_ms"] / 1000))))
    print()
    print("低分 / 拒识 / 模型错误三类样例：由自动化注入模型输出覆盖（test_vision.py），")
    print("对照表见 docs/week8-samples.md；本脚本只演示可用回放与纠正闭环，")
    print("不把静态标签冒充真实推理证据。")
    print()
    print("结果：%d 项检查，%d 项失败" % (total, len(failed)))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()