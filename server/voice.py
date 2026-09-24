"""第5周：语音链路 —— 录音上传、语音识别、语音合成与 voice_events 溯源表。

本周问题：从"说一句话"到"听到回答"，中间每个环节都必须可观察。
链路是：网页按钮录音 -> 本模块识别成文本 -> 文本原样交给第4周 assistant.ask()
-> 回答文本交给合成 -> 浏览器播放。本模块**不新建任何执行通道**：
识别结果只是"一句用户文本"，翻译与执行仍走第4周那一份白名单护栏。

延续项目三条口径，外加本周两条：

  - **音频来源与运行位置必须记录**。audio_source 说明声音从哪来
    （电脑麦克风 / 共享音频站 / 授权录音文件 / 设备麦克风），
    recognition_location / tts_location 说明识别与合成实际跑在哪
    （server / browser）。设备音频未就绪时用电脑音频是课程允许的，
    但"跑在哪"不能含糊——界面上逐条摆出来，也落库可查。
  - **失败必须可见且可理解**。无声、识别失败、识别超时、合成不可用
    各有独立错误码与文案（VOICE_ERROR_CATALOG 是唯一来源，前端不写死中文）。
    语音链路任何故障都不影响文字入口：/assistant/ask 一行没动。
  - 权威时间仍是 t_server_ms；识别/合成耗时是观测值，落库作参考。
  - 派生值不落库：播放状态文案、"第几态"都由前端现算，库里只记事实
    （played 0/1、tts_engine、tts_location、played_t_ms）。

HTTP 口径（与 assistant 一致）：
  - 协议级违规用 HTTP 状态码：401 无令牌、413 超大、415 格式不支持、404 事件不存在。
  - 语义级失败（无声/识别失败/超时/未配置）返回 **200 + ok=false + error**，
    因为"录了一段没声音"是用户能修正的正常结果，不是协议错误。
    唯一例外是 /voice/speak：成功时 body 必须是音频，失败只能 503 + JSON，
    并带 fallback 提示让浏览器合成兜底。
"""

import os

import httpx

from commands import CommandError, now_ms

# ---------------------------------------------------------------- 常量与口径
# 音频来源枚举。界面上每个选项的中文标签也由这里出，前后端共用一份。
AUDIO_SOURCES = (
    "pc_microphone",        # 本机麦克风（课程默认：设备音频未就绪时用电脑音频）
    "shared_audio_station", # 共享音频站（备用路径：现场语音操作补验）
    "authorized_recording", # 授权录音文件（备用路径：录音设备故障时上传真实录音）
    "device_microphone",    # 设备麦克风（端侧课提供能力后启用，本周不实现硬件）
)
SOURCE_LABELS = {
    "pc_microphone": "电脑麦克风",
    "shared_audio_station": "共享音频站",
    "authorized_recording": "授权录音文件",
    "device_microphone": "设备麦克风",
}
RUN_LOCATIONS = ("server", "browser")

# 浏览器 MediaRecorder 与常见录音文件的 MIME。不在此列 = 415，
# 宁可拒收也不把"不知道是什么的字节"送给识别服务。
ALLOWED_MIME = (
    "audio/webm", "audio/ogg", "audio/wav", "audio/x-wav",
    "audio/mpeg", "audio/mp4", "audio/flac",
)
MAX_UPLOAD_BYTES = int(os.environ.get("VOICE_MAX_UPLOAD_MB", "5")) * 1024 * 1024
# 小于这个字节数不可能含可懂语音（webm/opus 静音一秒也有约 1KB）。
# 在网络调用之前拦下，省一次请求，也避免把空文件说成"识别失败"。
SILENCE_MIN_BYTES = 256

RECOGNITION_TIMEOUT_S = float(os.environ.get("VOICE_RECOGNITION_TIMEOUT_S", "20"))
TTS_TIMEOUT_S = float(os.environ.get("VOICE_TTS_TIMEOUT_S", "20"))
DEFAULT_TTS_VOICE = os.environ.get("VOICE_TTS_VOICE", "alloy")

