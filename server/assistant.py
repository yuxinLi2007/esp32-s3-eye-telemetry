"""第4周：自然语言助手 —— 让大模型充当"意图翻译官"。

三条设计口径（都延续前3周"可追溯 / 可信度明确 / 失败可见"）：

1. 模型只翻译，不执行。
   大模型唯一的产出是**结构化意图**（intent + slots），不产出动作、不产出数字。
   真正的动作由本模块用白名单校验之后，调用已有的 VPS 接口完成。
   模型幻觉出来的设备、参数、动作，一律在落到指令表之前被挡掉——
   因为模型一旦有执行权，它编出来的 MAC 就是一个真实的越权操作。

2. 两个动作在接口层面就是两条路，不靠"猜"区分：
     query_history   -> 只读 GET /api/v1/readings，保留旧时间戳，绝不下发指令
     request_capture -> 写 POST /api/v1/commands(op=capture)，等新结果，样本带 request_id
   "查看上次"和"重新采集"的时间语义完全不同，所以判错代价也完全不同：
   把"查看"当成"采集"会白白扰动设备；把"采集"当成"查看"会给用户一份假的新数据。

3. 失败必须结构化。
   含糊 -> clarify，越界 -> reject，设备不回 -> device_unreachable / device_timeout，
   每一种都有稳定的 error.code。调用方（Web、测试、以后的语音前端）不需要解析中文。
   任何未预期异常都被兜成 internal_error，不允许把 500 抛给用户。

4. 没有 OPENAI_API_KEY 也能跑。
   规则解析（engine=rules）与 LLM 解析（engine=llm）产出同一种 Intent 结构，
   规则引擎同时是 LLM 的兜底：模型不可用、超时、返回非法 JSON，都不会让功能失效，
   只是引擎字段如实变成 rules 并在 llm_error 里说明原因。

关于"人话回复"：回复文本由服务端按事实模板生成，不让模型复述结果。
模型复述数字会失真，而本项目前3周所有的取舍都在拒绝"数字失真"。
"""

import json
import math
import os
import re
import time

import httpx

import commands
import db

VERSION = "0.4.0"

# ---------------------------------------------------------------- 动作白名单
QUERY_HISTORY = "query_history"
REQUEST_CAPTURE = "request_capture"
CLARIFY = "clarify"
REJECT = "reject"

ACTIONS = (QUERY_HISTORY, REQUEST_CAPTURE)
INTENTS = (QUERY_HISTORY, REQUEST_CAPTURE, CLARIFY, REJECT)

# 助手能碰的 op 只有这一个写操作。第2周还有 ping / selftest / notify，
# 它们不是本周的需求，就不放进助手的白名单——白名单越小，越界越难发生。
ALLOWED_OPS = ("capture",)

# 直接引用第2周状态机的"在飞"集合，助手不另抄一份。
LIVE_STATES = commands.LIVE

# ---------------------------------------------------------------- 错误码表
# level 决定前端怎么呈现：info=澄清，warn=拒绝，error=真实失败。
ERROR_CATALOG = {
    "ambiguous_request": ("info", "指令含糊，缺动作或缺对象。必须让用户二选一，不能替他猜"),
    "unrecognized_request": ("info", "完全没匹配到已知动作。绝不默认执行采集"),
    "need_device": ("info", "服务端看到多台设备，必须让用户指定是哪一台"),
    "unknown_device": ("warn", "没有任何已知设备（白名单为空且库里没有批次）"),
    "forbidden_device": ("warn", "目标设备不在白名单内（越界），拒绝且不下发"),
    "unsupported_action": ("warn", "要求的是本助手两个动作之外的写操作，拒绝"),
    "bad_params": ("warn", "参数不在允许范围内，且无法安全夹取"),
    "no_data": ("info", "查询命中 0 条数据。不是错误，但必须如实说明"),
    "device_unreachable": ("error", "指令在 ttl 内没有设备来领取（离线 / MAC 写错）"),
    "device_no_response": ("error", "在用户/调用方给定的等待窗口内，指令还没结束（设备慢或在重试）"),
    "device_timeout": ("error", "设备领了指令却一直不回结果"),
    "device_failed": ("error", "设备明确回报失败"),
    "command_cancelled": ("warn", "指令被撤销"),
    "too_many_live": ("warn", "该设备在飞指令已达上限"),
    "internal_error": ("error", "未预期异常已被兜底，不允许把 500 抛给用户"),
}

# ---------------------------------------------------------------- 词表
# 顺序要紧：先否掉"不能被本助手做的写操作"，再判采集，再判查询。
UNSUPPORTED_PATTERNS = (
    "停止采集", "关掉采集", "关闭采集", "暂停采集", "停采集", "停掉采集",
    "停止采样", "停止上传", "别采了", "不要采集", "别再采集", "不要再采集",
    "别上传了", "停止", "停下来", "停掉", "关掉", "关闭", "暂停",
    "重启", "复位", "恢复出厂", "关机", "刷机", "升级固件", "改固件",
    "删除数据", "删掉数据", "删除", "清空", "格式化", "清库",
    "改密码", "修改密码", "改密钥",
    "改时间", "校准时间", "对时",
    "打开摄像头", "拍照", "录音", "开始录音", "播放音乐", "开灯",
    "改成", "设置为", "设置成", "修改参数", "改参数",
)

CAPTURE_PATTERNS = (
    "重新采集", "重新采样", "重新采", "再采集", "再次采集", "再采一次", "再采",
    "重新测", "重测", "再测", "重新获取", "重新读取", "重新读", "重新取",
    "重新拿", "重新拉", "重新跑", "重新执行采集", "重新上传", "重新记录",
    "立刻采集", "立即采集", "马上采集", "现在采集", "手动采集", "主动采集",
    "立刻采", "立即采", "马上采", "现在采", "现在就采", "现在就采集",
    "采集一次", "采一次", "采一下", "采一组", "采集一组", "来一组",
    "取一次", "拉一次", "催采", "催一下采集", "新采集", "采集新的",
    "采集一轮", "采一轮", "跑一次采集", "执行一次采集",
)

