"""第4周：自然语言入口 —— 「查看已有数据」与「请求重新采集」的分类与诚实回答。

两条铁律（对着课程第 4 周的验收点）：
  1. 查询与请求采集必须分离：查询只读记录、绝不改设备行为；请求采集必须走
     指令通道（commands.create，op=capture）。一个句子要么是 query 要么是 collect，
     不存在"顺便看一下再采一次"这种混答。
  2. 诚实性：无设备完成证据不得回复"已采集成功"。回答只能由真实结果驱动：
       指令刚下发      -> 只能答"已受理/等待设备执行"（附 request_id）；
       设备执行完成且服务端实收样本 -> 才能报"采集完成，服务端实收 N 条"；
       失败/超时/无人领取 -> 如实报对应状态；
       查询旧数据时如果最后一批已经很旧，必须说明时间与"不能当作当前状态"。

语言模型只在"结构化、可校验"的前提下参与：
  - 模型必须吐 {"action": "query"|"collect", "device_mac": ..., "n": ..., "interval_ms": ...}
    这样的 JSON；解析失败 / 超时 / 无密钥一律降级到确定性关键字规则——
    任何情况下都必须给出答复，不允许整个功能失效。
  - 关键字规则同时是安全护栏：句子只要明确包含"查看 / 重新采集"这类词，
    就以关键字判定为准，不采信模型的相反判断（"查看上次数据"绝不能触发采集）。
  - 运行时语言模型只从环境变量读密钥（LLM_BASE_URL / LLM_API_KEY / LLM_MODEL），
    与开发助手分开配置，密钥不入库、不进前端。

设备范围：目标 MAC 必须是本系统见过的设备（batches 表里去重），
或环境变量 DEVICE_MAC_WHITELIST（逗号分隔）内的设备；参数校验复用
commands.validate_params 的唯一一份范围，前端不另抄。
"""

import json
import os
import re
import secrets
from datetime import datetime

import httpx

import commands
import db
import task_context

# 查询结果"新鲜"的门槛：超过这个时间就不再当作当前状态
QUERY_STALE_MS = 60_000

MAC_RE = commands.MAC_RE

# 确定性关键字规则（安全护栏，永不失效）。顺序无所谓，命中即判定。
COLLECT_WORDS = (
    "重新采集", "重新采", "再采集", "再采一次", "采集一次", "采一次",
    "立即采集", "现在采集", "采集", "采样", "采个样", "重新测", "再测一次",
    "测一次", "再测",
)
QUERY_WORDS = (
    "查看", "查询", "看看", "上次数据", "最近数据", "最新数据", "现在数据",
    "历史数据", "数据怎么样", "数据如何", "有没有数据", "上次", "最近", "最新",
    "数据",
)
# 询问某条采集指令的执行结果：只读状态、绝不新发指令
STATUS_WORDS = (
    "采集结果", "完成没有", "完成了吗", "好了吗", "成功了吗", "采完了",
    "有没有采到", "采到没有", "采到了吗", "有结果",
)
# 第6周：用户反悔 / 想停 —— 取消当前任务（含未领取的采集指令）、停止播报、回空闲。
# "停止采集" 在这里也归取消类：用户主观上是不想要这次采集了，交给取消语义处理。
CANCEL_WORDS = (
    "取消", "撤销", "算了", "别采了", "不采了", "不要了", "停下来",
    "停止采集", "停止播报", "别说了", "闭嘴", "停了吧", "别播了", "停",
)

TRUST_TEXT = {
    "ntp_fresh": "NTP 新鲜", "ntp_stale": "NTP 陈旧",
    "ntp_expired": "NTP 已过期", "unsynced": "未同步",
}


def known_devices(conn):
    """可对之操作的设备集合：白名单优先，否则用系统真正见过的设备。"""
    whitelist = os.environ.get("DEVICE_MAC_WHITELIST", "").strip()
    if whitelist:
        out = []
        for part in whitelist.split(","):
            mac = part.strip().upper()
            if MAC_RE.match(mac):
                out.append(mac)
        return out
    rows = conn.execute(
        "SELECT DISTINCT device_mac FROM batches"
    ).fetchall()
    return sorted({r["device_mac"] for r in rows})