RECOGNITION_ENGINE = "openai_whisper"
TTS_ENGINE = "openai_tts"
BROWSER_TTS_ENGINE = "browser_speechsynthesis"

# 错误码 -> (级别, 文案)。级别沿用界面既有语义：warn 黄、crit 红。
VOICE_ERROR_CATALOG = {
    "no_audio": ("warn", "录音里没有检测到有效人声，请靠近麦克风或提高音量后重录"),
    "voice_not_configured": ("warn", "未配置语音服务（OPENAI_API_KEY 为空），文字入口照常可用"),
    "recognition_failed": ("crit", "语音识别服务返回错误，本次录音已记入事件表可供排查"),
    "recognition_timeout": ("crit", "语音识别服务超时，请检查网络或稍后重试"),
    "audio_too_large": ("crit", "录音超过大小上限，请缩短录音后重试"),
    "audio_type_unsupported": ("crit", "音频格式不受支持，请使用本页录音或 WAV/MP3/OGG 文件"),
    "tts_unavailable": ("warn", "服务端语音合成不可用，已降级为浏览器合成"),
    "tts_failed": ("crit", "语音合成服务返回错误"),
    "tts_timeout": ("crit", "语音合成服务超时"),
    "tts_empty_text": ("warn", "合成文本为空，没有可播放的内容"),
    "bad_source": ("crit", "音频来源不在枚举内，请从下拉列表选择"),
    "bad_location": ("crit", "运行位置不在枚举内（server/browser）"),
    "unknown_event": ("crit", "语音事件不存在（id 有误或库已重建）"),
}


class VoiceError(CommandError):
    """语音链路业务错误。语义级失败 http_status=200，由调用方包成 ok=false 信封。"""


def _err(code, http_status=200, **extra):
    level, message = VOICE_ERROR_CATALOG[code]
    exc = VoiceError(message, code=code, http_status=http_status)
    exc.level = level
    exc.extra = extra
    return exc


# ---------------------------------------------------------------- 表结构
SCHEMA = """
CREATE TABLE IF NOT EXISTS voice_events (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    t_server_ms           INTEGER NOT NULL,   -- 权威时间：服务端收到录音的时刻
    audio_source          TEXT NOT NULL,      -- 声音从哪来（枚举）
    audio_mime            TEXT,
    audio_bytes           INTEGER,
    client_duration_ms    INTEGER,            -- 浏览器自报录音时长，只作参考
    client_ip             TEXT,
    recognition_engine    TEXT,
    recognition_location  TEXT,               -- server / browser
    recognition_latency_ms INTEGER,
    transcript            TEXT,               -- 识别原文（中间结果，必须可见）
    intent                TEXT,               -- 第4周翻译出的意图
    assistant_ok          INTEGER,            -- 第4周信封的 ok，0/1
    request_id            TEXT,               -- 若产生指令，指向 commands 表
    tts_engine            TEXT,               -- openai_tts / browser_speechsynthesis
    tts_location          TEXT,               -- server / browser
    played                INTEGER,            -- 1 播放成功 / 0 播放失败 / null 未播
    played_t_ms           INTEGER,
    error_code            TEXT,
    error_stage           TEXT                -- recognition / task / playback
);
CREATE INDEX IF NOT EXISTS idx_voice_events_t ON voice_events(t_server_ms);
"""


def init_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _row(conn, event_id):
    cur = conn.execute("SELECT * FROM voice_events WHERE id = ?", (int(event_id),))
    row = cur.fetchone()
    return dict(row) if row else None


def get_event(conn, event_id):
    row = _row(conn, event_id)
    if row is None:
        raise _err("unknown_event", 404)
    return row


