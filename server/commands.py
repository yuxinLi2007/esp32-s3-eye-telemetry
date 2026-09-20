"""第2周：Web 远程采集指令 —— 下发、领取、执行、回执与状态追踪。

延续第1周的三条口径：数据来源可追溯 / 时间戳可信度明确 / 失败必须可见。

  - request_id 由服务端生成，是贯穿全链路的唯一关联键：
    网页下发 -> 板端领取 -> 板端执行 -> 结果回执 -> 采集到的样本入库，
    都带同一个 request_id。界面上任何一条结果都能反查"是哪次点击产生的"，
    任何一批样本也能反查"是连续流还是某条指令采的"。
  - 每一次状态迁移都写进 command_events。不允许出现"状态凭空变了"。
  - 超时/过期不靠后台线程，而是在每次读写时惰性结算（sweep）。
    后台线程会让库里的状态取决于"线程有没有在跑"，测试也无法确定性复现；
    惰性结算只需要一个 now_ms 参数就能被断言。
  - 指令通道刻意不提供"改采集开关"这个 op：开关的事实来源是 control_log，
    再包一层必然出现两个来源互相漂移（与第1周 trust_level 不落库同理）。
    要远程停采集，用第1周的 /api/v1/control。
"""

import json
import re
import secrets
import sqlite3
import threading
import time

# ---------------------------------------------------------------- 状态机
PENDING = "pending"      # 已下发，等设备来领
CLAIMED = "claimed"      # 设备已领取，尚未开始/尚未上报进度
RUNNING = "running"      # 设备执行中（带 progress）
DONE = "done"            # 终态：设备执行成功并给出结果
FAILED = "failed"        # 终态：设备执行失败并给出原因
EXPIRED = "expired"      # 终态：ttl 内没有任何设备来领（离线/MAC 写错/固件不支持）
TIMEOUT = "timeout"      # 终态：领了却迟迟不给结果，且重试次数已用尽
CANCELLED = "cancelled"  # 终态：用户在设备领取前撤销

TERMINAL = frozenset({DONE, FAILED, EXPIRED, TIMEOUT, CANCELLED})
LIVE = frozenset({PENDING, CLAIMED, RUNNING})

# 迁移表写死在这里，而不是散落在几个函数里：
# 状态机是本周的核心交付物，它必须能被一眼看完、被测试穷举。
TRANSITIONS = {
    PENDING:   {CLAIMED, CANCELLED, EXPIRED},
    # claimed/running -> pending 是"重新入队"：设备领了却掉线（重启/断网），
    # 在重试预算内把指令放回去，让下一次轮询能再领一次。
    CLAIMED:   {RUNNING, DONE, FAILED, TIMEOUT, PENDING},
    RUNNING:   {RUNNING, DONE, FAILED, TIMEOUT, PENDING},
    DONE:      set(),
    FAILED:    set(),
    EXPIRED:   set(),
    TIMEOUT:   set(),
    CANCELLED: set(),
}

STATE_TEXT = {
    PENDING: "等待设备领取",
    CLAIMED: "设备已领取",
    RUNNING: "设备执行中",
    DONE: "成功",
    FAILED: "设备报错",
    EXPIRED: "超期未领取",
    TIMEOUT: "执行超时",
    CANCELLED: "已撤销",
}


def can_transition(src, dst):
    return dst in TRANSITIONS.get(src, set())


# ---------------------------------------------------------------- 指令集
INT = "int"
BOOL = "bool"

# 单次采集最长 10 秒。这不是拍的数：capture 与连续采集共用同一个主循环，
# 采集期间板子不会上传，全靠环形缓冲扛着。RING_CAPACITY=240 @20Hz = 12 秒，
# 所以只要把单次采集压在 10 秒内，缓冲就一定不会溢出、不会丢连续流的样本。
MAX_CAPTURE_DURATION_MS = 10_000

OPS = {
    "ping": {
        "params": {},
        "ttl_ms": 30_000,
        "timeout_ms": 10_000,
        "desc": "设备心跳：回报运行时长、WiFi RSSI、堆余量、缓冲区水位与当前 seq",
    },
    "selftest": {
        "params": {},
        "ttl_ms": 30_000,
        "timeout_ms": 20_000,
        "desc": "传感器自检：实测加速度合矢量与麦克风帧数，逐通道给出通过/不通过",
    },
    "capture": {
        "params": {
            "n": {"kind": INT, "lo": 1, "hi": 200, "default": 40},
            "interval_ms": {"kind": INT, "lo": 10, "hi": 1000, "default": 50},
        },
        "ttl_ms": 30_000,
        "timeout_ms": 20_000,
        "desc": "立即采集：按给定间隔真实读取传感器 N 次，样本带 request_id 入库",
    },
}

