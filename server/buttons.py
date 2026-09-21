"""第3周：按键事件闭环 —— 板上按键上报、Web 实时展示、Web 回应/取消回传设备。

延续前两周的三条口径：数据来源可追溯 / 时间戳可信度明确 / 失败必须可见。

  - 一次按键 = 一条 button_events 记录。幂等键是 (device_mac, boot_id, press_seq)：
    板端上传失败会重试，重试绝不能变成"用户按了两次"。
  - 权威时间是 t_server_ms（服务端收到的时刻）。板端时间照旧只作参考，
    并按第1周的口径带上 ntp_synced / ntp_sync_age_s，可信度由前端按同一规则打标。
  - Web 的"回应/取消"不新造一条下行通道：它通过 commands.create() 生成一条
    op="notify" 的指令，复用第2周整套领取/状态机/超时/审计。
    按钮事件表只记"这次回应产生了哪条指令"（request_id），
    指令自己的生死（expired/timeout/done）在 commands 表里看得见——
    不在两处各存一份状态，事件行里的 decision 只是"用户点了什么"的记录。
"""

import sqlite3

import commands
from commands import BOOT_RE, MAC_RE, CommandError, now_ms

# ---------------------------------------------------------------- 事件状态
# 事件本身只有三态：收到 / 已回应(ack) / 已取消(cancel)。
# 刻意不加"设备已确认"这个状态：设备执行 notify 指令的结果已经完整记录在
# commands 表里（done/failed/timeout/expired），再抄一份到这里就是第二个
# 事实来源，两边迟早对不上。界面要显示"设备真的收到回应了吗"，
# 看 request_id 关联的那条指令的状态即可。
RECEIVED = "received"
ACKED = "acked"
CANCELLED = "cancelled"

DECISION_TO_STATE = {"ack": ACKED, "cancel": CANCELLED}
DECISIONS = tuple(DECISION_TO_STATE)

