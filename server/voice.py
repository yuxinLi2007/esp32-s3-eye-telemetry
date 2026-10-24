"""第5周：语音链路（ASR 转写 / TTS 合成）。

设计口径：
- 语音服务的密钥只存在服务端环境变量（VOICE_ASR_URL / VOICE_TTS_URL /
  VOICE_API_KEY / VOICE_MODEL），前端与设备都不接触密钥。
- 未配置、超时、无语音、服务报错都必须返回明确的错误码与 HTTP 状态，
  不允许假装成功——与第 4 周"诚实回答"同一套口径。
- 音频最小化：只在用户主动录音时上传一次，不落库、不保存、不持续监听。
- 鉴权口径：转写/合成都是只读操作（不改变设备行为），与第 4 周"查询"一致（匿名），
  不需要 CONTROL_TOKEN。
"""

import os

import httpx

ASR_URL = os.environ.get("VOICE_ASR_URL", "").strip()
TTS_URL = os.environ.get("VOICE_TTS_URL", "").strip()
API_KEY = os.environ.get("VOICE_API_KEY", "").strip()
MODEL = os.environ.get("VOICE_MODEL", "").strip()
# 设备音频（板载 I2S 麦克风）流是否已接通：接通才在界面上标"设备音频"，否则标"电脑音频"。
DEVICE_AUDIO = os.environ.get("VOICE_DEVICE_AUDIO", "").strip() in ("1", "true", "yes")

MAX_AUDIO_BYTES = 10 * 1024 * 1024  # 一次录音的合理上限
ASR_TIMEOUT_S = 8.0
TTS_TIMEOUT_S = 12.0


class VoiceError(Exception):
    """语音接口的统一错误出口：code 给程序看，message 给人看。"""

    def __init__(self, code: str, message: str, http_status: int = 502):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def info() -> dict:
    """运行位置与配置状态：界面据此如实标注音频来源。"""
    return {
        "ok": True,
        "asr_configured": bool(ASR_URL),
        "tts_configured": bool(TTS_URL),
        "device_audio": bool(DEVICE_AUDIO),
    }


def transcribe(audio: bytes, content_type: str | None = None) -> str:
    """音频 -> 文本。任何失败都抛 VoiceError（带错误码），绝不返回假文本。"""
    if not audio:
        raise VoiceError("voice_no_audio", "识别失败：无有效语音", 400)
    if len(audio) > MAX_AUDIO_BYTES:
        raise VoiceError("voice_too_large", "识别失败：音频过大", 413)
    if not ASR_URL:
        raise VoiceError(
            "voice_asr_not_configured",
            "识别失败：语音识别服务未配置（VOICE_ASR_URL）",
            503,
        )
    data = _asr_request(audio, content_type or "audio/webm")
    text = ""
    if isinstance(data, dict):
        for k in ("text", "transcript", "result"):
            if isinstance(data.get(k), str):
                text = data[k]
                break
    elif isinstance(data, str):
        text = data
    text = (text or "").strip()
    if not text:
        raise VoiceError("voice_no_audio", "识别失败：无有效语音", 400)
    return text


def _asr_request(audio: bytes, content_type: str):
    """真正的 ASR 网络调用，测试用 monkeypatch 替换。"""
    headers = {"Accept": "application/json"}
    if API_KEY:
        headers["Authorization"] = "Bearer " + API_KEY
    files = {"file": ("ptt.webm", audio, content_type)}
    try:
        r = httpx.post(ASR_URL, files=files, headers=headers, timeout=ASR_TIMEOUT_S)
    except httpx.TimeoutException:
        raise VoiceError("voice_asr_timeout", "识别失败：识别服务超时", 504)
    except httpx.HTTPError:
        raise VoiceError("voice_asr_error", "识别失败：识别服务调用失败", 502)
    if r.status_code >= 400:
        raise VoiceError(
            "voice_asr_error", "识别失败：识别服务返回 %d" % r.status_code, 502
        )
    try:
        return r.json()
    except ValueError:
        return r.text


def speak(text: str) -> tuple[bytes, str]:
    """文本 -> (音频字节, media_type)。失败抛 VoiceError。"""
    if not TTS_URL:
        raise VoiceError(
            "voice_tts_not_configured",
            "合成失败：语音合成服务未配置（VOICE_TTS_URL）",
            503,
        )
    return _tts_request(text)


def _tts_request(text: str) -> tuple[bytes, str]:
    """真正的 TTS 网络调用，测试用 monkeypatch 替换。"""
    headers = {"Accept": "audio/*"}
    if API_KEY:
        headers["Authorization"] = "Bearer " + API_KEY
    payload: dict = {"text": text}
    if MODEL:
        payload["model"] = MODEL
    try:
        r = httpx.post(TTS_URL, json=payload, headers=headers, timeout=TTS_TIMEOUT_S)
    except httpx.TimeoutException:
        raise VoiceError("voice_tts_timeout", "合成失败：语音合成服务超时", 504)
    except httpx.HTTPError:
        raise VoiceError("voice_tts_error", "合成失败：语音合成服务调用失败", 502)
    if r.status_code >= 400:
        raise VoiceError(
            "voice_tts_error", "合成失败：语音合成服务返回 %d" % r.status_code, 502
        )
    if not r.content:
        raise VoiceError("voice_tts_error", "合成失败：服务端未返回音频", 502)
    media = r.headers.get("content-type") or "audio/mpeg"
    return r.content, media