QUERY_PATTERNS = (
    "查看", "查询", "查一下", "查查", "看看", "看一下", "看下", "瞧一眼",
    "显示", "展示", "给我看", "列一下", "列出", "汇总", "概览", "报告",
    "上次", "上一次", "上一回", "历史", "之前", "以前", "过去", "刚才",
    "最近", "最新", "当前", "现在多少", "数据", "记录", "读数", "采样值",
    "多少", "情况", "状态", "怎么样", "如何", "有没有数据", "有多少",
    "加速度", "声压", "spl", "合矢量", "波形", "曲线", "图上",
)

AMBIGUOUS_PATTERNS = (
    "帮我弄", "帮我搞", "帮我操作", "帮我处理", "帮个忙", "帮我看看怎么办",
    "弄一下", "搞一下", "整一下", "处理一下", "处理下", "操作一下", "操作下",
    "看着办", "看着弄", "随便", "都行", "你决定", "来一下", "安排一下",
    "帮我干", "帮我做", "帮忙弄", "帮忙搞", "折腾一下", "弄弄", "搞搞",
)

BARE_ACTION_WORDS = ("采集", "采样", "采", "测", "测量", "测试", "拿数据",
                    "取数据", "上个数据", "读数据", "查数据")

DEFAULT_DEVICE_WORDS = (
    "我的设备", "我的板子", "我的开发板", "我这块", "本机", "这台设备", "这个设备",
    "当前设备", "当前这台", "这块板子", "板子", "开发板", "设备",
)

MAC_RE = re.compile(r"\b([0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5})\b")

# 中文数字只做常用范围，够表达"采十次"这种说法即可；再大的数量本来就该走 UI。
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


# ============================================================ 设备白名单
def allowed_devices(conn):
    """允许助手操作的设备集合，按"最近上传优先"排序。

    DEVICE_ALLOWLIST 一旦设置就是**权威**白名单（公网部署口径）：
    此时即使库里被人灌了别的 MAC 的批次，也不会被助手碰。
    未设置时退化成"服务端见过的设备"，本地开发零配置——
    但"见过"来自 batches 而不是来自用户输入的任意字符串，所以
    "控制别人的设备"依然会命中 forbidden_device。
    """
    raw = (os.environ.get("DEVICE_ALLOWLIST") or "").strip()
    if raw:
        out = []
        for part in raw.split(","):
            mac = part.strip().upper().replace("-", ":")
            if mac and mac not in out:
                out.append(mac)
        return out
    rows = conn.execute(
        "SELECT device_mac, MAX(id) AS last_id FROM batches"
        " GROUP BY device_mac ORDER BY last_id DESC"
    ).fetchall()
    return [r["device_mac"] for r in rows]


def default_device(conn):
    devices = allowed_devices(conn)
    return devices[0] if len(devices) == 1 else None


def _struct_error(code, message=None, **extra):
    level, desc = ERROR_CATALOG.get(code, ("error", code))
    err = {"code": code, "level": level, "message": message or desc}
    for k, v in extra.items():
        if v is not None:
            err[k] = v
    return err


# ============================================================ 文本槽位抽取
def extract_mac(text):
    m = MAC_RE.search(text or "")
    if not m:
        return None
    return m.group(1).upper().replace("-", ":")


def _cn_to_int(tok):
    tok = (tok or "").strip()
    if not tok:
        return None
    if tok.isdigit():
        return int(tok)
    if tok in _CN_DIGITS:
        return _CN_DIGITS[tok]
    # 十一、十二、二十、二十三 这类两字组合
    if len(tok) == 2 and tok[0] in _CN_DIGITS and tok[1] in _CN_DIGITS:
        if tok[0] == "十":
            return 10 + _CN_DIGITS[tok[1]]
        if tok[1] == "十":
            return _CN_DIGITS[tok[0]] * 10
    if len(tok) == 3 and tok[1] == "十":
        return _CN_DIGITS.get(tok[0], 0) * 10 + _CN_DIGITS.get(tok[2], 0)
    return None


_NUM = r"(?:\d+|[零一二两三四五六七八九十]{1,3})"


def extract_capture_slots(text):
    """从一句话里抽 n / interval_ms。抽不到就用 OPS 目录里的默认值，
    范围与夹取也全部以 commands.OPS 为准，绝不在这里抄第二份上下限。"""
    slots = {}
    t = text or ""
    m = re.search(r"(" + _NUM + r")\s*(?:次|遍|个点|个样本|个|组)", t)
    if m:
        v = _cn_to_int(m.group(1))
        # "重新采集一次"/"采一遍"里的"一次/一遍"是语气词，不是"只要 1 个样本"。
        # 判据是它紧跟在采集动词后、且前面没有"采集N次"式的真数量词。
        before = t[:m.start()]
        idiom = (m.group(0).startswith("一次") or m.group(0).startswith("一遍")) and \
            re.search(r"(采集|采样|采|测|取|读|拉|跑|记录)\s*$", before)
        if v and not idiom:
            slots["n"] = v
    m = re.search(r"(?:每|间隔|每隔)\s*(" + _NUM + r")\s*(?:毫秒|ms|MS|Ms)", text or "")
    if m:
        v = _cn_to_int(m.group(1))
        if v:
            slots["interval_ms"] = v
    m = re.search(r"(" + _NUM + r")\s*(?:秒|s)\s*(?:一次|1次|采|间隔)", text or "")
    if m and "interval_ms" not in slots:
        v = _cn_to_int(m.group(1))
        if v:
            slots["interval_ms"] = int(v) * 1000
    return slots


def extract_query_slots(text):
    slots = {}
    m = re.search(r"(?:最近|最新|最后|近|前)\s*(" + _NUM + r")\s*(?:条|个|点|次)",
                  text or "")
    if not m:
        m = re.search(r"(" + _NUM + r")\s*(?:条|个|点)\s*(?:数据|记录|样本|读数)",
                      text or "")
    if m:
        v = _cn_to_int(m.group(1))
        if v:
            slots["limit"] = v
    return slots


