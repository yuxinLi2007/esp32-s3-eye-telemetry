"""SQLite 存储层。

分两张表是刻意的设计，不是过度拆分：
  batches  —— 一次成功的 HTTP 上传 = 一条"溯源记录"（谁、哪次启动、NTP 状态、服务端何时收到）
  readings —— 逐个样本，带 seq。同 boot_id 内 seq 不连续 = 确凿的数据丢失证据，
              不需要靠"时间间隔看起来变大了"去猜。

时间戳的可信度不落库成标签，而是原始事实（t_server_recv_ms / t_device_ntp_ms /
ntp_synced）加上一个派生函数 trust_level()。派生值只有一个来源，不会和时间事实漂移。
"""

import sqlite3
import time
from pathlib import Path

NTP_FRESH_THRESHOLD_S = 300
NTP_STALE_THRESHOLD_S = 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    device_mac         TEXT    NOT NULL,
    boot_id            TEXT    NOT NULL,
    fw_version         TEXT,
    seq_first          INTEGER NOT NULL,
    seq_last           INTEGER NOT NULL,
    n_readings         INTEGER NOT NULL,
    dropped_since_last INTEGER NOT NULL DEFAULT 0,
    ntp_synced         INTEGER NOT NULL,
    ntp_sync_age_s     INTEGER,
    t_device_ntp_ms    INTEGER,
    t_server_recv_ms   INTEGER NOT NULL,
    source_ip          TEXT,
    source_ua          TEXT,
    -- 第2周：非空表示这批样本是某条远程指令采的，而不是连续流的一部分。
    -- 两个流的 seq 各自独立计数，混在一起算缺口会把指令采集误报成丢样，
    -- 所以这个字段同时是 query_readings / find_gaps 的分流依据。
    request_id         TEXT
);

CREATE TABLE IF NOT EXISTS readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    device_mac  TEXT    NOT NULL,
    boot_id     TEXT    NOT NULL,
    seq         INTEGER NOT NULL,
    t_device_ms INTEGER NOT NULL,
    ax          REAL,
    ay          REAL,
    az          REAL,
    spl_db      REAL
);

CREATE INDEX IF NOT EXISTS idx_readings_seq     ON readings (boot_id, seq);
CREATE INDEX IF NOT EXISTS idx_readings_batch   ON readings (batch_id);
CREATE INDEX IF NOT EXISTS idx_batches_recv     ON batches (t_server_recv_ms);
CREATE INDEX IF NOT EXISTS idx_batches_request  ON batches (request_id);

-- 采集开关的变更历史。当前状态 = 最后一条，不另设单行表——
-- 同一个事实存两处必然会漂移（和 trust_level 不落库是同一个道理）。
-- 保留历史是为了让界面能把"某段空白"解释成用户主动停止，而不是设备故障：
-- 这两件事在图上长得一模一样，只能靠这里区分。
CREATE TABLE IF NOT EXISTS control_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    collect     INTEGER NOT NULL,
    t_server_ms INTEGER NOT NULL,
    note        TEXT
);
"""


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False：FastAPI 把同步依赖和同步端点分别丢进线程池，
    # 两者不保证落在同一个线程上。连接是每请求新建、用完即关，依赖先于端点返回，
    # 所以只是"跨线程先后使用"，不存在并发访问。默认的线程校验会误报 500。
    conn = sqlite3.connect(path, timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate(conn):
    """给第1周留下的旧库补列。

    CREATE TABLE IF NOT EXISTS 对已存在的表什么也不做，所以新增列必须显式 ALTER。
    加列而不是重建表：旧数据一行不动，request_id 为 NULL 就是"连续流样本"，
    语义天然正确，不需要回填。
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(batches)")}
    if not cols:
        return conn            # 全新的库：还没有表，交给 SCHEMA 直接建对
    if "request_id" not in cols:
        conn.execute("ALTER TABLE batches ADD COLUMN request_id TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_batches_request ON batches (request_id)"
        )
        conn.commit()
    return conn


def init_db(path):
    conn = connect(path)
    # 顺序不能反：SCHEMA 里有 CREATE INDEX ... ON batches(request_id)，
    # 旧库还没这一列时先建索引会直接 OperationalError，服务起不来。
    # 先 migrate 补列（表不存在时是空操作），再建表建索引。
    migrate(conn)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def trust_level(ntp_synced, ntp_sync_age_s):
    """时间戳可信度分级。

    永远以 t_server_recv_ms（服务端收到时刻）为权威时间。
    设备端时间只在 NTP 同步过、且同步足够新鲜时才可参考。
    """
    if not ntp_synced or ntp_sync_age_s is None:
        return "unsynced"
    if ntp_sync_age_s <= NTP_FRESH_THRESHOLD_S:
        return "ntp_fresh"
    if ntp_sync_age_s <= NTP_STALE_THRESHOLD_S:
        return "ntp_stale"
    return "ntp_expired"


