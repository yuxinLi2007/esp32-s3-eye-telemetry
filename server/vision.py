"""第8周：把视觉推理结果变成可理解反馈。

三句话讲清楚本周口径（对着课程第 8 周的验收点）：

- 模型返回"标签 + 分数 + 拒识 + 模型版本"，系统要能表达"看到了什么"
  和"还有什么不能确定"：结构化解析、低分/拒识如实说、给出重新采集与人工纠正入口。
- 分数未校准：score 是模型置信度输出，不是现实正确率；任何界面/答复都不得把
  score 写成"正确率"。场景粗分类（scene）只是"画面像什么"的粗略判断，
  不得转译成精确位置或人身安全判断。
- 推理来源如实标注：source='model' 是真实模型推理；source='replay' 是回放/规则
  占位（只用于界面与流程演练），一律标注"回放"，不计入真实推理证据。

输入-结果-反馈三者对应：inferences 行复制 snapshot 的 request_id 与采集时刻，
即使图像本体因"最少保留"被清理，审计链路（哪次请求 → 什么结果 → 谁纠正过）
依然可反查。
"""

import os
import re
import time
from datetime import datetime

import httpx

import snapshots

# ---------------------------------------------------------------- 配置（环境变量）
# 真实模型服务（教师基线 / 学生候选模型走同一接口）：
#   POST {VISION_BASE_URL}/vision/classify
#     header: X-Task: usability | scene
#     body: 图像字节流（image/jpeg）
#     → 200 {"task": "...", "label": "...", "score": 0.0..1.0,
#            "rejected": true|false, "model_version": "..."}
VISION_BASE_URL = os.environ.get("VISION_BASE_URL", "").strip().rstrip("/")
VISION_API_KEY = os.environ.get("VISION_API_KEY", "").strip()
VISION_MODEL = os.environ.get("VISION_MODEL", "").strip()
VISION_TIMEOUT_MS = int(os.environ.get("VISION_TIMEOUT_MS", "10000") or 10000)

# 分数语义：score 是模型置信度，未经校准。低于该值视为"低分/不确定"（提示重拍），
# 但无论高低都不得表述为正确率。
VISION_LOW_SCORE = float(os.environ.get("VISION_LOW_SCORE", "0.45") or 0.45)

# 回放/占位路径的固定参数。未配置 VISION_BASE_URL 时所有分析都是"回放"：
# 规则可复现、明确标注，不计入真实推理证据。
VISION_REPLAY_SCORE = float(os.environ.get("VISION_REPLAY_SCORE", "0.55") or 0.55)
VISION_REPLAY_LABEL = (os.environ.get("VISION_REPLAY_LABEL", "") or "").strip()
VISION_REPLAY_MODEL = "replay-rule-1"

_TASK_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

# 保底任务 = 画面可用性/遮挡识别；scene（场景粗分类）为拓展任务。
TASKS = ("usability", "scene")
USABILITY_LABELS = ("usable", "occluded", "blurred", "unknown")


