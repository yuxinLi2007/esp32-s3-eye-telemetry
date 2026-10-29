"""第10周：多源观测状态 —— 区分"观测时刻"与"接收时刻"，未尝试/失败/成功/陈旧。

本周问题：定位、运动和网络信息不是同时到达，怎样避免把旧数据当成当前状态？

口径（对应当堂验证）：
  1. 补传数据按原观测时间解释：状态与年龄一律用 t_obs_ms（设备观测时刻）计算，
     接收时刻 t_recv_ms 只说明"服务端何时收到"，两者分开保存、分开展示；
  2. GNSS 无有效解（no_fix）是"失败"的一种，但不是设备故障——reason 固定为
     no_fix，任何展示与答复都不得写成"设备故障"；
  3. 无证据时不补造位置：没有任何定位上报时状态 = 未尝试，绝不编造坐标。

来源划分：gnss（定位）/ net（网络在线）经 ingest.sources 事件上报；
         imu（惯性）从既有 readings 连续流样本派生，不另占上报字段。
来源缺失 = 未尝试（not_attempted），不假定全部硬件均已上线：
端侧没接 GPS 就是"没试过"，不是"定位失败"。
"""
import json
import os
import time

# ---------------------------------------------------------------- 配置
SOURCES_EVENT = ("gnss", "net")      # 经 ingest.sources 上报告的事件源
SOURCES_DERIVED = ("imu",)           # 从 readings 派生的源
SOURCES = SOURCES_EVENT + SOURCES_DERIVED

SRC_TEXT = {"gnss": "GNSS 定位", "net": "网络", "imu": "惯性"}

# 陈旧阈值（毫秒）：观测年龄超过该值 → 陈旧（stale），不冒充当前状态。
# 三源独立：定位 60s、惯性 30s、网络 90s；可用环境变量覆盖。
_STALE_DEFAULT = {"gnss": 60_000, "net": 90_000, "imu": 30_000}
STALE_BY_SOURCE = {
    src: int(os.environ.get("SOURCE_STALE_%s_MS" % src.upper(),
                            str(default)) or default)
    for src, default in _STALE_DEFAULT.items()
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_mac  TEXT    NOT NULL,
    boot_id     TEXT    NOT NULL,
    source      TEXT    NOT NULL,
    state       TEXT    NOT NULL,           -- ok | no_fix | offline
    t_obs_ms    INTEGER NOT NULL,           -- 观测时刻（设备侧；补传=原观测时间）
    t_recv_ms   INTEGER NOT NULL,           -- 接收时刻（服务端）
    replay      INTEGER NOT NULL DEFAULT 0, -- 回放数据：明确标注，非真实上报
    raw         TEXT,                       -- 原始观测 JSON，原样保留
    batch_id    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_source_events_dev
    ON source_events (device_mac, source, t_obs_ms DESC);
"""


def ensure_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def now_ms():
    return int(time.time() * 1000)


# ---------------------------------------------------------------- 写入
def record(conn, *, device_mac, boot_id, source, state, t_obs_ms, t_recv_ms,
           raw=None, replay=False, batch_id=None):
    """记录一条来源事件（原始观测原样保留在 raw JSON 里）。"""
    ensure_schema(conn)
    if source not in SOURCES_EVENT:
        raise ValueError("source 无效：%r（可用：%s）"
                         % (source, "、".join(SOURCES_EVENT)))
    if state not in ("ok", "no_fix", "offline"):
        raise ValueError("state 无效：%r" % state)
    cur = conn.execute(
        """INSERT INTO source_events
           (device_mac, boot_id, source, state, t_obs_ms, t_recv_ms,
            replay, raw, batch_id)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (device_mac, boot_id, source, state, int(t_obs_ms), int(t_recv_ms),
         1 if replay else 0,
         json.dumps(raw, ensure_ascii=False) if raw is not None else None,
         batch_id),
    )
    conn.commit()
    return cur.lastrowid


