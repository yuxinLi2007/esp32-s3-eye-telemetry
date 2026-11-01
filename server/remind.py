"""第12周：让用户纠错影响后续提醒 —— 主动提醒与用户纠错的受控闭环。

本周问题：用户说"这次不用提醒"，系统怎样记住当前选择，又能恢复默认？

设计口径（对着课程第 12 周的验收点）：

- **主动提醒触发、去重、冷却与显式偏好**：提醒只在事件匹配显式规则时触发
  （本项目的显式规则 = 第 9 周"画面不可用 → 要重拍吗"），同一事件重放
  （replay）不重复触发（fire_count 只增一次，重放只更新 last_seen）；
  用户最近一次表态后进入冷却/稍后窗口，窗口内不再打扰；偏好 = 显式规则
  （on / off / snooze），不引入自动个性化。
- **有限反馈规则**（确认 / 纠正 / 忽略 / 稍后 / 恢复默认）：
    - confirm       确认：同意提醒提出的动作（"要重拍吗" → 重拍）；
    - correct       纠正：提醒依据的判断有误（画面其实可用 → 纠正后询问作废）；
    - ignore        忽略：这条不要了（沿第 9 周，进冷却）；
    - later         稍后：暂不提醒，过一会再问（snooze 窗口）；
    - restore_default 恢复默认：清掉本轮偏好，回到"按默认规则提醒"。
- **用户反馈记录 + 短期上下文更新 + 配置版本回退**：feedback 表存每条反馈
  （带规则版本与当时的配置版本）；reminder_pref_log 存每一次配置变化与回退
  （from_value → to_value，版本单调递增）；config_version 可查看、可回退。
- **不把保存反馈等同于在线训练或自动学习成功**：反馈只调整显式规则
  （开/关/稍后/恢复），绝不改模型、绝不引入个性化学习；本课主责是
  "受控交互调整"，向后续课程输出结构化反馈与上下文样例。

范围：以显式规则完成基础要求（自动个性化不列为必做）。唯一渠道
channel='vision_ask'（第 9 周的重拍询问）；新渠道只需照同一套 gate 接入。
"""

import os
import time

import vision

# ---------------------------------------------------------------- 配置（环境变量）
RULE_VERSION = os.environ.get("REMIND_RULE_VERSION", "week12-r1").strip() or "week12-r1"

# 同一事件"重放"窗口：窗口内同一 event_key 不再触发新提醒（fire_count 不增）。
REMIND_DEDUP_MS = int(os.environ.get("REMIND_DEDUP_MS", "60000") or 60000)

# "稍后再说"的窗口：snooze 到期前不主动打扰，到期后按默认规则恢复提醒。
REMIND_LATER_MS = int(os.environ.get("REMIND_LATER_MS", "600000") or 600000)

# 全局默认偏好（无任何记录 = 按默认规则提醒）。
DEFAULT_PREF = "on"

# 偏好值集合（显式、有限）
PREF_VALUES = ("on", "off", "snooze")
# 用户反馈动词集合（有限反馈规则，与简报一一对应）
FEEDBACK_VERBS = ("confirm", "correct", "ignore", "later", "off",
                  "restore_default")

_GLOBAL = "*"          # 全局偏好（设备级记录优先，其次全局，再其次默认）
_CHANNELS = ("vision_ask",)


def now_ms() -> int:
    return int(time.time() * 1000)