def extract_mac(text):
    m = re.search(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", text or "")
    return m.group(0).upper() if m else None


# ---------------------------------------------------------------- 确定性规则
def _keyword_action(text):
    # 取消优先于一切：用户反悔/喊停时，绝不解释成"再来一次"
    if any(w in text for w in CANCEL_WORDS):
        return "cancel"
    has_collect = any(w in text for w in COLLECT_WORDS)
    has_query = any(w in text for w in QUERY_WORDS)
    if has_collect and has_query:
        return "mixed"
    if has_collect:
        return "collect"
    if has_query:
        return "query"
    return None


def _keyword_params(text):
    """从"采 100 个 / 间隔 50"这类表述里抠参数；抠不到就留给默认值。"""
    params = {}
    m = re.search(r"(\d+)\s*(?:个|条|样本)", text)
    if m:
        params["n"] = int(m.group(1))
    m = re.search(r"(?:间隔|每)\s*(\d+)\s*(?:ms|毫秒)?", text, re.I)
    if m:
        params["interval_ms"] = int(m.group(1))
    return params


# ---------------------------------------------------------------- 语言模型路径
def _parse_llm_json(content):
    """只接受严格 JSON 的意图对象；任何偏差都返回 None，交给关键字规则兜底。"""
    if not content:
        return None
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    action = obj.get("action")
    if action not in ("query", "collect", "cancel"):
        return None
    out = {"action": action}
    mac = obj.get("device_mac")
    if isinstance(mac, str) and MAC_RE.match(mac.strip().upper()):
        out["device_mac"] = mac.strip().upper()
    for k in ("n", "interval_ms"):
        v = obj.get(k)
        if isinstance(v, int) and not isinstance(v, bool):
            out[k] = v
    return out


def _ask_llm(text):
    base = os.environ.get("LLM_BASE_URL", "").strip().rstrip("/")
    key = os.environ.get("LLM_API_KEY", "").strip()
    model = os.environ.get("LLM_MODEL", "").strip()
    if not (base and key and model):
        return None
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": (
                "你是遥测系统的自然语言入口。把用户的请求分类成三类任务之一，"
                "只输出一个 JSON 对象，不要输出其它文字：\n"
                "1) 查看已有数据：{\"action\":\"query\"}\n"
                "2) 请求设备重新采集：{\"action\":\"collect\","
                "\"device_mac\":\"AA:BB:CC:DD:EE:FF\"或null,"
                "\"n\":\"样本数\"或null,\"interval_ms\":\"间隔毫秒\"或null}\n"
                "3) 取消/停止当前任务：{\"action\":\"cancel\"}\n"
                "设备 MAC 只在用户明确给出时填写；n / interval_ms 只在用户明确提出时填写。")},
            {"role": "user", "content": text},
        ],
        "temperature": 0,
        "max_tokens": 120,
    }
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    try:
        r = httpx.post(base + "/chat/completions", headers=headers,
                       json=payload, timeout=6.0)
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    try:
        content = r.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return _parse_llm_json(content)