def insert_batch(conn, meta):
    """meta: dict，含 device_mac/boot_id/... 和 readings 列表。返回 batch id。"""
    cur = conn.execute(
        """INSERT INTO batches
           (device_mac, boot_id, fw_version, seq_first, seq_last, n_readings,
            dropped_since_last, ntp_synced, ntp_sync_age_s, t_device_ntp_ms,
            t_server_recv_ms, source_ip, source_ua, request_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            meta["device_mac"],
            meta["boot_id"],
            meta.get("fw_version"),
            meta["seq_first"],
            meta["seq_last"],
            len(meta["readings"]),
            meta.get("dropped_since_last", 0),
            1 if meta.get("ntp_synced") else 0,
            meta.get("ntp_sync_age_s"),
            meta.get("t_device_ntp_ms"),
            meta["t_server_recv_ms"],
            meta.get("source_ip"),
            meta.get("source_ua"),
            meta.get("request_id"),
        ),
    )
    batch_id = cur.lastrowid
    conn.executemany(
        """INSERT INTO readings
           (batch_id, device_mac, boot_id, seq, t_device_ms, ax, ay, az, spl_db)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        [
            (
                batch_id,
                meta["device_mac"],
                meta["boot_id"],
                r["seq"],
                r["t_device_ms"],
                r.get("ax"),
                r.get("ay"),
                r.get("az"),
                r.get("spl_db"),
            )
            for r in meta["readings"]
        ],
    )
    conn.commit()
    return batch_id


def query_readings(conn, since_ms=None, limit=2000, device_mac=None,
                   request_id=None, include_command_samples=False):
    """查询样本。

    默认只返回连续流（request_id IS NULL）：指令采集是一次性的独立数据流，
    混进连续流会让第1周的曲线出现假缺口、假重启。
    要单独看某条指令采到的样本，传 request_id；要全都要，传
    include_command_samples=True。被排除的数量由 count_command_samples 另行报出，
    所以"默认不返回"不等于"悄悄藏起来"。
    """
    where, params = [], []
    if since_ms is not None:
        where.append("r.t_device_ms >= ?")
        params.append(since_ms)
    if device_mac:
        where.append("r.device_mac = ?")
        params.append(device_mac)
    if request_id:
        where.append("b.request_id = ?")
        params.append(request_id)
    elif not include_command_samples:
        where.append("b.request_id IS NULL")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)

    rows = conn.execute(
        f"""SELECT r.id, r.device_mac, r.boot_id, r.seq, r.t_device_ms,
                   r.ax, r.ay, r.az, r.spl_db,
                   b.t_server_recv_ms, b.ntp_synced, b.ntp_sync_age_s,
                   b.t_device_ntp_ms, b.dropped_since_last, b.source_ip,
                   b.request_id
            FROM readings r JOIN batches b ON b.id = r.batch_id
            {clause}
            ORDER BY r.id DESC LIMIT ?""",
        params,
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def count_command_samples(conn):
    """指令采集样本的总数。界面上要显示这个数，否则"默认过滤"就变成了看不见的丢弃。"""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM readings r JOIN batches b ON b.id = r.batch_id"
        " WHERE b.request_id IS NOT NULL"
    ).fetchone()
    return row["n"] if row else 0


def trust_counts(readings):
    """窗口内各可信度等级的样本数。

    只报"最后一批"的可信度会误导：一个窗口里可能混着 NTP 同步过和没同步过的批次。
    """
    counts = {}
    for r in readings:
        level = trust_level(r["ntp_synced"], r["ntp_sync_age_s"])
        counts[level] = counts.get(level, 0) + 1
    return counts


