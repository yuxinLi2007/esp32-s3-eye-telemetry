"""第5周验证：语音链路 —— 识别/合成接缝注入、voice_events 溯源、降级与鉴权。

跑法： cd server && python -m pytest test_voice.py -q

口径沿用 test_buttons.py：假时钟注入、服务商接缝用 monkeypatch 替换
（voice._post_transcription / voice._post_speech），不真联网、不真花 key。
真机验证（真麦克风 + 真 Whisper）在课堂做，docs/week5-voice.md 记录实测数字。
"""

import sys
import time
from pathlib import Path

import pytest
import httpx
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
import app as app_module  # noqa: E402
import commands  # noqa: E402
import db  # noqa: E402
import voice  # noqa: E402

WEBM = b"\x1aE\xdf\xa3" + b"x" * 600      # > SILENCE_MIN_BYTES 的假 webm
CTL = {"X-Control-Token": "ctl-voice"}


class Clock:
    def __init__(self):
        self.t = 1_700_000_000_000

    def ms(self):
        return self.t

    def advance(self, ms):
        self.t += int(ms)


class FakeResp:
    def __init__(self, status=200, payload=None, content=b"", bad_json=False):
        self.status_code = status
        self.payload = payload
        self.content = content
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self.payload


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def dbpath(tmp_path):
    return str(tmp_path / "voice.db")


@pytest.fixture
def client(dbpath, monkeypatch, clock):
    monkeypatch.setattr(app_module, "DB_PATH", dbpath)
    monkeypatch.setattr(commands, "now_ms", clock.ms)
    monkeypatch.setattr(voice, "now_ms", clock.ms)
    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture
def conn(client, dbpath):
    c = db.connect(dbpath)
    yield c
    c.close()


@pytest.fixture
def calls(monkeypatch):
    """记录服务商接缝被调了几次、返回什么。默认：识别成功、合成成功。"""
    box = {"transcribe": [], "speech": [],
           "resp": FakeResp(200, {"text": "帮我看看上次的数据"}),
           "speech_resp": FakeResp(200, content=b"ID3fake-mp3"),
           "raise": None, "speech_raise": None}

    def t(**kw):
        box["transcribe"].append(kw)
        if box["raise"]:
            raise box["raise"]
        return box["resp"]

    def s(**kw):
        box["speech"].append(kw)
        if box["speech_raise"]:
            raise box["speech_raise"]
        return box["speech_resp"]

    monkeypatch.setattr(voice, "_post_transcription", t)
    monkeypatch.setattr(voice, "_post_speech", s)
    return box


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-voice")
    return "sk-test-voice"


def transcribe(client, data=WEBM, mime="audio/webm", source="pc_microphone",
               headers=None, **form):
    return client.post(
        "/api/v1/voice/transcribe",
        files={"file": ("voice.webm", data, mime)},
        data=dict({"audio_source": source}, **form),
        headers=headers or {})


# ---------------------------------------------------------------- 能力自述
def test_info_lists_sources_and_error_catalog(client):
    r = client.get("/api/v1/voice/info")
    assert r.status_code == 200
    j = r.json()
    assert [s["id"] for s in j["sources"]] == list(voice.AUDIO_SOURCES)
    codes = {e["code"] for e in j["error_codes"]}
    assert {"no_audio", "recognition_timeout", "recognition_failed",
            "tts_unavailable", "voice_not_configured"} <= codes
    assert j["recognition"]["configured"] is False   # 没 key 是配置事实，不是秘密
    assert j["tts"]["fallback_engine"] == voice.BROWSER_TTS_ENGINE


# ---------------------------------------------------------------- 识别主路径
def test_transcribe_success_records_event(client, conn, calls, key):
    r = transcribe(client, headers=CTL, client_duration_ms="1200")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True and j["text"] == "帮我看看上次的数据"
    assert j["run_location"] == {"recognition": "server", "task": "server"}
    row = voice.get_event(conn, j["event_id"])
    assert row["audio_source"] == "pc_microphone"
    assert row["transcript"] == "帮我看看上次的数据"
    assert row["recognition_location"] == "server"
    assert row["recognition_engine"] == "openai_whisper"
    assert row["client_duration_ms"] == 1200
    assert row["error_code"] is None
    assert len(calls["transcribe"]) == 1