# 下发速率限制：同一台设备同时在飞的指令数上限。
# 没有它，一次连点就能排下几百条指令，板子会按队列老老实实执行几分钟，
# 而界面上只会看到"一堆等待中"——用户既停不下来也不知道为什么。
MAX_LIVE_PER_DEVICE = 8

MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")
REQUEST_ID_RE = re.compile(r"^req_[0-9a-z]{9}_[0-9a-f]{6}$")
BOOT_RE = re.compile(r"^[0-9A-Za-z_-]{1,32}$")
FW_RE = re.compile(r"^[0-9A-Za-z._+-]{1,32}$")
OP_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class CommandError(Exception):
    """业务错误。http_status 让调用方（FastAPI 层）不必自己判断该报几。"""

    def __init__(self, message, code="bad_request", http_status=400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status


# ---------------------------------------------------------------- 表结构
SCHEMA = """
CREATE TABLE IF NOT EXISTS commands (
    request_id       TEXT PRIMARY KEY,
    device_mac       TEXT NOT NULL,
    op               TEXT NOT NULL,
    params_json      TEXT NOT NULL DEFAULT '{}',
    state            TEXT NOT NULL,
    attempts         INTEGER NOT NULL DEFAULT 0,
    max_attempts     INTEGER NOT NULL DEFAULT 2,
    client_token     TEXT,
    note             TEXT,
    created_by       TEXT,
    t_created_ms     INTEGER NOT NULL,
    ttl_ms           INTEGER NOT NULL,
    timeout_ms       INTEGER NOT NULL,
    t_claimed_ms     INTEGER,
    t_started_ms     INTEGER,
    t_finished_ms    INTEGER,
    t_last_event_ms  INTEGER NOT NULL,
    claimed_boot_id  TEXT,
    claimed_fw       TEXT,
    progress         INTEGER,
    error_code       TEXT,
    error_message    TEXT,
    result_json      TEXT,
    samples_batch_id INTEGER,
    -- n_samples 只由 ingest 那条路写入（服务端真正收到了多少条样本），
    -- n_samples_device 是设备在回执里自称采了多少条。两者分开存，
    -- 因为只有"设备说采了 N 条、库里只有 M 条"这种对不上才是真信息：
    -- 那说明上传掉了，而任何一方单独都证明不了这件事。
    n_samples        INTEGER,
    n_samples_device INTEGER,
    requeues         INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_commands_state ON commands (state, t_created_ms);
CREATE INDEX IF NOT EXISTS idx_commands_mac   ON commands (device_mac, t_created_ms);

-- 幂等键。同一个 (设备, op, client_token) 只会存在一条指令：
-- 网页重试、双击、断网重发都拿回原来那条，而不是让板子执行两遍。
CREATE UNIQUE INDEX IF NOT EXISTS uq_commands_token
    ON commands (device_mac, op, client_token);

-- 状态变更审计。和 control_log 一样：当前状态是派生事实，
-- "这个状态是怎么来的"必须有原始记录，否则出了问题只能靠猜。
CREATE TABLE IF NOT EXISTS command_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id  TEXT NOT NULL,
    t_server_ms INTEGER NOT NULL,
    from_state  TEXT,
    to_state    TEXT NOT NULL,
    actor       TEXT,
    detail      TEXT
);

CREATE INDEX IF NOT EXISTS idx_cmd_events ON command_events (request_id, id);
"""


def migrate(conn):
    """给已经建过的 commands 表补后加的列。

    CREATE TABLE IF NOT EXISTS 对旧表什么也不做，所以每加一列都要在这里补一次。
    代价是几行样板，收益是升级时不必删库——库里的指令历史是有价值的记录。
    """
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(commands)")}
    except sqlite3.DatabaseError:
        return conn
    for col in ("n_samples_device", "requeues"):
        if cols and col not in cols:
            default = "0" if col == "requeues" else None
            conn.execute(
                "ALTER TABLE commands ADD COLUMN %s INTEGER%s"
                % (col, " NOT NULL DEFAULT %s" % default if default else "")
            )
    conn.commit()
    return conn


def init_schema(conn):
    conn.executescript(SCHEMA)
    migrate(conn)
    conn.commit()
    return conn


def now_ms():
    return int(time.time() * 1000)


# ---------------------------------------------------------------- request_id
_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _to_b36(value, width):
    """定宽 base36。定宽是必须的：宽度不齐时字典序就不等于时间序。"""
    if value < 0:
        value = 0
    out = []
    while value > 0:
        value, r = divmod(value, 36)
        out.append(_B36[r])
    s = "".join(reversed(out)) or "0"
    return s.rjust(width, "0")[-width:]


# 同一毫秒内已发出的随机后缀。只保留"当前这一毫秒"的集合，换毫秒就丢掉，
# 所以占用不会随运行时间增长。加锁是因为 uvicorn 可能多线程处理请求。
_RID_SPACE = 1 << 24                      # 6 位十六进制的取值空间
_rid_lock = threading.Lock()
_rid_ms = -1
_rid_seen = set()


def new_request_id(t_created_ms=None):
    """request_id 生成规则（详见 docs/week2-commands.md）：

        req_<创建时刻 epoch_ms 的 9 位定宽 base36>_<6 位十六进制随机>

      - 前缀 req_：日志里 grep 得到，念得出来，不会和 boot_id(8 位大写十六进制)混。
      - 时间片：字典序 ≈ 时间序，翻指令列表时不用先解析再排序。
      - 随机片：同一毫秒内并发下发也不会撞；secrets 而非 random，
        因为 request_id 会出现在 URL 与日志里，可预测就等于可枚举。
      - 权威时间仍然是 t_created_ms 这一列。id 里的时间片只是给人看的线索，
        任何逻辑都不许反解它来当时间用（派生值不能变成第二个事实来源）。

      随机片只有 3 字节 = 24 位，光靠运气是不够的：同一毫秒内抽 5000 次，
      按生日悖论期望碰撞约 0.75 次，也就是"大约一半概率撞一次"。撞了就是两条
      指令共用一个主键，后一条插不进去。所以这里记住当前毫秒已发过的后缀，
      撞了就重抽——既保证同进程同毫秒内绝对不重复，又不牺牲不可预测性
      （后缀仍然全部来自 secrets，没有掺计数器）。
      跨进程仍可能撞，那一层由 commands.request_id 的主键约束兜底。
    """
    global _rid_ms, _rid_seen
    if t_created_ms is None:
        t_created_ms = now_ms()
    slot = _to_b36(int(t_created_ms), 9)
    with _rid_lock:
        if t_created_ms != _rid_ms:
            _rid_ms, _rid_seen = t_created_ms, set()
        elif len(_rid_seen) >= _RID_SPACE - 1:
            # 一毫秒内发满 1600 万个 id 才会走到这里，现实中不可能；
            # 真走到了就清空重记，宁可退回"靠运气"也不要在这里死循环。
            _rid_seen = set()
        while True:
            suffix = secrets.token_hex(3)
            if suffix not in _rid_seen:
                _rid_seen.add(suffix)
                return "req_%s_%s" % (slot, suffix)


# ---------------------------------------------------------------- 参数校验
def _coerce(spec, name, value):
    kind = spec["kind"]
    if kind == INT:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CommandError("参数 %s 必须是整数，收到 %r" % (name, value), "bad_param")
        iv = int(value)
        if iv != value:
            raise CommandError("参数 %s 必须是整数，收到 %r" % (name, value), "bad_param")
        if "lo" in spec and iv < spec["lo"]:
            raise CommandError("参数 %s 不得小于 %s（收到 %d）" % (name, spec["lo"], iv),
                               "bad_param")
        if "hi" in spec and iv > spec["hi"]:
            raise CommandError("参数 %s 不得大于 %s（收到 %d）" % (name, spec["hi"], iv),
                               "bad_param")
        return iv
    if kind == BOOL:
        if isinstance(value, bool):
            return value
        raise CommandError("参数 %s 必须是布尔值，收到 %r" % (name, value), "bad_param")
    raise CommandError("未知参数类型 %s" % kind, "bad_param")


def validate_params(op, params):
    """返回补全默认值后的参数字典。任何非法输入都在这里被挡住，
    后面编码成板端文本协议时就不必再担心分隔符注入。"""
    if not OP_RE.match(op or ""):
        raise CommandError("op 非法：%r" % op, "bad_op")
    if op not in OPS:
        raise CommandError(
            "未知指令 %r，可用：%s" % (op, ", ".join(sorted(OPS))), "unknown_op", 404
        )
    spec = OPS[op]["params"]
    params = params or {}
    if not isinstance(params, dict):
        raise CommandError("params 必须是对象", "bad_param")
    unknown = set(params) - set(spec)
    if unknown:
        raise CommandError(
            "%s 不接受参数 %s" % (op, ", ".join(sorted(unknown))), "bad_param"
        )
    out = {}
    for name, s in spec.items():
        out[name] = _coerce(s, name, params[name]) if name in params else s["default"]
    if op == "capture":
        dur = out["n"] * out["interval_ms"]
        if dur > MAX_CAPTURE_DURATION_MS:
            raise CommandError(
                "单次采集时长 %d ms 超过上限 %d ms（n × interval_ms）。"
                "采集期间板子不上传连续流，全靠 %d 秒环形缓冲扛着，"
                "压在上限内才能保证不丢连续流的样本。"
                % (dur, MAX_CAPTURE_DURATION_MS, MAX_CAPTURE_DURATION_MS // 1000),
                "capture_too_long",
            )
        out["duration_ms"] = dur
    return out


def timeout_for(op, params):
    """执行超时窗口。capture 的窗口随参数增长——
    固定值会让一次正常的大采集被误判成超时，那是最坏的一类误报。"""
    base = OPS[op]["timeout_ms"]
    if op == "capture":
        base += 2 * params.get("duration_ms", 0)
    return base


# ---------------------------------------------------------------- 事件与迁移
def _event(conn, request_id, src, dst, actor, detail, t):
    conn.execute(
        "INSERT INTO command_events (request_id, t_server_ms, from_state, to_state,"
        " actor, detail) VALUES (?,?,?,?,?,?)",
        (
            request_id,
            t,
            src,
            dst,
            actor,
            json.dumps(detail, ensure_ascii=False) if detail else None,
        ),
    )


def _move(conn, row, dst, actor, t, detail=None, **fields):
    """唯一的状态写入口。所有迁移都经过它，因此"非法迁移"不可能悄悄发生。"""
    src = row["state"]
    if src == dst:
        sets, vals = [], []
    else:
        if not can_transition(src, dst):
            raise CommandError(
                "非法状态迁移 %s -> %s（request_id=%s）" % (src, dst, row["request_id"]),
                "bad_transition",
                409,
            )
        sets, vals = ["state = ?"], [dst]
    for k, v in fields.items():
        sets.append("%s = ?" % k)
        vals.append(v)
    sets.append("t_last_event_ms = ?")
    vals.append(t)
    vals.append(row["request_id"])
    conn.execute(
        "UPDATE commands SET %s WHERE request_id = ?" % ", ".join(sets), vals
    )
    _event(conn, row["request_id"], src, dst, actor, detail, t)
    return get_row(conn, row["request_id"])


# ---------------------------------------------------------------- 查询
def get_row(conn, request_id):
    return conn.execute(
        "SELECT * FROM commands WHERE request_id = ?", (request_id,)
    ).fetchone()


def events_of(conn, request_id):
    rows = conn.execute(
        "SELECT t_server_ms, from_state, to_state, actor, detail"
        " FROM command_events WHERE request_id = ? ORDER BY id",
        (request_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["detail"] = json.loads(d["detail"]) if d["detail"] else None
        except ValueError:
            pass
        out.append(d)
    return out


def deadline_of(row):
    """当前状态下的截止时刻。

    锚点是"最后一次有动静的时刻"，不是"领取时刻"：
    执行中的指令只要还在上报进度就说明设备活着，不该被一刀切掉；
    真正该判超时的是"领了之后彻底静默"。
    """
    if row["state"] == PENDING:
        return row["t_created_ms"] + row["ttl_ms"]
    return row["t_last_event_ms"] + row["timeout_ms"]


def to_dict(row, now=None, events=None):
    d = dict(row)
    now = now_ms() if now is None else now
    state = d["state"]
    try:
        d["params"] = json.loads(d.pop("params_json") or "{}")
    except ValueError:
        d["params"] = {}
    rj = d.pop("result_json", None)
    try:
        d["result"] = json.loads(rj) if rj else None
    except ValueError:
        # 库里存着解析不了的结果，这本身就是必须暴露的异常，不能悄悄丢成 None
        d["result"] = {"_unparsable": rj}
    dl = deadline_of(row)
    d.update(
        {
            "state_text": STATE_TEXT.get(state, state),
            "is_terminal": state in TERMINAL,
            "deadline_ms": dl if state in LIVE else None,
            "remaining_ms": max(0, dl - now) if state in LIVE else None,
            "age_ms": now - d["t_created_ms"],
            "elapsed_ms": (d["t_finished_ms"] or now) - d["t_created_ms"],
            # 下发 -> 领取 的排队时延。这个数直接反映"轮询间隔 + 设备在线状况"，
            # 是判断指令通道健康度的第一手指标。
            "queue_ms": (
                d["t_claimed_ms"] - d["t_created_ms"]
                if d["t_claimed_ms"] is not None
                else None
            ),
            "exec_ms": (
                d["t_finished_ms"] - d["t_started_ms"]
                if d["t_finished_ms"] is not None and d["t_started_ms"] is not None
                else None
            ),
            "op_desc": OPS.get(d["op"], {}).get("desc"),
            # 设备自称采到的样本数 与 服务端实际入库的样本数 对不上 = 上传掉了。
            # 这是"失败必须可见"在指令通道上的落点：光看 state=done 会以为一切正常。
            "sample_count_mismatch": (
                d["n_samples_device"] is not None
                and d["n_samples"] is not None
                and d["n_samples_device"] != d["n_samples"]
            ),
        }
    )
    if events is not None:
        d["events"] = events
    return d


# ---------------------------------------------------------------- 惰性结算
def sweep(conn, now=None, actor="server"):
    """把已经越过截止时刻的指令结算成终态（或重新入队）。

    返回 [(request_id, from, to)]。所有读接口都先调它，
    于是"界面上看到的状态"永远不等于"一个已经过期的旧状态"。
    """
    now = now_ms() if now is None else now
    rows = conn.execute(
        "SELECT * FROM commands WHERE state IN (?,?,?)",
        (PENDING, CLAIMED, RUNNING),
    ).fetchall()
    moved = []
    for row in rows:
        if now < deadline_of(row):
            continue
        if row["state"] == PENDING:
            _move(conn, row, EXPIRED, actor, now,
                  {"reason": "ttl_ms=%d 内没有设备领取" % row["ttl_ms"],
                   "device_mac": row["device_mac"]})
            moved.append((row["request_id"], PENDING, EXPIRED))
        elif row["state"] == CLAIMED and row["attempts"] < row["max_attempts"]:
            # 还有机会：放回队列，清掉上一轮的领取痕迹，否则下一次领取
            # 会看到"已经被别人领着"的旧字段。
            _move(conn, row, PENDING, actor, now,
                  {"reason": "领取后从未上报进度，静默超过 %d ms，重新入队"
                            % row["timeout_ms"],
                   "lost_boot_id": row["claimed_boot_id"]},
                  t_claimed_ms=None, t_started_ms=None, claimed_boot_id=None,
                  claimed_fw=None, progress=None,
                  requeues=row["requeues"] + 1)
            moved.append((row["request_id"], row["state"], PENDING))
        else:
            # RUNNING 一律不重排队：设备已经开始执行，静默可能只是网络抖动，
            # 而重跑一次采集会得到"两批数据对不上号"的更坏结果。
            # 判成超时让人看见、由人决定是否重发，比自动重试更诚实。
            reason = ("执行中静默超过 %d ms（progress=%s）"
                      % (row["timeout_ms"], row["progress"])
                      if row["state"] == RUNNING else
                      "领取后静默超过 %d ms，重试次数已用尽" % row["timeout_ms"])
            _move(conn, row, TIMEOUT, actor, now,
                  {"reason": reason,
                   "attempts": row["attempts"],
                   "lost_boot_id": row["claimed_boot_id"]})
            moved.append((row["request_id"], row["state"], TIMEOUT))
    if moved:
        conn.commit()
    return moved


def stats(conn, now=None):
    now = now_ms() if now is None else now
    sweep(conn, now)
    rows = conn.execute(
        "SELECT state, COUNT(*) AS n FROM commands GROUP BY state"
    ).fetchall()
    out = {s: 0 for s in list(LIVE) + list(TERMINAL)}
    for r in rows:
        out[r["state"]] = r["n"]
    out["total"] = sum(out.values())
    out["live"] = sum(out[s] for s in LIVE)
    return out


# ---------------------------------------------------------------- 下发
def create(conn, *, device_mac, op, params=None, client_token=None, note=None,
           created_by=None, now=None, max_attempts=2):
    """创建一条指令。返回 (row, deduped)。

    deduped=True 表示这个 client_token 之前已经提交过，返回的是原来那条 ——
    这就是服务端的防抖：网页重试、双击、断网重发都不会让板子执行两遍。
    """
    now = now_ms() if now is None else now
    mac = (device_mac or "").strip().upper()
    if not MAC_RE.match(mac):
        raise CommandError("device_mac 格式非法：%r（应为 AA:BB:CC:DD:EE:FF）"
                           % device_mac, "bad_mac")
    p = validate_params(op, params)
    if client_token is not None:
        client_token = str(client_token).strip()
        if not client_token or len(client_token) > 64:
            raise CommandError("client_token 长度必须在 1~64 之间", "bad_token")
        if not re.match(r"^[0-9A-Za-z._-]+$", client_token):
            raise CommandError("client_token 只允许字母数字与 . _ -", "bad_token")

    if client_token:
        old = conn.execute(
            "SELECT * FROM commands WHERE device_mac=? AND op=? AND client_token=?",
            (mac, op, client_token),
        ).fetchone()
        if old is not None:
            return old, True

    live = conn.execute(
        "SELECT COUNT(*) AS n FROM commands WHERE device_mac=? AND state IN (?,?,?)",
        (mac, PENDING, CLAIMED, RUNNING),
    ).fetchone()["n"]
    if live >= MAX_LIVE_PER_DEVICE:
        raise CommandError(
            "该设备已有 %d 条指令在飞（上限 %d）。设备是串行执行的，"
            "再排下去只是让队列变长；请等它们结束或超时。"
            % (live, MAX_LIVE_PER_DEVICE),
            "too_many_live",
            429,
        )

    ttl = OPS[op]["ttl_ms"]
    tmo = timeout_for(op, p)
    # request_id 撞库的概率是 16^6 分之一每毫秒，仍然要处理：
    # 真撞了就换一个，绝不能让一次碰撞变成 500。
    for _ in range(5):
        rid = new_request_id(now)
        try:
            conn.execute(
                """INSERT INTO commands
                   (request_id, device_mac, op, params_json, state, attempts,
                    max_attempts, client_token, note, created_by, t_created_ms,
                    ttl_ms, timeout_ms, t_last_event_ms)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rid, mac, op, json.dumps(p, sort_keys=True), PENDING, 0,
                 max_attempts, client_token, note, created_by, now, ttl, tmo, now),
            )
            break
        except sqlite3.IntegrityError as exc:
            text = str(exc)
            if "uq_commands_token" in text or "client_token" in text:
                # 并发双击：另一个请求先把这个 token 写进去了。
                # 返回它那条，而不是报错——幂等键的语义就是"同一个意图只有一个结果"。
                conn.rollback()
                old = conn.execute(
                    "SELECT * FROM commands WHERE device_mac=? AND op=? AND client_token=?",
                    (mac, op, client_token),
                ).fetchone()
                if old is not None:
                    return old, True
                raise CommandError("幂等键冲突：%s" % text, "duplicate", 409)
            if "commands.request_id" in text or "PRIMARY KEY" in text.upper():
                continue
            raise CommandError("写入指令失败：%s" % text, "db_error", 500)
    else:
        raise CommandError("request_id 连续 5 次碰撞，放弃", "id_collision", 500)

    row = get_row(conn, rid)
    _event(conn, rid, None, PENDING, created_by or "web",
           {"op": op, "params": p, "ttl_ms": ttl, "timeout_ms": tmo,
            "client_token": client_token}, now)
    conn.commit()
    return get_row(conn, rid), False


def cancel(conn, request_id, *, actor="web", now=None):
    """撤销。只允许撤还没被领取的指令。

    已经领走的指令不允许撤：板子可能正在采集，中途抽走会让"结果"和"指令"
    对不上号——那种半截状态比等它自己超时更难解释。
    """
    now = now_ms() if now is None else now
    sweep(conn, now)
    row = get_row(conn, request_id)
    if row is None:
        raise CommandError("指令不存在：%s" % request_id, "not_found", 404)
    if row["state"] != PENDING:
        raise CommandError(
            "只有等待领取的指令可以撤销，当前状态为 %s（%s）"
            % (row["state"], STATE_TEXT.get(row["state"], row["state"])),
            "not_cancellable",
            409,
        )
    row = _move(conn, row, CANCELLED, actor, now, {"by": actor})
    conn.commit()
    return row


def list_commands(conn, *, limit=50, state=None, device_mac=None, op=None, now=None):
    now = now_ms() if now is None else now
    sweep(conn, now)
    where, params = [], []
    if state:
        for s in state.split(","):
            s = s.strip()
            if s and s not in LIVE and s not in TERMINAL:
                raise CommandError("未知状态 %r" % s, "bad_state")
        states = [s.strip() for s in state.split(",") if s.strip()]
        if states:
            where.append("state IN (%s)" % ",".join("?" * len(states)))
            params.extend(states)
    if device_mac:
        where.append("device_mac = ?")
        params.append(device_mac.strip().upper())
    if op:
        where.append("op = ?")
        params.append(op)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    rows = conn.execute(
        "SELECT * FROM commands %s ORDER BY t_created_ms DESC, rowid DESC LIMIT ?"
        % clause,
        params,
    ).fetchall()
    return [to_dict(r, now) for r in rows]


def get_command(conn, request_id, *, with_events=True, now=None):
    now = now_ms() if now is None else now
    sweep(conn, now)
    row = get_row(conn, request_id)
    if row is None:
        raise CommandError("指令不存在：%s" % request_id, "not_found", 404)
    return to_dict(row, now, events_of(conn, request_id) if with_events else None)


# ---------------------------------------------------------------- 设备侧
# 板端不解析 JSON：多引一个库、多几百字节 RAM、多一个可能失败的分支，
# 而这里要传的东西完全可以用一行分隔文本表达。所以领取接口刻意是纯文本。
# 能这么干的前提是 op / 参数名 / 参数值都在上面的白名单里被校验过，
# 分隔符 | ; = 不可能出现在任何字段里，因此编码不会被注入打断。
CLAIM_NONE = "none"


def encode_claim(row):
    p = json.loads(row["params_json"] or "{}")
    kv = ";".join(
        "%s=%s" % (k, ("true" if v is True else "false" if v is False else v))
        for k, v in sorted(p.items())
        if k != "duration_ms"  # 派生值不下发：板端自己会算，两边算才是两份事实
    )
    return "%s|%s|%s|%d" % (row["request_id"], row["op"], kv, row["timeout_ms"])


def decode_claim(text):
    """把 encode_claim 的输出解回字典。板端与测试共用这一份实现。"""
    text = (text or "").strip()
    if not text or text == CLAIM_NONE:
        return None
    parts = text.split("|")
    if len(parts) != 4 or not REQUEST_ID_RE.match(parts[0]):
        raise CommandError("领取应答格式非法：%r" % text, "bad_claim_payload", 502)
    rid, op, kv, tmo = parts
    params = {}
    for item in kv.split(";"):
        if not item:
            continue
        if "=" not in item:
            raise CommandError("领取应答参数非法：%r" % item, "bad_claim_payload", 502)
        k, v = item.split("=", 1)
        params[k] = (True if v == "true" else False if v == "false"
                     else int(v) if re.match(r"^-?\d+$", v) else v)
    return {"request_id": rid, "op": op, "params": params, "timeout_ms": int(tmo)}


def encode_claim_request(device_mac, boot_id, fw_version):
    return "%s|%s|%s" % (device_mac.strip().upper(), boot_id, fw_version)


def decode_claim_request(body):
    body = (body or "").strip()
    parts = body.split("|")
    if len(parts) != 3:
        raise CommandError("领取请求应为 mac|boot_id|fw_version，收到 %r" % body,
                           "bad_claim_body")
    mac, boot, fw = parts[0].strip().upper(), parts[1].strip(), parts[2].strip()
    if not MAC_RE.match(mac):
        raise CommandError("device_mac 格式非法：%r" % parts[0], "bad_mac")
    if not BOOT_RE.match(boot):
        raise CommandError("boot_id 格式非法：%r" % boot, "bad_boot_id")
    if not FW_RE.match(fw):
        raise CommandError("fw_version 格式非法：%r" % fw, "bad_fw")
    return mac, boot, fw


def claim(conn, *, device_mac, boot_id, fw_version=None, now=None):
    """设备领取一条指令。返回 row 或 None（没有可领的）。

    幂等：同一次启动（boot_id）重复来领，拿到的是同一条，
    不会多发一条指令、也不会把 attempts 加两次。
    """
    now = now_ms() if now is None else now
    sweep(conn, now)          # 先结算：过期指令绝不能被发出去
    mac = device_mac.strip().upper()

    same = conn.execute(
        "SELECT * FROM commands WHERE device_mac=? AND claimed_boot_id=?"
        " AND state IN (?,?) ORDER BY t_created_ms LIMIT 1",
        (mac, boot_id, CLAIMED, RUNNING),
    ).fetchone()
    if same is not None:
        return same

    row = conn.execute(
        "SELECT * FROM commands WHERE device_mac=? AND state=? ORDER BY t_created_ms LIMIT 1",
        (mac, PENDING),
    ).fetchone()
    if row is None:
        return None
    # 用 "AND state='pending'" 做条件更新并检查 rowcount：
    # 两台设备（或两个进程）同时来领同一条时，只有一个能改成功。
    cur = conn.execute(
        "UPDATE commands SET state=?, attempts=attempts+1, t_claimed_ms=?,"
        " t_started_ms=?, t_last_event_ms=?, claimed_boot_id=?, claimed_fw=?,"
        " progress=?, error_code=NULL, error_message=NULL"
        " WHERE request_id=? AND state=?",
        (CLAIMED, now, now, now, boot_id, fw_version, None, row["request_id"], PENDING),
    )
    if cur.rowcount != 1:
        conn.rollback()
        return None
    _event(conn, row["request_id"], PENDING, CLAIMED, "device:" + boot_id,
           {"boot_id": boot_id, "fw_version": fw_version, "attempt": row["attempts"] + 1},
           now)
    conn.commit()
    return get_row(conn, row["request_id"])


RESULT_STATES = {"running": RUNNING, "done": DONE, "failed": FAILED}


def apply_result(conn, request_id, *, state, boot_id, device_mac=None, progress=None,
                 error_code=None, error_message=None, result=None,
                 samples_batch_id=None, n_samples=None, t_device_ms=None, now=None):
    """设备回执。state ∈ {running, done, failed}。

    running 只是进度心跳，它会刷新超时锚点（见 deadline_of）：
    长时间采集只要还在报进度就不会被误判超时，静默才会。
    """
    now = now_ms() if now is None else now
    sweep(conn, now)
    row = get_row(conn, request_id)
    if row is None:
        raise CommandError("指令不存在：%s" % request_id, "not_found", 404)
    if state not in RESULT_STATES:
        raise CommandError("state 只能是 running/done/failed，收到 %r" % state,
                           "bad_state")
    dst = RESULT_STATES[state]
    if row["state"] in TERMINAL:
        # 终态不可改。板子重启后补发的旧回执、超时之后姗姗来迟的成功，
        # 都只能被拒绝并如实告诉对方原因——覆盖掉的话，
        # 界面上刚刚显示过的"超时"就会凭空变成"成功"。
        raise CommandError(
            "指令已是终态 %s（%s），拒收 %s 回执"
            % (row["state"], STATE_TEXT.get(row["state"]), dst),
            "already_terminal",
            409,
        )
    if boot_id and row["claimed_boot_id"] and boot_id != row["claimed_boot_id"]:
        raise CommandError(
            "boot_id 不匹配：指令由 %s 领取，回执来自 %s（设备重启过？）"
            % (row["claimed_boot_id"], boot_id),
            "boot_mismatch",
            409,
        )
    if device_mac and device_mac.strip().upper() != row["device_mac"]:
        raise CommandError("device_mac 与指令不符", "mac_mismatch", 409)
    if row["state"] == PENDING:
        raise CommandError("指令尚未被领取，不能直接回执", "not_claimed", 409)

    fields = {}
    detail = {}
    if progress is not None:
        if not isinstance(progress, int) or isinstance(progress, bool):
            raise CommandError("progress 必须是整数", "bad_progress")
        if not 0 <= progress <= 100:
            raise CommandError("progress 必须在 0~100，收到 %d" % progress, "bad_progress")
        if row["progress"] is not None and progress < row["progress"]:
            # 进度回退意味着回执乱序或设备在重放旧数据，两者都必须暴露
            raise CommandError(
                "progress 回退：%s -> %d" % (row["progress"], progress), "progress_regressed",
                409,
            )
        fields["progress"] = progress
        detail["progress"] = progress
    if dst == DONE:
        fields.update(t_finished_ms=now, error_code=None, error_message=None,
                      progress=100)
        fields["result_json"] = json.dumps(result, ensure_ascii=False, sort_keys=True) \
            if result is not None else None
        detail["result_keys"] = sorted(result) if isinstance(result, dict) else None
    elif dst == FAILED:
        if not error_code:
            raise CommandError("failed 回执必须带 error_code", "missing_error_code")
        fields.update(t_finished_ms=now, error_code=str(error_code)[:64],
                      error_message=(str(error_message)[:512] if error_message else None))
        detail.update(error_code=fields["error_code"],
                      error_message=fields["error_message"])
        if result is not None:
            fields["result_json"] = json.dumps(result, ensure_ascii=False, sort_keys=True)
    if samples_batch_id is not None:
        fields["samples_batch_id"] = int(samples_batch_id)
        detail["samples_batch_id"] = int(samples_batch_id)
    if n_samples is not None:
        # 设备自报的数量不覆盖服务端实际入库的数量，只并排存着备查
        fields["n_samples_device"] = int(n_samples)
        detail["n_samples_device"] = int(n_samples)
    if t_device_ms is not None:
        detail["t_device_ms"] = int(t_device_ms)

    row = _move(conn, row, dst, "device:" + (boot_id or "?"), now, detail, **fields)
    conn.commit()
    return row


def link_samples(conn, request_id, batch_id, n_samples, now=None):
    """把入库的样本批次挂到指令上（ingest 带 request_id 时由 app 层调用）。"""
    now = now_ms() if now is None else now
    row = get_row(conn, request_id)
    if row is None:
        return False
    conn.execute(
        "UPDATE commands SET samples_batch_id=?, n_samples=COALESCE(?,0)+?,"
        " t_last_event_ms=? WHERE request_id=?",
        (batch_id, row["n_samples"], n_samples, now, request_id),
    )
    _event(conn, request_id, row["state"], row["state"], "ingest",
           {"samples_batch_id": batch_id, "n_samples": n_samples}, now)
    conn.commit()
    return True


def ops_catalog():
    """给界面用的指令清单：能点什么、参数范围是什么，都由服务端说了算。
    前端另抄一份范围，两边迟早会不一致。"""
    out = []
    for name, spec in OPS.items():
        params = []
        for pname, p in spec["params"].items():
            params.append({"name": pname, "kind": p["kind"], "lo": p.get("lo"),
                           "hi": p.get("hi"), "default": p.get("default")})
        out.append({"op": name, "desc": spec["desc"], "params": params,
                    "ttl_ms": spec["ttl_ms"], "timeout_ms": spec["timeout_ms"],
                    "max_capture_duration_ms": MAX_CAPTURE_DURATION_MS
                    if name == "capture" else None})
    return out