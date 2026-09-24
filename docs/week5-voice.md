# 第5周：语音链路 —— 从"说一句话"到"听到回答"，每一步都看得见

> 本周不动固件，纯服务端 + 网页。设备麦克风（`device_microphone`）只在枚举里预留，
> 端侧课提供能力后再接；声音采集用电脑麦克风——这是课程明确允许的替代，
> 但**声音从哪来、识别与合成跑在哪，必须记录、必须在界面上摆出来**，不能含糊。

设计与实现对应 `server/voice.py`（链路与落库）、`server/static/voice.js`（四态前端）、
`server/test_voice.py`（27 项单测）、`server/tools/ui_check.mjs` 第5周段（前端回归断言）。

## 1. 对照课程成果要求

| 课程要求 | 本周实现 | 在哪看 |
|---|---|---|
| 语音输入 → 识别 → 任务执行 → 语音合成播放 | 录音→`transcribe`→第4周`/assistant/ask`→`speak`→浏览器播放，全程一条 `voice_events` 行 | 界面"语音链路"面板 |
| 录音、识别、任务执行、合成播放四态可见 | badge 状态机（待命/录音中/识别中/任务执行中/播放中/完成）+ 时间轴逐段耗时 | `vc_state` / `vc_timeline` |
| 记录音频来源与运行位置 | `audio_source`（4 枚举）与 `recognition_location` / `tts_location`（server/browser）落库并回显 | 事件表 `vc_tbody`、`GET /voice/events` |
| 无声、识别失败、超时、合成不可用各有提示 | 13 个错误码，文案唯一来源 `VOICE_ERROR_CATALOG`，经 `/voice/info` 下发，前端不写死中文 | `vc_err` 提示区 |
| 语音失败不影响文字入口 | 识别文本只是"一句用户文本"，任务执行原样调第4周端点；`nl_text.disabled` 永远 false，ui_check 有断言守着 | 助手面板 + ui_check 第5周段 |
| 同一句话文字/语音结果对照 | 「对照」按钮把同句再走一次文字入口，两行并排，一致/不一致都有 badge | `vc_compare_out` |
| 备用路径：上传授权录音 | 「上传授权录音」入口始终可用（无麦克风环境的唯一路径），来源如实记 `authorized_recording` | `vc_upload` |

## 2. 链路与时序

```
[浏览器] 开始录音 ── 结束 ──► blob（容器/码率由浏览器决定，界面如实展示）
   │  POST /api/v1/voice/transcribe（X-Control-Token；multipart: file + audio_source + client_duration_ms）
   ▼
[服务端] record_start：先落 voice_events 一行 —— 识别失败时"有过这次尝试"同样是事实
   │  transcribe：大小上限(默认5MB) → MIME 白名单 → 无声门槛(256B) → OPENAI_API_KEY → whisper-1
   ▼  识别文本 = 中间结果，界面原样显示（引擎 + 运行位置一并标出）
[浏览器] POST /api/v1/assistant/ask —— 与文字入口**同一端点、同一信封**（复用第4周）
   │  回答文本 → POST /voice/events/{id}/bind（intent / assistant_ok / request_id 回写事件行）
   ▼
[浏览器] GET /api/v1/voice/speak?text=…
   │  成功：audio/mpeg + X-Voice-Engine: openai_tts（跑在 server）
   │  失败：503 + JSON {ok:false, error, fallback:"browser_speechsynthesis", fallback_location:"browser"}
   ▼
[浏览器] <audio> 播放；503 时 speechSynthesis(zh-CN) 兜底 —— 降级不是失败，但如实标注
   │  POST /voice/events/{id}/played（played 0/1 + tts_engine + tts_location 浏览器如实回告）
   ▼
voice_events 一行完整：来源 / 引擎 / 位置 / 耗时 / 识别原文 / 意图 / 播放结果 / 错误码
```

服务端只能记"浏览器回告了什么"：播放发生在这台电脑的声卡上，服务端不知道也不假装知道，
`played` 与 `tts_location=browser` 就是回告的事实。

## 3. 任务执行复用第4周，不新开任何通道

- 识别结果的唯一身份是**一句用户文本**。翻译、白名单、参数夹取、结构化错误，
  全部还是第4周 `assistant.ask()` 那一份实现——语音链路一行执行代码都没有。
- **「对照」按钮**是复用主张的验证方法：把同一句话再走一次文字入口
  （`POST /assistant/ask`），两行并排显示 intent / ok / answer；一致给绿 badge
  「两入口结果一致」，不一致给红 badge「两入口结果不一致（这是bug，请报）」。
  相同才叫复用，不同就是 bug。