def record_start(conn, *, audio_source, audio_mime=None, audio_bytes=None,
                 client_duration_ms=None, client_ip=None, now=None):
    """收到一段录音就先落一行：哪怕后面识别失败，"有过这次尝试"也是事实。"""
    src = (audio_source or "").strip()
    if src not in AUDIO_SOURCES:
        raise _err("bad_source", 400)
    now = now_ms() if now is None else now
    cur = conn.execute(
        "INSERT INTO voice_events (t_server_ms, audio_source, audio_mime,"
        " audio_bytes, client_duration_ms, client_ip)"
        " VALUES (?,?,?,?,?,?)",
        (now, src, audio_mime, audio_bytes, client_duration_ms, client_ip),
    )
    conn.commit()
    return _row(conn, cur.lastrowid)


def attach_result(conn, event_id, *, transcript=None, intent=None,
                  assistant_ok=None, request_id=None, recognition_engine=None,
                  recognition_location="server", recognition_latency_ms=None,
                  error_code=None, error_stage=None):
    """把识别/任务结果挂到事件行。只写非 None 字段，重试不会抹掉已有事实。"""
    get_event(conn, event_id)  # 不存在直接 404
    if recognition_location not in RUN_LOCATIONS:
        raise _err("bad_location", 400)
    sets, params = [], []
    for col, val in (
        ("transcript", transcript),
        ("intent", intent),
        ("request_id", request_id),
        ("recognition_engine", recognition_engine),
        ("recognition_latency_ms", recognition_latency_ms),
        ("error_code", error_code),
        ("error_stage", error_stage),
    ):
        if val is not None:
            sets.append(f"{col} = ?")
            params.append(val)
    if assistant_ok is not None:
        sets.append("assistant_ok = ?")
        params.append(1 if assistant_ok else 0)
    if recognition_engine is not None:
        sets.append("recognition_location = ?")
        params.append(recognition_location)
    if sets:
        params.append(int(event_id))
        conn.execute(
            f"UPDATE voice_events SET {', '.join(sets)} WHERE id = ?", params
        )
        conn.commit()
    return _row(conn, event_id)


def mark_played(conn, event_id, *, played, tts_engine, tts_location, now=None):
    """浏览器回告播放结果。播放发生在浏览器里，服务端只能记"它回告了什么"。"""
    get_event(conn, event_id)
    if tts_location not in RUN_LOCATIONS:
        raise _err("bad_location", 400)
    now = now_ms() if now is None else now
    conn.execute(
        "UPDATE voice_events SET played = ?, tts_engine = ?, tts_location = ?,"
        " played_t_ms = ? WHERE id = ?",
        (1 if played else 0, tts_engine, tts_location, now, int(event_id)),
    )
    conn.commit()
    return _row(conn, event_id)


def list_events(conn, *, limit=30, audio_source=None):
    where, params = [], []
    if audio_source:
        where.append("audio_source = ?")
        params.append(audio_source.strip())
    sql = "SELECT * FROM voice_events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 200)))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ---------------------------------------------------------------- 服务商接缝
# 两个 _post_* 是模块级函数，测试用 monkeypatch 替换它们注入失败，
# 不需要真 key、真网络——但真机验证时走的就是这两行真 HTTP。
def _openai_cfg():
    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    base = (os.environ.get("OPENAI_BASE_URL")
            or "https://api.openai.com/v1").rstrip("/")
    return key, base


def _post_transcription(*, key, base_url, filename, data_bytes, mime, timeout_s):
    with httpx.Client(timeout=timeout_s) as client:
        return client.post(
            f"{base_url}/audio/transcriptions",
            headers={"Authorization": f"Bearer {key}"},
            files={"file": (filename, data_bytes, mime)},
            data={"model": "whisper-1", "response_format": "json"},
        )


def _post_speech(*, key, base_url, text, voice, timeout_s):
    with httpx.Client(timeout=timeout_s) as client:
        return client.post(
            f"{base_url}/audio/speech",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": "tts-1", "voice": voice, "input": text},
        )