# ---------------------------------------------------------------- 意图解析
def parse_intent(text, devices):
    """返回 {action, device_mac, params, reason}。
    action ∈ query / collect / clarify / refuse。
    """
    t = (text or "").strip()
    if not t:
        return {"action": "clarify", "device_mac": None, "params": {},
                "reason": "没有收到你的问题，请再说一次。"}

    # 询问已有采集指令的结果：只读状态，永远不新发指令
    if any(w in t for w in STATUS_WORDS):
        return {"action": "status", "device_mac": None, "params": {},
                "reason": "Status query — 只读"}

    try:
        llm = _ask_llm(t)
    except Exception:
        llm = None

    kw = _keyword_action(t)
    if kw == "mixed":
        return {"action": "clarify", "device_mac": None, "params": {},
                "reason": "一句话里同时包含“查看”和“重新采集”，请分开说："
                          "要先看数据就说“查看数据”，要重新采就说“重新采集”。"}

    action = kw
    params = _keyword_params(t)
    if llm:
        # 关键字规则是安全护栏：词面明确的句子（查看/重新采集）不采信模型相反判断
        if action is None:
            action = llm["action"]
        for k in ("n", "interval_ms"):
            if k in params:
                continue          # 用户明说了数字，以用户为准
            if k in llm:
                params[k] = llm[k]
        llm_mac = llm.get("device_mac")

    if action is None:
        return {"action": "clarify", "device_mac": None, "params": {},
                "reason": "没听懂要做什么。可以说“查看数据”看最近状态，"
                          "或说“重新采集”让设备再采一次。"}

    if action == "cancel":
        # 取消/停止：不关心设备与参数，取消的是"当前进行中的任务"
        return {"action": "cancel", "device_mac": None, "params": {},
                "reason": "取消当前任务"}

    mac_in = extract_mac(t)
    device = mac_in or (llm_mac if llm else None)
    if device:
        if device not in devices:
            return {"action": "refuse", "device_mac": device, "params": {},
                    "reason": "设备 %s 不在已知设备列表中，无法对它操作。"
                              "本系统见过的设备：%s"
                              % (device, "、".join(devices) if devices else "（还没有）")}
        return {"action": action, "device_mac": device, "params": params,
                "reason": "ok"}

    if action == "collect":
        if not devices:
            return {"action": "refuse", "device_mac": None, "params": {},
                    "reason": "本系统还没有见过任何设备上报数据，无法请求采集。"}
        if len(devices) > 1:
            return {"action": "clarify", "device_mac": None, "params": params,
                    "reason": "检测到多台已知设备：%s；请说明针对哪一台。"
                              % "、".join(sorted(devices))}
        return {"action": action, "device_mac": devices[0], "params": params,
                "reason": "ok"}

    # query：没指定设备也没关系，就答全系统最近的数据
    return {"action": "query", "device_mac": devices[0] if devices else None,
            "params": params, "reason": "ok"}


# ---------------------------------------------------------------- 三种回答
def _fmt_age(ms):
    if ms < 60_000:
        return "%.0f 秒" % (ms / 1000)
    return "%.1f 分钟" % (ms / 60_000)


def _fmt_clock(ms):
    return datetime.fromtimestamp(ms / 1000).strftime("%H:%M:%S")