def test_transcribe_requires_control_token(client, monkeypatch):
    monkeypatch.setattr(app_module, "CONTROL_TOKEN", "ctl-voice")
    r = transcribe(client)
    assert r.status_code == 401


def test_transcribe_mime_with_codecs_accepted(client, calls, key):
    r = transcribe(client, mime="audio/webm;codecs=opus", headers=CTL)
    assert r.status_code == 200 and r.json()["ok"] is True


# ---------------------------------------------------------------- 语义级失败
def test_no_audio_when_transcript_empty(client, conn, calls, key):
    calls["resp"] = FakeResp(200, {"text": "   "})
    r = transcribe(client, headers=CTL)
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is False and j["error"]["code"] == "no_audio"
    row = voice.get_event(conn, j["event_id"])
    assert row["error_code"] == "no_audio" and row["error_stage"] == "recognition"


def test_no_audio_tiny_payload_never_hits_provider(client, calls, key):
    r = transcribe(client, data=b"ab", headers=CTL)
    assert r.json()["error"]["code"] == "no_audio"
    assert calls["transcribe"] == []        # 空文件不该浪费一次识别请求


def test_not_configured_is_honest_and_keeps_provider_idle(client, calls):
    r = transcribe(client, headers=CTL)     # 没有 OPENAI_API_KEY
    j = r.json()
    assert j["ok"] is False and j["error"]["code"] == "voice_not_configured"
    assert calls["transcribe"] == []


def test_recognition_timeout(client, calls, key):
    calls["raise"] = httpx.TimeoutException("slow")
    r = transcribe(client, headers=CTL)
    assert r.json()["error"]["code"] == "recognition_timeout"


def test_recognition_failed_on_provider_500(client, calls, key):
    calls["resp"] = FakeResp(500, {"error": "boom"})
    r = transcribe(client, headers=CTL)
    assert r.json()["error"]["code"] == "recognition_failed"


def test_recognition_failed_on_bad_json(client, calls, key):
    calls["resp"] = FakeResp(200, bad_json=True)
    r = transcribe(client, headers=CTL)
    assert r.json()["error"]["code"] == "recognition_failed"


# ---------------------------------------------------------------- 协议级失败
def test_unsupported_mime_415(client, conn, key):
    r = transcribe(client, mime="text/plain", headers=CTL)
    assert r.status_code == 415
    assert r.json()["error"]["code"] == "audio_type_unsupported"


def test_oversize_413(client, monkeypatch, key):
    monkeypatch.setattr(voice, "MAX_UPLOAD_BYTES", 10)
    r = transcribe(client, headers=CTL)
    assert r.status_code == 413


def test_bad_source_400(client, key):
    r = transcribe(client, source="magic-mic", headers=CTL)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_source"


# ---------------------------------------------------------------- 结果挂载
def test_bind_attaches_task_result(client, conn, calls, key):
    eid = transcribe(client, headers=CTL).json()["event_id"]
    r = client.post(f"/api/v1/voice/events/{eid}/bind",
                    json={"intent": "query_history", "assistant_ok": True},
                    headers=CTL)
    assert r.status_code == 200
    row = voice.get_event(conn, eid)
    assert row["intent"] == "query_history" and row["assistant_ok"] == 1


def test_bind_unknown_event_404(client, key):
    r = client.post("/api/v1/voice/events/9999/bind",
                    json={"intent": "clarify"}, headers=CTL)
    assert r.status_code == 404


def test_played_marks_row_and_rejects_bad_location(client, conn, calls, key):
    eid = transcribe(client, headers=CTL).json()["event_id"]
    r = client.post(f"/api/v1/voice/events/{eid}/played",
                    json={"played": True, "tts_engine": "openai_tts",
                          "tts_location": "server"}, headers=CTL)
    assert r.status_code == 200
    row = voice.get_event(conn, eid)
    assert row["played"] == 1 and row["tts_location"] == "server"
    r2 = client.post(f"/api/v1/voice/events/{eid}/played",
                     json={"played": True, "tts_engine": "x",
                           "tts_location": "cloud"}, headers=CTL)
    assert r2.status_code == 400


