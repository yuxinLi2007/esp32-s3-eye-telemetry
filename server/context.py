"""第11周：根据多源证据组织交互上下文 —— 原始观测 / 派生状态 / 交互决策 三层。

本周问题：系统知道"最近报告的位置"和"当前是否移动"后，应该回答什么、还应询问什么？

三层模型（三层分开保存、分开表达，第1周数据链路原样作 L1）：
  L1 原始观测（raw observations）：读数 / 来源事件 / 推理结果 —— 由 db、sources、
     vision 保存，本模块**只引用不复制**（raw_refs 给出对照索引，附到 docs 三层对照表）；
  L2 派生状态（derived state）：用"有限规则 + 有效时间窗"从 L1 推出的事实，
     每条事实带 **证据来源（evidence）**、**更新时间（updated_ms）**，整体带
     **规则版本（RULE_VERSION）**；不要求设备端同时采样——每个源只用它自己
     最新的观测，规则只做时间窗内关联；
  L3 交互决策（interaction decision）：在 L2 事实上决定"已知事实"答复哪些、
     "待确认信息"追问什么；无法确认的状态**保留为待确认**，不猜、不补。

当堂验证口径（固定写死在答复文本里）：
  - 摄像头遮挡 → 只解释为"画面被遮挡（模型推断）"**绝不说危险**；
  - 陈旧坐标 → 只说"上次报告的位置（X 前）"，**绝不包装成当前位置**；
  - 模型推断与观测明确区分：每条事实都带 观测/推断 标注与来源。

范围边界（照抄任务书）：定位融合算法、情感推断**不进入**本任务；固定规则即可。
"""
import math
import os
import statistics
import time

import sources

# ---------------------------------------------------------------- 规则版本与时间窗
RULE_VERSION = "week11-r1"

# 有效时间窗（毫秒）：只有落在窗内的观测才参与派生；窗外的一律"待确认"。
# 每个源独立开窗（不要求同一时刻采样）：惯性 10s、画面 120s、位置沿用第10周阈值。
MOVE_WINDOW_MS = int(os.environ.get("CONTEXT_MOVE_WINDOW_MS", "10000") or 10000)
VISION_WINDOW_MS = int(os.environ.get("CONTEXT_VISION_WINDOW_MS", "120000")
                       or 120000)
MOVE_SAMPLES_MAX = int(os.environ.get("CONTEXT_MOVE_SAMPLES_MAX", "30") or 30)
# 惯性"移动中"判定：最近窗内加速度幅度（模长）的方差超过该值 → 移动中；否则静止。
# 这只是幅度波动的粗略启发（有限规则），不是位移/路径估计，也不做定位融合。
MOVE_VAR_THRESHOLD = float(os.environ.get("CONTEXT_MOVE_VAR_THRESHOLD", "0.02")
                           or 0.02)

LAYER_OBS = "observation"    # 观测（传感器数值 / 事件）
LAYER_INF = "inference"      # 推断（模型输出）
LAYER_TEXT = {LAYER_OBS: "观测", LAYER_INF: "推断"}


def now_ms():
    return int(time.time() * 1000)


def _age(ms, now):
    return now - ms