def _last_event(conn, device_mac, source, state=None):
    """该源最近一条"到达"的事件（按接收时刻排序）。

    语义：最近到达的消息是当前状态的事实来源；而它的**观测时刻**决定年龄与
    新鲜度（补传按原观测时间解释）。两者分开，正是本周要区分的两件事——
    不按观测时刻当"最新"，否则一条新到的补传会被更晚观测的旧消息盖掉。
    """
    if state:
        row = conn.execute(
            "SELECT * FROM source_events WHERE device_mac=? AND source=?"
            " AND state=? ORDER BY t_recv_ms DESC, id DESC LIMIT 1",
            (device_mac, source, state),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM source_events WHERE device_mac=? AND source=?"
            " ORDER BY t_recv_ms DESC, id DESC LIMIT 1",
            (device_mac, source),
        ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------- 派生（imu）
def _imu_state(conn, device_mac, now):
    """惯性源：取连续流里观测时刻最新的一个样本（不要求与其它源同时采样）。"""
    row = conn.execute(
        """SELECT r.t_device_ms, r.ax, r.ay, r.az
           FROM readings r JOIN batches b ON b.id = r.batch_id
           WHERE r.device_mac = ? AND b.request_id IS NULL
           ORDER BY r.t_device_ms DESC, r.id DESC LIMIT 1""",
        (device_mac,),
    ).fetchone()
    if row is None:
        return {"status": "not_attempted", "reason": None, "detail": None,
                "last_obs_ms": None, "last_recv_ms": None, "age_ms": None,
                "last_success_obs_ms": None, "replay": False}
    age = now - row["t_device_ms"]
    status = "success" if age <= STALE_BY_SOURCE["imu"] else "stale"
    return {"status": status, "reason": None,
            "detail": "ax=%.3f ay=%.3f az=%.3f" % (row["ax"] or 0,
                                                    row["ay"] or 0,
                                                    row["az"] or 0),
            "last_obs_ms": row["t_device_ms"], "last_recv_ms": None,
            "age_ms": age, "last_success_obs_ms": row["t_device_ms"],
            "replay": False}


# ---------------------------------------------------------------- 状态计算
def snapshot(conn, device_mac, now=None):
    """各源独立状态：未尝试/失败/成功/陈旧 + 观测/接收时刻 + 年龄。

    年龄一律按观测时刻算（补传按原观测时间解释）；
    接收时刻单独展示，说明"服务端何时收到"，两者不混。
    """
    now = now_ms() if now is None else now
    out = {}
    for src in SOURCES_EVENT:
        last = _last_event(conn, device_mac, src)
        last_ok = _last_event(conn, device_mac, src, state="ok")
        out[src] = _event_state(src, last, last_ok, now)
    out["imu"] = _imu_state(conn, device_mac, now)
    return out


def _event_state(src, last, last_ok, now):
    if last is None:
        return {"status": "not_attempted", "reason": None, "detail": None,
                "last_obs_ms": None, "last_recv_ms": None, "age_ms": None,
                "last_success_obs_ms": None, "replay": False}
    age = now - last["t_obs_ms"]
    replay = bool(last["replay"])
    raw = json.loads(last["raw"]) if last["raw"] else None
    base = {"last_obs_ms": last["t_obs_ms"], "last_recv_ms": last["t_recv_ms"],
            "age_ms": age, "replay": replay, "raw": raw}
    if last["state"] != "ok":
        base.update({
            "status": "failed", "reason": last["state"],
            "last_success_obs_ms": (last_ok["t_obs_ms"] if last_ok else None),
            "detail": "观测于 %s（%s 前），%s"
                      % (_clock(last["t_obs_ms"]), _age_human(age, now),
                         "无有效解" if last["state"] == "no_fix" else "离线"),
        })
        return base
    # state=ok：看观测年龄是否超过该源阈值
    stale = STALE_BY_SOURCE.get(src, 60_000)
    status = "stale" if age > stale else "success"
    base.update({
        "status": status, "reason": None,
        "last_success_obs_ms": last["t_obs_ms"],
        "detail": "观测于 %s（%s 前）" % (_clock(last["t_obs_ms"]),
                                          _age_human(age, now)),
    })
    return base


def _clock(ms):
    """把毫秒时间戳变成可读时钟（本地时区，演示够用）。"""
    return time.strftime("%H:%M:%S", time.localtime(ms / 1000.0))


def _age_human(age_ms, now_ms=None):
    """年龄人话：秒/分钟/小时，超过一天按天数。"""
    if age_ms is None:
        return "—"
    sec = max(0, int(age_ms / 1000))
    if sec < 60:
        return "%d 秒" % sec
    if sec < 3600:
        return "%d 分钟" % (sec // 60)
    if sec < 86400:
        return "%d 小时" % (sec // 3600)
    return "%d 天" % (sec // 86400)


# ---------------------------------------------------------------- 人话（用户提示对照）
def answer(conn, device_mac, now=None):
    """多源状态一页人话：与多源状态页对照，回答"各源现在什么情况"。

    诚实口径（对应当堂验证）：
      - 补传/陈旧 → 明说"不冒充当前状态"；
      - no_fix → 明说"不是设备故障"；有旧位置 → "有旧位置但本次 no_fix"；
      - 无证据 → "未尝试，不假定硬件已上线" / "不补造位置"。
    """
    now = now_ms() if now is None else now
    sts = snapshot(conn, device_mac, now)
    lines = []
    order = ("gnss", "imu", "net")
    for src in order:
        s = sts[src]
        tag = "（回放数据，非真实上报）" if s.get("replay") else ""
        if s["status"] == "not_attempted":
            lines.append("%s：无任何上报%s——未尝试，不假定硬件已上线"
                         % (SRC_TEXT[src], tag))
            continue
        if s["status"] == "failed":
            if src == "gnss" and s["reason"] == "no_fix":
                base = "GNSS 定位：无有效解（不是设备故障）"
                if s.get("last_success_obs_ms"):
                    base += "；有旧位置但本次 no_fix——上次有效定位在 %s 前"
                    base += "（陈旧，不冒充当前位置）"
                    base = base % _age_human(now - s["last_success_obs_ms"], now)
                else:
                    base += "；从未有过有效定位，不补造位置"
                lines.append(base + tag + "。")
            else:
                lines.append("%s：%s（失败态）%s。"
                             % (SRC_TEXT[src], s["reason"], tag))
            continue
        # success / stale
        verb = "正常" if s["status"] == "success" else "数据陈旧"
        warn = "，不冒充当前状态" if s["status"] == "stale" else ""
        lines.append("%s：%s——观测于 %s（%s 前）%s%s。"
                     % (SRC_TEXT[src], verb, _clock(s["last_obs_ms"]),
                        _age_human(now - s["last_obs_ms"], now), warn, tag))
    # 案例对照："网络在线但位置未更新"
    g, n = sts["gnss"], sts["net"]
    if n["status"] == "success" and g["status"] in ("stale", "failed",
                                                    "not_attempted"):
        if g["status"] == "failed":
            gword = "本次无有效解"
        elif g["status"] == "not_attempted":
            gword = "从未上报定位"
        else:
            gword = "位置停留在 %s 前" % _age_human(
                now - (g.get("last_success_obs_ms") or g["last_obs_ms"]), now)
        lines.append("案例：网络在线但位置未更新——网络在线，而定位%s，两者不同步，"
                     "答复不会把旧位置说成当前位置。" % gword)
    return "；".join(lines) + "。"