def test_played_false_records_failure(client, conn, calls, key):
    eid = transcribe(client, headers=CTL).json()["event_id"]
    client.post(f"/api/v1/voice/events/{eid}/played",
                json={"played": False, "tts_engine": "browser_speechsynthesis",
                      "tts_location": "browser"}, headers=CTL)
    row = voice.get_event(conn, eid)
    assert row["played"] == 0 and row["tts_engine"] == "browser_speechsynthesis"


# ---------------------------------------------------------------- 合成
def test_speak_returns_audio(client, calls, key):
    r = client.get("/api/v1/voice/speak", params={"text": "已为你查到三条"},
                   headers=CTL)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/mpeg")
    assert r.headers["x-voice-engine"] == "openai_tts"
    assert r.content == b"ID3fake-mp3"


def test_speak_not_configured_503_with_browser_fallback(client, calls):
    r = client.get("/api/v1/voice/speak", params={"text": "你好"}, headers=CTL)
    assert r.status_code == 503
    j = r.json()
    assert j["error"]["code"] == "tts_unavailable"
    assert j["fallback"] == "browser_speechsynthesis"
    assert j["fallback_location"] == "browser"


def test_speak_timeout_503(client, calls, key):
    calls["speech_raise"] = httpx.TimeoutException("slow")
    r = client.get("/api/v1/voice/speak", params={"text": "你好"}, headers=CTL)
    assert r.status_code == 503 and r.json()["error"]["code"] == "tts_timeout"


def test_speak_empty_text_503_without_provider(client, calls, key):
    r = client.get("/api/v1/voice/speak", params={"text": "   "}, headers=CTL)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "tts_empty_text"
    assert calls["speech"] == []


def test_speak_requires_control_token(client, monkeypatch, key):
    monkeypatch.setattr(app_module, "CONTROL_TOKEN", "ctl-voice")
    r = client.get("/api/v1/voice/speak", params={"text": "你好"})
    assert r.status_code == 401


# ---------------------------------------------------------------- 事件列表
def test_events_public_and_filterable(client, calls, key):
    transcribe(client, headers=CTL, source="pc_microphone")
    transcribe(client, headers=CTL, source="authorized_recording")
    r = client.get("/api/v1/voice/events")
    assert r.status_code == 200 and len(r.json()["events"]) == 2
    r2 = client.get("/api/v1/voice/events",
                    params={"audio_source": "authorized_recording"})
    evs = r2.json()["events"]
    assert len(evs) == 1 and evs[0]["audio_source"] == "authorized_recording"


def test_failed_attempt_still_listed(client, calls, key):
    calls["raise"] = httpx.TimeoutException("slow")
    j = transcribe(client, headers=CTL).json()
    evs = client.get("/api/v1/voice/events").json()["events"]
    assert evs[0]["id"] == j["event_id"]
    assert evs[0]["error_code"] == "recognition_timeout"


# ---------------------------------------------------------------- 回归与建表
def test_text_entry_unaffected_by_voice_outage(client):
    """语音服务没配置时，第4周文字入口必须照常工作（课程当堂验证要求）。"""
    r = client.post("/api/v1/assistant/ask", json={"text": "查看最近 3 条"})
    assert r.status_code == 200
    assert "intent" in r.json()


def test_init_schema_idempotent(conn):
    voice.init_schema(conn)
    voice.init_schema(conn)     # 旧库重启不炸


def test_old_db_without_voice_table_still_boots(dbpath, monkeypatch, clock):
    """第1~4周的旧库：lifespan 幂等建表后一切照常。"""
    c = db.connect(dbpath)
    commands.init_schema(c)
    c.close()
    monkeypatch.setattr(app_module, "DB_PATH", dbpath)
    monkeypatch.setattr(commands, "now_ms", clock.ms)
    with TestClient(app_module.app) as tc:
        r = tc.get("/api/v1/voice/events")
        assert r.status_code == 200 and r.json()["events"] == []