class VisionError(Exception):
    """视觉链路（第8周）的业务错误出口，shape 与 SnapshotError/CommandError 一致。"""

    def __init__(self, message, code="bad_request", http_status=400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status


def now_ms():
    return int(time.time() * 1000)


# ---------------------------------------------------------------- 表结构
SCHEMA = """
CREATE TABLE IF NOT EXISTS inferences (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id   INTEGER NOT NULL,
    request_id    TEXT NOT NULL,
    task          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'ok',   -- ok | rejected | error
    label         TEXT,
    score         REAL,
    rejected      INTEGER NOT NULL DEFAULT 0,
    model_version TEXT,
    source        TEXT NOT NULL DEFAULT 'model',-- model | replay
    latency_ms    INTEGER,
    t_server_ms   INTEGER NOT NULL,
    error_code    TEXT,
    error_message TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_inferences_snap_task
    ON inferences (snapshot_id, task);
CREATE INDEX IF NOT EXISTS idx_inferences_request ON inferences (request_id);

CREATE TABLE IF NOT EXISTS corrections (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id  INTEGER NOT NULL,
    request_id   TEXT NOT NULL,
    inference_id INTEGER,
    kind         TEXT NOT NULL DEFAULT 'label',-- 目前只支持 label（纠正类别）
    value        TEXT NOT NULL,
    note         TEXT,
    t_server_ms  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_corrections_snapshot ON corrections (snapshot_id);
"""


def ensure_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ---------------------------------------------------------------- 模型接口
def _call_model(data, task, *, client=None):
    """调用教师基线/学生候选模型（同一接口）。失败时抛 VisionError。

    只负责"把模型输出变成可信的结构化数据"：
      1) HTTP 层失败 → 503 model_unavailable（诚实报错，不编造标签）；
      2) 200 但结构非法 / 分数越界 / 缺关键字段 → 502 bad_model_response
         （结构化解析失败要显式报错，不静默吞掉）。
    """
    if not VISION_BASE_URL:
        raise VisionError("未配置 VISION_BASE_URL，无法调用真实模型", "model_unavailable", 503)
    url = VISION_BASE_URL + "/vision/classify"
    headers = {"Content-Type": "image/jpeg", "X-Task": task}
    if VISION_API_KEY:
        headers["Authorization"] = "Bearer " + VISION_API_KEY
    try:
        r = (client or httpx).post(url, content=data, headers=headers,
                                   timeout=VISION_TIMEOUT_MS / 1000.0)
    except httpx.HTTPError as exc:
        raise VisionError("模型服务不可达：%s" % exc.__class__.__name__,
                          "model_unavailable", 503)
    if r.status_code != 200:
        raise VisionError("模型服务返回 %s（%s）"
                          % (r.status_code, (r.text or "")[:120]),
                          "model_service_error", 502)
    return _parse_model_json(r.text)


def _parse_model_json(text):
    """模型输出 → 结构化字段。任何偏差都报错，不猜不补。"""
    import json
    try:
        obj = json.loads(text)
    except ValueError:
        raise VisionError("模型输出不是合法 JSON", "bad_model_response", 502)
    if not isinstance(obj, dict):
        raise VisionError("模型输出不是 JSON 对象", "bad_model_response", 502)
    task = obj.get("task")
    label = obj.get("label")
    score = obj.get("score")
    rejected = obj.get("rejected", False)
    if not isinstance(task, str) or not _TASK_RE.match(task):
        raise VisionError("模型输出缺 task 或非法", "bad_model_response", 502)
    if not isinstance(label, str) or not label.strip():
        raise VisionError("模型输出缺 label 或为空", "bad_model_response", 502)
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise VisionError("模型输出缺 score 或非数值", "bad_model_response", 502)
    score = float(score)
    if not (0.0 <= score <= 1.0):
        raise VisionError("score 越界（%.3f），应落在 [0,1]" % score,
                          "bad_model_response", 502)
    rejected = bool(rejected)
    version = obj.get("model_version")
    if not isinstance(version, str) or not version.strip():
        version = VISION_MODEL or "unknown"
    return {
        "task": task, "label": label.strip(), "score": score,
        "rejected": rejected, "model_version": version.strip(),
    }


# ---------------------------------------------------------------- 回放路径
def _replay_infer(data, task):
    """规则占位的"回放"推理：可复现、明确标注、不计入真实推理证据。

    只用图像字节做确定性伪随机选择，保证同图同结果（教学可复现）；
    形如"可用/被遮挡"的判断由 VISION_REPLAY_SCORE / VISION_REPLAY_LABEL
    （环境变量，示范用）或字节规则给出，**不是**真实模型推理。
    """
    if VISION_REPLAY_LABEL:
        label = VISION_REPLAY_LABEL
    else:
        import hashlib
        h = int(hashlib.sha256(data).hexdigest(), 16)
        label = ("usable" if h % 7 else "occluded")
        label = (label if h % 11 else "blurred")
    score = VISION_REPLAY_SCORE
    return {
        "task": task, "label": label, "score": score, "rejected": False,
        "model_version": VISION_REPLAY_MODEL,
    }


# ---------------------------------------------------------------- 读写
def get(conn, snapshot_id, task="usability"):
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM inferences WHERE snapshot_id=? AND task=?",
        (int(snapshot_id), task),
    ).fetchone()
    return dict(row) if row else None


