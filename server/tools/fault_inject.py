"""故障注入：向一个隔离实例灌入"带缺陷"的批次流，验证界面是否如实报警。

存在的意义：本项目最核心的要求是"无数据与上传失败必须在界面上被看见"，
而那是一个否定性要求——正常情况下永远看不到。必须主动制造失败才能验证。

刻意制造四种失败，它们必须都在界面上看得见：
  1) 同 boot 内 seq 跳号    -> 板上丢样（图上红带 + "检测到丢样"）
  2) dropped_since_last > 0 -> 上传失败导致环形缓冲溢出，板上记账
  3) 更早的批次 NTP 未同步  -> 时间戳可信度不足
  4) 之后彻底停止上报       -> 设备失联（红色报警条 + "此后无数据"空白区）

用法（务必用独立库，别污染真实数据）：
    TELEMETRY_DB=/tmp/fault.db python -m uvicorn app:app --port 8001   # 另开一个终端
    python tools/fault_inject.py --url http://127.0.0.1:8001
    node tools/ui_check.mjs --origin http://127.0.0.1:8001
"""
import argparse
import json
import time
import urllib.request

INTERVAL_MS = 50
BOOT = "FAULT001"
MAC = "94:A9:90:1C:6F:D4"


def post(url, seqs, t_base, dropped=0, ntp_synced=True, ntp_age=20):
    readings = [
        {
            "seq": s,
            "t_device_ms": t_base + i * INTERVAL_MS,
            "ax": 0.02 + 0.004 * ((i % 7) - 3),
            "ay": -0.01,
            "az": 0.999,
            "spl_db": 52.0 + 3.0 * ((i % 5) - 2),
        }
        for i, s in enumerate(seqs)
    ]
    body = {
        "device_mac": MAC,
        "boot_id": BOOT,
        "fw_version": "0.1.0",
        "ntp_synced": ntp_synced,
        "ntp_sync_age_s": ntp_age if ntp_synced else None,
        "t_device_ntp_ms": int(time.time() * 1000) if ntp_synced else None,
        "dropped_since_last": dropped,
        "readings": readings,
    }
    req = urllib.request.Request(
        url + "/api/v1/ingest",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)["batch_id"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8001")
    ap.add_argument("--batch", type=int, default=40, help="每批样本数")
    ap.add_argument("--gap", type=int, default=7, help="人为跳过的 seq 数量")
    ap.add_argument("--silence", type=int, default=30, help="最后静默秒数(触发失联)")
    args = ap.parse_args()

    seq, t = 0, 0
    n = args.batch

    def normal(count, **kw):
        nonlocal seq, t
        for _ in range(count):
            post(args.url, range(seq, seq + n), t, **kw)
            seq += n
            t += 2000
            time.sleep(2.0)

    print("1) 3 个正常批次，但 NTP 未同步（触发可信度告警）")
    normal(3, ntp_synced=False)

    print("2) 3 个 NTP 新鲜的正常批次")
    normal(3, ntp_age=15)

    print("3) 制造 seq 跳号：丢 %d 个样本" % args.gap)
    post(args.url, [seq - 1, seq + args.gap], t, ntp_age=16)
    seq += args.gap + 1
    t += 2000
    time.sleep(2.0)

    print("4) 3 个正常批次")
    normal(3, ntp_age=17)

    print("5) 上报 dropped_since_last=5（模拟上传失败导致板上丢弃）")
    post(args.url, range(seq, seq + n), t, dropped=5, ntp_age=18)
    seq += n

    print("6) 停止上报 %d 秒，等待失联判定" % args.silence)
    for i in range(1, args.silence + 1):
        time.sleep(1)
        if i % 5 == 0:
            print("   已静默 %ds" % i)
    print("完成，共灌入 %d 个样本" % seq)


if __name__ == "__main__":
    main()
