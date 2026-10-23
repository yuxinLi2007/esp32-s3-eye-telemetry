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
    if action not in ("query", "collect"):
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
                "你是遥测系统的自然语言入口。把用户的请求分类成两种任务之一，"
                "只输出一个 JSON 对象，不要输出其它文字：\n"
                "1) 查看已有数据：{\"action\":\"query\"}\n"
                "2) 请求设备重新采集：{\"action\":\"collect\","
                "\"device_mac\":\"AA:BB:CC:DD:EE:FF\"或null,"
                "\"n\":\"样本数\"或null,\"interval_ms\":\"间隔毫秒\"或null}\n"
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
            return {"action": "clarify", "device_mac": None, "params": {},
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
def chat(conn, text):
    """一次自然语言对话。返回 {intent, answer, command, error_code}。

    只有 action == collect 才会走 commands.create（改变设备行为），
    因此也只有这个分支需要 CONTROL_TOKEN（app 层做）。
    """
    devices = known_devices(conn)
    intent = parse_intent(text, devices)
    action = intent["action"]

    if action == "clarify":
        answer = intent["reason"]
    elif action == "refuse":
        answer = intent["reason"]
    elif action == "status":
        answer = status_answer(conn)
    elif action == "query":
        answer = query_answer(conn, intent)
    elif action == "collect":
        out = collect_answer(conn, text, intent)
        return out
    else:
        answer = "无法理解这条请求，请换种说法。"

    return {"intent": intent, "answer": answer, "command": None, "error_code": None}