def query_answer(conn, intent):
    mac = intent.get("device_mac")
    if mac:
        row = conn.execute(
            "SELECT * FROM batches WHERE device_mac=? ORDER BY id DESC LIMIT 1",
            (mac,),
        ).fetchone()
    else:
        row = conn.execute("SELECT * FROM batches ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return "服务端还没有收到任何数据。"

    row = dict(row)
    now = commands.now_ms()
    age_ms = now - row["t_server_recv_ms"]
    trust = db.trust_level(row["ntp_synced"], row["ntp_sync_age_s"])
    parts = (
        "最近数据来自 %s（boot %s）：服务端 %s 接收，批内 %d 条样本，"
        "时间戳可信度 %s。"
        % (row["device_mac"], row["boot_id"], _fmt_clock(row["t_server_recv_ms"]),
           row["n_readings"], TRUST_TEXT.get(trust, trust))
    )
    if age_ms > QUERY_STALE_MS:
        parts += "该数据接收于 %s 前，已不能当作当前状态。" % _fmt_age(age_ms)
    return parts


def collect_answer(conn, text, intent):
    """请求采集：走指令通道下发 capture；回答只看真实状态。"""
    raw_params = intent.get("params") or {}
    try:
        # 先用自己的范围校验把错挡在指令通道前面（给出可读回答）；
        # 注意别把校验后的 params 传回 create——validate_params 会附加派生字段
        # duration_ms，而 create 内部还会校验一遍，传回去会被当未知参数拒绝。
        checked = commands.validate_params("capture", raw_params)
    except commands.CommandError as exc:
        return {"intent": intent, "answer": "参数无法执行：%s" % exc.message,
                "command": None, "error_code": exc.code}
    token = "ast-" + secrets.token_hex(8)
    try:
        row, deduped = commands.create(conn, device_mac=intent["device_mac"],
                                       op="capture", params=raw_params,
                                       client_token=token,
                                       note="assistant: " + text[:120],
                                       created_by="assistant")
    except commands.CommandError as exc:
        return {"intent": intent, "answer": "下发失败：%s" % exc.message,
                "command": None, "error_code": exc.code}

    cmd = commands.to_dict(row, now=commands.now_ms(),
                           events=commands.events_of(conn, row["request_id"]))
    rid = cmd["request_id"]
    if deduped:
        answer = "该请求刚刚提交过，未重复下发：仍是指令 %s（%s）。" % (
            rid, cmd["state_text"])
    else:
        n, iv = checked["n"], checked["interval_ms"]
        answer = (
            "已受理：采集指令 %s 已下发（%d 条 × %d ms，设备最迟约 %d 秒领取）。"
            "执行结果会以界面上该指令的真实状态为准，请稍候。"
            % (rid, n, iv, cmd["ttl_ms"] // 1000)
        )
    return {"intent": intent, "answer": answer, "command": cmd, "error_code": None}


def status_answer(conn):
    """查询最近一条采集指令的真实状态（只读，不新发指令）。"""
    commands.sweep(conn)   # 先把超期未领/执行超时结算掉，再回答
    row = conn.execute(
        "SELECT * FROM commands WHERE op='capture'"
        " ORDER BY t_created_ms DESC, rowid DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return "还没有下发过采集指令。"
    c = commands.to_dict(row)
    rid = c["request_id"]
    st = c["state"]
    if st == "pending":
        return "采集指令 %s 仍在等待设备领取。" % rid
    if st in ("claimed", "running"):
        pct = c["progress"] if c["progress"] is not None else "—"
        return "设备正在执行采集（进度 %s%%）。" % pct
    if st == "done":
        if c["n_samples"] and c["n_samples"] > 0:
            extra = ("，设备自报 %d 条" % c["n_samples_device"]) \
                if c["n_samples_device"] else ""
            return "采集完成：服务端实收 %d 条样本%s。" % (c["n_samples"], extra)
        return "设备报告完成，但服务端没有收到任何新样本。"   # 自查 n_samples，绝不凭空报成功
    if st == "failed":
        return "采集失败：%s" % (c["error_message"] or c["error_code"] or "未知原因")
    if st == "expired":
        return "采集指令 %s 超期未被设备领取（设备离线或固件未更新？）。" % rid
    if st == "timeout":
        return "采集指令 %s 执行超时，没有任何结果回执。" % rid
    if st == "cancelled":
        return "采集指令 %s 已被撤销。" % rid
    return "采集指令 %s 当前状态：%s。" % (rid, c["state_text"])


# ---------------------------------------------------------------- 对外入口
def _observe_active_terminal(conn, ctx):
    """懒结算：会话里挂着的采集指令若已进终态，记录完成延迟并清空 active。

    跟 status/命令列表一样的"轮询时顺手结账"思路：不需要后台线程，
    任何一次后续对话或 context 查询都会把时序账结掉。
    """
    active = ctx.get("active")
    if not active:
        return
    rid = active["request_id"]
    try:
        c = commands.get_command(conn, rid, with_events=False)
    except commands.CommandError:
        return
    if c["state"] in commands.TERMINAL:
        t_terminal = c.get("t_finished_ms") or commands.now_ms()
        task_context.observe_terminal(ctx["context_id"], rid, t_terminal)


def _cn_num(s) -> int | None:
    table = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    if s.isdigit():
        return int(s)
    return table.get(s)


def _resolve_pick_device(text, intent, devices):
    """澄清"选哪台设备"的后续输入：
    直接给 MAC → 用之；说"第 N 台 / N 号" → 取排序后第 N 台。解析不了返回 None。"""
    mac = extract_mac(text)
    if mac:
        return {**intent, "device_mac": mac} if mac in devices else None
    m = re.search(r"第\s*([一二三四五六七八九十\d两]+)\s*台", text or "")
    if not m:
        m = re.search(r"([一二三四五六七八九十\d两]+)\s*号", text or "")
    if m:
        idx = _cn_num(m.group(1))
        if idx and 1 <= idx <= len(devices):
            return {**intent, "device_mac": sorted(devices)[idx - 1]}
    return None


def _cancel_answer(conn, ctx):
    """第6周取消：未领取的采集指令真撤销；已领取/执行中的停止关注；回空闲。"""
    ctx_id = ctx["context_id"]
    active = ctx.get("active")
    rid = (active or {}).get("request_id")
    if not rid:
        task_context.mark_cancelled(ctx_id)
        return "当前没有进行中的任务，已回到空闲。"
    try:
        c = commands.get_command(conn, rid, with_events=False)
    except commands.CommandError:
        task_context.mark_cancelled(ctx_id, rid)
        return "之前关联的采集指令已不存在，已回到空闲。"
    st = c.get("state")
    if st == "pending":
        try:
            commands.cancel(conn, rid, actor="voice")
        except commands.CommandError:
            pass        # 与设备领取竞态，按已领取处理
        else:
            task_context.mark_cancelled(ctx_id, rid)
            return "已撤销采集指令 %s，不会再有结果播报。" % rid
    if st in ("claimed", "running"):
        task_context.mark_cancelled(ctx_id, rid)
        return "设备已在执行采集，无法中途撤销；已停止关注该任务，迟到结果不会播报。"
    # 终态：本来就是结束的，把时序账结掉再回空闲
    t_terminal = c.get("t_finished_ms") or commands.now_ms()
    task_context.observe_terminal(ctx_id, rid, t_terminal)
    task_context.mark_cancelled(ctx_id, rid)
    return "该任务已经结束（%s），无需取消，已回到空闲。" % (c.get("state_text") or st)


def _execute(conn, text, intent, devices, ctx, t0):
    """按意图分支执行，返回 (intent, answer, command, error_code)。

    ctx 为 None 时与第 4/5 周行为完全一致（无会话上下文）。
    """
    ctx_id = ctx["context_id"] if ctx else None
    action = intent["action"]

    if action == "cancel":
        if ctx_id is None:
            return intent, "当前没有会话上下文，无任务可取消。", None, None
        ans = _cancel_answer(conn, ctx)
        task_context.log_event(ctx_id, "cancel_intent")
        return intent, ans, None, None

    if action == "clarify":
        answer = intent["reason"]
        if ctx_id is not None and "多台" in answer:
            # 澄清是"哪台设备"，挂起的必须是可执行的采集意图（device 待补），
            # 不是本轮这个 clarify 意图本身。
            pending_intent = {"action": "collect", "device_mac": None,
                              "params": intent.get("params", {}),
                              "reason": intent["reason"]}
            task_context.set_pending(ctx_id, "pick_device", 2, intent["reason"],
                                     pending_intent)
            task_context.log_event(ctx_id, "clarify_asked")
        return intent, answer, None, None

    if action == "refuse":
        return intent, intent["reason"], None, None

    if action == "status":
        return intent, status_answer(conn), None, None

    if action == "query":
        return intent, query_answer(conn, intent), None, None

    if action == "collect":
        out = collect_answer(conn, text, intent)
        if ctx_id is not None and out.get("command"):
            rid = out["command"]["request_id"]
            task_context.set_active(ctx_id, rid, "capture",
                                    out["command"].get("t_created_ms") or t0)
            task_context.log_event(ctx_id, "collect_issued", rid)
        return intent, out["answer"], out["command"], out["error_code"]

    return intent, "无法理解这条请求，请换种说法。", None, None


def _answer(conn, ctx, text, devices, t0):
    """先看有没有挂起的澄清；有则优先解决它对得上号的输入，否则正常解析。"""
    if ctx is not None:
        pending = ctx.get("pending")
        if pending and pending["kind"] == "pick_device":
            resolved = _resolve_pick_device(text, pending["intent"], devices)
            if resolved is None:
                kw = _keyword_action(text)
                if kw in ("collect", "query", "cancel"):
                    # 用户在澄清中途改主意 / 换任务 / 喊停：新意图优先，
                    # 挂起的澄清直接作废（防止旧问题的答案覆盖新任务）。
                    task_context.clear_pending(ctx["context_id"])
                    task_context.log_event(ctx["context_id"], "clarify_abandoned",
                                           detail=kw)
                    intent = parse_intent(text, devices)
                    return _execute(conn, text, intent, devices, ctx, t0)
                rounds = pending["rounds_left"] - 1
                if rounds <= 0:
                    task_context.clear_pending(ctx["context_id"])
                    task_context.log_event(ctx["context_id"], "clarify_exhausted")
                    return ({"action": "clarify", "device_mac": None, "params": {},
                             "reason": "澄清次数用完"},
                            pending["question"] + " 算了，请重新完整说一遍："
                            "要重新采集就说“重新采集”，要查看数据就说“查看数据”。",
                            None, None)
                task_context.set_pending(ctx["context_id"], "pick_device", rounds,
                                         pending["question"], pending["intent"])
                task_context.log_event(ctx["context_id"], "clarify_again",
                                       detail="rounds_left=%d" % rounds)
                return (pending["intent"],
                        "再确认一次：" + pending["question"]
                        + "（还能再答 %d 次）" % rounds, None, None)
            task_context.clear_pending(ctx["context_id"])
            task_context.log_event(ctx["context_id"], "clarify_resolved",
                                   detail=resolved["device_mac"])
            return _execute(conn, text, resolved, devices, ctx, t0)
    intent = parse_intent(text, devices)
    return _execute(conn, text, intent, devices, ctx, t0)


def chat(conn, text, context_id=None):
    """一次自然语言对话。返回 {intent, answer, command, error_code[, context]}。

    提供 context_id 时启用第 6 周会话上下文：有限多轮澄清、取消、迟到结果
    隔离（epoch）、request_id 关联日志与计时都挂在会话上；context 视图随响应返回。
    只有 action == collect 或 cancel（都会改变设备行为/状态）需要 CONTROL_TOKEN，
    由 app 层在拿到 result 后按其 intent.action 校验。
    """
    devices = known_devices(conn)
    t0 = commands.now_ms()
    ctx = task_context.get(context_id, create=True) if context_id else None
    if ctx is not None:
        task_context.touch(ctx["context_id"])
        _observe_active_terminal(conn, ctx)
        task_context.log_event(ctx["context_id"], "chat_in",
                               request_id=None, detail=text[:60])
        if not task_context.push_turn(ctx["context_id"], "user", text):
            task_context.drop(ctx["context_id"])
            return {"intent": {"action": "refuse", "device_mac": None,
                               "params": {}, "reason": "轮次超限"},
                    "answer": "这次对话的轮数已到上限，请重新发起（会清空本轮上下文）。",
                    "command": None, "error_code": None}

    intent, answer, command, error_code = _answer(conn, ctx, text, devices, t0)

    if ctx is not None:
        ttff = commands.now_ms() - t0
        task_context.set_first_feedback(ctx["context_id"], t0, ttff)
        task_context.log_event(ctx["context_id"], "chat_out", detail="ttff_ms=%d" % ttff)
        ctx_view = task_context.public_view(ctx["context_id"])
        out = {"intent": intent, "answer": answer, "command": command,
               "error_code": error_code, "context": ctx_view}
    else:
        out = {"intent": intent, "answer": answer, "command": command,
               "error_code": error_code}
    return out