STATE_TEXT = {
    RECEIVED: "待回应",
    ACKED: "已回应",
    CANCELLED: "已取消",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS button_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    device_mac        TEXT NOT NULL,
    boot_id           TEXT NOT NULL,
    -- press_seq 在"本次启动"内自增，配合 boot_id 构成幂等键：
    -- 重启后 seq 从 0 重来也没关系，boot_id 变了就是新的一次启动。
    press_seq         INTEGER NOT NULL,
    fw_version        TEXT,
    -- 板端事实（全部只作参考，权威时间是 t_server_ms）：
    t_press_uptime_ms INTEGER,          -- 按下时刻（板上单调时钟）
    t_device_ntp_ms   INTEGER,          -- 按下时刻（板端 NTP 换算，可为空）
    ntp_synced        INTEGER NOT NULL DEFAULT 0,
    ntp_sync_age_s    INTEGER,
    queue_dropped     INTEGER NOT NULL DEFAULT 0,  -- 板端队列里发不出去而丢弃的历史按键数
    t_server_ms       INTEGER NOT NULL,            -- 权威时间：服务端收到的时刻
    state             TEXT NOT NULL DEFAULT 'received',
    decision          TEXT,             -- 最近一次回应的决定：ack / cancel
    request_id        TEXT,             -- 最近一次回应产生的 notify 指令
    t_decided_ms      INTEGER,
    decided_by        TEXT,
    -- 同一事件可以被多次回应（例如上一条 notify 因设备离线 expired，用户点重发）。
    -- 每一次回应都对应一条新指令；这里只留最近一次的引用，历史在 commands 表里
    -- （params_json 带 event_id，可反查），不必再抄一份。
    respond_count     INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_button_press
    ON button_events (device_mac, boot_id, press_seq);
CREATE INDEX IF NOT EXISTS idx_button_state ON button_events (state, t_server_ms);
CREATE INDEX IF NOT EXISTS idx_button_rid   ON button_events (request_id);
"""


def migrate(conn):
    """给已建过的 button_events 表补后加的列（与 commands.migrate 同一套路）。

    现在还没有要补的列；函数先立在这里，是因为"升级不删库"从第一列开始就要成立。
    """
    return conn


def init_schema(conn):
    conn.executescript(SCHEMA)
    migrate(conn)
    conn.commit()
    return conn


# ---------------------------------------------------------------- 设备侧
def record_press(conn, *, device_mac, boot_id, press_seq, fw_version=None,
                 t_press_uptime_ms=None, t_device_ntp_ms=None, ntp_synced=False,
                 ntp_sync_age_s=None, queue_dropped=0, now=None):
    """记下一次按键。返回 (row, deduped)。

    deduped=True 表示 (mac, boot_id, press_seq) 已经存在——板端的重试撞上了
    自己上一次的成功上传（或并发重复请求）。拿回原来那条，不新增：
    "用户按了几下"这个事实只能由板上的序号决定，网络重试不能把它变多。
    """
    now = now_ms() if now is None else now
    mac = (device_mac or "").strip().upper()
    if not MAC_RE.match(mac):
        raise CommandError("device_mac 格式非法：%r（应为 AA:BB:CC:DD:EE:FF）"
                           % device_mac, "bad_mac")
    boot = (boot_id or "").strip()
    if not BOOT_RE.match(boot):
        raise CommandError("boot_id 格式非法：%r" % boot_id, "bad_boot_id")
    if not isinstance(press_seq, int) or isinstance(press_seq, bool) or press_seq < 0:
        raise CommandError("press_seq 必须是非负整数，收到 %r" % press_seq, "bad_seq")

    try:
        conn.execute(
            """INSERT INTO button_events
               (device_mac, boot_id, press_seq, fw_version, t_press_uptime_ms,
                t_device_ntp_ms, ntp_synced, ntp_sync_age_s, queue_dropped,
                t_server_ms, state)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (mac, boot, press_seq, fw_version, t_press_uptime_ms,
             t_device_ntp_ms, 1 if ntp_synced else 0, ntp_sync_age_s,
             int(queue_dropped or 0), now, RECEIVED),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        # sqlite 的报错文本是 "UNIQUE constraint failed: button_events.列名..."，
        # 不带索引名；这里按表名+列名认，别的一律当数据库错误如实抛出。
        text = str(exc)
        if not ("UNIQUE constraint failed" in text and "button_events" in text):
            raise CommandError("写入按键事件失败：%s" % exc, "db_error", 500)
        conn.rollback()
        row = _by_key(conn, mac, boot, press_seq)
        if row is None:
            raise CommandError("幂等键冲突但查不到原记录：%s" % exc, "db_error", 500)
        return row, True
    return _by_key(conn, mac, boot, press_seq), False


def _by_key(conn, mac, boot, press_seq):
    return conn.execute(
        "SELECT * FROM button_events WHERE device_mac=? AND boot_id=? AND press_seq=?",
        (mac, boot, press_seq),
    ).fetchone()


# ---------------------------------------------------------------- 查询
def get_event(conn, event_id):
    return conn.execute(
        "SELECT * FROM button_events WHERE id = ?", (event_id,)
    ).fetchone()


def to_dict(row, now=None, command=None):
    d = dict(row)
    now = now_ms() if now is None else now
    d["ntp_synced"] = bool(d["ntp_synced"])
    d["state_text"] = STATE_TEXT.get(d["state"], d["state"])
    d["age_ms"] = now - d["t_server_ms"]
    # 关联 notify 指令的当前状态。事件行只存 request_id，不存指令状态的副本：
    # 指令状态每次现查，永远和指令面板看到的是同一个事实。
    d["command"] = command
    return d


def _commands_of(conn, rids):
    if not rids:
        return {}
    q = ("SELECT request_id, state, error_code FROM commands"
         " WHERE request_id IN (%s)" % ",".join("?" * len(rids)))
    out = {}
    for c in conn.execute(q, rids):
        out[c["request_id"]] = {
            "request_id": c["request_id"],
            "state": c["state"],
            "state_text": commands.STATE_TEXT.get(c["state"], c["state"]),
            "is_terminal": c["state"] in commands.TERMINAL,
            "error_code": c["error_code"],
        }
    return out


def command_of(conn, request_id):
    """单条 notify 指令的当前状态摘要（给 app 层的单事件响应体用）。"""
    if not request_id:
        return None
    return _commands_of(conn, [request_id]).get(request_id)


def list_events(conn, *, limit=30, device_mac=None, state=None, now=None):
    now = now_ms() if now is None else now
    # 先结算指令超时：界面上"notify 指令是否还活着"必须和指令面板一致。
    # 复用同一个惰性 sweep，不另起线程、不另存状态。
    commands.sweep(conn, now)
    where, params = [], []
    if device_mac:
        where.append("device_mac = ?")
        params.append(device_mac.strip().upper())
    if state:
        states = [s.strip() for s in state.split(",") if s.strip()]
        for s in states:
            if s not in STATE_TEXT:
                raise CommandError("未知按键事件状态 %r" % s, "bad_state")
        if states:
            where.append("state IN (%s)" % ",".join("?" * len(states)))
            params.extend(states)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    rows = conn.execute(
        "SELECT * FROM button_events %s ORDER BY t_server_ms DESC, id DESC LIMIT ?"
        % clause, params,
    ).fetchall()
    cmds = _commands_of(conn, [r["request_id"] for r in rows if r["request_id"]])
    return [to_dict(r, now, cmds.get(r["request_id"])) for r in rows]


def stats(conn):
    out = {s: 0 for s in STATE_TEXT}
    rows = conn.execute(
        "SELECT state, COUNT(*) AS n FROM button_events GROUP BY state"
    ).fetchall()
    for r in rows:
        if r["state"] in out:
            out[r["state"]] = r["n"]
    out["total"] = sum(out.values())
    return out


# ---------------------------------------------------------------- Web 回应
def respond(conn, event_id, *, decision, client_token=None, actor=None, now=None):
    """Web 对一次按键作出回应：生成一条 op=notify 的指令并更新事件行。

    返回 (event_row, command_row, deduped)。

    - 指令走 commands.create()，于是幂等键、在飞上限、状态机、审计事件
      全部复用第2周那一份，这里一行都不重写。
    - client_token 未显式给出时按 "btn<id>-<decision>-<respond_count+1>" 派生：
      同一次 HTTP 请求的重试（还没提交、计数未变）会命中同一条指令，
      而用户之后 intentional 的再次点击（计数已加）会产生新指令——
      "网络重发不重复、用户重点即重发"两种语义都成立。
    """
    now = now_ms() if now is None else now
    if decision not in DECISION_TO_STATE:
        raise CommandError("decision 只能是 %s，收到 %r"
                           % ("/".join(DECISIONS), decision), "bad_decision")
    row = get_event(conn, event_id)
    if row is None:
        raise CommandError("按键事件不存在：id=%r" % event_id, "not_found", 404)

    token = client_token or "btn%d-%s-r%d" % (row["id"], decision,
                                              row["respond_count"] + 1)
    cmd, deduped = commands.create(
        conn,
        device_mac=row["device_mac"],
        op="notify",
        params={"decision": decision, "event_id": row["id"]},
        client_token=token,
        note="按键事件 #%d 的%s" % (row["id"], "回应" if decision == "ack" else "取消"),
        created_by=actor,
        now=now,
    )
    if not deduped:
        conn.execute(
            "UPDATE button_events SET state=?, decision=?, request_id=?,"
            " t_decided_ms=?, decided_by=?, respond_count=respond_count+1"
            " WHERE id=?",
            (DECISION_TO_STATE[decision], decision, cmd["request_id"],
             now, actor, row["id"]),
        )
        conn.commit()
    return get_event(conn, event_id), cmd, deduped