def looks_like_bare_action(text):
    t = text or ""
    return any(w in t for w in BARE_ACTION_WORDS)


# ============================================================ 规则引擎
def parse_rules(text, *, devices, default_mac):
    """把一句话翻译成 Intent（规则版）。

    返回 dict：{intent, device_mac, slots, confidence, error, notes}
    规则引擎是 LLM 的兜底，也定义了"什么算含糊"的最低标准。
    """
    raw = (text or "").strip()
    notes = []
    if not raw:
        return {"intent": CLARIFY, "device_mac": None, "slots": {}, "confidence": 0.0,
                "error": _struct_error("unrecognized_request", "输入为空，没听到任何动作"),
                "notes": notes}

    lower = raw.lower()
    hit_unsupported = [p for p in UNSUPPORTED_PATTERNS if p in raw]
    hit_capture = [p for p in CAPTURE_PATTERNS if p in raw]
    hit_query = [p for p in QUERY_PATTERNS if p in raw or p in lower]
    hit_ambiguous = [p for p in AMBIGUOUS_PATTERNS if p in raw]

    # 0) 越界优先：句子里显式点名了白名单外的 MAC，直接拒绝，且不再往下判动作。
    #    这是最硬的一道边界——不管他要求的是查看还是采集，"控制别人的设备"都不行。
    named = extract_mac(raw)
    if named and named not in devices:
        return {"intent": REJECT, "device_mac": None, "slots": {}, "confidence": 0.98,
                "error": _struct_error(
                    "forbidden_device",
                    "设备 %s 不在本服务端的允许名单里，助手不会对它做任何操作。"
                    % named,
                    requested_device=named, allowed_devices=list(devices)),
                "notes": notes}

    # 1) 白名单之外的写操作：一律拒绝，绝不放行到命令通道。
    if hit_unsupported and not hit_capture:
        return {"intent": REJECT, "device_mac": None, "slots": {}, "confidence": 0.95,
                "error": _struct_error(
                    "unsupported_action",
                    "这句话要求的是「%s」。助手只做两件事：查看历史数据、请求一次新采集。"
                    "停止/重启/删除这类写操作请走指令面板或第1周的采集开关。" % hit_unsupported[0]),
                "notes": notes}

    # 2) 含糊：有客气的废话但没有可执行的动作。
    if hit_ambiguous and not hit_capture and not hit_query:
        return {"intent": CLARIFY, "device_mac": None, "slots": {}, "confidence": 0.4,
                "error": _struct_error(
                    "ambiguous_request",
                    "「%s」没说明要做什么。是想【查看已有数据】，还是【立刻重新采集一次】？"
                    % hit_ambiguous[0]),
                "notes": notes}

    # 3) 两类动作。
    if hit_capture:
        slots = extract_capture_slots(raw)
        intent = REQUEST_CAPTURE
        conf = 0.9
    elif hit_query:
        slots = extract_query_slots(raw)
        intent = QUERY_HISTORY
        conf = 0.85
    elif looks_like_bare_action(raw):
        # 裸动作词但如果带了明确的采集参数（N 次 / 间隔），用户显然是要一次新采集，
        # 不是要查看历史——这种不算含糊。
        probe = extract_capture_slots(raw)
        if probe:
            return {"intent": REQUEST_CAPTURE, "device_mac": None, "slots": probe,
                    "confidence": 0.8, "error": None, "notes": notes}
        # 只说了"采集/测一下"这种裸动作词：两种解释都成立，必须澄清，不能替用户选。
        return {"intent": CLARIFY, "device_mac": None, "slots": {}, "confidence": 0.3,
                "error": _struct_error(
                    "ambiguous_request",
                    "只说了「采集」但没说是哪一种：是要【查看已有数据】（不碰设备），"
                    "还是【重新采集一次】（会下发指令并等新结果）？"),
                "notes": notes}
    else:
        return {"intent": CLARIFY, "device_mac": None, "slots": {}, "confidence": 0.1,
                "error": _struct_error(
                    "unrecognized_request",
                    "没听懂「%s」。助手能做的两件事是：查看历史数据、请求一次新采集。" % raw),
                "notes": notes}

    device_mac, derr = _resolve_device(raw, devices, default_mac)
    if derr:
        return {"intent": REJECT, "device_mac": None, "slots": slots,
                "confidence": conf, "error": derr, "notes": notes}
    return {"intent": intent, "device_mac": device_mac, "slots": slots,
            "confidence": conf, "error": None, "notes": notes}


def _resolve_device(text, devices, default_mac):
    """定设备。三步：显式 MAC -> 指向词 -> 唯一设备兜底。

    显式 MAC 不在白名单时直接 forbidden_device，而且**不告诉他库里有哪些设备**——
    越界请求不该换来一份设备清单。
    """
    named = extract_mac(text)
    if named:
        if not devices:
            return None, _struct_error("unknown_device",
                                       "还没有任何已知设备，无法确定要操作哪一台")
        if named not in devices:
            return None, _struct_error(
                "forbidden_device",
                "设备 %s 不在本服务端的允许名单里，助手不会对它下发任何指令。" % named,
                requested_device=named, allowed_devices=list(devices))
        return named, None

    if not devices:
        return None, _struct_error(
            "unknown_device",
            "还没有任何已知设备。先让板子上传一次数据，或用 DEVICE_ALLOWLIST 配置允许的设备。")

    if len(devices) == 1:
        return devices[0], None

    if any(w in text for w in DEFAULT_DEVICE_WORDS):
        return devices[0], None

    return None, _struct_error(
        "need_device",
        "服务端看到 %d 台设备，请说明是哪一台（例如直接给出 MAC）。" % len(devices),
        allowed_devices=list(devices))


