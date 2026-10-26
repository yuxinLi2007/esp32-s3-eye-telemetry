"""第6周：语音/文字任务的会话上下文（进程内、有界、按会话隔离）。

职责：
- 存"正在进行的任务"：意图、参数、待澄清项、当前采集指令 request_id、取消标记；
- 记"request_id 关联的事件日志与计时"（输入结束→首次反馈 ttff；采集→完成延迟）；
- 所有状态都是内存态、有上限（会话数 / 每会话轮数 / TTL），重启即清空，
  不承担持久化职责——与"音频最小化不落库"同一口径。

迟到结果隔离的前端一半由 epoch 承担：context.epoch 在"新任务 / 取消"时递增，
前端拿 epoch 判断迟到应答该不该播。指令服务端的隔离由既有关状态机兜底
（终态后不接收 result），这里只管"语音任务的迟到播报/回答"不越界。
"""

import re
import threading
import time
import uuid

# 有界参数（模块级常量，测试可直接改）
MAX_CONTEXTS = 32        # 同时在内存的会话数上限
MAX_TURNS = 8            # 每会话轮数上限（超限后强制"重新发起"）
TTL_MS = 10 * 60_000     # 会话存活时间（毫秒）

# 前端生成的 context_id 走 UUID hex；这里只做格式兜底，防脏 key。
CONTEXT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

_store: "dict[str, dict]" = {}
_lock = threading.Lock()
_real_now = None  # 测试可替换


def _now_ms() -> int:
    if _real_now is not None:
        return _real_now()
    return int(time.time() * 1000)


def new_context_id() -> str:
    """服务端兜底生成（客户端一般自己生成 UUID hex）。"""
    return uuid.uuid4().hex[:24]


def reset() -> None:
    """清空全部上下文（测试用）。"""
    with _lock:
        _store.clear()


def drop(context_id: str) -> None:
    """丢弃单个会话（轮数超限 / 用户要求重新发起时用）。"""
    with _lock:
        _store.pop(context_id, None)


def _expired(c: dict) -> bool:
    return _now_ms() - c["last_ms"] > TTL_MS


def get(context_id: str, *, create: bool = False) -> dict | None:
    """取会话。过期即弃；create=True 时新建（并保证总量 ≤ MAX_CONTEXTS）。"""
    if not context_id or not CONTEXT_ID_RE.match(context_id):
        return None
    with _lock:
        c = _store.get(context_id)
        if c is not None and _expired(c):
            del _store[context_id]
            c = None
        if c is None and create:
            c = _new(context_id)
            _store[context_id] = c
            while len(_store) > MAX_CONTEXTS:
                _store.pop(next(iter(_store)))
        return c


def _new(context_id: str) -> dict:
    now = _now_ms()
    return {
        "context_id": context_id,
        "created_ms": now,
        "last_ms": now,
        "epoch": 0,            # 新任务 / 取消时 +1，前端据此丢弃迟到应答
        "turns": [],           # [{t, role, text?}]
        "pending": None,       # {kind, rounds_left, question, intent}
        "active": None,        # {request_id, op, t_created_ms}
        "cancelled": False,
        "log": [],             # [{t, event, request_id?}]
        "timing": {},          # 最近一次任务：{ttff_ms, t_done_ms, latency_ms}
    }


def touch(context_id: str) -> None:
    with _lock:
        c = _store.get(context_id)
        if c is not None:
            c["last_ms"] = _now_ms()


def push_turn(context_id: str, role: str, text: str | None = None) -> bool:
    """追加一轮。返回 False 表示轮数已到上限（调用方应强制重新发起）。"""
    c = get(context_id)
    if c is None:
        return True
    c["turns"].append({"t": _now_ms(), "role": role,
                       "text": (text or "")[:200]})
    c["last_ms"] = _now_ms()
    return len(c["turns"]) <= MAX_TURNS


def log_event(context_id: str, event: str, request_id: str | None = None,
              detail: str | None = None) -> None:
    c = get(context_id)
    if c is None:
        return
    entry = {"t": _now_ms(), "event": event}
    if request_id:
        entry["request_id"] = request_id
    if detail:
        entry["detail"] = (detail or "")[:160]
    c["log"].append(entry)
    # 日志也要有界：只留最近 40 条，避免会话活太久把内存吃穿
    if len(c["log"]) > 40:
        del c["log"][: len(c["log"]) - 40]
    c["last_ms"] = _now_ms()


def bump_epoch(context_id: str) -> int:
    c = get(context_id)
    if c is None:
        return 0
    c["epoch"] += 1
    c["last_ms"] = _now_ms()
    return c["epoch"]


def set_pending(context_id: str, kind: str, rounds_left: int,
                question: str, intent: dict) -> None:
    c = get(context_id)
    if c is None:
        return
    c["pending"] = {"kind": kind, "rounds_left": rounds_left,
                    "question": question, "intent": intent}
    c["last_ms"] = _now_ms()


def clear_pending(context_id: str) -> None:
    c = get(context_id)
    if c is not None:
        c["pending"] = None


def set_active(context_id: str, request_id: str, op: str,
               t_created_ms: int) -> None:
    c = get(context_id)
    if c is None:
        return
    c["active"] = {"request_id": request_id, "op": op,
                   "t_created_ms": t_created_ms, "t_done_ms": None}
    c["timing"] = {"t_input_end_ms": None, "ttff_ms": None,
                   "t_done_ms": None, "latency_ms": None}
    c["last_ms"] = _now_ms()


def set_first_feedback(context_id: str, t_input_end_ms: int, ttff_ms: int) -> None:
    """记录本回合"输入结束 → 首次反馈"的延迟；采集任务后续的完成延迟是叠加项。"""
    c = get(context_id)
    if c is None:
        return
    c["timing"]["t_input_end_ms"] = t_input_end_ms
    c["timing"]["ttff_ms"] = max(0, ttff_ms)
    c["last_ms"] = _now_ms()


def mark_cancelled(context_id: str, request_id: str | None = None) -> None:
    c = get(context_id)
    if c is None:
        return
    c["cancelled"] = True
    log_event(context_id, "task_cancelled", request_id)
    bump_epoch(context_id)
    c["pending"] = None


def observe_terminal(context_id: str, request_id: str,
                     t_terminal_ms: int) -> None:
    """懒结算：观察到采集指令进入终态时记录完成时刻与延迟（不清已有 ttff）。"""
    c = get(context_id)
    if c is None or not c.get("active"):
        return
    if c["active"]["request_id"] != request_id:
        return
    created = c["active"]["t_created_ms"]
    c["active"]["t_done_ms"] = t_terminal_ms
    t = c["timing"]
    if t.get("t_input_end_ms") is None and created:
        t["t_input_end_ms"] = created
    t["t_done_ms"] = t_terminal_ms
    if created:
        t["latency_ms"] = max(0, t_terminal_ms - created)
    log_event(context_id, "task_terminal", request_id,
              "state=done latency_ms=%s" % t.get("latency_ms"))
    c["active"] = None


def public_view(context_id: str) -> dict | None:
    """可供接口返回的视图：不暴露内部对象引用。"""
    c = get(context_id)
    if c is None:
        return None
    return {
        "context_id": c["context_id"],
        "epoch": c["epoch"],
        "turns": list(c["turns"]),
        "pending": c["pending"],
        "active": c["active"],
        "cancelled": c["cancelled"],
        "log": list(c["log"]),
        "timing": dict(c["timing"]),
    }