- 语音链路任何失败（没麦克风、没 key、识别超时、合成挂了）都不碰文字入口。

## 4. voice_events：来源与位置是事实，不是形容词

| 字段 | 含义 |
|---|---|
| `t_server_ms` | 权威时间：服务端收到录音的时刻（延续全项目口径） |
| `audio_source` | 声音从哪来：`pc_microphone` / `shared_audio_station` / `authorized_recording` / `device_microphone` |
| `audio_mime` / `audio_bytes` / `client_duration_ms` / `client_ip` | 这段音频的原始事实（时长是浏览器自报，只作参考） |
| `recognition_engine` / `recognition_location` / `recognition_latency_ms` | 识别跑在哪、花了多久（观测值） |
| `transcript` | 识别原文——中间结果必须可见、可查 |
| `intent` / `assistant_ok` / `request_id` | 第4周翻译结果；若产生指令，指向 `commands` 表 |
| `tts_engine` / `tts_location` / `played` / `played_t_ms` | 合成引擎与位置、播放结果（1 成功 / 0 失败 / null 未播） |
| `error_code` / `error_stage` | 失败落在哪一段：recognition / task / playback |

三条纪律（都有测试守着）：

1. **先落行再识别**：`record_start` 在任何网络调用之前，失败尝试也留痕
   （`test_failed_attempt_still_listed`）。
2. **`attach_result` 只写非 None 字段**：bind/played 回告不会抹掉已有事实，重试安全。
3. **派生值不落库**：库里只记 `played 0/1`，"已播放/播放失败/未播放"文案由前端现算。

## 5. 错误契约

HTTP 口径与 assistant 一致——**协议违规用状态码，语义失败用 200 + ok=false**：

| 情形 | 状态码 | body 形状 |
|---|---|---|
| 缺 `X-Control-Token`（transcribe/bind/played/speak） | 401 | `{detail}` |
| `audio_source` 不在枚举 / `tts_location` 非法 | 400 | `ok=false + error` |
| 事件 id 不存在 | 404 | `ok=false + error` |
| 超过大小上限（`VOICE_MAX_UPLOAD_MB`，默认 5） | 413 | `ok=false + error` |
| MIME 不在白名单（webm/ogg/wav/mpeg/mp4/flac） | 415 | `ok=false + error` |
| 无声 / 未配置 / 识别失败 / 识别超时 | 200 | `ok=false + error` |
| `/voice/speak` 合成不可用或失败 | 503 | `ok=false + error + fallback` |

错误码目录（`VOICE_ERROR_CATALOG`，13 个；level 沿用界面语义 warn 黄 / crit 红）：

| code | level | 触发点 |
|---|---|---|
| `no_audio` | warn | 上传小于 256 字节，或识别结果为空 |
| `voice_not_configured` | warn | `OPENAI_API_KEY` 为空（文字入口照常可用） |
| `recognition_failed` | crit | 识别服务返回非 200 或坏 JSON |
| `recognition_timeout` | crit | 识别超过 `VOICE_RECOGNITION_TIMEOUT_S`（默认 20s） |
| `audio_too_large` | crit | 超过大小上限（413） |
| `audio_type_unsupported` | crit | MIME 不在白名单（415） |
| `tts_unavailable` | warn | 合成未配置（503，浏览器兜底） |
| `tts_failed` | crit | 合成服务返回非 200（503，浏览器兜底） |
| `tts_timeout` | crit | 合成超时（503，浏览器兜底） |
| `tts_empty_text` | warn | 没有可合成的文本（503） |
| `bad_source` | crit | 音频来源不在枚举（400） |
| `bad_location` | crit | 运行位置不是 server/browser（400） |
| `unknown_event` | crit | 事件 id 不存在（404） |

两个前端纪律：

- 文案全部来自 `/api/v1/voice/info` 下发的目录，**前端不写死中文**——
  单测断言的和用户看到的是同一份契约。
- 响应不在契约内（`ok=false` 却缺 `error` 字段）也是必须可见的失败：
  界面显示「服务端响应异常（HTTP xxx，缺 error 字段）」并附原始响应。
  这不是防御性摆设——ui_check 就是靠它抓到一个真 bug（见开发复盘阶段 18）。

## 6. 接口