# ---------------------------------------------------------------- 识别 / 合成
def transcribe(audio_bytes, mime, *, filename="voice.bin", timeout_s=None):
    """音频 -> 文本。返回 dict(text/engine/latency_ms/run_location)。

    语义级失败抛 VoiceError(http_status=200)，调用方包成 ok=false 信封；
    协议级失败（超大/格式）抛 413/415。
    """
    data = audio_bytes or b""
    if len(data) > MAX_UPLOAD_BYTES:
        raise _err("audio_too_large", 413)
    mime = (mime or "").split(";")[0].strip().lower()
    if mime not in ALLOWED_MIME:
        raise _err("audio_type_unsupported", 415)
    if len(data) < SILENCE_MIN_BYTES:
        raise _err("no_audio")
    key, base = _openai_cfg()
    if not key:
        raise _err("voice_not_configured")
    timeout_s = RECOGNITION_TIMEOUT_S if timeout_s is None else timeout_s
    started = now_ms()
    try:
        resp = _post_transcription(
            key=key, base_url=base, filename=filename,
            data_bytes=data, mime=mime, timeout_s=timeout_s,
        )
    except httpx.TimeoutException:
        raise _err("recognition_timeout") from None
    except httpx.HTTPError:
        raise _err("recognition_failed") from None
    if resp.status_code != 200:
        raise _err("recognition_failed")
    try:
        payload = resp.json()
    except ValueError:
        raise _err("recognition_failed") from None
    text = (payload.get("text") or "").strip()
    if not text:
        raise _err("no_audio")
    return {
        "text": text,
        "engine": RECOGNITION_ENGINE,
        "latency_ms": now_ms() - started,
        "run_location": "server",
    }


def synthesize(text, *, voice=None, timeout_s=None):
    """文本 -> (音频字节, mime, engine)。失败抛 VoiceError，由 /voice/speak 转 503。"""
    text = (text or "").strip()
    if not text:
        raise _err("tts_empty_text", 503)
    key, base = _openai_cfg()
    if not key:
        raise _err("tts_unavailable", 503)
    timeout_s = TTS_TIMEOUT_S if timeout_s is None else timeout_s
    try:
        resp = _post_speech(
            key=key, base_url=base, text=text[:4000],
            voice=voice or DEFAULT_TTS_VOICE, timeout_s=timeout_s,
        )
    except httpx.TimeoutException:
        raise _err("tts_timeout", 503) from None
    except httpx.HTTPError:
        raise _err("tts_failed", 503) from None
    if resp.status_code != 200:
        raise _err("tts_failed", 503)
    return resp.content, "audio/mpeg", TTS_ENGINE


def info():
    """能力自述：前端据此渲染来源下拉、降级提示与错误文案，不写死任何中文。"""
    key, _ = _openai_cfg()
    return {
        "ok": True,
        "sources": [{"id": s, "label": SOURCE_LABELS[s]} for s in AUDIO_SOURCES],
        "run_locations": list(RUN_LOCATIONS),
        "recognition": {
            "engine": RECOGNITION_ENGINE,
            "configured": bool(key),
            "run_location": "server",
            "timeout_s": RECOGNITION_TIMEOUT_S,
        },
        "tts": {
            "engine": TTS_ENGINE,
            "configured": bool(key),
            "run_location": "server",
            "fallback_engine": BROWSER_TTS_ENGINE,
            "fallback_location": "browser",
            "timeout_s": TTS_TIMEOUT_S,
        },
        "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
        "allowed_mime": list(ALLOWED_MIME),
        "error_codes": [
            {"code": c, "level": lv, "message": msg}
            for c, (lv, msg) in VOICE_ERROR_CATALOG.items()
        ],
        "note": ("识别文本只是'一句用户文本'，任务翻译与执行仍走第4周 assistant，"
                 "文字入口在任何语音故障下保持可用"),
    }