def ensure_schema(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS reminder_prefs (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             device_mac TEXT NOT NULL DEFAULT '*',
             channel TEXT NOT NULL,
             value TEXT NOT NULL,
             until_ms INTEGER,
             config_version INTEGER NOT NULL,
             source TEXT NOT NULL,
             note TEXT,
             t_server_ms INTEGER NOT NULL,
             UNIQUE(device_mac, channel))"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS reminder_pref_log (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             t_server_ms INTEGER NOT NULL,
             device_mac TEXT NOT NULL,
             channel TEXT NOT NULL,
             action TEXT NOT NULL,
             from_value TEXT,
             to_value TEXT,
             config_version INTEGER NOT NULL,
             prev_version INTEGER NOT NULL,
             source TEXT,
             reason TEXT)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS reminder_events (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             event_key TEXT NOT NULL,
             channel TEXT NOT NULL,
             device_mac TEXT NOT NULL,
             first_seen_ms INTEGER NOT NULL,
             last_seen_ms INTEGER NOT NULL,
             fire_count INTEGER NOT NULL DEFAULT 0,
             last_fired_ms INTEGER,
             last_note TEXT,
             UNIQUE(event_key, channel, device_mac))"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS feedback (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             t_server_ms INTEGER NOT NULL,
             device_mac TEXT NOT NULL,
             channel TEXT NOT NULL,
             round_id TEXT,
             verb TEXT NOT NULL,
             rule_version TEXT NOT NULL,
             config_version INTEGER,
             detail TEXT)"""
    )
    conn.commit()


# ---------------------------------------------------------------- 读取
def config_version(conn) -> int:
    """当前配置版本：每次偏好变更 +1（回退也算一次变更，可审计）。"""
    ensure_schema(conn)
    row = conn.execute(
        "SELECT MAX(config_version) AS v FROM reminder_prefs"
    ).fetchone()
    row2 = conn.execute(
        "SELECT MAX(config_version) AS v FROM reminder_pref_log"
    ).fetchone()
    return max(row["v"] or 0, row2["v"] or 0)


def _pref_row(conn, device_mac, channel):
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM reminder_prefs WHERE device_mac=? AND channel=?",
        (device_mac, channel),
    ).fetchone()
    return dict(row) if row else None


def get_pref(conn, device_mac, channel):
    """有效偏好：设备级记录优先，其次全局（'*'），再其次默认 on。"""
    row = _pref_row(conn, device_mac, channel)
    if row is None and device_mac != _GLOBAL:
        row = _pref_row(conn, _GLOBAL, channel)
    return row


def view(conn, device_mac):
    """本轮偏好视图（用户可见、可撤回）。返回给 GET /api/v1/reminders/prefs。"""
    ensure_schema(conn)
    channels = {}
    for ch in _CHANNELS:
        p = get_pref(conn, device_mac, ch)
        if p is None:
            channels[ch] = {"value": DEFAULT_PREF, "until_ms": None,
                            "config_version": None, "source": "default",
                            "note": None, "t_server_ms": None}
        else:
            channels[ch] = {"value": p["value"], "until_ms": p["until_ms"],
                            "config_version": p["config_version"],
                            "source": p["source"], "note": p["note"],
                            "t_server_ms": p["t_server_ms"]}
    return {
        "device_mac": device_mac,
        "default": DEFAULT_PREF,
        "rule_version": RULE_VERSION,
        "config_version": config_version(conn),
        "channels": channels,
        "answer": _answer(conn, device_mac),
    }


# ---------------------------------------------------------------- 提醒门（触发/去重/冷却）
def _touch_event(conn, device_mac, channel, event_key, now):
    ensure_schema(conn)
    conn.execute(
        """INSERT INTO reminder_events (event_key, channel, device_mac,
                                        first_seen_ms, last_seen_ms, fire_count)
           VALUES (?,?,?,?,?,0)
           ON CONFLICT(event_key, channel, device_mac)
           DO UPDATE SET last_seen_ms=excluded.last_seen_ms""",
        (event_key, channel, device_mac, now, now),
    )
    conn.commit()


def gate(conn, device_mac, channel, event_key, now=None, dedup_ms=None):
    """这次事件该不该触发新提醒？返回 {"fire": bool, "reason": str}。

    判定顺序（全部显式规则）：
      1. 偏好 off            → 不提醒（pref_off，用户关闭后不继续打扰）；
      2. 偏好 snooze 且在窗口 → 不提醒（snooze_until，稍后再问）；
      3. 同一事件在该窗口内已触发过 → 不提醒（dedup_replay，重放不重复触发）；
      4. 否则 → 提醒（ok）。
    只负责"该不该"，不负责创建；真正新建提醒后由 mark_fired 记账。
    """
    device_mac = device_mac or _GLOBAL
    channel = channel or _CHANNELS[0]
    now = now_ms() if now is None else now
    dedup_ms = REMIND_DEDUP_MS if dedup_ms is None else dedup_ms
    _touch_event(conn, device_mac, channel, event_key, now)

    p = get_pref(conn, device_mac, channel)
    if p is not None:
        if p["value"] == "off":
            return {"fire": False, "reason": "pref_off"}
        if p["value"] == "snooze" and p["until_ms"] is not None \
                and now < p["until_ms"]:
            return {"fire": False, "reason": "snooze_until"}
    row = conn.execute(
        "SELECT last_fired_ms FROM reminder_events"
        " WHERE event_key=? AND channel=? AND device_mac=?",
        (event_key, channel, device_mac),
    ).fetchone()
    if row and row["last_fired_ms"] is not None \
            and now - row["last_fired_ms"] < dedup_ms:
        return {"fire": False, "reason": "dedup_replay"}
    return {"fire": True, "reason": "ok"}


def mark_fired(conn, device_mac, channel, event_key, note=None, now=None):
    """真正开了一条新提醒后记账：fire_count +1（重放不重复触发 = 只加一次）。"""
    device_mac = device_mac or _GLOBAL
    now = now_ms() if now is None else now
    ensure_schema(conn)
    conn.execute(
        """INSERT INTO reminder_events (event_key, channel, device_mac,
                                        first_seen_ms, last_seen_ms,
                                        fire_count, last_fired_ms, last_note)
           VALUES (?,?,?,?,?,1,?,?)
           ON CONFLICT(event_key, channel, device_mac)
           DO UPDATE SET fire_count=fire_count+1, last_fired_ms=excluded.last_fired_ms,
                         last_note=excluded.last_note, last_seen_ms=excluded.last_seen_ms""",
        (event_key, channel, device_mac, now, now, now, note),
    )
    conn.commit()


def handle_vision_ask(conn, snap, now=None):
    """第9周"要重拍吗"接入第12周提醒门：app 分析端点原来调 vision.open_ask 的位置
    改调这里，响应形状不变（返回 (ask|None, reason)）。

    行为对照（无任何偏好时与第 9 周完全一致）：
      - 门放行 → vision.open_ask（新建成功才 mark_fired 记账）；
      - 门拦截（pref_off / snooze_until / dedup_replay）→ 若该画面已有
        未表态的询问，照常浮出（那是用户还没答的问题，不算重触发）；
        否则不建新提醒，reason 说明原因。
    """
    device_mac = snap["device_mac"] or _GLOBAL
    event_key = "snapshot:%d" % snap["id"]
    g = gate(conn, device_mac, "vision_ask", event_key, now=now)
    if g["fire"]:
        ask, reason = vision.open_ask(conn, snap, now=now)
        if ask is not None and ask["state"] == "open" and reason == "ok":
            mark_fired(conn, device_mac, "vision_ask", event_key,
                       note="round_id=%d" % ask["id"], now=now)
        return ask, reason
    existing = vision.get_ask(conn, snap["id"])
    if existing is not None and existing["state"] == "open":
        return existing, "existing:" + g["reason"]
    return None, g["reason"]


# ---------------------------------------------------------------- 偏好变更与回退
def apply_pref(conn, device_mac, channel, action, note=None,
               source="api", now=None):
    """变更偏好（有限动作集）：off / on / later(=snooze) / restore。

    每次变更：config_version +1，reminder_pref_log 记 from_value→to_value
    （回退记录），feedback 记用户反馈。返回 {"action", "value", "until_ms",
    "config_version", "prev_version", "restored"}。
    """
    device_mac = device_mac or _GLOBAL
    channel = channel or _CHANNELS[0]
    now = now_ms() if now is None else now
    ensure_schema(conn)

    action = (action or "").strip().lower()
    if action not in ("off", "on", "later", "restore"):
        raise ValueError("action 只能取 off / on / later / restore：%r" % action)

    prev = config_version(conn)
    cur = get_pref(conn, device_mac, channel)
    from_value = cur["value"] if cur is not None else DEFAULT_PREF

    if action == "restore":
        # 恢复默认：清掉设备级（或全局）记录，回到 DEFAULT_PREF。
        conn.execute("DELETE FROM reminder_prefs WHERE device_mac=? AND channel=?",
                     (device_mac, channel))
        to_value = DEFAULT_PREF
        until = None
        version = prev + 1
        conn.execute(
            """INSERT INTO reminder_pref_log
               (t_server_ms, device_mac, channel, action, from_value, to_value,
                config_version, prev_version, source, reason)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (now, device_mac, channel, "restore", from_value, to_value,
             version, prev, source, (note or "restore_default")[:200]),
        )
        conn.commit()
        record_feedback(conn, device_mac, "restore_default", channel=channel,
                        detail=(note or "恢复默认"[:80]), now=now)
        return {"action": action, "value": to_value, "until_ms": until,
                "config_version": version, "prev_version": prev,
                "restored": True}
    # 非 restore：写入/覆盖该键的偏好行
    if action == "later":
        to_value = "snooze"
        until = now + REMIND_LATER_MS   # 稍后窗口；测试传 now 冻结时间即可
    elif action == "off":
        to_value = "off"
        until = None
    else:  # on
        to_value = "on"
        until = None

    version = prev + 1
    conn.execute(
        """INSERT INTO reminder_prefs (device_mac, channel, value, until_ms,
                                       config_version, source, note, t_server_ms)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(device_mac, channel)
           DO UPDATE SET value=excluded.value, until_ms=excluded.until_ms,
                         config_version=excluded.config_version,
                         source=excluded.source, note=excluded.note,
                         t_server_ms=excluded.t_server_ms""",
        (device_mac, channel, to_value, until, version, source,
         (note or "")[:200], now),
    )
    conn.execute(
        """INSERT INTO reminder_pref_log
           (t_server_ms, device_mac, channel, action, from_value, to_value,
            config_version, prev_version, source, reason)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (now, device_mac, channel, "set_" + to_value, from_value, to_value,
         version, prev, source, (note or "")[:200]),
    )
    conn.commit()
    verb = {"off": "off", "later": "later"}.get(action, action)
    if verb in ("off", "later"):
        record_feedback(conn, device_mac, verb, channel=channel,
                        detail=(note or ("稍后提醒" if action == "later"
                                          else "关闭本轮提醒"))[:80], now=now)
    return {"action": action, "value": to_value, "until_ms": until,
            "config_version": version, "prev_version": prev, "restored": False}


def record_feedback(conn, device_mac, verb, channel="vision_ask",
                    round_id=None, detail=None, rule_version=None, now=None):
    """记一条用户反馈（确认/纠正/忽略/稍后/关闭/恢复默认），带规则与配置版本。

    反馈只用于"受控交互调整"的记录与对照，任何字段都不进模型、不触发学习。
    """
    if verb not in FEEDBACK_VERBS:
        raise ValueError("verb 只能取 %s：%r" % ("/".join(FEEDBACK_VERBS), verb))
    ensure_schema(conn)
    now = now_ms() if now is None else now
    conn.execute(
        """INSERT INTO feedback (t_server_ms, device_mac, channel, round_id,
                                 verb, rule_version, config_version, detail)
           VALUES (?,?,?,?,?,?,?,?)""",
        (now, device_mac or _GLOBAL, channel, round_id,
         verb, rule_version or RULE_VERSION, config_version(conn),
         (detail or "")[:200]),
    )
    conn.commit()


# ---------------------------------------------------------------- 审计查询
def list_feedback(conn, limit=50):
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT * FROM feedback ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def list_events(conn, limit=50):
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT * FROM reminder_events ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def pref_history(conn, limit=50):
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT * FROM reminder_pref_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def report(conn, device_mac=None, channel="vision_ask"):
    """前后对照（实践2）：偏好变化边界之前/之后的提醒次数 + 任务结果 + 反馈汇总。

    边界 = 该渠道第一条配置变更记录（pref_log）的时刻；没有变更 = 全部算"之后"。
    """
    ensure_schema(conn)
    mac = device_mac or _GLOBAL
    scope = "AND device_mac=?" if device_mac else ""
    args = (device_mac,) if device_mac else ()
    first = conn.execute(
        "SELECT MIN(t_server_ms) AS t FROM reminder_pref_log"
        " WHERE channel=?" + scope, (channel, *args),
    ).fetchone()
    boundary = first["t"] if first and first["t"] is not None else None

    ev_rows = conn.execute(
        "SELECT * FROM reminder_events WHERE channel=?" + scope,
        (channel, *args),
    ).fetchall()
    # 前后对照：边界前的提醒触发数 / 边界后（含无边界全部）的触发数；
    # 重放次数 = 同一事件在触发之后又被看到、但没有新增触发的次数。
    fired_before = fired_after = replays = 0
    for r in ev_rows:
        fired = r["last_fired_ms"] or 0
        if r["fire_count"] > 0:
            if boundary is not None and fired < boundary:
                fired_before += 1
            elif boundary is None or fired >= boundary:
                fired_after += 1
        if r["fire_count"] > 0 and r["last_seen_ms"] > fired:
            replays += 1

    # 任务结果：询问的终局分布（open/confirmed/ignored/superseded）
    ask_rows = conn.execute(
        "SELECT state, COUNT(*) AS n FROM asks WHERE kind=?"
        " GROUP BY state", ("recapture",),
    ).fetchall() if _has_table(conn, "asks") else []
    ask_states = {r["state"]: r["n"] for r in ask_rows}
    ask_total = sum(ask_states.values())

    fb_rows = conn.execute(
        "SELECT verb, COUNT(*) AS n FROM feedback WHERE channel=?" + scope
        + " GROUP BY verb", (channel, *args),
    ).fetchall()
    fb_counts = {r["verb"]: r["n"] for r in fb_rows}

    return {
        "rule_version": RULE_VERSION,
        "config_version": config_version(conn),
        "channel": channel,
        "boundary_ms": boundary,
        "fired_before_pref_change": fired_before,
        "fired_after_pref_change": fired_after,
        "replays_not_refired": replays,
        "asks": {"total": ask_total, **ask_states},
        "feedback": fb_counts,
    }


def _has_table(conn, name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------- 人话答复
def _age_text(ms):
    if ms is None:
        return "—"
    sec = max(0, int(ms // 1000))
    if sec < 60:
        return "%d 秒" % sec
    if sec < 3600:
        return "%d 分钟" % (sec // 60)
    return "%.1f 小时" % (sec / 3600)


def _answer(conn, device_mac):
    p = get_pref(conn, device_mac, "vision_ask")
    if p is None or p["value"] == "on":
        state = "按默认规则提醒（开）"
    elif p["value"] == "off":
        state = "已关闭（本轮不主动提醒）"
    else:
        remain = max(0, (p["until_ms"] or 0) - now_ms())
        state = "稍后提醒（剩余 %s）" % _age_text(remain)
    return ("提醒偏好（规则版本 %s，配置版本 v%d）：%s。"
            "反馈只调整显式规则，不包含自动个性化。"
            % (RULE_VERSION, config_version(conn), state))