def _age_human(ms, now):
    sec = max(0, (now - ms) // 1000)
    if sec < 60:
        return "%d 秒" % sec
    if sec < 3600:
        return "%d 分钟" % (sec // 60)
    if sec < 86400:
        return "%d 小时" % (sec // 3600)
    return "%d 天" % (sec // 86400)


# ---------------------------------------------------------------- L1 引用（不复制）
def raw_refs(conn, device_mac, now=None):
    """三层对照用的 L1 索引：每个源最近一条原始观测的摘要（引用不复制）。"""
    now = now_ms() if now is None else now
    refs = {}
    row = conn.execute(
        """SELECT r.t_device_ms, r.ax, r.ay, r.az
           FROM readings r JOIN batches b ON b.id = r.batch_id
           WHERE r.device_mac=? AND b.request_id IS NULL
           ORDER BY r.t_device_ms DESC, r.id DESC LIMIT 1""",
        (device_mac,),
    ).fetchone()
    if row:
        refs["imu"] = {"kind": LAYER_OBS, "source": "readings",
                       "t_obs_ms": row["t_device_ms"], "age_ms": _age(
                           row["t_device_ms"], now),
                       "note": "最近样本 ax=%.3f ay=%.3f az=%.3f"
                               % (row["ax"] or 0, row["ay"] or 0, row["az"] or 0)}
    for src in ("gnss", "net"):
        st = sources._last_event(conn, device_mac, src)
        if st:
            import json
            raw = json.loads(st["raw"]) if st["raw"] else None
            refs[src] = {"kind": LAYER_OBS, "source": "source_events",
                         "state": st["state"], "t_obs_ms": st["t_obs_ms"],
                         "t_recv_ms": st["t_recv_ms"],
                         "age_ms": _age(st["t_obs_ms"], now),
                         "raw": raw, "replay": bool(st["replay"])}
    inf = _latest_inference(conn, device_mac)
    if inf is not None:
        refs["vision"] = {"kind": LAYER_INF, "source": inf["source"],
                          "task": inf["task"], "t_server_ms": inf["t_server_ms"],
                          "age_ms": _age(inf["t_server_ms"], now),
                          "label": inf["label"], "status": inf["status"],
                          "error_code": inf["error_code"]}
    return refs


def _latest_inference(conn, device_mac, task="usability"):
    """该设备最近一次可用性推理（任务内最新，按服务端时间）。"""
    row = conn.execute(
        """SELECT i.*, s.device_mac AS dev_mac
           FROM inferences i JOIN snapshots s ON s.id = i.snapshot_id
           WHERE s.device_mac=? AND i.task=?
           ORDER BY i.t_server_ms DESC, i.id DESC LIMIT 1""",
        (device_mac, task),
    ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------- L2 派生状态（有限规则）
def _movement(conn, device_mac, now):
    """移动性判据：窗内惯性样本幅度波动（观测 → 派生）。"""
    rows = conn.execute(
        """SELECT r.t_device_ms, r.ax, r.ay, r.az
           FROM readings r JOIN batches b ON b.id = r.batch_id
           WHERE r.device_mac=? AND b.request_id IS NULL
             AND r.t_device_ms >= ?
           ORDER BY r.t_device_ms ASC, r.id ASC LIMIT ?""",
        (device_mac, now - MOVE_WINDOW_MS, MOVE_SAMPLES_MAX),
    ).fetchall()
    if not rows:
        # 看看是"从未有样本"还是"样本已陈旧"
        st = sources.snapshot(conn, device_mac, now)["imu"]
        if st["status"] == "not_attempted":
            return None, {"key": "movement",
                          "question": "设备是否在移动？无惯性证据（未尝试）",
                          "because": "no_imu_evidence"}
        return None, {"key": "movement",
                      "question": "设备是否在移动？惯性样本已陈旧（%s）"
                                  % _age_human(st["last_obs_ms"], now),
                      "because": "imu_stale"}
    mags = [math.sqrt((r["ax"] or 0) ** 2 + (r["ay"] or 0) ** 2
                      + (r["az"] or 0) ** 2) for r in rows]
    var = statistics.pvariance(mags) if len(mags) > 1 else 0.0
    moving = var > MOVE_VAR_THRESHOLD
    updated = rows[-1]["t_device_ms"]
    fact = {
        "key": "movement",
        "value": "移动中" if moving else "静止",
        "extra": {"move_var": round(var, 4),
                  "samples": len(rows),
                  "magnitude_mean_g": round(sum(mags) / len(mags), 3)},
        "layer": LAYER_OBS,
        "evidence": [{"kind": LAYER_OBS, "source": "imu 连续流样本",
                      "updated_ms": updated, "note": "最近 %d 个样本"
                                                     % len(rows)}],
        "updated_ms": updated,
        "rule": "movement:r1",
    }
    return fact, None


def _position(conn, device_mac, now):
    """位置事实：直接引用第10周 sources 的 gnss 状态；陈旧 ≠ 当前位置。"""
    st = sources.snapshot(conn, device_mac, now)["gnss"]
    def fact(value, updated, extra=None, question=None, because=None):
        f = {"key": "position", "value": value, "extra": extra or {},
             "layer": LAYER_OBS,
             "evidence": [{"kind": LAYER_OBS, "source": "gnss 事件",
                           "updated_ms": updated}],
             "updated_ms": updated, "rule": "position:r1"}
        return f, ({"key": "position", "question": question, "because": because}
                   if question else None)
    if st["status"] == "not_attempted":
        return None, {"key": "position",
                      "question": "设备在哪里？无定位证据（GNSS 未尝试）",
                      "because": "no_gnss_evidence"}
    # sources.snapshot 已把 raw 解析成 dict（或 None），直接引用，不重复解析
    raw = st.get("raw") if isinstance(st.get("raw"), dict) else None
    lat, lon = (raw or {}).get("lat"), (raw or {}).get("lon")
    if st["status"] == "success":
        if lat is None or lon is None:
            return None, {"key": "position",
                          "question": "位置已上报但缺少坐标内容，需要重新获取位置吗？",
                          "because": "position_missing_fields"}
        return fact("最近报告的位置", st["last_obs_ms"],
                    extra={"lat": lat, "lon": lon, "age_ms": st["age_ms"],
                           "fresh": True})
    if st["status"] == "stale":
        return fact("上次报告的位置（%s 前，陈旧，不作为当前位置）"
                    % _age_human(st["last_obs_ms"], now), st["last_obs_ms"],
                    extra={"lat": lat, "lon": lon, "age_ms": st["age_ms"],
                           "fresh": False},
                    question="设备现在还在刚才报告的位置附近吗？",
                    because="position_stale")
    # failed（no_fix / offline）
    if st.get("last_success_obs_ms"):
        return fact("上次有效定位在 %s 前（本次无有效解）"
                    % _age_human(st["last_success_obs_ms"], now),
                    st["last_success_obs_ms"],
                    extra={"age_ms": _age(st["last_success_obs_ms"], now),
                           "fresh": False},
                    question="本次无有效定位解，需要重新获取位置吗？",
                    because="gnss_no_fix")
    return None, {"key": "position",
                  "question": "本次无有效定位解，且从未有过有效定位，需要重新获取位置吗？",
                  "because": "gnss_no_fix_never"}


def _camera_view(conn, device_mac, now):
    """画面前提：模型推断（推断层），绝不把遮挡解释成危险。"""
    inf = _latest_inference(conn, device_mac)
    if inf is None:
        return [], None
    if inf["t_server_ms"] < now - VISION_WINDOW_MS:
        return [], {"key": "camera_view",
                    "question": "画面识别结果已陈旧（%s 前），需要重新拍一张吗？"
                                % _age_human(inf["t_server_ms"], now),
                    "because": "vision_stale"}
    import vision
    eff_label, eff_status = vision.effective_label(conn, inf["snapshot_id"])
    label = eff_label if eff_label is not None else inf["label"]
    src_text = "真实模型推理" if inf["source"] == "model" else "回放标注"
    corrected = eff_status == "corrected"
    base_ev = {"kind": LAYER_INF, "source": inf["source"],
               "updated_ms": inf["t_server_ms"],
               "note": "%s%s" % (src_text, "（已人工纠正）" if corrected else "")}
    facts, q = [], None
    if label in ("usable",) or (inf["status"] == "ok" and label == "usable"):
        facts.append({"key": "camera_view", "value": "画面可用",
                      "layer": LAYER_INF,
                      "evidence": [base_ev], "updated_ms": inf["t_server_ms"],
                      "rule": "vision:r1",
                      "extra": {"corrected": corrected}})
    elif label == "occluded":
        facts.append({"key": "camera_view", "value": "画面被遮挡",
                      "layer": LAYER_INF,
                      "evidence": [base_ev], "updated_ms": inf["t_server_ms"],
                      "rule": "vision:r1",
                      "extra": {"corrected": corrected}})
        q = {"key": "camera_view",
             "question": "画面被遮挡，需要重拍一张确认吗？（遮挡不是危险信号）",
             "because": "occluded"}
    elif label == "blurred":
        facts.append({"key": "camera_view", "value": "画面模糊", "layer": LAYER_INF,
                      "evidence": [base_ev], "updated_ms": inf["t_server_ms"],
                      "rule": "vision:r1", "extra": {"corrected": corrected}})
        q = {"key": "camera_view", "question": "画面模糊，需要重拍一张吗？",
             "because": "blurred"}
    else:
        facts.append({"key": "camera_view", "value": "画面无法确认",
                      "layer": LAYER_INF,
                      "evidence": [base_ev], "updated_ms": inf["t_server_ms"],
                      "rule": "vision:r1",
                      "extra": {"label": label, "corrected": corrected}})
        q = {"key": "camera_view", "question": "画面无法确认，需要重拍一张吗？",
             "because": "unknown"}
    return facts, q


def derive(conn, device_mac, now=None):
    """L1 → L2 → L3：三层结果一次给出（facts / open_questions / raw_refs）。"""
    now = now_ms() if now is None else now
    # 完全未知的设备（连一条批次/来源事件都没有）→ 不编任何事实与追问；
    # "有批次但缺个别源"（含读数已清空）才是"缺失输入保留为待确认"。
    known = conn.execute(
        """SELECT 1 FROM batches WHERE device_mac=?
           UNION SELECT 1 FROM source_events WHERE device_mac=? LIMIT 1""",
        (device_mac, device_mac),
    ).fetchone()
    refs = raw_refs(conn, device_mac, now)
    if known is None:
        return {"rule_version": RULE_VERSION, "generated_ms": now,
                "facts": [], "open_questions": [], "raw_refs": refs}
    facts, questions = [], []
    mv, mq = _movement(conn, device_mac, now)
    if mv:
        facts.append(mv)
    if mq:
        questions.append(mq)
    pf, pq = _position(conn, device_mac, now)
    if pf:
        facts.append(pf)
    if pq:
        questions.append(pq)
    cam_facts, cam_q = _camera_view(conn, device_mac, now)
    facts.extend(cam_facts)
    if cam_q:
        questions.append(cam_q)
    # 网络（观测，低优先级事实）：在线/离线/未尝试，用于"三层对照"里补全视图
    net = sources.snapshot(conn, device_mac, now)["net"]
    if net["status"] in ("success", "failed"):
        ev = sources._last_event(conn, device_mac, "net")
        facts.append({"key": "network",
                      "value": ("网络在线" if net["status"] == "success"
                                else "网络离线（%s）" % net["reason"]),
                      "layer": LAYER_OBS,
                      "evidence": [{"kind": LAYER_OBS, "source": "net 事件",
                                    "updated_ms": ev["t_obs_ms"]
                                    if ev else None}],
                      "updated_ms": ev["t_obs_ms"] if ev else None,
                      "rule": "net:r1"})
    elif net["status"] == "not_attempted":
        facts.append({"key": "network", "value": "网络状态未尝试", "layer": LAYER_OBS,
                      "evidence": [], "updated_ms": None, "rule": "net:r1"})
    return {
        "rule_version": RULE_VERSION,
        "generated_ms": now,
        "facts": facts,
        "open_questions": questions,
        "raw_refs": refs,
    }


# ---------------------------------------------------------------- L3 交互决策（答复与追问）
def answer(conn, device_mac, now=None):
    """人话一页：已知事实 + 待确认信息（对应当堂验证三条）。

    每条事实都带 [观测]/[推断] 与来源；待确认的问题单独成段，
    任何一条都不越过"证据允许的范围"。
    """
    now = now_ms() if now is None else now
    ctx = derive(conn, device_mac, now)
    lines = []
    if ctx["facts"]:
        parts = []
        for f in ctx["facts"]:
            tag = LAYER_TEXT[f["layer"]]
            extra = f.get("extra") or {}
            if f["key"] == "position" and extra.get("lat") is not None:
                display = "%s（%s，%s；%s）" % (
                    f["value"], extra["lat"], extra["lon"], tag)
            else:
                display = "%s（%s）" % (f["value"], tag)
            if f.get("evidence"):
                display += "，来源：%s" % "、".join(
                    "%s%s" % (
                        e["note"] if e.get("note") else e["source"],
                        "@%s" % _age_human(e["updated_ms"], now)
                        if e.get("updated_ms") else "")
                    for e in f["evidence"])
            parts.append(display)
        lines.append("已知：%s" % "；".join(parts))
    else:
        lines.append("已知：目前没有任何可确认的上下文事实（全部源都未提供证据）")
    if ctx["open_questions"]:
        lines.append("仍需确认：%s" % "；".join(
            q["question"] for q in ctx["open_questions"]))
    else:
        lines.append("仍需确认：没有需要追问的，当前上下文一致")
    lines.append("规则版本：%s" % RULE_VERSION)
    return "。".join(lines) + "。"