| 方法 | 路径 | 鉴权 | 作用 |
|---|---|---|---|
| GET | `/api/v1/voice/info` | 无 | 能力自述：来源枚举、引擎与配置状态、大小/格式上限、错误码目录 |
| POST | `/api/v1/voice/transcribe` | `X-Control-Token` | multipart 录音 → 识别文本（先落 `voice_events` 行） |
| POST | `/api/v1/voice/events/{id}/bind` | `X-Control-Token` | 回写任务结果：intent / assistant_ok / request_id |
| POST | `/api/v1/voice/events/{id}/played` | `X-Control-Token` | 回写播放结果：played / tts_engine / tts_location |
| GET | `/api/v1/voice/events` | 无（只读公开） | 事件列表（`limit` 1~200，默认 30） |
| GET | `/api/v1/voice/speak?text=…` | `X-Control-Token` | 合成：成功 `audio/mpeg`+`X-Voice-Engine`；失败 503+JSON（带 fallback） |

transcribe / bind / played 要令牌，因为它们能触发识别计费、改库；events 列表纯只读，公开。

## 7. 前端：四态状态机与降级路径

- 状态 badge：`待命 → 录音中 → 识别中 → 任务执行中 → 播放中 → 完成`；
  时间轴逐段追加 `录音 / 识别 xxms / 执行 xxms / 播放`，失败停在哪一段一眼可见。
- **无麦克风能力**（无 `getUserMedia` / `MediaRecorder`，例如 file:// 或被策略禁用）：
  录音按钮禁用并说明原因，「上传授权录音」入口始终可用——课程备用路径不是摆设。
- 录音防抖：`getUserMedia` 在飞时连点只生效一次（ui_check 断言连点 2 次 → 1 次调用）。
- 上传文件走**同一条** `runChain`，仅 `audio_source` 如实记 `authorized_recording`。
- 事件表 5 秒轮询，是旁路：拉不到不影响主链路。
- 按钮用 `onclick` 而非 `addEventListener`：与 button.js 同风格，
  也让 ui_check 的最小 DOM 桩能真的"点"到它做断言。

## 8. 验证（2026-09-24 实测数字）

```bash
cd server
python -m pytest test_voice.py -q        # 27 passed
python -m pytest -q                      # 187 passed（27+78+25+30+27，第1~5周）
node tools/ui_check.mjs --origin http://127.0.0.1:8001   # EXIT=0
```

- 单测通过服务商接缝（`_post_transcription` / `_post_speech` monkeypatch）注入
  超时/500/坏 JSON，**不需要真 key、真网络**；真机验证时走的就是同一行真 HTTP。
- 覆盖：info 自述与错误码目录、成功链路落库、401、带 `;codecs=` 的 MIME、
  无声两判（空文本 / 256B 门槛且不触网）、未配置时服务商零调用、415/413/400、
  bind/played 回写与 bad_location、speak 音频与 503+fallback、事件列表公开、
  失败尝试仍留痕、**语音全挂时文字入口不受影响**、init_schema 幂等、
  老库（无 voice_events 表）照常启动。
- ui_check 第5周段断言：错误码契约 4 关键码齐全、来源下拉 4 项、
  无麦克风时录音键禁用且降级提示指向"上传授权录音"、`nl_text` 永不禁用、
  连点录音 `getUserMedia` 只 1 次、transcribe POST 只 1 次、
  无 key 时界面如实显示 `voice_not_configured` 文案、时间轴含"识别"态。
- 本机验证环境**没有** `OPENAI_API_KEY`，跑通的是完整降级形态。

课堂演示前还差两样（环境，不是代码）：

1. `OPENAI_API_KEY`（.env 里配）→ 真识别 + 真合成；
2. 真麦克风 + 浏览器授权 → 现场说一句话走全链路，再点「对照」验证同句一致。

## 9. 已知边界

- **MediaRecorder 的容器与采样率由浏览器和设备决定**（Chrome 桌面通常是
  webm/opus），服务端不做重采样，原样送 whisper；识别质量由服务商负责。
- **识别/合成默认超时各 20s**（`VOICE_RECOGNITION_TIMEOUT_S` / `VOICE_TTS_TIMEOUT_S`），
  超时如实报 `recognition_timeout` / `tts_timeout`，不装作成功。
- **`device_microphone` 本周只是枚举预留**：板端 I2S 麦克风数据仍走第1周采集链路，
  没有"板端录音→上传识别"的通道，端侧课提供能力后再接。
- **浏览器兜底合成音质取决于系统语音包**：部分系统没有 zh-CN 语音时
  `speechSynthesis` 会失败——如实回告 `played=0`，界面上只剩文字回答，不假装播过。
- **`speak` 是 GET + query 传文本（≤4000 字符）、非流式**：首字节延迟等于整段合成时长，
  长回答会明显等待。
- **`voice_events` 是第四张只增不减的表**，归档/清理与 `control_log` 等是同一件事，进 TODO。