# ============================================================ LLM 引擎
LLM_SYSTEM_PROMPT = """你是物联网遥测系统的"意图翻译官"。你唯一的职责是把用户的一句话翻译成结构化意图 JSON。

你只能输出下面两种动作之一：
- "query_history"：查看/查询/显示**已经存在**的历史数据。绝不触碰设备。
- "request_capture"：请求设备**立刻重新采集一次**新数据。

以及两种拒绝：
- "clarify"：用户的话含糊、缺动作或指代不明，无法判断是上面哪一件。
- "reject"：用户要求的事情超出上面两个动作（例如停止采集、重启设备、删除数据、控制不在名单里的设备）。

输出 JSON，字段固定为：
{"intent": "query_history|request_capture|clarify|reject",
 "device_mac": "AA:BB:CC:DD:EE:FF 或 null",
 "slots": {"limit": 整数或null, "n": 整数或null, "interval_ms": 整数或null},
 "reason": "一句话说明你的判断依据"}

铁律：
1. device_mac 只能从下面给出的 allowed_devices 里选，或为 null。用户提到名单外的设备时，
   必须输出 reject，并把名单外 MAC 原样填进 device_mac，让服务端记录这次越界尝试。
2. 不确定是"查看"还是"采集"时，输出 clarify，不要猜。猜错会白白扰动设备或给出假的新数据。
3. 不要编造数据、不要编造数字、不要输出任何数据内容。你只翻译意图。
4. slots 只允许上面三个字段。多一个字段都会被服务端丢弃。
"""