def find_gaps(readings):
    """同一 boot_id 内 seq 不连续 = 丢失的样本。

    跨 boot_id 的跳变不算丢失（那是设备重启），但要单独报出来，
    因为非预期重启同样意味着数据不可信。
    """
    gaps = []
    prev = None
    # 流标识 = boot_id + request_id。指令采集的 seq 是它自己那一轮从 0 开始数的，
    # 和连续流的 seq 没有可比性；不分开算就会把"一次指令采集"报成"丢了几百个样本"。
    def stream_of(r):
        rid = r["request_id"] if "request_id" in r.keys() else None
        return r["boot_id"] if rid is None else r["boot_id"] + "#" + rid

    for r in readings:
        if prev is not None and stream_of(prev) == stream_of(r):
            expected = prev["seq"] + 1
            if r["seq"] > expected:
                gaps.append(
                    {
                        "boot_id": r["boot_id"],
                        "after_seq": prev["seq"],
                        "before_seq": r["seq"],
                        "missing": r["seq"] - expected,
                        "after_t_device_ms": prev["t_device_ms"],
                        "before_t_device_ms": r["t_device_ms"],
                        "after_t_server_recv_ms": prev["t_server_recv_ms"],
                        "before_t_server_recv_ms": r["t_server_recv_ms"],
                    }
                )
        prev = r
    return gaps


def get_status(conn):
    last = conn.execute(
        "SELECT * FROM batches ORDER BY id DESC LIMIT 1"
    ).fetchone()

    totals = conn.execute(
        """SELECT
             (SELECT COUNT(*) FROM batches)                        AS total_batches,
             (SELECT COUNT(*) FROM readings)                       AS total_readings,
             (SELECT COALESCE(SUM(dropped_since_last),0) FROM batches) AS total_dropped,
             (SELECT COUNT(DISTINCT boot_id) FROM batches)         AS total_boots"""
    ).fetchone()

    now_ms = int(time.time() * 1000)
    recent = conn.execute(
        "SELECT COUNT(*) AS n FROM readings WHERE t_device_ms >= 0 AND batch_id IN "
        "(SELECT id FROM batches WHERE t_server_recv_ms >= ?)",
        (now_ms - 60_000,),
    ).fetchone()

    boot_ids = [
        r["boot_id"]
        for r in conn.execute(
            "SELECT DISTINCT boot_id FROM batches ORDER BY id DESC LIMIT 20"
        )
    ]

    status = {
        "server_time_ms": now_ms,
        "total_batches": totals["total_batches"],
        "total_readings": totals["total_readings"],
        "total_dropped": totals["total_dropped"],
        "total_boots": totals["total_boots"],
        "readings_last_60s": recent["n"],
        "boot_ids": boot_ids,
        "last_batch": None,
    }

    if last:
        last = dict(last)
        age_ms = now_ms - last["t_server_recv_ms"]
        skew_ms = (
            last["t_device_ntp_ms"] - last["t_server_recv_ms"]
            if last["t_device_ntp_ms"] is not None
            else None
        )
        status["last_batch"] = {
            "device_mac": last["device_mac"],
            "boot_id": last["boot_id"],
            "fw_version": last["fw_version"],
            "source_ip": last["source_ip"],
            "n_readings": last["n_readings"],
            "dropped_since_last": last["dropped_since_last"],
            "t_server_recv_ms": last["t_server_recv_ms"],
            "t_device_ntp_ms": last["t_device_ntp_ms"],
            "age_ms": age_ms,
            "device_clock_skew_ms": skew_ms,
            "ntp_synced": bool(last["ntp_synced"]),
            "ntp_sync_age_s": last["ntp_sync_age_s"],
            "t_trust": trust_level(last["ntp_synced"], last["ntp_sync_age_s"]),
        }
    return status


def get_control(conn, history_limit=50):
    """当前采集开关，附带最近的变更历史。

    没有任何记录时默认"采集开"——旧库升级上来行为不变，也不会因为忘了初始化而停机。
    """
    rows = conn.execute(
        "SELECT collect, t_server_ms, note FROM control_log ORDER BY id DESC LIMIT ?",
        (history_limit,),
    ).fetchall()
    if not rows:
        return {
            "collect": True,
            "t_server_ms": None,
            "changed_ago_ms": None,
            "note": "尚无控制指令，按默认采集",
            "history": [],
        }
    latest = dict(rows[0])
    now_ms = int(time.time() * 1000)
    return {
        "collect": bool(latest["collect"]),
        "t_server_ms": latest["t_server_ms"],
        "changed_ago_ms": now_ms - latest["t_server_ms"],
        "note": latest["note"],
        "history": [dict(r) for r in reversed(rows)],
    }


def set_control(conn, collect, note=None):
    """设置采集开关。

    状态没变就不写新记录，否则设备每轮轮询、用户每次误点都会往历史里塞重复行，
    历史就不再是"变更历史"了。
    """
    if get_control(conn)["collect"] == collect:
        return get_control(conn)
    conn.execute(
        "INSERT INTO control_log (collect, t_server_ms, note) VALUES (?,?,?)",
        (1 if collect else 0, int(time.time() * 1000), note),
    )
    conn.commit()
    return get_control(conn)