def list_inferences(conn, limit=50, request_id=None):
    ensure_schema(conn)
    if request_id:
        rows = conn.execute(
            "SELECT * FROM inferences WHERE request_id=?"
            " ORDER BY t_server_ms DESC, id DESC LIMIT ?", (request_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM inferences ORDER BY t_server_ms DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def _view(row):
    """推理结果对外视图：结构化的"标签+分数+拒识+版本+来源"。"""
    return {
        "id": row["id"],
        "snapshot_id": row["snapshot_id"],
        "request_id": row["request_id"],
        "task": row["task"],
        "status": row["status"],
        "label": row["label"],
        "score": row["score"],
        "rejected": bool(row["rejected"]),
        "model_version": row["model_version"],
        "source": row["source"],
        "playback": row["source"] == "replay",
        "latency_ms": row["latency_ms"],
        "t_server_ms": row["t_server_ms"],
        "error_code": row["error_code"],
        "error_message": row["error_message"],
    }


def analyze_snapshot(conn, snapshot, task="usability", *, force=False,
                     now=None, model=None):
    """对一张已采集的图像做推理（一次任务一张图一条记录，幂等）。

    snapshot: snapshots.get() 的行 dict（调用方已确认存在）。
    返回推理公共视图 dict。模型未配置 → 回放标注的占位结果；
    模型不可达 / 输出非法 → status='error' 的结果行（HTTP 仍 200：
    失败本身就是需要呈现的结果，UI 要能看到"识别失败"并可重试）。
    """
    if task not in TASKS:
        raise VisionError("未知任务类型：%r（可用：%s）"
                          % (task, "、".join(TASKS)), "bad_task")
    now = now_ms() if now is None else now
    sid = snapshot["id"]
    existing = get(conn, sid, task)
    if existing is not None and not force:
        return _view(existing)

    p = snapshots.path_for(snapshot)
    if not p.is_file():
        raise VisionError("图像文件已缺失（最少保留已清理或磁盘异常），无法分析",
                          "image_file_missing", 404)

    data = p.read_bytes()
    t0 = now_ms()
    status, label, score, rejected, version, source, err_code, err_msg = (
        "ok", None, None, False, None, "model", None, None)
    if VISION_BASE_URL:
        try:
            parsed = (model or _call_model)(data, task)
            label = parsed["label"]
            score = parsed["score"]
            rejected = parsed["rejected"]
            version = parsed["model_version"]
            status = "rejected" if rejected else "ok"
            source = "model"
        except VisionError as exc:
            status, err_code, err_msg = "error", exc.code, exc.message
            label, score, version, source = None, None, None, "model"
    else:
        parsed = _replay_infer(data, task)
        label, score = parsed["label"], parsed["score"]
        rejected, version, source = False, parsed["model_version"], "replay"
    latency = now_ms() - t0

    if existing is not None:            # force 重跑：先清旧行，避免唯一索引冲突
        conn.execute("DELETE FROM inferences WHERE id=?", (existing["id"],))
    cur = conn.execute(
        """INSERT INTO inferences
           (snapshot_id, request_id, task, status, label, score, rejected,
            model_version, source, latency_ms, t_server_ms,
            error_code, error_message)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (sid, snapshot["request_id"], task, status, label, score,
         1 if rejected else 0, version, source, latency, now,
         err_code, err_msg),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM inferences WHERE id=?",
                       (cur.lastrowid,)).fetchone()
    return _view(dict(row))


# ---------------------------------------------------------------- 人工纠正
def add_correction(conn, snapshot, kind="label", value=None, note=None, now=None):
    """为一次模型判断补纠错入口。value 为空 / 超长一律拒绝（不存垃圾）。"""
    if kind != "label":
        raise VisionError("暂只支持纠正 label，kind=%r" % kind, "bad_correction")
    value = (value or "").strip()
    if not value:
        raise VisionError("纠正值不能为空", "bad_correction")
    if len(value) > 32:
        raise VisionError("纠正值过长（≤32 字符）", "bad_correction")
    now = now_ms() if now is None else now
    inf = get(conn, snapshot["id"])
    cur = conn.execute(
        """INSERT INTO corrections
           (snapshot_id, request_id, inference_id, kind, value, note, t_server_ms)
           VALUES (?,?,?,?,?,?,?)""",
        (snapshot["id"], snapshot["request_id"],
         inf["id"] if inf else None, kind, value,
         (note or "").strip()[:200], now),
    )
    conn.commit()
    return conn.execute("SELECT * FROM corrections WHERE id=?",
                        (cur.lastrowid,)).fetchone()


def corrections_of(conn, snapshot_id):
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT * FROM corrections WHERE snapshot_id=?"
        " ORDER BY t_server_ms DESC, id DESC", (int(snapshot_id),),
    ).fetchall()
    return [dict(r) for r in rows]


def effective_label(conn, snapshot_id):
    """界面上应展示的标签：用户纠正（最新一条）优先于模型推断。"""
    corr = conn.execute(
        "SELECT * FROM corrections WHERE snapshot_id=?"
        " ORDER BY t_server_ms DESC, id DESC LIMIT 1", (int(snapshot_id),),
    ).fetchone()
    if corr is not None:
        return corr["value"], "corrected"
    inf = get(conn, snapshot_id)
    if inf is not None:
        return inf["label"], inf["status"]
    return None, "no_inference"


# ---------------------------------------------------------------- 人话
def _fmt_clock(ms):
    return datetime.fromtimestamp(ms / 1000).strftime("%H:%M:%S")


SOURCE_TEXT = {"model": "真实模型推理", "replay": "回放标注（非真实推理，仅界面演示）"}
LABEL_TEXT = {"usable": "可用", "occluded": "遮挡", "blurred": "模糊",
              "unknown": "未知", "rejected": "无法判断"}


def _label_human(label):
    """标签人话显示：已知的可用性标签附中文名，场景类别保留原样（未知 schema 不猜）。"""
    zh = LABEL_TEXT.get(label)
    return "%s（%s）" % (zh, label) if zh else (label or "—")


def human_line(inference, corrected=None, captured_ms=None):
    """把一条推理翻译成人话：先说"看到了什么"，再说"还有什么不能确定"。

    开头固定交代来源（回放绝不冒充实拍），结尾固定"分数未校准"
    ——校验口径就藏在每次回答里。captured_ms 若给出（图像接收时刻）则显示之。
    """
    if inference is None:
        return "还没有这条画面的识别结果。"
    src = SOURCE_TEXT.get(inference["source"], inference["source"] or "")
    if inference["status"] == "error":
        return ("画面识别失败（%s）：%s。"
                % (inference["error_code"], inference["error_message"] or "模型无输出"))
    base = "（%s）" % src
    label = corrected if corrected is not None else inference["label"]
    label_disp = _label_human(label)
    score = inference["score"]
    if corrected is not None:
        base += "（已按你的纠正显示）"
    t_clock = _fmt_clock(captured_ms if captured_ms is not None
                         else inference["t_server_ms"])
    if inference["status"] == "rejected":
        return ("%s 画面识别**拒识**：模型无法判断画面内容（分数语义缺失/画面过暗等）。"
                "不确定看到了什么。可试着重拍一张（说“重拍”）。" % base)
    if inference["task"] == "usability":
        if score is not None and score < VISION_LOW_SCORE:
            return ("%s 画面识别为“%s”，但分数偏低（%.2f，未校准，低于 %.2f）——"
                    "不太确定，建议重拍一张（说“重拍”）。分数是模型置信度，不代表现实正确率。"
                    % (base, label_disp, score, VISION_LOW_SCORE))
        return ("%s 画面识别为“%s”（分数 %.2f，未校准；服务端 %s 收到图像）。"
                "分数是模型的置信度，不代表现实正确率。"
                % (base, label_disp, score, t_clock))
    # scene 拓展：粗分类边界——不转精确位置/安全结论
    return ("%s 场景粗分类为“%s”（分数 %.2f，未校准）。这只是画面像什么场景的粗略判断，"
            "不代表精确位置，也不构成任何安全结论。"
            % (base, label, score))