def llm_config():
    return {
        "api_key": os.environ.get("OPENAI_API_KEY") or "",
        "base_url": (os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/"),
        "model": os.environ.get("OPENAI_MODEL") or "gpt-4o-mini",
        "timeout_s": float(os.environ.get("ASSISTANT_LLM_TIMEOUT_S") or "8"),
        "enabled": (os.environ.get("ASSISTANT_LLM_ENABLED") or "1").lower()
                   not in ("0", "false", "no", "off"),
    }


def llm_available():
    cfg = llm_config()
    return bool(cfg["enabled"] and cfg["api_key"])


def parse_llm(text, *, devices, default_mac):
    """调大模型做意图翻译。返回 (intent_dict, llm_error)。任何异常都只降级，不抛出。"""
    cfg = llm_config()
    if not cfg["enabled"]:
        return None, "ASSISTANT_LLM_ENABLED=0"
    if not cfg["api_key"]:
        return None, "未配置 OPENAI_API_KEY"

    user_payload = json.dumps(
        {"utterance": text, "allowed_devices": list(devices),
         "default_device": default_mac},
        ensure_ascii=False,
    )
    body = {
        "model": cfg["model"],
        "temperature": 0,
        "messages": [
            {"role": "system", "content": LLM_SYSTEM_PROMPT},
            {"role": "user", "content": user_payload},
        ],
        "response_format": {"type": "json_object"},
    }
    url = cfg["base_url"] + "/chat/completions"
    headers = {"Authorization": "Bearer " + cfg["api_key"],
               "Content-Type": "application/json"}
    try:
        resp = httpx.post(url, json=body, headers=headers, timeout=cfg["timeout_s"])
        if resp.status_code == 400:
            # 部分兼容端点不认 response_format：去掉它重试一次，
            # 而不是因为一个可选参数就把整个 LLM 通道判死。
            body.pop("response_format", None)
            resp = httpx.post(url, json=body, headers=headers, timeout=cfg["timeout_s"])
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        raw = json.loads(content)
    except Exception as exc:  # noqa: BLE001 —— 降级路径必须吞掉一切异常
        return None, "%s: %s" % (type(exc).__name__, exc)

    return _sanitize_llm(raw, devices=devices, default_mac=default_mac)


def _sanitize_llm(raw, *, devices, default_mac):
    """把模型输出"洗"成受控 Intent。这一步是模型越权的唯一一道闸门。

    模型输出的一切都按不可信输入处理：intent 必须在白名单里、MAC 必须在白名单里、
    slots 只留三个已知字段、多出来的字段全丢。
    """
    if not isinstance(raw, dict):
        return None, "模型返回的不是 JSON 对象"
    kind = str(raw.get("intent") or "").strip()
    if kind not in INTENTS:
        return None, "模型返回的 intent 不在白名单：%r" % kind

    slots_in = raw.get("slots") if isinstance(raw.get("slots"), dict) else {}
    reason = str(raw.get("reason") or "").strip()[:300]
    named_raw = raw.get("device_mac")
    named = None
    if isinstance(named_raw, str) and named_raw.strip():
        named = named_raw.strip().upper().replace("-", ":")
        if not MAC_RE.fullmatch(named):
            return None, "模型返回的 device_mac 格式非法：%r" % named_raw

    if named and devices and named not in devices:
        return {"intent": REJECT, "device_mac": None, "slots": {}, "confidence": 0.9,
                "error": _struct_error(
                    "forbidden_device",
                    "设备 %s 不在允许名单里，助手不会对它下发任何指令。" % named,
                    requested_device=named, allowed_devices=list(devices), source="llm"),
                "notes": [], "llm_reason": reason}

    if named and not devices:
        return {"intent": REJECT, "device_mac": None, "slots": {}, "confidence": 0.9,
                "error": _struct_error("unknown_device", "还没有任何已知设备", source="llm"),
                "notes": [], "llm_reason": reason}

    if kind in (CLARIFY, REJECT):
        code = "ambiguous_request" if kind == CLARIFY else "unsupported_action"
        return {"intent": kind, "device_mac": None, "slots": {}, "confidence": 0.8,
                "error": _struct_error(code, reason or None, source="llm"),
                "notes": [], "llm_reason": reason}

    # 动作类：设备按"点名 -> 默认"补齐，仍然要过白名单。
    device_mac = named or default_mac
    if device_mac is None and len(devices) == 1:
        device_mac = devices[0]
    if device_mac is None:
        return {"intent": CLARIFY, "device_mac": None, "slots": {}, "confidence": 0.5,
                "error": _struct_error(
                    "need_device",
                    "请说明要操作哪台设备（服务端当前已知 %d 台）。" % len(devices),
                    allowed_devices=list(devices), source="llm"),
                "notes": [], "llm_reason": reason}

    slots = {}
    if kind == QUERY_HISTORY:
        lim = slots_in.get("limit")
        if isinstance(lim, (int, float)) and lim > 0:
            slots["limit"] = int(lim)
    else:
        for name in ("n", "interval_ms"):
            v = slots_in.get(name)
            if isinstance(v, (int, float)) and v > 0:
                slots[name] = int(v)

    return {"intent": kind, "device_mac": device_mac, "slots": slots, "confidence": 0.9,
            "error": None, "notes": [], "llm_reason": reason}


# ============================================================ 动作执行
# 说明：下面两个执行器复用各接口的**同一份实现**（db.query_readings / commands.create
# / commands.get_command），而不是在助手内部另抄一套 SQL 或状态机。
# app.py 的 /api/v1/readings 与 /api/v1/commands 包的是同一批函数，
# 所以"助手调用的就是已有接口的能力"这一点由代码结构保证，而不是靠文档声明。
TOOL_READINGS = {"tool": "readings", "method": "GET", "endpoint": "/api/v1/readings"}
TOOL_COMMANDS = {"tool": "commands", "method": "POST", "endpoint": "/api/v1/commands"}
TOOL_COMMAND_GET = {"tool": "commands", "method": "GET", "endpoint": "/api/v1/commands/{request_id}"}


def _iso(ms):
    from datetime import datetime, timezone
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _reading_view(row):
    """给用户看的样本视图。把"服务端接收时刻"和"设备时刻"分开摆，
    并给服务端时刻一个 UTC 字符串——前端要"保留旧时间"时用的就是它。"""
    return {
        "id": row.get("id"),
        "seq": row.get("seq"),
        "boot_id": row.get("boot_id"),
        "t_device_ms": row.get("t_device_ms"),
        "t_server_recv_ms": row.get("t_server_recv_ms"),
        "t_server_recv_utc": _iso(row.get("t_server_recv_ms")),
        "t_device_ntp_ms": row.get("t_device_ntp_ms"),
        "ax": row.get("ax"),
        "ay": row.get("ay"),
        "az": row.get("az"),
        "spl_db": row.get("spl_db"),
        "mag_g": _mag(row.get("ax"), row.get("ay"), row.get("az")),
        "t_trust": db.trust_level(row.get("ntp_synced"), row.get("ntp_sync_age_s")),
        "request_id": row.get("request_id"),
    }


def _mag(ax, ay, az):
    if ax is None or ay is None or az is None:
        return None
    return math.sqrt(ax * ax + ay * ay + az * az)


def fit_capture_params(slots):
    """把助手抽到的槽位夹进 commands.OPS 的真实范围，并把每次调整都报出来。

    上下限只从 OPS 读，不抄第二份；夹取而不要报错，是因为"采 500 次"这种话
    用户想表达的是"尽量多采"，直接失败反而不如夹到 200 并明说。
    但时长上限必须真的压住：采集期间连续流全靠环形缓冲扛着（见 commands.py）。
    """
    spec = commands.OPS["capture"]["params"]
    dur_cap = commands.MAX_CAPTURE_DURATION_MS
    n = slots.get("n") or spec["n"]["default"]
    interval = slots.get("interval_ms") or spec["interval_ms"]["default"]
    adj = []

    n = int(n)
    if n < spec["n"]["lo"]:
        adj.append("n=%d 小于下限，已按 %d 处理" % (n, spec["n"]["lo"]))
        n = spec["n"]["lo"]
    if n > spec["n"]["hi"]:
        adj.append("n=%d 超过上限，已夹到 %d" % (n, spec["n"]["hi"]))
        n = spec["n"]["hi"]

    interval = int(interval)
    if interval < spec["interval_ms"]["lo"]:
        adj.append("interval_ms=%d 小于下限，已按 %d 处理" % (interval, spec["interval_ms"]["lo"]))
        interval = spec["interval_ms"]["lo"]
    if interval > spec["interval_ms"]["hi"]:
        adj.append("interval_ms=%d 超过上限，已夹到 %d" % (interval, spec["interval_ms"]["hi"]))
        interval = spec["interval_ms"]["hi"]

    if n * interval > dur_cap:
        new_n = max(spec["n"]["lo"], dur_cap // interval)
        adj.append("n×interval_ms=%d 超过单次采集上限 %d ms，n 已降到 %d"
                   % (n * interval, dur_cap, new_n))
        n = new_n
    return {"n": n, "interval_ms": interval}, adj


def fit_query_limit(slots):
    lim = slots.get("limit") or 200
    adj = []
    if lim < 1:
        adj.append("limit=%d 小于下限，已按 1 处理" % lim)
        lim = 1
    if lim > 500:
        adj.append("limit=%d 超过上限，已夹到 500" % lim)
        lim = 500
    return int(lim), adj


def execute_query_history(conn, *, device_mac, slots, server_now_ms):
    """只读：查已有数据，并**原样保留** t_server_recv_ms。

    默认只返回连续流（和 /api/v1/readings 的默认口径一致）；"查看上次"这类
    指向最新一条的说法会把 limit 收成 1，让用户拿到的是"上次那一条"，
    而不是一坨最近数据。
    """
    latest_only = bool(slots.get("_latest_only"))
    limit, adj = fit_query_limit(slots)
    if latest_only:
        limit = 1
    rows = db.query_readings(conn, limit=limit, device_mac=device_mac)
    view = [_reading_view(r) for r in rows]
    trust = db.trust_counts(rows)
    gaps = db.find_gaps(rows)
    latest = view[-1] if view else None
    data = {
        "kind": "history",
        "is_new_sample": False,
        "device_mac": device_mac,
        "count": len(view),
        "latest": latest,
        "readings": view,
        "trust_counts": trust,
        "gaps": gaps,
        "filters": {"limit": limit, "device_mac": device_mac},
        "adjustments": adj,
        "note": "这些是库里已经存在的数据，时间戳是入库时记下的服务端接收时刻，未被改写。",
    }
    return data, (TOOL_READINGS | {"params": {"limit": limit, "device_mac": device_mac}})


class _NoopSleep:
    """测试用的可替换睡眠。默认是 time.sleep。"""

    def __call__(self, seconds):
        time.sleep(seconds)


SLEEP = _NoopSleep()
POLL_INTERVAL_S = 0.5

# 测试用的确定性钩子：每次轮询前调用 callable(conn, request_id, attempt)。
# 生产路径上它永远是 None，不影响行为；有了它，"设备何时领取、何时回执"
# 就不必靠 sleep + 线程去撞时间。
_TEST_POLL_HOOK = None


def execute_request_capture(conn, *, device_mac, slots, wait_ms, client_token,
                            server_now_ms, note=None, created_by=None):
    """写操作：下发 capture 指令，等设备真的采集完，再把**新样本**取回来。

    "等待新结果"这件事必须落在设备回执上，不能靠"等 2 秒再查库"：
    后者在网络抖动时会拿旧样本冒充新样本，这正是本周测试要防的错。
    """
    params, adj = fit_capture_params(slots)
    if client_token is None:
        import secrets as _secrets
        client_token = "nl-" + _secrets.token_hex(8)
    cmd_note = (note or "自然语言助手请求重新采集")[:200]

    try:
        row, deduped = commands.create(
            conn, device_mac=device_mac, op="capture", params=params,
            client_token=client_token, note=cmd_note, created_by=created_by,
            now=server_now_ms,
        )
    except commands.CommandError as exc:
        return None, (TOOL_COMMANDS | {"params": params}), _struct_error(
            exc.code if exc.code in ERROR_CATALOG else "bad_params", exc.message)

    rid = row["request_id"]
    created_ms = row["t_created_ms"]
    budget_ms = wait_ms
    if budget_ms is None:
        budget_ms = row["ttl_ms"] + row["timeout_ms"] + 1000
    budget_ms = max(0, int(budget_ms))
    deadline = server_now_ms + budget_ms

    # 轮询时钟用真实时间：这里是在等一个物理世界的事件（设备上网、采集、回执），
    # 用假时间会变成"时钟跳过去但设备没动"的自欺。
    waited_ms = 0
    polled = 0
    # 先调一次钩子再读状态：测试可以在这里推进假时钟或模拟设备动作，
    # 于是"设备在窗口内到底做了什么"是确定的，而不是靠 sleep 去撞。
    if _TEST_POLL_HOOK is not None:
        _TEST_POLL_HOOK(conn, rid, polled)
    cmd = commands.get_command(conn, rid, with_events=False)
    while cmd["state"] not in commands.TERMINAL and waited_ms < budget_ms:
        polled += 1
        SLEEP(POLL_INTERVAL_S)
        waited_ms += int(POLL_INTERVAL_S * 1000)
        if cmd["state"] in LIVE_STATES and waited_ms >= budget_ms:
            break
        if _TEST_POLL_HOOK is not None:
            _TEST_POLL_HOOK(conn, rid, polled)
        cmd = commands.get_command(conn, rid, with_events=False)

    action = TOOL_COMMANDS | {
        "request_id": rid,
        "op": "capture",
        "params": params,
        "state": cmd["state"],
        "state_text": cmd["state_text"],
        "deduped": deduped,
        "waited_ms": waited_ms,
        "polls": polled,
        "adjustments": adj,
        "client_token": client_token,
        "queue_url": "/api/v1/commands/%s" % rid,
    }

    if cmd["state"] != commands.DONE:
        if cmd["state"] in commands.LIVE:
            # 指令还活着，只是等待窗口用完了。绝不能谎称"设备超时"——
            # 它可能下一秒就回执。如实报告，并让调用方拿着 request_id 继续等。
            code = "device_no_response"
            msg = None   # 用错误码表里那句"还在等"，不要说成"已结束"
        else:
            code = _terminal_code(cmd)
            msg = _terminal_message(cmd, device_mac)
        return None, action, _struct_error(
            code, msg,
            request_id=rid, command_state=cmd["state"],
            command_state_text=cmd["state_text"],
            error_code=cmd.get("error_code"),
            error_message=cmd.get("error_message"),
        )

    samples = db.query_readings(conn, request_id=rid, limit=500)
    view = [_reading_view(r) for r in samples]
    is_new = bool(view) and all((r["t_server_recv_ms"] or 0) >= created_ms for r in view)
    data = {
        "kind": "capture",
        "is_new_sample": is_new,
        "device_mac": device_mac,
        "request_id": rid,
        "count": len(view),
        "latest": view[-1] if view else None,
        "readings": view,
        "capture_started_ms": created_ms,
        "trust_counts": db.trust_counts(samples),
        "gaps": db.find_gaps(samples),
        "adjustments": adj,
        "note": "这批样本由本次 capture 指令产生（request_id 非空），"
                "t_server_recv_ms 晚于指令下发时刻。",
    }
    if not view:
        # 指令说成功但一条样本都没入库：这本身就是必须暴露的异常，不能装作成功。
        return data, action, _struct_error(
            "device_failed", "设备回报采集成功，但没查到属于这条 request_id 的样本",
            request_id=rid, command_state=cmd["state"])
    return data, action, None


def _terminal_code(cmd):
    return {
        commands.EXPIRED: "device_unreachable",
        commands.TIMEOUT: "device_timeout",
        commands.FAILED: "device_failed",
        commands.CANCELLED: "command_cancelled",
    }.get(cmd["state"], "device_timeout")


def _terminal_message(cmd, device_mac):
    st = cmd["state"]
    if st == commands.EXPIRED:
        return ("设备 %s 在 %d 秒内没有来领取这条采集指令（可能离线、"
                "WiFi 不通或 MAC 不对）。指令已判为超期未领取，没有数据产生。"
                % (device_mac, cmd["ttl_ms"] // 1000))
    if st == commands.TIMEOUT:
        return ("设备 %s 领取了采集指令，但 %d 秒内没回结果，已判为执行超时。"
                "指令不会自动重跑：重复采集会产生两批对不上号的数据。"
                % (device_mac, cmd["timeout_ms"] // 1000))
    if st == commands.FAILED:
        return "设备明确回报失败：%s" % (cmd.get("error_message") or cmd.get("error_code") or "未给出原因")
    if st == commands.CANCELLED:
        return "这条采集指令在设备领取前被撤销了。"
    return "指令以 %s 状态结束，未能拿到新数据。" % st


# ============================================================ 总入口
def _decide_engine(text, devices, default_mac, engine):
    """选引擎并做安全对账。返回 (choice, chain, llm_error)。

    安全优先的三条对账规则：
      1. 规则引擎判定为 reject（越界/不支持写操作）时，一律以规则为准。
         模型再客气也不能把一次越权请求"翻译"成合法动作——拒绝优先。
      2. 规则与模型给出**两个不同的动作**时，视为模型越界，强制澄清并附诊断。
         这是本周最危险的一类错：把"查看上次"翻成"重新采集"，或反过来。
         两者时间语义相反，谁都不许悄悄替用户选。
      3. 模型说含糊、规则却高置信命中动作时，按规则执行，避免过度追问。
         规则引擎的词表是本项目自己维护的、可测试的权威；模型不是。
    """
    rules = parse_rules(text, devices=devices, default_mac=default_mac)
    chain = ["rules"]
    llm_error = None
    llm = None
    if engine in ("auto", "llm"):
        try:
            llm, llm_error = parse_llm(text, devices=devices, default_mac=default_mac)
        except Exception as exc:  # noqa: BLE001
            # 模型通道的任何异常都只意味着"这次翻译用规则"，绝不把一次
            # 本来能回答的查询变成 internal_error。降级必须比失败更常见。
            llm, llm_error = None, "%s: %s" % (type(exc).__name__, exc)
        if llm is not None:
            chain.append("llm")

    if engine == "rules":
        return rules, chain, llm_error

    chosen = None
    if rules["intent"] == REJECT:
        chosen = rules
        chosen["notes"] = list(chosen.get("notes") or []) + ["规则引擎判定为拒绝，拒绝优先"]
    elif llm is None:
        chosen = rules
    elif llm["intent"] in ACTIONS and rules["intent"] in ACTIONS \
            and llm["intent"] != rules["intent"]:
        chosen = {
            "intent": CLARIFY, "device_mac": None, "slots": {}, "confidence": 0.5,
            "error": _struct_error(
                "ambiguous_request",
                "这句话可以理解成两种相反的动作：规则引擎读成「%s」，模型读成「%s」。"
                "为避免把「查看旧数据」误当成「重新采集」（或反过来），请明确说："
                "是「查看上次数据」还是「重新采集一次」？" % (rules["intent"], llm["intent"])),
            "notes": ["规则与模型动作不一致，强制澄清（安全优先）"],
        }
    elif llm["intent"] == CLARIFY and rules["intent"] in ACTIONS and rules["confidence"] >= 0.85:
        chosen = rules
        chosen["notes"] = list(chosen.get("notes") or []) + [
            "模型倾向于澄清，但规则引擎高置信命中动作，按规则执行（避免过度追问）"]
    else:
        chosen = llm
    return chosen, chain, llm_error


def ask(conn, text, *, device_mac=None, engine="auto", wait_ms=None,
        client_token=None, created_by=None, now=None):
    """第4周总入口：一句话 -> 受控意图 -> 调用已有接口 -> 结构化结果。

    返回值永远是同一个信封，无论成功、澄清、越界还是设备不回。
    调用方不需要 try/except，因为这里不允许把异常抛出去。
    """
    t0 = now if now is not None else int(time.time() * 1000)
    started = time.time()
    try:
        return _ask_inner(conn, text, device_mac=device_mac, engine=engine,
                          wait_ms=wait_ms, client_token=client_token,
                          created_by=created_by, now=t0, started=started)
    except Exception as exc:  # noqa: BLE001 —— 兜底，绝不让助手把 500 抛给用户
        return _envelope(
            ok=False, text=("助手内部出错了，已记录并兜底：%s: %s"
                            % (type(exc).__name__, exc)),
            intent=None, engine=engine, confidence=0.0, device_mac=device_mac,
            action=None, data=None,
            error=_struct_error("internal_error", "%s: %s" % (type(exc).__name__, exc)),
            extra={"trace": {"server_time_ms": t0, "elapsed_ms": int((time.time() - started) * 1000),
                             "version": VERSION}},
        )


def _ask_inner(conn, text, *, device_mac, engine, wait_ms, client_token,
               created_by, now, started):
    devices = allowed_devices(conn)
    default_mac = default_device(conn)

    explicit = (device_mac or "").strip().upper().replace("-", ":") or None
    if explicit:
        if not MAC_RE.fullmatch(explicit):
            return _envelope(
                ok=False, text="你指定的设备 MAC 格式不对：%s" % device_mac,
                intent=None, engine=engine, confidence=1.0, device_mac=None,
                action=None, data=None,
                error=_struct_error("forbidden_device",
                                    "device_mac 格式非法，应为 AA:BB:CC:DD:EE:FF",
                                    requested_device=device_mac),
                extra={"trace": _trace(now, started),
                       "allowed_devices": devices})
        if explicit not in devices:
            return _envelope(
                ok=False, text="设备 %s 不在允许名单里，助手不会对它下发指令。" % explicit,
                intent=REJECT, engine=engine, confidence=1.0, device_mac=None,
                action=None, data=None,
                error=_struct_error("forbidden_device",
                                    "设备 %s 不在本服务端的允许名单里。" % explicit,
                                    requested_device=explicit, allowed_devices=devices),
                extra={"trace": _trace(now, started)})
        default_mac = explicit

    chosen, chain, llm_error = _decide_engine(text, devices, default_mac, engine)
    intent = chosen["intent"]
    conf = chosen.get("confidence", 0.0)
    err = chosen.get("error")
    trace = _trace(now, started)
    trace["engine_chain"] = chain
    trace["engine_requested"] = engine
    trace["engine_used"] = "llm" if ("llm" in chain and engine != "rules"
                                     and not llm_error) else "rules"
    trace["llm_used"] = trace["engine_used"] == "llm"
    if llm_error:
        trace["llm_error"] = llm_error
        trace["llm_used"] = False

    if intent in (CLARIFY, REJECT):
        return _envelope(
            ok=False,
            text=chosen.get("error", {}).get("message") or "没听懂，请换一种说法。",
            intent=intent, engine=engine, confidence=conf, device_mac=None,
            action=None, data=None, error=err, extra={"trace": trace})

    # 显式 MAC 是硬约束：句子里点名的设备与它不一致时也不能悄悄换设备。
    resolved = chosen.get("device_mac") or default_mac
    if explicit and resolved and resolved != explicit:
        return _envelope(
            ok=False, text="这句话点名的设备（%s）与你指定的设备（%s）不一致，已拒绝。"
                 % (resolved, explicit),
            intent=REJECT, engine=engine, confidence=conf, device_mac=None,
            action=None, data=None,
            error=_struct_error("forbidden_device",
                                "句内设备与请求指定设备不一致",
                                requested_device=resolved, allowed_devices=devices),
            extra={"trace": trace})
    if resolved is not None and devices and resolved not in devices:
        # 最后一道闸门：不管意图来自规则还是模型（甚至来自被替换的实现），
        # 只要设备不在白名单里就拒绝。安全不能依赖"上游一定守规矩"。
        return _envelope(
            ok=False, text="设备 %s 不在允许名单里，助手不会对它下发任何指令。" % resolved,
            intent=REJECT, engine=engine, confidence=conf, device_mac=None,
            action=None, data=None,
            error=_struct_error("forbidden_device",
                                "意图解析结果里的设备不在本服务端允许名单内。",
                                requested_device=resolved, allowed_devices=devices),
            extra={"trace": trace})
    if resolved is None:
        return _envelope(
            ok=False, text="请说明要操作哪台设备。",
            intent=CLARIFY, engine=engine, confidence=conf, device_mac=None,
            action=None, data=None,
            error=_struct_error("need_device", None, allowed_devices=devices),
            extra={"trace": trace})

    slots = dict(chosen.get("slots") or {})
    if intent == QUERY_HISTORY:
        slots["_latest_only"] = bool(
            re.search(r"上次|上一次|上一回|刚才|最新|最后|这[一二三]?次", text or ""))
        data, action = execute_query_history(
            conn, device_mac=resolved, slots=slots, server_now_ms=now)
        if data["count"] == 0:
            err = _struct_error(
                "no_data",
                "设备 %s 目前没有任何已入库的样本（连续流为空）。"
                "可以让我重新采集一次，或先用 /api/v1/status 看设备是否在线。" % resolved)
            answer = err["message"]
        else:
            latest = data["latest"]
            head = ("这是设备 %s 已存在的最近 1 条数据" % resolved) if slots["_latest_only"] \
                else ("这是设备 %s 已存在的最近 %d 条数据" % (resolved, data["count"]))
            answer = ("%s，服务端接收时刻 t_server_recv_ms=%s（%s），"
                      "设备时刻 t_device_ms=%s，时间戳可信度 %s。数据来自库里，没有重新采集。"
                      % (head, latest["t_server_recv_ms"], latest["t_server_recv_utc"],
                         latest["t_device_ms"], latest["t_trust"]))
        action = action | {"state": "ok" if data["count"] else "empty"}
        if data["count"] == 0:
            return _envelope(ok=False, text=answer, intent=intent, engine=engine,
                             confidence=conf, device_mac=resolved, action=action,
                             data=data, error=err, extra={"trace": trace,
                                                          "allowed_devices": devices})
        return _envelope(ok=True, text=answer, intent=intent, engine=engine,
                         confidence=conf, device_mac=resolved, action=action,
                         data=data, error=err, extra={"trace": trace,
                                                      "allowed_devices": devices})

    # request_capture
    data, action, err = execute_request_capture(
        conn, device_mac=resolved, slots=slots, wait_ms=wait_ms,
        client_token=client_token, server_now_ms=now, created_by=created_by)
    if err is not None:
        answer = ("已向设备 %s 下发重新采集指令（%s）。%s"
                  % (resolved, action.get("request_id"), err["message"]))
        return _envelope(ok=False, text=answer, intent=intent, engine=engine,
                         confidence=conf, device_mac=resolved, action=action,
                         data=data, error=err, extra={"trace": trace,
                                                      "allowed_devices": devices})
    latest = data["latest"]
    answer = ("设备 %s 已重新采集完成：%d 个新样本（request_id=%s）。"
              "本次新数据 t_server_recv_ms=%s（%s），晚于指令下发时刻 %s；"
              "这不是上一次的旧数据。"
              % (resolved, data["count"], data["request_id"],
                 latest["t_server_recv_ms"], latest["t_server_recv_utc"],
                 _iso(data["capture_started_ms"])))
    return _envelope(ok=True, text=answer, intent=intent, engine=engine,
                     confidence=conf, device_mac=resolved, action=action,
                     data=data, error=None, extra={"trace": trace,
                                                   "allowed_devices": devices})


def _trace(now, started):
    return {
        "server_time_ms": now,
        "server_time_utc": _iso(now),
        "elapsed_ms": int((time.time() - started) * 1000),
        "version": VERSION,
    }


def _envelope(*, ok, text, intent, engine, confidence, device_mac, action, data,
              error, extra):
    out = {
        "ok": ok,
        "intent": intent,
        "engine": engine,
        "confidence": round(float(confidence or 0.0), 3),
        "device_mac": device_mac,
        "answer": text,
        "action": action,
        "data": data,
        "error": error,
    }
    out.update(extra or {})
    return out
