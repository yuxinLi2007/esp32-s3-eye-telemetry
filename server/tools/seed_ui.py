#!/usr/bin/env python3
"""为 ui_check.mjs 预置一份确定性的演示数据库（无需启动服务端）。

直接调用 db.py / commands.py 写库：生成约 4000 个样本，刻意注入四类可判定的现象，
让 `node tools/ui_check.mjs` 的输出包含分段/缺口断言的完整素材：
  - 同 boot 内 seq 跳号 200 → "丢样"红带；
  - boot_id 变化           → "设备重启"红带；
  - 采集停止区间内的 12s 上传空洞 → "已停止采集"中性带（control_log 覆盖该空洞）；
  - 尾部一批 NTP 未同步样本 → 时间戳可信度不足（warn 报警链）。

用法：
  python server/tools/seed_ui.py [--db <路径>] [--mac 94:A9:90:1C:6F:D4]
   默认写到 server/data/ui-seed.db（已在 .gitignore 内）。
"""

import argparse
import time
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import commands  # noqa: E402
import db  # noqa: E402

SAMPLE_MS = 50          # 与固件一致：20 Hz
BATCH_SIZE = 500        # 与 app.MAX_READINGS_PER_BATCH 一致
MAC = "94:A9:90:1C:6F:D4"
T0_OFFSET_S = 24        # 第一条批次的接收时刻 = now - 24s


def batch_of(conn, device_mac, boot_id, seq_first, t_recv_ms, ntp_synced=True,
             ntp_age_s=20, dropped=0):
    """插一批样本；seq 从 seq_first 连续递增，t_device_ms 按 20Hz 单调递增。"""
    readings = []
    for i in range(BATCH_SIZE):
        seq = seq_first + i
        readings.append({
            "seq": seq,
            "t_device_ms": seq * SAMPLE_MS,
            "ax": 0.01, "ay": -0.02, "az": 1.0,
            "spl_db": 40.0 + (seq % 17),   # 有点波动，避免画成一条直线
        })
    return db.insert_batch(conn, {
        "device_mac": device_mac,
        "boot_id": boot_id,
        "fw_version": "0.1.0",
        "seq_first": seq_first,
        "seq_last": seq_first + BATCH_SIZE - 1,
        "dropped_since_last": dropped,
        "ntp_synced": ntp_synced,
        "ntp_sync_age_s": ntp_age_s if ntp_synced else None,
        "t_device_ntp_ms": t_recv_ms - 800 if ntp_synced else None,
        "t_server_recv_ms": t_recv_ms,
        "source_ip": "127.0.0.1",
        "source_ua": "seed_ui.py",
        "readings": readings,
    })


def main():
    ap = argparse.ArgumentParser(description="为 ui_check 预置演示数据")
    ap.add_argument("--db", default=str(ROOT / "data" / "ui-seed.db"))
    ap.add_argument("--mac", default=MAC)
    args = ap.parse_args()

    conn = db.init_db(args.db)
    commands.init_schema(conn)
    now = int(time.time() * 1000)
    t0 = now - T0_OFFSET_S * 1000
    ticks = [t0 + i * 2000 for i in range(6)]   # t0 .. t0+10s：5 批（b1..b5）
    mac = args.mac.upper()

    seq = 0
    for t in ticks[:5]:                          # boot SEED01，seq 0..2499（连续）
        batch_of(conn, mac, "SEED01", seq, t)
        seq += BATCH_SIZE

    # 采集停止区间：t0+6s 停，t0+22s 恢复（覆盖下面 12s 上传空洞）
    db.set_control(conn, False)
    conn.execute(
        "UPDATE control_log SET t_server_ms=?, note=? WHERE id=(SELECT MAX(id) FROM control_log)",
        (t0 + 6_000, "seed: 演示停止区间"),)
    db.set_control(conn, True)
    conn.execute(
        "UPDATE control_log SET t_server_ms=?, note=? WHERE id=(SELECT MAX(id) FROM control_log)",
        (t0 + 22_000, "seed: 演示恢复采集"),)

    # b6：同一 boot，seq 从 2700 起（2499 → 2700 跳号 200 = 丢样）
    t6 = t0 + 20_000
    batch_of(conn, mac, "SEED01", 2700, t6)
    # boot 变化：SEED02（重启），随后一批 NTP 未同步（时间戳可信度不足）
    batch_of(conn, mac, "SEED02", 0, t0 + 22_000)
    batch_of(conn, mac, "SEED02", 500, t0 + 24_000, ntp_synced=False)

    # 指令面板给两条：一条完成（ping），一条等待领取（capture）
    row, _ = commands.create(conn, device_mac=mac, op="ping",
                             client_token="seed-ping-1", note="seed: ping")
    commands.claim(conn, device_mac=mac, boot_id="SEED02", fw_version="0.1.0")
    commands.apply_result(conn, row["request_id"], state="done", boot_id="SEED02",
                          result={"uptime_s": 120, "rssi_dbm": -45, "free_heap": 220000})
    commands.create(conn, device_mac=mac, op="capture",
                    params={"n": 40, "interval_ms": 50},
                    client_token="seed-capture-1", note="seed: capture")

    total = conn.execute("SELECT COUNT(*) AS n FROM readings").fetchone()["n"]
    n_boot = conn.execute("SELECT COUNT(DISTINCT boot_id) AS n FROM batches").fetchone()["n"]
    n_batch = conn.execute("SELECT COUNT(*) AS n FROM batches").fetchone()["n"]
    n_cmd = conn.execute("SELECT COUNT(*) AS n FROM commands").fetchone()["n"]
    conn.close()

    print(f"seed ok: {args.db}")
    print(f"  readings={total}  batches={n_batch}"
          f"  boot_id 数={n_boot}  commands={n_cmd}")


if __name__ == "__main__":
    main()