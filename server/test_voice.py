"""第5周验证：语音接口（转写 + 合成）的错误码与状态、ASR 文本复用第 4 周 NL 路径。

跑法： cd server && python -m pytest test_voice.py -v

验收点（prompts/week-5.md 六、DoD 1）：
- 转写 / 合成接口可用；
- 未配置 / 超时 / 无语音 / 服务报错都有明确的错误码与 HTTP 状态；
- 不做任何"假装成功"（无假文本、无空音频冒充成功）。
"""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
import app as app_module  # noqa: E402
import voice  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    # 环境无关：显式清空语音配置，避免本机 .env / 环境变量泄漏进来影响断言。
    monkeypatch.setattr(app_module, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(voice, "ASR_URL", "")
    monkeypatch.setattr(voice, "TTS_URL", "")
    monkeypatch.setattr(voice, "DEVICE_AUDIO", False)
    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(voice, "ASR_URL", "https://asr.example/v1")
    monkeypatch.setattr(voice, "TTS_URL", "https://tts.example/v1")


def transcribe(client, body=b"fake-audio", content_type="audio/webm"):
    return client.post(
        "/api/v1/voice/transcribe",
        content=body,
        headers={"Content-Type": content_type},
    )


class TestTranscribe:
    def test_no_audio_rejected(self, client):
        r = transcribe(client, b"")
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "voice_no_audio"

    def test_not_configured(self, client):
        r = transcribe(client)
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "voice_asr_not_configured"

    def test_timeout(self, client, configured, monkeypatch):
        def boom(_audio, _ct):
            raise voice.VoiceError("voice_asr_timeout", "识别失败：识别服务超时", 504)

        monkeypatch.setattr(voice, "_asr_request", boom)
        r = transcribe(client)
        assert r.status_code == 504
        assert r.json()["error"]["code"] == "voice_asr_timeout"

    def test_service_error(self, client, configured, monkeypatch):
        def boom(_audio, _ct):
            raise voice.VoiceError(
                "voice_asr_error", "识别失败：识别服务返回 500", 502
            )

        monkeypatch.setattr(voice, "_asr_request", boom)
        r = transcribe(client)
        assert r.status_code == 502
        assert r.json()["error"]["code"] == "voice_asr_error"

    def test_success_text_field(self, client, configured, monkeypatch):
        monkeypatch.setattr(voice, "_asr_request", lambda _a, _c: {"text": "查看上次数据"})
        r = transcribe(client)
        assert r.status_code == 200
        assert r.json()["text"] == "查看上次数据"

    def test_success_transcript_field(self, client, configured, monkeypatch):
        monkeypatch.setattr(
            voice, "_asr_request", lambda _a, _c: {"transcript": "重新采集一次"}
        )
        assert transcribe(client).json()["text"] == "重新采集一次"

    def test_success_plain_text(self, client, configured, monkeypatch):
        monkeypatch.setattr(voice, "_asr_request", lambda _a, _c: "查看上次数据")
        assert transcribe(client).json()["text"] == "查看上次数据"

    def test_empty_text_is_no_audio(self, client, configured, monkeypatch):
        monkeypatch.setattr(voice, "_asr_request", lambda _a, _c: {"text": "   "})
        r = transcribe(client)
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "voice_no_audio"

    def test_too_large(self, client, configured, monkeypatch):
        big = b"x" * (voice.MAX_AUDIO_BYTES + 1)
        # 大音频直接按长度拒，不调网络
        r = transcribe(client, big)
        assert r.status_code == 413
        assert r.json()["error"]["code"] == "voice_too_large"


class TestSpeak:
    def test_not_configured(self, client):
        r = client.post("/api/v1/voice/speak", json={"text": "你好"})
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "voice_tts_not_configured"

    def test_empty_text_rejected(self, client):
        r = client.post("/api/v1/voice/speak", json={"text": ""})
        assert r.status_code == 422

    def test_too_long_rejected(self, client):
        r = client.post("/api/v1/voice/speak", json={"text": "x" * 501})
        assert r.status_code == 422

    def test_success(self, client, configured, monkeypatch):
        monkeypatch.setattr(
            voice, "_tts_request", lambda _t: (b"\xff\xf3\xc4", "audio/mpeg")
        )
        r = client.post("/api/v1/voice/speak", json={"text": "查看上次数据"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("audio/mpeg")
        assert r.content == b"\xff\xf3\xc4"

    def test_service_error(self, client, configured, monkeypatch):
        def boom(_text):
            raise voice.VoiceError(
                "voice_tts_error", "合成失败：语音合成服务返回 500", 502
            )

        monkeypatch.setattr(voice, "_tts_request", boom)
        r = client.post("/api/v1/voice/speak", json={"text": "hi"})
        assert r.status_code == 502
        assert r.json()["error"]["code"] == "voice_tts_error"

    def test_timeout(self, client, configured, monkeypatch):
        def boom(_text):
            raise voice.VoiceError("voice_tts_timeout", "合成失败：语音合成服务超时", 504)

        monkeypatch.setattr(voice, "_tts_request", boom)
        r = client.post("/api/v1/voice/speak", json={"text": "hi"})
        assert r.status_code == 504
        assert r.json()["error"]["code"] == "voice_tts_timeout"

    def test_service_returns_no_audio_is_error(self, client, configured, monkeypatch):
        # 服务"成功"但没返回音频 = 合成失败，不能当成功（防护在网络层 _tts_request）

        class FakeResp:
            status_code = 200
            content = b""
            headers = {"content-type": "audio/mpeg"}

        monkeypatch.setattr(voice.httpx, "post", lambda *a, **k: FakeResp())
        with pytest.raises(voice.VoiceError) as ei:
            voice._tts_request("hi")
        assert ei.value.code == "voice_tts_error"


class TestInfo:
    def test_defaults(self, client):
        j = client.get("/api/v1/voice/info").json()
        assert j["ok"] is True
        assert j["asr_configured"] is False
        assert j["tts_configured"] is False
        assert j["device_audio"] is False

    def test_configured_flags(self, client, configured, monkeypatch):
        monkeypatch.setattr(voice, "DEVICE_AUDIO", True)
        j = client.get("/api/v1/voice/info").json()
        assert j["asr_configured"] is True
        assert j["tts_configured"] is True
        assert j["device_audio"] is True