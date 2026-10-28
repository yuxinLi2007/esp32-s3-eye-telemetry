"""第7周当堂验证：按需环境快照的完整 HTTP 场景（对着 127.0.0.1:8001 跑）。

用法：
  python tools/demo_week7.py [--origin http://127.0.0.1:8001] [--token X]

场景（一个不漏地对到当堂验证 / 提交要求）：
  A. 语音/文字入口触发：chat "拍张照" -> image 指令 -> 设备领取 -> 上传图像
     （输入样例：X-Captured-At-Ms 设备声称采集时刻 + 宽高；服务端回传权威
     t_server_recv_ms）-> done 回执 -> 结果体带 image_id / captured_at_ms /
     sha256；连续两次采集拿到两张不同的图、两个不同的服务端时刻——
     上次成功图像绝不被标为本次采集成功。
  B. 失败保持明确状态：回执 failed(error_code=occluded) -> 指令详情与
     自然语言状态回答（"图像采集完成了吗"）都如实显示"镜头遮挡"。
  C. 迟到/重放拒收：已终态的指令再传图 -> HTTP 409 already_terminal。

退出码 0 = 全部通过；1 = 有场景失败。
"""
import argparse
import sys
import time

import httpx

MAC = "94:A9:90:1C:6F:D4"
BOOT = "W7DEMO"

IMG_ENV_A = b"\xff\xd8\xff\xe0 " + b"DEMO-ENV-IMG-A" * 8    # 输入样例：一张"环境图"
IMG_ENV_B = b"\xff\xd8\xff\xe0 " + b"DEMO-ENV-IMG-B" * 8


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--origin", default="http://127.0.0.1:8001")
    ap.add_argument("--token", default="")
    args = ap.parse_args()
    base = args.origin.rstrip("/")
    hdr = {"X-Control-Token": args.token} if args.token else {}
    failed = []
    total = 0

    def check(name, cond, extra=""):
        nonlocal total
        total += 1
        print(("PASS  " if cond else "FAIL  ") + name + ("  " + extra if extra else ""))
        if not cond:
            failed.append(name)

    c = httpx.Client(base_url=base, timeout=10.0)
    check("服务端可达 /health", c.get("/health").status_code == 200)

    # 先让系统"见过"这台设备（device 范围与第 4 周一致）
    now = int(time.time() * 1000)
    r = c.post("/api/v1/ingest", json={
        "device_mac": MAC, "boot_id": "boot-seed", "fw_version": "0.8.0",
        "ntp_synced": True, "ntp_sync_age_s": 5, "t_device_ntp_ms": now,
        "dropped_since_last": 0,
        "readings": [{"seq": 0, "t_device_ms": now - 100, "ax": 0.1,
                      "ay": 0.2, "az": 1.0, "spl_db": 42.0}],
    })
    check("预置已知设备", r.status_code == 201)

    # ------------------------------------------------ 场景 A：两次成功各拿各的图
    times_ok, refs = [], []
    for i, img in enumerate((IMG_ENV_A, IMG_ENV_B), 1):
        tag = "第 %d 次" % i
        r = c.post("/api/v1/assistant/chat", headers=hdr,
                   json={"text": "拍张照"})
        check(tag + " 意图=image", r.status_code == 200
              and r.json()["intent"]["action"] == "image", str(r.json()["intent"]))
        j = r.json()
        check(tag + " 回答不提前声称成功", "已受理" in j["answer"]
              and "成功" not in j["answer"], j["answer"][:40])
        rid = j["command"]["request_id"]
        claim = c.post("/api/v1/commands/claim",
                       content="%s|%s%d|0.8.0" % (MAC, BOOT, i))
        check(tag + " 设备领取", claim.status_code == 200 and rid in claim.text)
        up = c.post("/api/v1/commands/%s/image" % rid, content=img,
                    headers={"X-Device-Mac": MAC, "X-Boot-Id": "%s%d" % (BOOT, i),
                             "Content-Type": "image/jpeg",
                             "X-Captured-At-Ms": str(now + i),   # 输入样例：设备声称的采集时刻
                             "X-Width": "640", "X-Height": "480"})
        check(tag + " 图像上传", up.status_code == 200, str(up.json() if up.status_code == 200 else up.text)[:80])
        snap = up.json()["snapshot"]
        done = c.post("/api/v1/commands/%s/result" % rid,
                      json={"state": "done", "boot_id": "%s%d" % (BOOT, i)})
        check(tag + " done 回执", done.status_code == 200)
        res = done.json()["command"]["result"]
        times_ok.append(res["captured_at_ms"] == snap["t_server_recv_ms"])
        refs.append((rid, res["image_id"], res["captured_at_ms"]))
        print("    %s: request_id=%s image_id=%s 采集时刻(服务端权威)=%d"
              % (tag, rid, res["image_id"], res["captured_at_ms"]))
    check("两次采集时刻（服务端）各不相同", refs[0][2] != refs[1][2])
    check("两次采集是两张不同的图", refs[0][1] != refs[1][1])
    g1 = c.get("/api/v1/snapshots/%d/image" % refs[0][1])
    g2 = c.get("/api/v1/snapshots/%d/image" % refs[1][1])
    check("图像可取回且互不相同", g1.status_code == 200 and g2.status_code == 200
          and g1.content == IMG_ENV_A and g2.content == IMG_ENV_B)
    check("结果体采集时刻 = 服务端接收时刻", all(times_ok))
    only1 = c.get("/api/v1/snapshots", params={"request_id": refs[0][0]}).json()
    check("按 request_id 过滤不串台", only1["count"] == 1
          and only1["snapshots"][0]["id"] == refs[0][1])

    # ------------------------------------------------ 场景 B：失败保持明确状态
    r = c.post("/api/v1/assistant/chat", headers=hdr, json={"text": "看看环境"})
    check("B 语音入口 '看看环境' 触发图像", r.json()["intent"]["action"] == "image")
    rid = r.json()["command"]["request_id"]
    claim = c.post("/api/v1/commands/claim", content="%s|%sF|0.8.0" % (MAC, BOOT))
    check("B 设备领取", claim.status_code == 200)
    fb = c.post("/api/v1/commands/%s/result" % rid,
                json={"state": "failed", "boot_id": "%sF" % BOOT,
                      "error_code": "occluded",
                      "error_message": "lens occluded"})
    check("B 回执 failed(occluded)", fb.status_code == 200)
    detail = c.get("/api/v1/commands/%s" % rid).json()["command"]
    check("B 失败状态保持可见", detail["state"] == "failed"
          and detail["error_code"] == "occluded")
    sr = c.post("/api/v1/assistant/chat", headers=hdr,
                json={"text": "图像采集完成了吗"})
    check("B 状态回答翻译成人话", "镜头遮挡" in sr.json()["answer"], sr.json()["answer"])

    # ------------------------------------------------ 场景 C：迟到图像拒收
    late = c.post("/api/v1/commands/%s/image" % rid, content=IMG_ENV_B,
                  headers={"X-Device-Mac": MAC, "X-Boot-Id": "%sF" % BOOT,
                           "Content-Type": "image/jpeg"})
    check("C 终态后补传图像被拒", late.status_code == 409
          and late.json()["error"]["code"] == "already_terminal")

    print()
    print("结果：%d 项检查，%d 项失败" % (total, len(failed)))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()